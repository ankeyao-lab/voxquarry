from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from reporting import job_runtime as runtime
from reporting.storage import atomic_json, utc_now


LOCK_FILENAME = ".reporting-dispatch.lock"
REQUEST_FILENAME = ".reporting-drain-request.json"
SCHEMA = "voxquarry.reporting-drain/v1"
REQUEST_ID = "55181acd-082c-4de0-8639-3a441f3469fb"


class ReportingDrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.supervisor = runtime.JobSupervisor(
            snapshot_root=self.root / "snapshot", var_root=self.root / "runtime"
        )
        self.var_root = self.supervisor.var_root
        self.addCleanup(self._close_supervisor)

    def _close_supervisor(self) -> None:
        for worker in self.supervisor._active.values():
            worker.log_handle.close()
        self.supervisor._active.clear()
        self.supervisor.close()

    def _queue_both(self) -> tuple[str, str]:
        report = self.supervisor.reports.create_job(
            {"report_name": "Fixture", "scope": {}, "analysis_version": "test"}
        )
        persona = self.supervisor.personas.create_job({"brand": "EGO"})
        return report["job_id"], persona["job_id"]

    def _hold_dispatch(self) -> int:
        descriptor = os.open(self.var_root / LOCK_FILENAME, os.O_RDONLY | os.O_CREAT, 0o644)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def _write_request(self, **changes: object) -> None:
        request = {
            "schema_version": SCHEMA,
            "request_id": REQUEST_ID,
            "requested_at": utc_now(),
            **changes,
        }
        atomic_json(self.var_root / REQUEST_FILENAME, request)

    def _status(self) -> dict:
        self.supervisor._write_status()
        return runtime.read_worker_status(self.var_root) or {}

    def test_unlocked_worker_dispatches_both_kinds_with_shared_lock(self) -> None:
        self._queue_both()
        descriptor = os.open(self.var_root / LOCK_FILENAME, os.O_RDONLY | os.O_CREAT, 0o644)
        self.addCleanup(os.close, descriptor)
        started = []

        def start(item: runtime.WorkItem) -> None:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started.append(item.kind)

        with patch.object(self.supervisor, "_start_item", side_effect=start):
            self.supervisor._dispatch_queued()
        self.assertEqual(started, ["report", "brand-persona"])
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        self.assertEqual(self._status()["drain"]["state"], "running")

    def test_drain_preserves_queued_jobs_and_acknowledges_own_token(self) -> None:
        report_id, persona_id = self._queue_both()
        self._hold_dispatch()
        self._write_request()
        self.supervisor._leader = True
        with patch.object(self.supervisor, "_start_item") as start:
            self.supervisor._dispatch_queued()
        start.assert_not_called()
        status = self._status()
        self.assertTrue(status["leader"])
        self.assertEqual(status["worker_instance_id"], os.environ.get("HOSTNAME") or "local")
        self.assertIsNotNone(runtime._age_seconds(status["updated_at"]))
        self.assertEqual(status["drain"], {
            "schema_version": SCHEMA, "request_id": REQUEST_ID, "state": "drained"
        })
        self.assertEqual(status["queued_report_jobs"], 1)
        self.assertEqual(status["queued_brand_persona_jobs"], 1)
        self.assertEqual(self.supervisor.reports.get_job(report_id)["status"], "queued")
        self.assertEqual(self.supervisor.personas.get_job(persona_id)["status"], "queued")

    def test_existing_process_finishes_normally_while_drain_waits(self) -> None:
        report_id, persona_id = self._queue_both()
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.object(runtime.subprocess, "Popen", return_value=process):
            self.supervisor._start_item(runtime.WorkItem("report", report_id, 0, utc_now()))
            self.supervisor._start_item(runtime.WorkItem("brand-persona", persona_id, 1, utc_now()))
        self._hold_dispatch()
        self._write_request()
        with patch.object(self.supervisor, "_stop_process") as stop:
            self.supervisor._monitor()
            self.supervisor._dispatch_queued()
            self.assertEqual(self._status()["drain"]["state"], "draining")
            self.supervisor.reports.update_job(report_id, "completed", "完成", 100)
            self.supervisor.personas.update_job(
                persona_id, status="completed", stage="完成", progress=100
            )
            process.poll.return_value = 0
            self.supervisor._monitor()
            self.supervisor._dispatch_queued()
            self.assertEqual(self._status()["drain"]["state"], "drained")
        stop.assert_not_called()
        self.assertEqual(self.supervisor.reports.get_job(report_id)["status"], "completed")
        self.assertEqual(self.supervisor.personas.get_job(persona_id)["status"], "completed")

    def test_supervisor_loop_continues_monitoring_and_publishing_while_drained(self) -> None:
        self._queue_both()
        self._hold_dispatch()
        self._write_request()
        with (
            patch.object(self.supervisor, "_monitor", wraps=self.supervisor._monitor) as monitor,
            patch.object(self.supervisor, "_start_item") as start,
            patch.object(self.supervisor._wake, "wait", side_effect=lambda **_: self.supervisor._stop.set()),
        ):
            self.supervisor.run_forever()
        monitor.assert_called_once()
        start.assert_not_called()
        status = runtime.read_worker_status(self.var_root)
        self.assertEqual(status["drain"]["state"], "drained")
        self.assertEqual(status["queued_report_jobs"], 1)
        self.assertEqual(status["queued_brand_persona_jobs"], 1)

    def test_queue_added_after_ack_cannot_start_until_lock_released(self) -> None:
        descriptor = self._hold_dispatch()
        self._write_request()
        self.supervisor._dispatch_queued()
        self.assertEqual(self._status()["drain"]["state"], "drained")
        report_id, persona_id = self._queue_both()
        with patch.object(self.supervisor, "_start_item") as start:
            self.supervisor._dispatch_queued()
            start.assert_not_called()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            self.supervisor._dispatch_queued()
        self.assertEqual([call.args[0].job_id for call in start.call_args_list], [report_id, persona_id])
        self.assertEqual(self._status()["drain"], {
            "schema_version": SCHEMA, "request_id": None, "state": "running"
        })
        self.assertTrue((self.var_root / REQUEST_FILENAME).exists())

    def test_missing_or_malformed_request_blocks_without_ack(self) -> None:
        self._hold_dispatch()
        self._queue_both()
        request_path = self.var_root / REQUEST_FILENAME
        for content in (None, "{broken", "[]", json.dumps({
            "schema_version": SCHEMA,
            "request_id": "not-a-uuid",
            "requested_at": utc_now(),
        }), json.dumps({
            "schema_version": SCHEMA,
            "request_id": REQUEST_ID,
            "requested_at": "2026-09-08T10:00:00",
        })):
            with self.subTest(content=content):
                if content is not None:
                    request_path.write_text(content, encoding="utf-8")
                with patch.object(self.supervisor, "_start_item") as start:
                    self.supervisor._dispatch_queued()
                start.assert_not_called()
                drain = self._status()["drain"]
                self.assertEqual(drain["state"], "blocked")
                self.assertIsNone(drain["request_id"])
                self.assertTrue(drain["error"])

    def test_replacement_supervisor_observes_same_drain_and_resumes_queue(self) -> None:
        report_id, persona_id = self._queue_both()
        descriptor = self._hold_dispatch()
        self._write_request()
        replacement = runtime.JobSupervisor(
            snapshot_root=self.root / "new-snapshot", var_root=self.var_root
        )
        self.addCleanup(replacement.close)
        self.assertTrue(replacement._acquire_leader())
        replacement._recover()
        with patch.object(replacement, "_start_item") as start:
            replacement._dispatch_queued()
            replacement._write_status()
            start.assert_not_called()
            self.assertEqual(runtime.read_worker_status(self.var_root)["drain"]["state"], "drained")
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            replacement._dispatch_queued()
        self.assertEqual([call.args[0].job_id for call in start.call_args_list], [report_id, persona_id])

    def test_lock_opens_read_only_without_relaxing_permissions(self) -> None:
        path = self.var_root / LOCK_FILENAME
        path.touch(mode=0o444)
        descriptor = runtime.open_dispatch_lock(self.var_root)
        self.addCleanup(os.close, descriptor)
        self.assertEqual(fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
        self.assertFalse(os.get_inheritable(descriptor))

    def test_lock_rejects_symlink_and_non_regular_file(self) -> None:
        path = self.var_root / LOCK_FILENAME
        target = self.root / "external"
        target.touch()
        path.symlink_to(target)
        with self.assertRaises((OSError, RuntimeError)):
            runtime.open_dispatch_lock(self.var_root)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises((OSError, RuntimeError)):
            runtime.open_dispatch_lock(self.var_root)

    def test_unusable_lock_does_not_dispatch_or_stop_monitor_loop(self) -> None:
        self._queue_both()
        with (
            patch.object(runtime, "open_dispatch_lock", side_effect=PermissionError("denied")),
            patch.object(self.supervisor, "_start_item") as start,
        ):
            self.supervisor._dispatch_queued()
        start.assert_not_called()
        self.assertEqual(self._status()["drain"]["state"], "blocked")

    def test_drain_request_cannot_follow_symlink(self) -> None:
        self._hold_dispatch()
        self._queue_both()
        self._write_request()
        path = self.var_root / REQUEST_FILENAME
        target = self.root / "external-request.json"
        path.rename(target)
        path.symlink_to(target)
        with patch.object(self.supervisor, "_start_item") as start:
            self.supervisor._dispatch_queued()
        start.assert_not_called()
        self.assertEqual(self._status()["drain"]["state"], "blocked")

    def test_dispatch_exception_releases_shared_lock(self) -> None:
        self._queue_both()
        with patch.object(self.supervisor, "_start_item", side_effect=RuntimeError("spawn failed")):
            with self.assertRaisesRegex(RuntimeError, "spawn failed"):
                self.supervisor._dispatch_queued()
        self._hold_dispatch()


if __name__ == "__main__":
    unittest.main()
