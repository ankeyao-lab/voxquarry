import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from reporting.agent_report_workflow import AgentReportWorkflow
from reporting.agent_tools import AgentToolError, ToolAuthority
from reporting.storage import ReportStore
from reporting.contracts import normalize_request, default_scope
from reporting.config import ANALYSIS_VERSION


CATALOG = {"source_snapshot": "fixture-v1", "tasks": [
    {"id": "job_one", "category_id": "walk_behind_mower", "brand": "EGO", "model": "LM2236SP",
     "channel": "lowes", "market_region": "North America", "energy_type": "battery", "eligible_reviews": 604, "eligible_valid_text_reviews": 600},
    {"id": "job_empty", "category_id": "walk_behind_mower", "brand": "EGO", "model": "LM9999",
     "channel": "lowes", "market_region": "North America", "energy_type": "battery", "eligible_reviews": 0}]}


class ReportWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ReportStore(root / "runtime")
        self.created = []
        def create(payload, idempotency_key):
            normalize_request(payload, defaults=default_scope(), analysis_version=ANALYSIS_VERSION)
            self.created.append(payload)
            return self.store.create_job(payload, idempotency_key=idempotency_key)
        self.workflow = AgentReportWorkflow(SimpleNamespace(store=self.store, snapshot_root=root / "snapshot"), create, "test-secret")
        self.actor = ToolAuthority("internal-pm", "user-a", None)
        self.catalog_patch = patch("reporting.agent_report_workflow.catalog", return_value=CATALOG)
        self.catalog_patch.start()
        self.candidate = self.workflow.invoke("catalog", {}, self.actor)["items"][0]
        self.payload = {"candidate_id": self.candidate["candidate_id"], "date_range": {}}

    def tearDown(self):
        self.catalog_patch.stop()
        self.tmp.cleanup()

    def test_only_existing_collected_scope_is_created_and_reused(self):
        self.assertEqual(len(self.workflow.invoke("catalog", {}, self.actor)["items"]), 1)
        first = self.workflow.invoke("ensure", self.payload, self.actor)
        second = self.workflow.invoke("ensure", self.payload, self.actor)
        self.assertEqual(first, second)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.created[0]["scope"]["job_ids"], ["job_one"])
        self.assertEqual(set(self.created[0]), {"scope"})

    def test_product_source_scope_is_preserved_through_creation(self):
        scope = {"job_ids": ["job_one"], "source_product_ids": ["source-target"]}
        candidate = self.workflow.invoke('catalog', {'scope': scope}, self.actor)['items'][0]
        self.workflow.invoke('ensure', {'candidate_id': candidate['candidate_id'], 'scope': scope, 'date_range': {}}, self.actor)
        self.assertEqual(self.created[0]['scope']['source_product_ids'], ['source-target'])
        self.assertEqual(self.workflow.invoke('catalog', {'scope': {'job_ids': ['absent']}}, self.actor)['items'], [])

    def test_returned_candidate_scope_can_be_reused_and_regions_are_enforced(self):
        candidate = self.workflow.invoke('catalog', {'scope': {'category_id': 'walk_behind_mower', 'market_regions': ['North America']}}, self.actor)['items'][0]
        self.workflow.invoke('ensure', {**self.payload, 'scope': candidate['scope']}, self.actor)
        self.assertEqual(self.created[0]['scope']['market_regions'], ['North America'])
        self.assertEqual(self.workflow.invoke('catalog', {'scope': {'market_regions': ['Europe']}}, self.actor)['items'], [])
        self.assertEqual(self.workflow.invoke('catalog', {'scope': {'energy_type': 'gas'}} , self.actor)['items'], [])

    def test_platform_candidate_combines_matching_models_without_other_voltage(self):
        tasks = [{**CATALOG['tasks'][0], 'id': 'a', 'model': 'A', 'brand': 'RYOBI', 'voltage_platform_key': 'ryobi|18V'},
                 {**CATALOG['tasks'][0], 'id': 'b', 'model': 'B', 'brand': 'RYOBI', 'voltage_platform_key': 'ryobi|18V'},
                 {**CATALOG['tasks'][0], 'id': 'c', 'model': 'C', 'brand': 'RYOBI', 'voltage_platform_key': 'ryobi|40V'}]
        with patch('reporting.agent_report_workflow.catalog', return_value={**CATALOG, 'tasks': tasks}):
            items = self.workflow.invoke('catalog', {'scope': {'voltage_platform_keys': ['ryobi|18V']}}, self.actor)['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['scope']['job_ids'], ['a', 'b'])
        self.assertEqual(items[0]['scope']['models'], ['A', 'B'])

    def test_allowlist_cannot_expand_to_raw_catalog_or_generation(self):
        limited = ToolAuthority("internal-pm", "user-a", frozenset({"rpt_existing"}))
        for operation, payload in (("catalog", {}), ("ensure", self.payload)):
            with self.subTest(operation=operation), self.assertRaises(AgentToolError) as failure:
                self.workflow.invoke(operation, payload, limited)
            self.assertEqual(failure.exception.http_status, 403)
        self.assertFalse(self.created)

    def test_job_token_is_bound_to_principal_and_cancel_is_bounded(self):
        prepared = self.workflow.invoke("ensure", self.payload, self.actor)
        args = {"job_id": prepared["job"]["job_id"], "job_token": prepared["job_token"]}
        with self.assertRaises(AgentToolError):
            self.workflow.invoke("cancel", args, ToolAuthority("internal-pm", "user-b", None))
        self.assertEqual(self.store.get_job(args["job_id"])["status"], "queued")
        cancelled = self.workflow.invoke("cancel", args, self.actor)
        self.assertTrue(cancelled["job"]["cancel_requested"])
        self.store.update_job(args["job_id"], "cancelled", "已取消", 0)
        retried = self.workflow.invoke("ensure", self.payload, self.actor)
        self.assertNotEqual(retried["job"]["job_id"], args["job_id"])
        self.assertEqual(self.workflow.invoke("ensure", self.payload, self.actor), retried)

    def test_scope_injection_stale_snapshot_and_invalid_dates_do_not_create(self):
        for payload in ({**self.payload, "scope": {"job_ids": ["outside"]}},
                        {**self.payload, "candidate_id": "rc_unknown"},
                        {**self.payload, "date_range": {"start": "2026-02-30", "end": "2026-03-30"}},
                        {**self.payload, "date_range": {"start": "2026-09-30", "end": "2026-03-30"}},
                        {**self.payload, "date_range": {"start": "2026-01-01"}}):
            with self.subTest(payload=payload), self.assertRaises(AgentToolError):
                self.workflow.invoke("ensure", payload, self.actor)
        self.assertFalse(self.created)

    def test_insufficient_written_reviews_explains_block_without_creating_job(self):
        small = {**self.candidate, "written_review_count": 25}
        with patch.object(self.workflow, "_candidates", return_value=[small]), self.assertRaises(AgentToolError) as failure:
            self.workflow.invoke("ensure", self.payload, self.actor)
        self.assertEqual(failure.exception.code, "report_insufficient_comments")
        self.assertIn("500", failure.exception.message)
        self.assertFalse(self.created)

    def test_private_api_needs_both_switches_and_gateway_identity(self):
        from fastapi.testclient import TestClient
        from reporting.app import create_app
        for tools_enabled, workflow_enabled in (("false", "true"), ("true", "false"), ("true", "true")):
            with self.subTest(tools_enabled=tools_enabled, workflow_enabled=workflow_enabled), patch.dict("os.environ", {
                "VOXQUARRY_AGENT_TOOLS_ENABLED": tools_enabled,
                "VOXQUARRY_WENQUAN_REPORT_GENERATION_ENABLED": workflow_enabled,
                "VOXQUARRY_AGENT_GATEWAY_TOKEN": "test-secret"}), patch("reporting.app.build_catalog_payload", return_value={"tasks": [], "defaults": {"scope": {}}}):
                root = Path(self.tmp.name) / (tools_enabled + workflow_enabled)
                (root / "snapshot").mkdir(parents=True)
                with TestClient(create_app(root / "snapshot", root / "runtime")) as client:
                    path = "/reporting/api/v1/agent-reports/catalog"
                    enabled = tools_enabled == workflow_enabled == "true"
                    self.assertEqual(client.post(path, json={}).status_code, 401 if enabled else 404)
                    if enabled:
                        headers = {"X-VoxQuarry-Gateway-Token": "test-secret", "X-VoxQuarry-Authority-Id": "internal-pm",
                                   "X-VoxQuarry-Principal-Id": "user-a", "X-VoxQuarry-Allowed-Report-Ids": "*"}
                        self.assertEqual(client.post(path, json={}, headers=headers).status_code, 200)
