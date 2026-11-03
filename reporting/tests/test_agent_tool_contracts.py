from __future__ import annotations

import json
import os
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

if find_spec("fastapi") is not None:
    from fastapi.testclient import TestClient

    from reporting.app import create_app
else:  # The lightweight quality-gate runtime intentionally omits API extras.
    TestClient = None  # type: ignore[assignment,misc]
    create_app = None  # type: ignore[assignment,misc]

if find_spec("jsonschema") is not None:
    from jsonschema import Draft202012Validator
else:
    Draft202012Validator = None  # type: ignore[assignment,misc]

from reporting.agent_tools import (
    AgentToolError,
    AgentToolService,
    TOOL_CONTRACT_VERSION,
    TOOL_NAMES,
    ToolAuthority,
    authority_from_gateway,
)
from reporting.evidence_store import EvidenceRepository
from reporting.storage import ReportStore


REPORT_ID = "rpt_agentfixture1234567890"
AGENT_HTTP_PATHS = (
    "/reporting/api/v1/agent-tools/zhuojian.get_report@v1",
    "/reporting/api/v1/agent-tools/zhuojian.get_report_section@v1",
    "/reporting/api/v1/agent-tools/zhuojian.query_report_reviews@v1",
    "/reporting/api/v1/agent-tools/zhuojian.aggregate_report_reviews@v1",
    "/reporting/api/v1/agent-citations/validate",
    "/reporting/api/v1/tools/create_report",
    "/reporting/api/v1/tools/query_brand_persona",
    "/reporting/api/v1/tools/create_brand_persona",
    "/reporting/api/v1/tools/get_brand_persona",
    "/reporting/api/v1/tools/get_brand_persona_evidence",
    "/reporting/api/v1/tools/get_report",
    "/reporting/api/v1/tools/list_reports",
    "/reporting/api/v1/tools/query_report_metric",
    "/reporting/api/v1/tools/get_report_evidence",
    "/reporting/api/v1/tools/search_report_evidence",
    "/reporting/api/v1/tools/query_product_insight",
)


def fixture_report() -> dict:
    return {
        "report_id": REPORT_ID,
        "title": "EGO Leaf Blower 全渠道量化洞察",
        "schema_version": "report-json/v1",
        "analysis_version": "voxquarry-reporting/test",
        "status": "completed",
        "generated_at": "2026-08-17T00:00:00+00:00",
        "data_snapshot_id": "snap_agentfixture1234567890",
        "hashes": {
            "data": "sha256:" + "1" * 64,
            "core_result": "sha256:" + "2" * 64,
        },
        "research_plan": {
            "primary_direction": "satisfaction-overview",
            "analysis_modules": ["overview"],
            "factors": ["ratings"],
        },
        "scope": {
            "category_id": "leaf_blower",
            "brands": ["EGO"],
            "job_ids": ["fixture-job"],
        },
        "samples": {
            "review_entities": 20,
            "written_review_entities": 18,
        },
        "quality": {
            "schema_version": "report-data-quality/v1",
            "source_completeness": [],
            "limited_source_jobs": 0,
        },
        "metric_definitions": [
            {
                "metric_id": "positive_rating_rate",
                "name": "好评率",
                "definition": "4–5 星实体数 / 有效评分实体数",
                "unit": "ratio",
                "population_unit": "review_entity",
                "version": "voxquarry-reporting/test",
            }
        ],
        "metrics": [
            {
                "metric_id": "positive_rating_rate",
                "value": 0.75,
                "sample_size": 20,
                "numerator": 15,
                "denominator": 20,
                "interval_95": [0.53, 0.89],
                "filters": {},
                "quality_flags": [],
            }
        ],
        "tables": [],
        "findings": [],
        "sections": [],
        "summary_text": "样本覆盖 20 个唯一评论实体。",
        "limitations": [
            {
                "code": "observational_source",
                "severity": "informational",
                "message": "网评为观察性来源。",
            }
        ],
        "method": {
            "llm_used": False,
            "market_contract": {
                "mode": "current",
                "version": "review-market-eligibility/1.1.0",
            },
        },
    }


