from __future__ import annotations

import gc
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.contracts import AgentError
from agent.store import WenquanStore


class RuntimeFileRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = WenquanStore(self.root / "private" / "agent.sqlite3")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def sidecar(self, suffix: str = "-wal") -> Path:
        return Path(str(self.store.database_path) + suffix)

    def write_private(self, path: Path, content: str = "synthetic") -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    def test_sqlite_last_connection_cleanup_during_final_lstat_is_accepted(self) -> None:
        # Closing the real last SQLite connection deletes WAL and SHM. Inject
        # that lifecycle event at the exact second-lstat race seen in QA.
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix):
                # sqlite3 Connection context managers commit/rollback; they do
                # not close. Collect prior initializer cycles so this writer
                # really is the last live connection for the cleanup probe.
                gc.collect()
                writer = sqlite3.connect(self.store.database_path)
                writer.execute("UPDATE schema_meta SET schema_version=schema_version")
                writer.commit()
                target = self.sidecar(suffix)
                self.assertTrue(target.exists())
                original_lstat = os.lstat
                calls = 0

                def cleanup_before_second_lstat(path, *args, **kwargs):
                    nonlocal calls
                    if Path(path) == target:
                        calls += 1
                        if calls == 2:
                            writer.close()
                    return original_lstat(path, *args, **kwargs)

                try:
                    with patch("agent.store.os.lstat", side_effect=cleanup_before_second_lstat):
                        self.store._secure_runtime_files(create_database=False)
                    self.assertGreaterEqual(calls, 2)
                    self.assertFalse(target.exists())
                finally:
                    writer.close()

    def test_sidecar_disappearance_during_permission_tightening_is_accepted(self) -> None:
        target = self.sidecar()
        self.write_private(target)
        target.chmod(0o644)
        original_chmod = os.chmod
        removed = False

        def remove_before_chmod(path, *args, **kwargs):
            nonlocal removed
            if Path(path) == target and not removed:
                removed = True
                target.unlink()
            return original_chmod(path, *args, **kwargs)

        with patch("agent.store.os.chmod", side_effect=remove_before_chmod):
            self.store._secure_runtime_files(create_database=False)
        self.assertTrue(removed)

    def test_transient_inode_replacement_gets_fresh_permission_checks(self) -> None:
        target = self.sidecar()
        self.write_private(target)
        replacement = self.root / "replacement"
        replacement.write_text("new SQLite sidecar", encoding="utf-8")
        replacement.chmod(0o644)
        original_lstat = os.lstat
        calls = 0

        def replace_on_final_lstat(path, *args, **kwargs):
            nonlocal calls
            if Path(path) == target:
                calls += 1
                if calls == 2:
                    os.replace(replacement, target)
            return original_lstat(path, *args, **kwargs)

        with patch("agent.store.os.lstat", side_effect=replace_on_final_lstat):
            self.store._secure_runtime_files(create_database=False)
        self.assertGreaterEqual(calls, 4, "a replacement inode needs a fresh complete check")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.read_text(encoding="utf-8"), "new SQLite sidecar")

    def test_chmod_enoent_from_dangling_symlink_is_rechecked_and_rejected(self) -> None:
        target = self.sidecar()
        self.write_private(target)
        target.chmod(0o644)
        missing_target = self.root / "missing-external-target"
        original_chmod = os.chmod
        injected = False

        def swap_before_chmod(path, *args, **kwargs):
            nonlocal injected
            if Path(path) == target and not injected:
                injected = True
                target.unlink()
                target.symlink_to(missing_target)
            return original_chmod(path, *args, **kwargs)

        with patch("agent.store.os.chmod", side_effect=swap_before_chmod):
            with self.assertRaises(AgentError) as error:
                self.store._secure_runtime_files(create_database=False)
        self.assertTrue(injected)
        self.assertEqual(error.exception.code, "storage_boundary_invalid")
        self.assertTrue(target.is_symlink())
        self.assertFalse(missing_target.exists())

    def test_replacement_symlink_is_rejected_without_accepting_old_metadata(self) -> None:
        target = self.sidecar()
        self.write_private(target)
        external = self.root / "external"
        external.write_text("must stay untouched", encoding="utf-8")
        external.chmod(0o644)
        original_lstat = os.lstat
        calls = 0

        def swap_on_final_lstat(path, *args, **kwargs):
            nonlocal calls
            if Path(path) == target:
                calls += 1
                if calls == 2:
                    target.unlink()
                    target.symlink_to(external)
            return original_lstat(path, *args, **kwargs)

        with patch("agent.store.os.lstat", side_effect=swap_on_final_lstat):
            with self.assertRaises(AgentError) as error:
                self.store._secure_runtime_files(create_database=False)
        self.assertEqual(error.exception.code, "storage_boundary_invalid")
        self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o644)
        self.assertEqual(external.read_text(encoding="utf-8"), "must stay untouched")

    def test_runtime_audit_preserves_sqlite_writer_lock_against_another_process(self) -> None:
        writer = sqlite3.connect(self.store.database_path)
        writer.execute("BEGIN IMMEDIATE")
        probe = """import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0)
try:
    connection.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
    print('locked' if 'locked' in str(error) else str(error))
else:
    connection.rollback()
    print('unlocked')
finally:
    connection.close()
"""
        try:
            for audited in (False, True):
                if audited:
                    self.store._secure_runtime_files(create_database=False)
                result = subprocess.run([sys.executable, "-c", probe, str(self.store.database_path)], capture_output=True, text=True, check=True, timeout=10)
                self.assertEqual(result.stdout.strip(), "locked", f"audit={audited} must preserve SQLite's writer lock")
        finally:
            writer.rollback()
            writer.close()

    def test_nontransient_files_disappearing_or_replaced_are_rejected(self) -> None:
        for suffix in ("", ".backup-copy", ".restore-copy", ".tmp-copy"):
            for change in ("missing", "replacement"):
                with self.subTest(suffix=suffix, change=change):
                    target = Path(str(self.store.database_path) + suffix)
                    self.write_private(target)
                    replacement = self.root / "replacement"
                    self.write_private(replacement)
                    original_lstat = os.lstat
                    calls = 0

                    def mutate_before_final_lstat(path, *args, **kwargs):
                        nonlocal calls
                        if Path(path) == target:
                            calls += 1
                            if calls == 2:
                                if change == "missing":
                                    target.unlink()
                                else:
                                    os.replace(replacement, target)
                        return original_lstat(path, *args, **kwargs)

                    with patch("agent.store.os.lstat", side_effect=mutate_before_final_lstat):
                        with self.assertRaises(AgentError) as error:
                            self.store._secure_regular_file(target, create=False)
                    self.assertEqual(error.exception.code, "storage_boundary_invalid")

    def test_missing_main_database_cannot_be_silently_recreated_on_connect(self) -> None:
        self.store.database_path.unlink()
        with self.assertRaises(AgentError) as error:
            self.store._connect()
        self.assertEqual(error.exception.code, "storage_boundary_invalid")
        self.assertFalse(self.store.database_path.exists())


if __name__ == "__main__":
    unittest.main()
