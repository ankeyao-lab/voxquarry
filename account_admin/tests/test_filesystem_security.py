from __future__ import annotations

import os
import stat
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from account_admin.backup import BackupError, create_backup, restore_backup
from account_admin.filesystem import FilesystemSecurityError
from account_admin.store import AccountAdminStore
from account_admin import filesystem


@contextmanager
def process_umask(mask: int):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class FilesystemSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="account-admin-fs-security-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_initialize_and_reopen_enforce_private_modes_under_umask_022(self) -> None:
        data_dir = self.root / "data"
        with process_umask(0o022):
            AccountAdminStore(data_dir).initialize()

        self.assertEqual(mode(data_dir), 0o700)
        self.assertEqual(mode(data_dir / "auth.sqlite3"), 0o600)
        self.assertEqual(mode(data_dir / "audit.sqlite3"), 0o600)

        os.chmod(data_dir, 0o755)
        os.chmod(data_dir / "auth.sqlite3", 0o644)
        os.chmod(data_dir / "audit.sqlite3", 0o644)
        with process_umask(0o022):
            AccountAdminStore(data_dir).initialize()

        self.assertEqual(mode(data_dir), 0o700)
        self.assertEqual(mode(data_dir / "auth.sqlite3"), 0o600)
        self.assertEqual(mode(data_dir / "audit.sqlite3"), 0o600)

    def test_wal_and_shm_are_private_while_live_under_umask_022(self) -> None:
        data_dir = self.root / "data"
        with process_umask(0o022):
            store = AccountAdminStore(data_dir)
            store.initialize()
            connection = store.auth_connection()
            try:
                connection.execute("CREATE TABLE permission_probe(value TEXT NOT NULL)")
                connection.execute("INSERT INTO permission_probe(value) VALUES ('sensitive')")
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(store.auth_path) + suffix)
                    self.assertTrue(sidecar.is_file(), suffix)
                    self.assertEqual(mode(sidecar), 0o600)
                connection.commit()
            finally:
                connection.close()

    def test_online_backup_and_restore_enforce_private_modes_under_umask_022(self) -> None:
        data_dir = self.root / "data"
        with process_umask(0o022):
            store = AccountAdminStore(data_dir)
            store.initialize()
            writer = store.auth_connection()
            try:
                writer.execute("CREATE TABLE online_backup_probe(value TEXT NOT NULL)")
                writer.execute("INSERT INTO online_backup_probe(value) VALUES ('committed-in-wal')")
                writer.commit()
                backup_dir = self.root / "backup"
                create_backup(data_dir, backup_dir)
            finally:
                writer.close()

        self.assertEqual(mode(backup_dir), 0o700)
        self.assertEqual(mode(backup_dir / "manifest.json"), 0o600)
        for filename in ("auth.sqlite3", "audit.sqlite3"):
            self.assertEqual(mode(backup_dir / filename), 0o600)

        with process_umask(0o022):
            restored_dir = self.root / "restored"
            restore_backup(backup_dir, restored_dir)

        self.assertEqual(mode(restored_dir), 0o700)
        for filename in ("auth.sqlite3", "audit.sqlite3"):
            self.assertEqual(mode(restored_dir / filename), 0o600)
        restored = AccountAdminStore(restored_dir)
        restored.initialize()
        with restored.auth_connection() as connection:
            value = connection.execute("SELECT value FROM online_backup_probe").fetchone()[0]
        self.assertEqual(value, "committed-in-wal")

    def test_initialize_rejects_symlink_and_special_database_paths(self) -> None:
        symlink_dir = self.root / "symlink-data"
        symlink_dir.mkdir(mode=0o700)
        target = self.root / "outside.sqlite3"
        target.write_bytes(b"not-a-database")
        (symlink_dir / "auth.sqlite3").symlink_to(target)
        with self.assertRaisesRegex(FilesystemSecurityError, "symlink"):
            AccountAdminStore(symlink_dir).initialize()

        special_dir = self.root / "special-data"
        special_dir.mkdir(mode=0o700)
        os.mkfifo(special_dir / "auth.sqlite3", 0o600)
        with self.assertRaisesRegex(FilesystemSecurityError, "regular file"):
            AccountAdminStore(special_dir).initialize()

    def test_initialize_rejects_owner_mismatch(self) -> None:
        data_dir = self.root / "data"
        AccountAdminStore(data_dir).initialize()
        with mock.patch("account_admin.filesystem.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(FilesystemSecurityError, "owner"):
                AccountAdminStore(data_dir).initialize()

    def test_optional_sqlite_sidecar_can_disappear_during_security_check(self) -> None:
        store = AccountAdminStore(self.root / "data")
        store.initialize()
        for suffix in filesystem.SQLITE_SIDECAR_SUFFIXES:
            sidecar = Path(str(store.auth_path) + suffix)
            for phase in ("symlink_check", "metadata", "open"):
                with self.subTest(suffix=suffix, phase=phase):
                    sidecar.write_bytes(b"synthetic-sqlite-sidecar")
                    os.chmod(sidecar, 0o644)
                    if phase == "symlink_check":
                        original = filesystem.path_lexists
                        def disappear(path):
                            present = original(path)
                            if path == sidecar and present:
                                sidecar.unlink()
                            return present
                        patch = mock.patch.object(filesystem, "path_lexists", side_effect=disappear)
                    elif phase == "metadata":
                        original = filesystem._secure_existing_file
                        def disappear(path):
                            if path == sidecar:
                                sidecar.unlink()
                            return original(path)
                        patch = mock.patch.object(filesystem, "_secure_existing_file", side_effect=disappear)
                    else:
                        original = os.open
                        def disappear(path, flags, *args, **kwargs):
                            if path == sidecar:
                                sidecar.unlink()
                            return original(path, flags, *args, **kwargs)
                        patch = mock.patch.object(filesystem.os, "open", side_effect=disappear)
                    with patch:
                        filesystem.secure_sqlite_family(store.auth_path)
                    self.assertFalse(sidecar.exists())
                    self.assertEqual(mode(store.auth_path), 0o600)

    def test_sqlite_security_checks_preserve_an_active_writer_lock(self) -> None:
        store = AccountAdminStore(self.root / "data")
        store.initialize()
        writer = sqlite3.connect(store.auth_path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            filesystem.secure_sqlite_family(store.auth_path)
            child = subprocess.run([sys.executable, "-c", """
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=0.1)
try:
    c.execute('BEGIN IMMEDIATE')
    print('acquired')
except sqlite3.OperationalError as exc:
    assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY, exc.sqlite_errorname
    print('blocked')
finally:
    c.close()
""", str(store.auth_path)], capture_output=True, text=True, check=True)
            self.assertEqual(child.stdout.strip(), "blocked", "security checks released SQLite's writer lock")
        finally:
            writer.rollback()
            writer.close()

    def test_sidecar_race_tolerance_does_not_relax_security_failures(self) -> None:
        store = AccountAdminStore(self.root / "data")
        store.initialize()
        sidecar = Path(str(store.auth_path) + "-shm")
        outside = self.root / "outside"
        outside.write_bytes(b"private")
        os.chmod(outside, 0o644)
        sidecar.symlink_to(outside)
        with self.assertRaisesRegex(FilesystemSecurityError, "symlink"):
            filesystem.secure_sqlite_family(store.auth_path)
        self.assertEqual(mode(outside), 0o644)
        sidecar.unlink()
        sidecar.write_bytes(b"synthetic")
        original = filesystem._secure_existing_file
        def other_missing_file(path):
            if path == sidecar:
                raise FileNotFoundError(2, "missing", str(self.root / "different-file"))
            return original(path)
        with mock.patch.object(filesystem, "_secure_existing_file", side_effect=other_missing_file):
            with self.assertRaises(FileNotFoundError):
                filesystem.secure_sqlite_family(store.auth_path)
        store.auth_path.unlink()
        with self.assertRaisesRegex(FilesystemSecurityError, "required file is missing"):
            filesystem.secure_sqlite_family(store.auth_path)

    def test_backup_rejects_symlink_source_and_owner_mismatch(self) -> None:
        data_dir = self.root / "data"
        AccountAdminStore(data_dir).initialize()
        auth_path = data_dir / "auth.sqlite3"
        real_path = data_dir / "auth-real.sqlite3"
        auth_path.rename(real_path)
        auth_path.symlink_to(real_path.name)
        with self.assertRaisesRegex(BackupError, "symlink"):
            create_backup(data_dir, self.root / "bad-backup")

        auth_path.unlink()
        real_path.rename(auth_path)
        with mock.patch("account_admin.filesystem.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(BackupError, "owner"):
                create_backup(data_dir, self.root / "owner-backup")

    def test_restore_rejects_symlink_source_and_target(self) -> None:
        data_dir = self.root / "data"
        AccountAdminStore(data_dir).initialize()
        backup_dir = self.root / "backup"
        create_backup(data_dir, backup_dir)

        backup_auth = backup_dir / "auth.sqlite3"
        backup_real = backup_dir / "auth-real.sqlite3"
        backup_auth.rename(backup_real)
        backup_auth.symlink_to(backup_real.name)
        with self.assertRaisesRegex(BackupError, "symlink"):
            restore_backup(backup_dir, self.root / "bad-source-restore")

        backup_auth.unlink()
        backup_real.rename(backup_auth)
        target_dir = self.root / "bad-target-restore"
        target_dir.mkdir(mode=0o700)
        (target_dir / "auth.sqlite3").symlink_to("missing.sqlite3")
        with self.assertRaisesRegex(BackupError, "must not contain"):
            restore_backup(backup_dir, target_dir)


if __name__ == "__main__":
    unittest.main()