def fixture_evidence() -> tuple[list[dict], dict[str, dict]]:
    rows = [
        {
            "evidence_id": "ev_agent_1",
            "evidence_type": "review_quote",
            "review_entity_id": "re_agent_1",
            "review_id": "review-agent-1",
            "channel": "ego",
            "brand": "EGO",
            "model": "LB7654",
            "market_region": "North America",
            "market_region_evidence": "source_market",
            "display_site": "egopowerplus.com",
            "source_site": "egopowerplus.com",
            "rating": 5,
            "submission_date": "2026-08-01",
            "kind": "aspect:power",
            "label": "动力",
            "polarity": "positive",
            "quote": "Moves wet leaves without slowing down.",
            "matched_text": "wet leaves",
            "source_url": "https://example.test/review-agent-1",
        }
    ]
    groups = {
        "evg_agent_power": {
            "path": "aspect.power.positive",
            "label": "动力 · 正向",
            "evidence_ids": ["ev_agent_1"],
            "representative_evidence_ids": ["ev_agent_1"],
        }
    }
    return rows, groups


class AgentToolContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = ReportStore(root)
        self.evidence = EvidenceRepository(root)
        self.report = fixture_report()
        self.store.store_report(self.report)
        rows, groups = fixture_evidence()
        self.evidence.store(REPORT_ID, rows, groups)
        self.service = AgentToolService(self.store, self.evidence)
        self.authority = ToolAuthority(
            authority_id="gateway-internal",
            principal_id="pm-agent-fixture",
            allowed_report_ids=frozenset({REPORT_ID}),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_read_only_tools_have_strict_independent_schemas(self) -> None:
        self.assertEqual(
            TOOL_NAMES,
            (
                "zhuojian.list_reports@v1",
                "zhuojian.get_report@v1",
                "zhuojian.get_metric@v1",
                "zhuojian.list_evidence_groups@v1",
                "zhuojian.query_evidence@v1",
                "zhuojian.get_data_quality@v1",
                "zhuojian.get_report_section@v1",
                "zhuojian.query_report_reviews@v1",
                "zhuojian.aggregate_report_reviews@v1",
            ),
        )
        schema_root = Path(__file__).resolve().parents[1] / "schemas"
        for tool in TOOL_NAMES:
            filename = tool.replace("@", "-") + ".schema.json"
            schema = json.loads(
                (schema_root / filename).read_text(encoding="utf-8")
            )
            self.assertFalse(schema["additionalProperties"], filename)
        envelope = json.loads(
            (schema_root / "zhuojian-tool-envelope-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(envelope["additionalProperties"])
        self.assertEqual(
            set(envelope["required"]),
            {
                "contract",
                "tool",
                "status",
                "snapshot",
                "report",
                "authority",
                "quality",
                "limitations",
                "citations",
                "page",
                "error",
                "data",
            },
        )

    def test_gateway_authority_is_not_part_of_model_payload(self) -> None:
        authority = authority_from_gateway(
            {
                "x-voxquarry-gateway-token": "secret",
                "x-voxquarry-authority-id": "internal-pm",
                "x-voxquarry-principal-id": "agent-1",
                "x-voxquarry-allowed-report-ids": REPORT_ID,
            },
            "secret",
        )
        self.assertTrue(authority.permits(REPORT_ID))
        with self.assertRaisesRegex(AgentToolError, "未知字段"):
            self.service.invoke(
                "zhuojian.get_report@v1",
                {"report_id": REPORT_ID, "authority_id": "model-choice"},
                authority,
            )
        with self.assertRaisesRegex(AgentToolError, "可信 Gateway"):
            authority_from_gateway({}, "secret")

    def test_report_and_metric_are_read_from_saved_artifact(self) -> None:
        report_result = self.service.invoke(
            "zhuojian.get_report@v1",
            {"report_id": REPORT_ID},
            self.authority,
        )
        self.assertEqual(report_result["contract"], TOOL_CONTRACT_VERSION)
        self.assertEqual(report_result["report"]["report_id"], REPORT_ID)
        self.assertEqual(
            report_result["snapshot"]["snapshot_id"],
            "snap_agentfixture1234567890",
        )
        self.assertNotIn("evidence", report_result["data"])

        metric_result = self.service.invoke(
            "zhuojian.get_metric@v1",
            {"report_id": REPORT_ID, "metric_id": "positive_rating_rate"},
            self.authority,
        )
        self.assertEqual(metric_result["data"]["value"], 0.75)
        self.assertEqual(metric_result["data"]["numerator"], 15)
        self.assertEqual(metric_result["data"]["denominator"], 20)
        self.assertEqual(
            metric_result["data"]["definition"]["version"],
            "voxquarry-reporting/test",
        )
        self.assertEqual(
            metric_result["citations"][0]["citation_type"], "metric"
        )

    def test_authority_filters_list_and_blocks_direct_report_access(self) -> None:
        other = {**self.report, "report_id": "rpt_otherfixture1234567890"}
        self.store.store_report(other)
        result = self.service.invoke(
            "zhuojian.list_reports@v1", {}, self.authority
        )
        self.assertEqual(result["page"]["total"], 1)
        self.assertEqual(result["data"]["items"][0]["report_id"], REPORT_ID)
        with self.assertRaisesRegex(AgentToolError, "允许范围"):
            self.service.invoke(
                "zhuojian.get_report@v1",
                {"report_id": other["report_id"]},
                self.authority,
            )

    def test_evidence_defaults_to_20_caps_at_50_and_citations_validate(self) -> None:
        groups = self.service.invoke(
            "zhuojian.list_evidence_groups@v1",
            {"report_id": REPORT_ID},
            self.authority,
        )
        self.assertEqual(groups["page"]["limit"], 20)
        self.assertEqual(groups["data"]["items"][0]["evidence_rows"], 1)
        result = self.service.invoke(
            "zhuojian.query_evidence@v1",
            {
                "report_id": REPORT_ID,
                "evidence_group_id": "evg_agent_power",
            },
            self.authority,
        )
        self.assertEqual(result["page"]["limit"], 20)
        citation = result["citations"][0]
        self.assertEqual(citation["evidence_id"], "ev_agent_1")
        self.assertTrue(citation["quote_sha256"].startswith("sha256:"))
        checked = self.service.validate_citations([citation], self.authority)
        self.assertTrue(checked["valid"])
        tampered = {**citation, "quote_sha256": "sha256:" + "0" * 64}
        self.assertFalse(
            self.service.validate_citations([tampered], self.authority)["valid"]
        )
        injected = {**citation, "authority_id": "model-choice"}
        checked_injected = self.service.validate_citations(
            [injected], self.authority
        )
        self.assertFalse(checked_injected["valid"])
        self.assertEqual(
            checked_injected["items"][0]["error_code"], "invalid_citation"
        )
        with self.assertRaisesRegex(AgentToolError, "1–50"):
            self.service.invoke(
                "zhuojian.query_evidence@v1",
                {
                    "report_id": REPORT_ID,
                    "evidence_group_id": "evg_agent_power",
                    "limit": 51,
                },
                self.authority,
            )

    def test_data_quality_and_error_envelope_are_uniform(self) -> None:
        result = self.service.invoke(
            "zhuojian.get_data_quality@v1",
            {"report_id": REPORT_ID},
            self.authority,
        )
        self.assertEqual(result["quality"]["status"], "available")
        self.assertEqual(result["data"]["samples"]["review_entities"], 20)
        error = AgentToolError(
            "metric_not_found", "fixture", http_status=404
        )
        envelope = self.service.error_envelope(
            "zhuojian.get_metric@v1", self.authority, error
        )
        self.assertEqual(set(envelope), set(result))
        self.assertEqual(envelope["status"], "error")
        self.assertEqual(envelope["error"]["code"], "metric_not_found")

    @unittest.skipIf(
        Draft202012Validator is None,
        "JSON Schema validator dependency unavailable",
    )
    def test_success_and_error_outputs_validate_against_common_schema(self) -> None:
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas"
                / "zhuojian-tool-envelope-v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        success = self.service.invoke(
            "zhuojian.get_metric@v1",
            {"report_id": REPORT_ID, "metric_id": "positive_rating_rate"},
            self.authority,
        )
        self.assertEqual(list(validator.iter_errors(success)), [])
        failure = self.service.error_envelope(
            "zhuojian.get_metric@v1",
            self.authority,
            AgentToolError("metric_not_found", "fixture", http_status=404),
        )
        self.assertEqual(list(validator.iter_errors(failure)), [])

    @unittest.skipIf(Draft202012Validator is None, "JSON Schema validator dependency unavailable")
    def test_research_inputs_sources_and_100_row_envelopes_match_versioned_schemas(self) -> None:
        from reporting.tests.test_research_data_tools import build_research_fixture

        fixture = build_research_fixture(Path(self.temporary.name) / "research")
        service = AgentToolService(fixture["store"], fixture["evidence"])
        report_id = fixture["report"]["report_id"]
        authority = ToolAuthority("research-contract", "fixture", frozenset({report_id}))
        root = Path(__file__).resolve().parents[1] / "schemas"
        load = lambda filename: Draft202012Validator(json.loads((root / filename).read_text(encoding="utf-8")))
        envelope = load("zhuojian-tool-envelope-v1.schema.json")
        source = load("zhuojian-research-source-v1.schema.json")
        for tool in ("zhuojian.get_report_section@v1", "zhuojian.query_report_reviews@v1", "zhuojian.aggregate_report_reviews@v1"):
            with self.subTest(tool=tool):
                validator = load(tool.replace("@", "-") + ".schema.json")
                payload = {"report_id": report_id}
                self.assertEqual(list(validator.iter_errors(payload)), [])
                self.assertTrue(list(validator.iter_errors({**payload, "snapshot_id": "model-chosen"})))
                if tool != "zhuojian.get_report_section@v1":
                    for mode in ("substring", "word"):
                        self.assertEqual(list(validator.iter_errors({**payload, "match_mode": mode})), [])
                    self.assertTrue(list(validator.iter_errors({**payload, "match_mode": "regex"})))
                result = service.invoke(tool, payload, authority)
                self.assertEqual(list(envelope.iter_errors(result)), [])
                self.assertTrue(result["data"]["sources"])
                for ref in result["data"]["sources"]:
                    self.assertEqual(list(source.iter_errors(ref)), [])
                    self.assertTrue(list(source.iter_errors({**ref, "authority": "model-chosen"})))

    def test_legacy_agent_mutations_and_unscoped_report_aliases_are_retired(self) -> None:
        app_source = (
            Path(__file__).resolve().parents[1] / "app.py"
        ).read_text(encoding="utf-8")
        self.assertIn("Agent mutation 已关闭", app_source)
        self.assertIn("zhuojian.get_report@v1", app_source)
        self.assertIn("VOXQUARRY_AGENT_GATEWAY_TOKEN", app_source)
        self.assertNotIn("zhuojian.create", TOOL_NAMES)

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    @unittest.skipIf(TestClient is None, "FastAPI runtime dependency unavailable")
    def test_disabled_or_missing_agent_tools_are_absent_and_return_404(self) -> None:
        for marker in (None, "false"):
            with self.subTest(marker=marker):
                root = Path(self.temporary.name) / f"disabled-{marker}"
                snapshot = root / "snapshot"
                snapshot.mkdir(parents=True)
                with patch.dict("os.environ", {}, clear=False):
                    os.environ.pop("VOXQUARRY_AGENT_TOOLS_ENABLED", None)
                    if marker is not None:
                        os.environ["VOXQUARRY_AGENT_TOOLS_ENABLED"] = marker
                    with patch(
                        "reporting.app.build_catalog_payload",
                        return_value={"tasks": [], "defaults": {"scope": {}}},
                    ):
                        app = create_app(snapshot, root / "var")
                openapi = app.openapi()
                exposed_paths = set(openapi["paths"])
                self.assertFalse(
                    any(
                        path.startswith("/reporting/api/v1/agent-tools")
                        or path.startswith("/reporting/api/v1/agent-citations")
                        or path.startswith("/reporting/api/v1/tools")
                        for path in exposed_paths
                    )
                )
                serialized = json.dumps(openapi, sort_keys=True)
                for tool_name in TOOL_NAMES:
                    self.assertNotIn(tool_name, serialized)
                self.assertNotIn("zhuojian-agent-tools/v1", serialized)
                with TestClient(app) as client:
                    for path in AGENT_HTTP_PATHS:
                        for method in ("get", "post", "options"):
                            response = client.request(method, path, json={})
                            self.assertEqual(
                                response.status_code, 404, f"{method} {path}"
                            )
                            self.assertEqual(
                                response.json(), {"detail": "Not Found"}
                            )
                    ordinary = client.get("/reporting/api/v1/health")
                    self.assertEqual(ordinary.status_code, 200)

    @unittest.skipIf(TestClient is None, "FastAPI runtime dependency unavailable")
    def test_invalid_agent_tools_switch_prevents_application_start(self) -> None:
        root = Path(self.temporary.name) / "invalid-switch"
        snapshot = root / "snapshot"
        snapshot.mkdir(parents=True)
        for marker in ("", "TRUE", "yes", "0", "sometimes"):
            with self.subTest(marker=marker), patch.dict(
                "os.environ",
                {"VOXQUARRY_AGENT_TOOLS_ENABLED": marker},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "VOXQUARRY_AGENT_TOOLS_ENABLED must be exactly true or false",
                ):
                    create_app(snapshot, root / "var")

    @unittest.skipIf(TestClient is None, "FastAPI runtime dependency unavailable")
    def test_enabled_http_api_preserves_gateway_and_retired_alias_boundaries(self) -> None:
        root = Path(self.temporary.name) / "api"
        snapshot = root / "snapshot"
        snapshot.mkdir(parents=True)
        with patch.dict(
            "os.environ",
            {"VOXQUARRY_AGENT_TOOLS_ENABLED": "true"},
            clear=False,
        ):
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [], "defaults": {"scope": {}}},
            ):
                app = create_app(snapshot, root / "var")
        app.state.reporting.store.store_report(self.report)
        rows, groups = fixture_evidence()
        app.state.reporting.evidence.store(REPORT_ID, rows, groups)
        headers = {
            "X-VoxQuarry-Gateway-Token": "fixture-secret",
            "X-VoxQuarry-Authority-Id": "internal-pm",
            "X-VoxQuarry-Principal-Id": "agent-fixture",
            "X-VoxQuarry-Allowed-Report-Ids": REPORT_ID,
        }
        with patch.dict(
            "os.environ",
            {
                "VOXQUARRY_AGENT_TOOLS_ENABLED": "true",
                "VOXQUARRY_AGENT_GATEWAY_TOKEN": "fixture-secret",
            },
        ):
            with TestClient(app) as client:
                openapi_paths = set(client.get("/openapi.json").json()["paths"])
                self.assertIn(
                    "/reporting/api/v1/agent-tools/{tool_name}", openapi_paths
                )
                self.assertIn(
                    "/reporting/api/v1/agent-citations/validate", openapi_paths
                )
                self.assertIn(
                    "/reporting/api/v1/tools/create_report", openapi_paths
                )
                unauthorized = client.post(
                    "/reporting/api/v1/agent-tools/zhuojian.get_report@v1",
                    json={"report_id": REPORT_ID},
                )
                self.assertEqual(unauthorized.status_code, 401)
                self.assertEqual(unauthorized.json()["status"], "error")
                response = client.post(
                    "/reporting/api/v1/agent-tools/zhuojian.get_report@v1",
                    headers=headers,
                    json={"report_id": REPORT_ID},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["report"]["report_id"], REPORT_ID)
                invalid_authority = client.post(
                    "/reporting/api/v1/agent-tools/zhuojian.get_report@v1",
                    headers={
                        "X-VoxQuarry-Gateway-Token": "fixture-secret",
                        "X-VoxQuarry-Principal-Id": "agent-fixture",
                        "X-VoxQuarry-Allowed-Report-Ids": REPORT_ID,
                    },
                    json={"report_id": REPORT_ID},
                )
                self.assertEqual(invalid_authority.status_code, 403)
                blocked_mutation = client.post(
                    "/reporting/api/v1/tools/create_report",
                    json={},
                )
                self.assertEqual(blocked_mutation.status_code, 410)

        with patch.dict(
            "os.environ",
            {"VOXQUARRY_AGENT_TOOLS_ENABLED": "true"},
            clear=False,
        ):
            os.environ.pop("VOXQUARRY_AGENT_GATEWAY_TOKEN", None)
            with TestClient(app) as client:
                unconfigured = client.post(
                    "/reporting/api/v1/agent-tools/zhuojian.get_report@v1",
                    json={"report_id": REPORT_ID},
                )
                self.assertEqual(unconfigured.status_code, 503)


if __name__ == "__main__":
    unittest.main()
