from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reporting.brand_persona_store import BrandPersonaStore
from reporting.job_runtime import RuntimePolicy, WorkItem, can_start_item, queued_work_items
from reporting.storage import ReportStore


class ReportingResilienceTests(unittest.TestCase):
    def test_runtime_policy_is_bounded_and_process_based(self) -> None:
        policy = RuntimePolicy.from_environment({})
        self.assertEqual(policy.execution_mode, "process")
        self.assertEqual(policy.concurrency, 2)
        self.assertEqual(policy.brand_concurrency, 1)
        self.assertGreaterEqual(policy.heartbeat_timeout_seconds, 60)
        self.assertGreater(policy.brand_timeout_seconds, 298.512)

    def test_interactive_reports_precede_scheduled_personas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = ReportStore(root)
            personas = BrandPersonaStore(root)
            scheduled = personas.create_job(
                {"brand": "EGO"},
                purpose="featured_full",
                trigger="schedule",
            )
            report = reports.create_job(
                {"report_name": "Patio", "scope": {}, "analysis_version": "test"}
            )
            custom = personas.create_job({"brand": "RYOBI"})

            items = queued_work_items(reports, personas, active_ids=set())
            self.assertEqual(items[0].kind, "report")
            self.assertEqual(items[0].job_id, report["job_id"])
            self.assertEqual(items[1].job_id, custom["job_id"])
            self.assertEqual(items[2].job_id, scheduled["job_id"])

    def test_brand_work_reserves_capacity_for_reports(self) -> None:
        policy = RuntimePolicy.from_environment({})
        active = [WorkItem("brand-persona", "bpjob_a", 3, "2026-08-11")]
        self.assertFalse(
            can_start_item(
                WorkItem("brand-persona", "bpjob_b", 3, "2026-08-11"),
                active,
                policy,
            )
        )
        self.assertTrue(
            can_start_item(
                WorkItem("report", "rjob_a", 0, "2026-08-11"),
                active,
                policy,
            )
        )

    # Deployment-specific seed or retired frontend is outside the public package.

    def test_worker_status_is_small_and_versioned(self) -> None:
        policy = RuntimePolicy.from_environment({})
        payload = policy.status_payload(
            leader=True,
            active=[],
            queued_reports=2,
            queued_personas=1,
        )
        self.assertEqual(payload["schema_version"], "report-worker-status/v1")
        self.assertEqual(payload["concurrency"], 2)
        self.assertEqual(payload["brand_concurrency"], 1)
        json.dumps(payload)

    def test_heartbeats_are_sidecars_and_do_not_overwrite_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = ReportStore(root)
            personas = BrandPersonaStore(root)
            report = reports.create_job(
                {"report_name": "Fan", "scope": {}, "analysis_version": "test"}
            )
            reports.update_job(report["job_id"], "extracting", "全量提取", 56)
            persona = personas.create_job({"brand": "EGO"})
            personas.update_job(
                persona["job_id"], status="running", stage="全量语义提取", progress=45
            )

            reports.touch_worker(report["job_id"], worker_pid=101)
            personas.touch_worker(persona["job_id"], worker_pid=202)

            self.assertEqual(reports.get_job(report["job_id"])["progress"], 56)
            self.assertEqual(personas.get_job(persona["job_id"])["progress"], 45)
            self.assertEqual(
                reports.get_worker_heartbeat(report["job_id"])["worker_pid"], 101
            )
            self.assertEqual(
                personas.get_worker_heartbeat(persona["job_id"])["worker_pid"], 202
            )


if __name__ == "__main__":
    unittest.main()
