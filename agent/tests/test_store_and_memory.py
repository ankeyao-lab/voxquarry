from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from agent.contracts import AgentError, new_id, utc_now
from agent.store import WenquanStore
from agent.tests.helpers import identity


class StoreAndMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = WenquanStore(Path(self.temporary.name) / "wenquan.sqlite3")
        self.store.initialize()
        self.user_a = identity("user-a")
        self.user_b = identity("user-b")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_owner_scope_isolation_includes_admin_role(self) -> None:
        session = self.store.create_session(self.user_a, "我的研究")
        self.store.add_message(
            self.user_a, session["session_id"], role="user", content="只属于用户 A"
        )
        with self.assertRaises(AgentError) as denied:
            self.store.get_session(self.user_b, session["session_id"])
        self.assertEqual(denied.exception.code, "session_not_found")
        admin = identity("admin-user", roles=("admin",))
        with self.assertRaises(AgentError):
            self.store.list_messages(admin, session["session_id"])
        self.assertEqual(self.store.list_sessions(self.user_b), [])

    def test_preference_requires_exact_confirmation_and_disable_is_zero_write(self) -> None:
        preview = {"preference_key": "market", "value": "North America"}
        confirmation = self.store.create_confirmation(
            self.user_a,
            action="memory.preference",
            preview=preview,
            expires_at="2099-01-01T00:00:00.000+00:00",
        )
        with self.assertRaises(AgentError) as missing:
            self.store.write_preference(
                self.user_a,
                preference_key="market",
                value="North America",
                confirmation_id=confirmation["confirmation_id"],
            )
        self.assertEqual(missing.exception.code, "confirmation_required")
        self.store.confirm(
            self.user_a,
            confirmation["confirmation_id"],
            confirmation["payload_hash"],
        )
        stored = self.store.write_preference(
            self.user_a,
            preference_key="market",
            value="North America",
            confirmation_id=confirmation["confirmation_id"],
        )
        self.assertEqual(stored["revision"], 1)
        self.assertEqual(self.store.list_preferences(self.user_a)[0]["value"], "North America")

        self.store.set_memory_enabled(self.user_a, False)
        with sqlite3.connect(self.store.database_path) as connection:
            before = connection.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]
        next_confirmation = self.store.create_confirmation(
            self.user_a,
            action="memory.preference",
            preview={"preference_key": "language", "value": "中文"},
            expires_at="2099-01-01T00:00:00.000+00:00",
        )
        self.store.confirm(
            self.user_a,
            next_confirmation["confirmation_id"],
            next_confirmation["payload_hash"],
        )
        with self.assertRaises(AgentError) as disabled:
            self.store.write_preference(
                self.user_a,
                preference_key="language",
                value="中文",
                confirmation_id=next_confirmation["confirmation_id"],
            )
        self.assertEqual(disabled.exception.code, "memory_disabled")
        with sqlite3.connect(self.store.database_path) as connection:
            after = connection.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(self.store.list_preferences(self.user_a), [])

    def test_identical_pending_confirmation_is_reused_without_duplicate_object(self) -> None:
        session = self.store.create_session(self.user_a, "Idempotent confirmation")
        preview = {"action": "create_collection_task", "product_ids": ["prd-1"]}
        first = self.store.create_confirmation(
            self.user_a,
            session_id=session["session_id"],
            action="business.create_collection_task",
            preview=preview,
            expires_at="2099-01-01T00:00:00.000+00:00",
        )
        second = self.store.create_confirmation(
            self.user_a,
            session_id=session["session_id"],
            action="business.create_collection_task",
            preview=preview,
            expires_at="2099-01-01T00:00:00.000+00:00",
        )
        self.assertEqual(first["confirmation_id"], second["confirmation_id"])
        events = self.store.list_audit_events(self.user_a)
        self.assertEqual(
            [item["event_type"] for item in events],
            ["confirmation.created", "confirmation.reused"],
        )

    def test_concurrent_identical_confirmations_share_one_stable_object(self) -> None:
        session = self.store.create_session(self.user_a, "Concurrent idempotency")
        preview = {"action": "create_collection_task", "product_ids": ["prd-1"]}

        def create() -> str:
            return self.store.create_confirmation(
                self.user_a,
                session_id=session["session_id"],
                action="business.create_collection_task",
                preview=preview,
                expires_at="2099-01-01T00:00:00.000+00:00",
            )["confirmation_id"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            confirmation_ids = list(executor.map(lambda _index: create(), range(8)))
        self.assertEqual(len(set(confirmation_ids)), 1)
        with sqlite3.connect(self.store.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0]
        self.assertEqual(count, 1)

    def test_sensitive_preference_is_rejected_without_value_write(self) -> None:
        preview = {"preference_key": "note", "value": "API_KEY=super-secret-value"}
        confirmation = self.store.create_confirmation(
            self.user_a,
            action="memory.preference",
            preview=preview,
            expires_at="2099-01-01T00:00:00.000+00:00",
        )
        self.store.confirm(
            self.user_a, confirmation["confirmation_id"], confirmation["payload_hash"]
        )
        with self.assertRaises(AgentError) as rejected:
            self.store.write_preference(
                self.user_a,
                preference_key="note",
                value="API_KEY=super-secret-value",
                confirmation_id=confirmation["confirmation_id"],
            )
        self.assertEqual(rejected.exception.code, "sensitive_content_rejected")
        self.assertEqual(self.store.list_preferences(self.user_a), [])

    def test_audit_events_are_append_only_and_store_only_hashes(self) -> None:
        audit_id = new_id("aud")
        self.store.append_audit_event(
            self.user_a,
            audit_id=audit_id,
            event_type="tool_call.started",
            status="started",
            request_id=new_id("req"),
            tool_name="zhuojian.get_metric@v1",
            input_hash="sha256:" + "1" * 64,
        )
        events = self.store.list_audit_events(self.user_a, audit_id=audit_id)
        self.assertEqual(len(events), 1)
        self.assertNotIn("payload", events[0])
        with sqlite3.connect(self.store.database_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE audit_events SET status='tampered' WHERE audit_id=?", (audit_id,)
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_events WHERE audit_id=?", (audit_id,))

    def test_run_steps_artifacts_and_feedback_are_recoverable(self) -> None:
        session = self.store.create_session(self.user_a, "恢复测试")
        run = self.store.create_run(
            self.user_a,
            session["session_id"],
            intent="synthetic test",
            plan=[{"title": "读取报告", "tool_name": "zhuojian.get_report@v1"}],
            status="running",
        )
        step_id = run["steps"][0]["step_id"]
        self.store.update_step_status(self.user_a, step_id, "running")
        self.store.update_step_status(self.user_a, step_id, "completed")
        self.store.add_artifact(
            self.user_a,
            run_id=run["run_id"],
            artifact_type="report_reference",
            upstream_id="rpt_synthetic12345678",
            contract_version="report-json/v1",
            status="available",
            display_name="Synthetic Report",
        )
        message = self.store.add_message(
            self.user_a,
            session["session_id"],
            role="assistant",
            content="已完成",
            answer_status="supported",
            run_id=run["run_id"],
        )
        feedback = self.store.add_feedback(
            self.user_a,
            session_id=session["session_id"],
            message_id=message["message_id"],
            run_id=run["run_id"],
            rating="up",
            reason_codes=["answer_useful"],
            comment=None,
            context={"answer_sha256": "sha256:" + "2" * 64},
        )
        self.assertEqual(feedback["rating"], "up")
        bundle = self.store.session_bundle(self.user_a, session["session_id"])
        self.assertEqual(bundle["runs"][0]["steps"][0]["status"], "completed")
        self.assertEqual(bundle["runs"][0]["artifacts"][0]["status"], "available")


class StorageBoundaryTests(unittest.TestCase):
    @staticmethod
    def mode(path: Path) -> int:
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_umask_022_cannot_widen_directory_database_wal_or_shm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "wenquan-data"
            store = WenquanStore(data_dir / "wenquan.sqlite3")
            previous = os.umask(0o022)
            connection = None
            try:
                store.initialize()
                connection = store._connect()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE schema_meta SET schema_version=schema_version WHERE singleton=1"
                )
                self.assertEqual(self.mode(data_dir), 0o700)
                self.assertEqual(self.mode(store.database_path), 0o600)
                self.assertEqual(
                    self.mode(Path(str(store.database_path) + "-wal")), 0o600
                )
                self.assertEqual(
                    self.mode(Path(str(store.database_path) + "-shm")), 0o600
                )
            finally:
                if connection is not None:
                    connection.rollback()
                    connection.close()
                os.umask(previous)

    def test_restart_tightens_existing_database_backup_restore_and_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "wenquan-data"
            store = WenquanStore(data_dir / "wenquan.sqlite3")
            store.initialize()
            managed = [
                store.database_path,
                data_dir / "wenquan.sqlite3.backup-before-migration",
                data_dir / "wenquan.sqlite3.restore-candidate",
                data_dir / "wenquan.sqlite3.tmp-copy",
            ]
            for path in managed[1:]:
                path.write_text("synthetic", encoding="utf-8")
            os.chmod(data_dir, 0o755)
            for path in managed:
                os.chmod(path, 0o644)

            restarted = WenquanStore(store.database_path)
            restarted.initialize()
            self.assertEqual(self.mode(data_dir), 0o700)
            self.assertTrue(all(self.mode(path) == 0o600 for path in managed))
            security = restarted.health()["storage_security"]
            self.assertEqual(security["data_directory_mode"], "0700")
            self.assertEqual(security["database_mode"], "0600")

    def test_restart_rejects_symlink_and_special_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for kind in ("symlink", "fifo"):
                with self.subTest(kind=kind):
                    data_dir = root / kind
                    store = WenquanStore(data_dir / "wenquan.sqlite3")
                    store.initialize()
                    candidate = data_dir / f"wenquan.sqlite3.backup-{kind}"
                    if kind == "symlink":
                        target = root / f"target-{kind}"
                        target.write_text("synthetic", encoding="utf-8")
                        candidate.symlink_to(target)
                    else:
                        os.mkfifo(candidate, 0o600)
                    with self.assertRaises(AgentError) as rejected:
                        store.initialize()
                    self.assertEqual(
                        rejected.exception.code, "storage_boundary_invalid"
                    )

    def test_data_directory_and_database_symlinks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_data = root / "real-data"
            real_data.mkdir()
            linked_data = root / "linked-data"
            linked_data.symlink_to(real_data, target_is_directory=True)
            with self.assertRaises(AgentError) as directory_error:
                WenquanStore(linked_data / "wenquan.sqlite3").initialize()
            self.assertEqual(
                directory_error.exception.code, "storage_boundary_invalid"
            )

            data_dir = root / "database-link"
            data_dir.mkdir()
            target = root / "database-target"
            target.write_text("synthetic", encoding="utf-8")
            (data_dir / "wenquan.sqlite3").symlink_to(target)
            with self.assertRaises(AgentError) as database_error:
                WenquanStore(data_dir / "wenquan.sqlite3").initialize()
            self.assertEqual(
                database_error.exception.code, "storage_boundary_invalid"
            )


if __name__ == "__main__":
    unittest.main()
