from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent.app import create_app
from agent.config import LLMSettings
from agent.contracts import AgentError
from agent.identity import TrustedHeaderIdentityProvider
from agent.llm import OpenAICompatibleLLMRuntime
from agent.service import WenquanService
from agent.store import WenquanStore
from agent.tests.helpers import (
    GROUP_ID,
    METRIC_ID,
    REPORT_ID,
    SyntheticZhuojianTransport,
    build_reporting_service,
    identity,
    signed_headers,
)
from agent.tool_registry import ToolRegistry
from agent.upstream import HttpZhuojianTransport, ToolExecutor


class ApiIdentityAndLLMTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.secret = "s" * 64
        self.actor = identity()
        self.store = WenquanStore(root / "wenquan.sqlite3")
        self.store.initialize()
        registry = ToolRegistry(zhuojian_status="synthetic_test_only")
        transport = SyntheticZhuojianTransport(build_reporting_service(root / "reporting"))
        executor = ToolExecutor(
            registry=registry,
            transport=transport,
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
            registry=registry,
            executor=executor,
            identity_status="synthetic_test_only",
            llm_status="unavailable",
        )
        provider = TrustedHeaderIdentityProvider(self.secret)
        self.client = TestClient(create_app(service=service, identity_provider=provider))
        self.headers = signed_headers(self.secret, self.actor)

    def tearDown(self) -> None:
        self.client.app.state.readonly.close()
        self.client.close()
        self.temporary.cleanup()

    def test_health_dependency_and_synthetic_readiness_are_distinct(self) -> None:
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        ready = self.client.get("/ready")
        self.assertEqual(ready.status_code, 503)
        self.assertFalse(ready.json()["ready"])
        dependencies = self.client.get("/dependencies").json()["dependencies"]
        self.assertEqual(dependencies["identity"]["status"], "synthetic_test_only")
        self.assertEqual(dependencies["zhuojian"]["status"], "synthetic_test_only")
        self.assertEqual(self.client.get("/wenquan/api/v1/dependencies").status_code, 401)
        scoped = self.client.get("/wenquan/api/v1/dependencies", headers=self.headers)
        self.assertEqual(scoped.status_code, 200)
        self.assertEqual(scoped.json()["dependencies"]["llm"]["status"], "unavailable")
        catalog = self.client.get("/wenquan/api/v1/reports", headers=self.headers).json()
        self.assertEqual(catalog["items"][0]["report_id"], REPORT_ID)

    def test_signed_identity_is_required_and_body_cannot_forge_owner(self) -> None:
        missing = self.client.get("/wenquan/api/v1/sessions")
        self.assertEqual(missing.status_code, 401)
        forged = dict(self.headers)
        forged["x-voxquarry-identity-signature"] = "sha256:" + "0" * 64
        self.assertEqual(
            self.client.get("/wenquan/api/v1/sessions", headers=forged).status_code,
            401,
        )
        injected = self.client.post(
            "/wenquan/api/v1/sessions",
            headers=self.headers,
            json={"title": "Injected", "owner_user_id": "attacker"},
        )
        self.assertEqual(injected.status_code, 422)
        created = self.client.post(
            "/wenquan/api/v1/sessions", headers=self.headers, json={"title": "本人会话"}
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["session"]["owner_user_id"], self.actor.owner_user_id)

    def test_api_synthetic_answer_and_owner_isolation(self) -> None:
        session = self.client.post(
            "/wenquan/api/v1/sessions", headers=self.headers, json={"title": "证据回答"}
        ).json()["session"]
        response = self.client.post(
            f"/wenquan/api/v1/sessions/{session['session_id']}/answers",
            headers=self.headers,
            json={
                "question": "好评率和证据是什么？",
                "report_id": REPORT_ID,
                "metric_id": METRIC_ID,
                "evidence_group_id": GROUP_ID,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer_status"], "supported")
        other_headers = signed_headers(self.secret, identity("user-b"))
        denied = self.client.get(
            f"/wenquan/api/v1/sessions/{session['session_id']}", headers=other_headers
        )
        self.assertEqual(denied.status_code, 404)

    def test_unrouted_natural_language_turn_fails_closed_and_is_recoverable(self) -> None:
        session = self.client.post(
            "/wenquan/api/v1/sessions", headers=self.headers, json={"title": "自然语言入口"}
        ).json()["session"]
        response = self.client.post(
            f"/wenquan/api/v1/sessions/{session['session_id']}/turns",
            headers=self.headers,
            json={"question": "分析这款产品", "request_id": "test-request-1"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["accepted"])
        self.client.app.state.readonly.close()
        restored = self.client.get(
            f"/wenquan/api/v1/sessions/{session['session_id']}", headers=self.headers
        ).json()
        self.assertEqual(restored["runs"][0]["status"], "blocked")
        self.assertEqual(restored["messages"][-1]["answer_status"], "insufficient_evidence")
        self.assertEqual(restored["messages"][-1]["metadata"]["error"]["code"], "report_selection_required")

    def test_memory_preview_confirm_write_disable_and_delete_interfaces(self) -> None:
        preview = self.client.post(
            "/wenquan/api/v1/memory/preferences/preview",
            headers=self.headers,
            json={"preference_key": "market", "value": "North America"},
        ).json()["confirmation"]
        self.assertEqual(preview["status"], "pending")
        confirmed = self.client.post(
            f"/wenquan/api/v1/confirmations/{preview['confirmation_id']}/confirm",
            headers=self.headers,
            json={"payload_hash": preview["payload_hash"]},
        ).json()["confirmation"]
        self.assertEqual(confirmed["status"], "confirmed")
        write = self.client.post(
            "/wenquan/api/v1/memory/preferences",
            headers=self.headers,
            json={
                "preference_key": "market",
                "value": "North America",
                "confirmation_id": preview["confirmation_id"],
            },
        )
        self.assertEqual(write.status_code, 201)
        self.assertEqual(self.client.get("/wenquan/api/v1/memory", headers=self.headers).json()["preferences"][0]["value"], "North America")
        disabled = self.client.patch(
            "/wenquan/api/v1/memory", headers=self.headers, json={"enabled": False}
        )
        self.assertFalse(disabled.json()["status"]["enabled"])
        self.assertEqual(
            self.client.get("/wenquan/api/v1/memory", headers=self.headers).json()["preferences"],
            [],
        )

    def test_business_action_preview_enforces_risk_confirmation_without_execution(self) -> None:
        session = self.client.post(
            "/wenquan/api/v1/sessions", headers=self.headers, json={"title": "动作确认"}
        ).json()["session"]
        base_payload = {
            "action": "create_collection_task",
            "product_ids": ["prd-synthetic-1"],
            "channels": ["synthetic-channel"],
            "time_range": {"from": "2026-08-01", "to": "2026-08-28"},
            "estimated_review_count": 100,
            "estimated_duration_minutes": 10,
            "reused_object_ids": [],
            "created_object_types": ["collection_task"],
            "scope_expanded": False,
            "external_paid": False,
        }
        preview = self.client.post(
            f"/wenquan/api/v1/sessions/{session['session_id']}/action-confirmations/preview",
            headers=self.headers,
            json=base_payload,
        ).json()["confirmation"]
        self.assertEqual(preview["required_count"], 1)
        self.assertEqual(
            preview["preview"]["execution_status"],
            "capability_unavailable_until_domain_tool_contracts",
        )

        risky_payload = dict(base_payload)
        risky_payload["product_ids"] = ["prd-synthetic-1", "prd-synthetic-2"]
        risky_payload["estimated_review_count"] = 50_000
        risky = self.client.post(
            f"/wenquan/api/v1/sessions/{session['session_id']}/action-confirmations/preview",
            headers=self.headers,
            json=risky_payload,
        ).json()["confirmation"]
        self.assertEqual(risky["required_count"], 2)
        first = self.client.post(
            f"/wenquan/api/v1/confirmations/{risky['confirmation_id']}/confirm",
            headers=self.headers,
            json={"payload_hash": risky["payload_hash"]},
        ).json()["confirmation"]
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["confirmed_count"], 1)
        second = self.client.post(
            f"/wenquan/api/v1/confirmations/{risky['confirmation_id']}/confirm",
            headers=self.headers,
            json={"payload_hash": risky["payload_hash"]},
        ).json()["confirmation"]
        self.assertEqual(second["status"], "confirmed")
        self.assertEqual(second["confirmed_count"], 2)
        self.assertNotIn("execute", str(second).lower())

    def test_openapi_has_no_shell_web_sql_subagent_or_upstream_mutation(self) -> None:
        openapi = self.client.get("/wenquan/api/v1/openapi.json").json()
        serialized = str(openapi).lower()
        for token in ("shell", "filesystem", "arbitrary_http", "subagent", "create_report", "delete_report"):
            self.assertNotIn(token, serialized)

    def test_llm_runtime_maps_enable_thinking_and_redacts_key(self) -> None:
        settings = LLMSettings(
            api_key="secret-do-not-print",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="deepseek-v4.1-flash-1017",
            enable_thinking=True,
        )
        runtime = OpenAICompatibleLLMRuntime(settings)
        payload = runtime.request_payload(
            messages=[{"role": "user", "content": "synthetic"}], tools=[]
        )
        self.assertIs(payload["enable_thinking"], True)
        self.assertEqual(payload["model"], "deepseek-v4.1-flash-1017")
        self.assertNotIn("secret-do-not-print", repr(settings))
        self.assertNotIn("secret-do-not-print", str(payload))

    def test_zhuojian_transport_rejects_public_network_target(self) -> None:
        with self.assertRaisesRegex(AgentError, "loopback/private-only"):
            HttpZhuojianTransport(
                base_url="https://public.example.com",
                gateway_token="synthetic-token-never-sent",
            )
        private = HttpZhuojianTransport(
            base_url="http://127.0.0.1:8787/",
            gateway_token="synthetic-token-never-sent",
        )
        self.assertEqual(private._base_url, "http://127.0.0.1:8787")


if __name__ == "__main__":
    unittest.main()
