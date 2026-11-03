from __future__ import annotations

import gzip
import json
import tempfile
import threading
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from reporting.analysis import build_report
from reporting.tests.test_reporting import fixture_facts, manifest

if find_spec("fastapi") is not None:
    from fastapi.testclient import TestClient

    from reporting.app import ReportingService, create_app
else:  # The repository's lightweight quality-gate Python omits API extras.
    ReportingService = None  # type: ignore[assignment,misc]
    TestClient = None  # type: ignore[assignment,misc]
    create_app = None  # type: ignore[assignment,misc]


class ReportingQueueTests(unittest.TestCase):
    @unittest.skipIf(ReportingService is None, "FastAPI runtime dependency unavailable")
    def test_archive_restore_api_keeps_active_and_archived_lists_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [], "defaults": {"scope": {}}},
            ):
                app = create_app(snapshot, root / "var")
            store = app.state.reporting.store
            facts = fixture_facts()
            report = build_report(facts, manifest(facts))
            store.store_report(report)
            job = store.create_job(
                {
                    "report_name": report["title"],
                    "scope": report["scope"],
                    "analysis_version": "test",
                }
            )
            store.update_job(
                job["job_id"],
                "completed",
                "已完成",
                100,
                report_id=report["report_id"],
            )

            with TestClient(app) as client:
                invalid = client.post(
                    "/reporting/api/v1/reports/rpt_INVALID/archive",
                    json={},
                )
                self.assertEqual(invalid.status_code, 422)

                archived = client.post(
                    f"/reporting/api/v1/reports/{report['report_id']}/archive",
                    json={},
                )
                self.assertEqual(archived.status_code, 200)
                self.assertTrue(archived.json()["archived"])
                self.assertEqual(
                    client.get("/reporting/api/v1/reports").json()["items"],
                    [],
                )
                archived_items = client.get(
                    "/reporting/api/v1/reports?include_archived=true"
                ).json()["items"]
                self.assertEqual(len(archived_items), 1)
                self.assertEqual(
                    archived_items[0]["report_id"], report["report_id"]
                )
                self.assertTrue(archived_items[0]["archived"])

                restored = client.post(
                    f"/reporting/api/v1/reports/{report['report_id']}/restore",
                    json={},
                )
                self.assertEqual(restored.status_code, 200)
                self.assertFalse(restored.json()["archived"])
                active_items = client.get(
                    "/reporting/api/v1/reports"
                ).json()["items"]
                self.assertEqual(len(active_items), 1)
                self.assertEqual(
                    active_items[0]["report_id"], report["report_id"]
                )

    @unittest.skipIf(ReportingService is None, "FastAPI runtime dependency unavailable")
    def test_report_generation_uses_one_process_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [], "defaults": {"scope": {}}},
            ):
                service = ReportingService(root / "snapshot", root / "var")
            first = service.store.create_job(
                {"report_name": "first", "scope": {}, "analysis_version": "test"}
            )
            second = service.store.create_job(
                {"report_name": "second", "scope": {}, "analysis_version": "test"}
            )
            first_started = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()

            def fake_run(job_id: str) -> None:
                if job_id == first["job_id"]:
                    first_started.set()
                    release_first.wait(timeout=2)
                else:
                    second_started.set()

            service._run_job = fake_run  # type: ignore[method-assign]
            service.start_job(first["job_id"])
            self.assertTrue(first_started.wait(timeout=1))
            service.start_job(second["job_id"])
            self.assertFalse(second_started.wait(timeout=0.1))
            self.assertEqual(
                service.store.get_job(second["job_id"])["status"], "queued"
            )

            release_first.set()
            self.assertTrue(second_started.wait(timeout=1))
            for thread in service._threads.values():
                thread.join(timeout=1)

    @unittest.skipIf(ReportingService is None, "FastAPI runtime dependency unavailable")
    def test_delete_failed_report_job_api_is_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [], "defaults": {"scope": {}}},
            ):
                app = create_app(snapshot, root / "var")
            store = app.state.reporting.store
            failed = store.create_job(
                {"report_name": "failed", "scope": {}, "analysis_version": "test"}
            )
            store.update_job(
                failed["job_id"],
                "failed",
                "生成失败",
                56,
                error={"code": "TEST", "message": "fixture", "retryable": True},
            )
            retry = store.retry_job(failed["job_id"])
            queued = store.create_job(
                {"report_name": "queued", "scope": {}, "analysis_version": "test"},
                idempotency_key="api-delete-queued",
            )

            with TestClient(app) as client:
                history = client.get(
                    "/reporting/api/v1/report-job-groups/"
                    f"{failed['job_group_id']}"
                )
                self.assertEqual(history.status_code, 200)
                self.assertEqual(history.json()["attempt_count"], 2)
                self.assertEqual(history.json()["attempts"][0]["job_id"], retry["job_id"])

                blocked = client.delete(
                    f"/reporting/api/v1/report-jobs/{queued['job_id']}"
                )
                self.assertEqual(blocked.status_code, 409)
                self.assertIsNotNone(store.get_job(queued["job_id"]))

                deleted = client.delete(
                    f"/reporting/api/v1/report-jobs/{failed['job_id']}"
                )
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(
                    deleted.json(),
                    {"job_id": failed["job_id"], "deleted": True},
                )
                self.assertIsNone(store.get_job(failed["job_id"]))

                missing = client.delete(
                    "/reporting/api/v1/report-jobs/rjob_missing"
                )
                self.assertEqual(missing.status_code, 404)

    @unittest.skipIf(ReportingService is None, "FastAPI runtime dependency unavailable")
    def test_report_review_fact_json_export_is_immutable_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            var_root = root / "var"
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [], "defaults": {"scope": {}}},
            ):
                app = create_app(snapshot, var_root)
            store = app.state.reporting.store
            report = {
                "report_id": "rpt_1234567890abcdef12345678",
                "title": "Fixture report",
                "generated_at": "2026-08-10T00:00:00+00:00",
                "data_snapshot_id": "snap_1234567890abcdef12345678",
                "scope": {"category_id": "leaf_blower"},
            }
            store.store_report(report)
            fact_dir = var_root / "snapshots" / report["data_snapshot_id"]
            fact_dir.mkdir(parents=True)
            rows = [
                {
                    "review_id": "review-1",
                    "review_text": "Full review one",
                    "rating": 5,
                    "source_site": "example.test",
                },
                {
                    "review_id": "review-2",
                    "review_text": "Full review two",
                    "rating": 2,
                    "source_site": "example.test",
                },
            ]
            with gzip.open(
                fact_dir / "review_fact.preview.jsonl.gz",
                "wt",
                encoding="utf-8",
            ) as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

            with TestClient(app) as client:
                response = client.get(
                    f"/reporting/api/v1/reports/{report['report_id']}/export/reviews-json"
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["schema_version"], "report-review-export/v1")
                self.assertEqual(payload["report_id"], report["report_id"])
                self.assertEqual(payload["data_snapshot_id"], report["data_snapshot_id"])
                self.assertEqual(payload["record_count"], 2)
                self.assertEqual(payload["records"], rows)
                self.assertIn(
                    "attachment;",
                    response.headers["content-disposition"],
                )
                self.assertEqual(
                    response.headers["x-voxquarry-export-schema"],
                    "report-review-export/v1",
                )

                (fact_dir / "review_fact.preview.jsonl.gz").unlink()
                unavailable = client.get(
                    f"/reporting/api/v1/reports/{report['report_id']}/export/reviews-json"
                )
                self.assertEqual(unavailable.status_code, 409)


if __name__ == "__main__":
    unittest.main()
