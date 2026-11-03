from __future__ import annotations

import sqlite3
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from agent.contracts import AgentError
from agent.service import WenquanService
from agent.store import WenquanStore
from agent.tests.helpers import (
    GROUP_ID,
    METRIC_ID,
    QUOTE,
    REPORT_ID,
    FailingZhuojianTransport,
    SyntheticZhuojianTransport,
    build_reporting_service,
    identity,
)
from agent.tool_registry import ZHUOJIAN_TOOL_NAMES, ToolRegistry
from agent.upstream import ToolExecutor


class RegistryAndPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = WenquanStore(root / "wenquan.sqlite3")
        self.store.initialize()
        self.reporting = build_reporting_service(root / "reporting")
        self.registry = ToolRegistry(zhuojian_status="synthetic_test_only")
        self.transport = SyntheticZhuojianTransport(self.reporting)
        self.executor = ToolExecutor(
            registry=self.registry,
            transport=self.transport,
            store=self.store,
            envelope_schema_path=str(
                Path(__file__).resolve().parents[2]
                / "reporting"
                / "schemas"
                / "zhuojian-tool-envelope-v1.schema.json"
            ),
            allow_synthetic=True,
        )
        self.service = WenquanService(
            store=self.store,
            registry=self.registry,
            executor=self.executor,
            identity_status="synthetic_test_only",
            llm_status="unavailable",
        )
        self.actor = identity()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registry_is_exact_read_only_and_synthetic_is_not_model_visible(self) -> None:
        payload = self.registry.payload(include_schema=True)
        self.assertEqual(tuple(item["name"] for item in payload["tools"]), ZHUOJIAN_TOOL_NAMES)
        self.assertTrue(all(item["effect"] == "read_only" for item in payload["tools"]))
        self.assertEqual(payload["model_visible_tool_count"], 0)
        self.assertEqual(self.registry.payload(model_visible_only=True)["tools"], [])
        serialized = str(payload).lower()
        for forbidden in ("shell@", "filesystem@", "sql@", "subagent@", "create_report"):
            self.assertNotIn(forbidden, serialized)
        with self.assertRaises(AgentError) as closed:
            self.registry.validate_input(
                "zhuojian.get_metric@v1",
                {"report_id": REPORT_ID, "metric_id": METRIC_ID, "owner_user_id": "forged"},
            )
        self.assertEqual(closed.exception.code, "invalid_request")

    def test_versioned_wenquan_schemas_are_valid_and_registry_conforms(self) -> None:
        schema_root = Path(__file__).resolve().parents[1] / "schemas"
        schemas = {}
        for path in sorted(schema_root.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            schemas[path.name] = schema
        # Read-only Q&A adds a distinct success-only, source-bound answer contract.
        self.assertEqual(len(schemas), 9)
        Draft202012Validator(
            schemas["voxquarry-agent-tools-v1.schema.json"]
        ).validate(self.registry.payload(include_schema=True))

    def test_synthetic_e2e_lists_report_gets_metric_queries_evidence_and_cites(self) -> None:
        session = self.service.create_session(self.actor, "Synthetic evidence flow")
        result = self.service.answer_metric_with_evidence(
            self.actor,
            session_id=session["session_id"],
            question="这份报告的好评率及证据是什么？",
            report_id=REPORT_ID,
            metric_id=METRIC_ID,
            evidence_group_id=GROUP_ID,
        )
        self.assertEqual(result["answer_status"], "supported")
        self.assertEqual(
            self.transport.calls,
            [
                "zhuojian.list_reports@v1",
                "zhuojian.get_metric@v1",
                "zhuojian.query_evidence@v1",
                "zhuojian.citation_validator@v1",
            ],
        )
        claim = result["claims"][0]
        self.assertEqual(claim["report_id"], REPORT_ID)
        self.assertEqual(claim["metric_id"], METRIC_ID)
        self.assertEqual(claim["evidence_group_id"], GROUP_ID)
        self.assertGreaterEqual(len(claim["citation_ids"]), 2)
        self.assertTrue(all(item["contract"] == "zhuojian-citation/v1" for item in result["citations"]))
        self.assertEqual(result["run"]["status"], "completed")
        self.assertEqual([item["status"] for item in result["run"]["steps"]], ["completed"] * 4)
        self.assertEqual(len(result["run"]["artifacts"]), 2)

        audits = self.store.list_audit_events(self.actor)
        self.assertEqual(len(audits), 8)
        self.assertEqual(sum(item["status"] == "ok" for item in audits), 4)
        with sqlite3.connect(self.store.database_path) as connection:
            serialized = "\n".join(
                row[0]
                for row in connection.execute(
                    "SELECT content FROM messages WHERE owner_user_id=?", (self.actor.owner_user_id,)
                )
            )
        self.assertNotIn(QUOTE, serialized)

    def test_upstream_failure_returns_insufficient_evidence_without_claims(self) -> None:
        failing = FailingZhuojianTransport(self.reporting)
        executor = ToolExecutor(
            registry=self.registry,
            transport=failing,
            store=self.store,
            envelope_schema_path=str(
                Path(__file__).resolve().parents[2]
                / "reporting"
                / "schemas"
                / "zhuojian-tool-envelope-v1.schema.json"
            ),
            allow_synthetic=True,
        )
        service = WenquanService(
            store=self.store,
            registry=self.registry,
            executor=executor,
            identity_status="synthetic_test_only",
        )
        session = service.create_session(self.actor, "Failure flow")
        result = service.answer_metric_with_evidence(
            self.actor,
            session_id=session["session_id"],
            question="给我结论",
            report_id=REPORT_ID,
            metric_id=METRIC_ID,
            evidence_group_id=GROUP_ID,
        )
        self.assertEqual(result["answer_status"], "insufficient_evidence")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["citations"], [])
        self.assertIn("证据不足", result["message"]["content"])
        self.assertEqual(result["run"]["status"], "failed")

    def test_citation_tamper_fails_closed(self) -> None:
        metric = self.executor.invoke(
            self.actor,
            tool_name="zhuojian.get_metric@v1",
            payload={"report_id": REPORT_ID, "metric_id": METRIC_ID},
        ).result
        citation = dict(metric["citations"][0])
        citation["metric_id"] = "forged_metric"
        checked = self.executor.validate_citations(self.actor, [citation]).result
        self.assertFalse(checked["valid"])


if __name__ == "__main__":
    unittest.main()
