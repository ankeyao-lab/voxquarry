"""Authenticated FastAPI integration for the independent research backend.

Private stores and routes are real. Only Harness execution and upstream data
are scripted; this suite makes no assertions about real model answer quality.
"""
from __future__ import annotations

import json
import unittest
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from agent.app import create_app
from agent.identity import TrustedHeaderIdentityProvider
from agent.research_routes import ResearchBodyLimit
from agent.tests.helpers import signed_headers
from agent.tests.test_agent_trace import actor
from agent.tests.test_harness_research import DATASET_ID, REVIEW_TEXT, HarnessFixture
from agent.tests.test_research_attachments import PDF
from unittest.mock import patch


class ResearchRoutesTests(HarnessFixture, unittest.TestCase):
    def test_inline_image_is_owned_validated_and_materials_survive_reload(self):
        from agent.research_attachments import IsolatedAttachmentParser
        from agent.tests.test_harness_images import PNG
        self.attachments.parser = IsolatedAttachmentParser(lambda args: {"text": "", "parser_version": "image-fixture/v1", "locators": [], "source_sha256": args["source_sha256"]}, "fixture")
        item = self.attachments.create(self.user, self.sid, filename="photo.png", content=PNG, media_type="image/png")
        href = self.base + "/attachments/" + item["attachment_id"] + "/image"
        self.assertEqual(self.request("GET", href).status_code, 404)
        self.attachments.parse_pending(self.user, self.sid, item["attachment_id"])
        response = self.request("GET", href)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, PNG)
        self.assertEqual(self.request("GET", href, actor_override=actor("other")).status_code, 404)
        self.assertEqual(self.client.get(href).status_code, 401)
        text = self.attachments.create(self.user, self.sid, filename="note.txt", content=b"hello", media_type="text/plain")
        self.assertEqual(self.request("GET", self.base + "/attachments/" + text["attachment_id"] + "/image").status_code, 404)
        run = self.accept(attachment_ids=[item["attachment_id"], text["attachment_id"]], product_urls=["https://example.com/product"])
        restored = self.request("GET", self.base).json()["runs"][0]
        self.assertEqual(restored["request"]["attachment_ids"], run["request"]["attachment_ids"])
        self.assertEqual(restored["request"]["product_urls"], ["https://example.com/product"])

    def setUp(self):
        super().setUp()
        self.secret = "synthetic-identity-hmac-secret-for-local-tests"
        self.app = create_app(service=self.service, identity_provider=TrustedHeaderIdentityProvider(self.secret), research_backend=self.research)
        self.client = TestClient(self.app)
        self.headers = signed_headers(self.secret, self.user)
        self.base = "/wenquan/api/v1/sessions/" + self.sid

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def request(self, method, path, *, actor_override=None, **kwargs):
        headers = dict(self.headers if actor_override is None else signed_headers(self.secret, actor_override))
        headers.update(kwargs.pop("headers", {}))
        return self.client.request(method, path, headers=headers, **kwargs)

    def completed_turn(self, request_id="route-request"):
        self.manager.on_start = self.read_and_answer
        response = self.request("POST", self.base + "/turns", json={"question": "解释这条评论", "request_id": request_id, "dataset_ids": [DATASET_ID]})
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run"]["run_id"]
        self.assertEqual(len(self.pool.calls), 1)
        function, args, kwargs = self.pool.calls.pop()
        function(*args, **kwargs)
        run = self.state.get(self.user, run_id)
        self.assertEqual(run["status"], "completed")
        message = next(m for m in self.store.list_messages(self.user, self.sid) if m["run_id"] == run_id and m["role"] == "assistant")
        return run, message

    def test_archive_list_and_restore_keep_content_private_and_intact(self):
        self.completed_turn()
        before = self.request("GET", self.base).json()
        self.assertEqual(self.request("PATCH", self.base, json={"archived": True}).status_code, 200)
        self.assertEqual(self.request("GET", "/wenquan/api/v1/sessions").json()["items"], [])
        rows = self.request("GET", "/wenquan/api/v1/sessions?include_archived=true").json()["items"]
        self.assertEqual([(row["session_id"], row["archived"]) for row in rows], [(self.sid, True)])
        other = actor("different-owner", roles=("admin",))
        self.assertEqual(self.request("GET", "/wenquan/api/v1/sessions?include_archived=true", actor_override=other).json()["items"], [])
        self.assertEqual(self.request("PATCH", self.base, actor_override=other, json={"archived": False}).status_code, 404)
        self.assertEqual(self.request("PATCH", self.base, json={"archived": False}).status_code, 200)
        after = self.request("GET", self.base).json()
        self.assertFalse(after["session"]["archived"])
        self.assertEqual(after["messages"], before["messages"])
        self.assertEqual(after["runs"], before["runs"])

    def test_catalog_and_settings_require_authentication_and_never_return_provider_secrets(self):
        self.assertEqual(self.client.get("/wenquan/api/v1/models").status_code, 401)
        response = self.request("GET", "/wenquan/api/v1/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 6)
        self.assertNotIn("synthetic-never-transmitted", response.text)
        self.assertNotIn("base_url", response.text)
        saved = self.request("PATCH", self.base + "/settings", json={"model_key": "qwen3.8-max", "effort": "low", "web_enabled": False})
        self.assertEqual(saved.status_code, 200, saved.text)
        bundle = self.request("GET", self.base).json()
        self.assertEqual(bundle["session"]["settings"]["model_key"], "qwen3.8-max")
        self.assertFalse(bundle["session"]["settings"]["web_enabled"])
        for values in ({"effort": "high"}, {"api_key": "must-not-be-accepted"}, {"owner_user_id": "other"}):
            self.assertEqual(self.request("PATCH", self.base + "/settings", json=values).status_code, 422)

    def test_turn_accept_is_idempotent_freezes_settings_and_bundle_contains_citations(self):
        self.request("PATCH", self.base + "/settings", json={"model_key": "qwen3.8-max", "effort": "low", "web_enabled": False})
        payload = {"question": "解释评论中的条件", "request_id": "stable-request"}
        response = self.request("POST", self.base + "/turns", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run"]["run_id"]
        again = self.request("POST", self.base + "/turns", json=payload)
        self.assertEqual(again.status_code, 202)
        self.assertTrue(again.json()["duplicate"])
        self.assertEqual(len(self.pool.calls), 1)
        self.request("PATCH", self.base + "/settings", json={"model_key": "glm-5.3", "effort": "max", "web_enabled": True})
        self.manager.on_start = self.read_and_answer
        function, args, kwargs = self.pool.calls.pop()
        function(*args, **kwargs)
        bundle = self.request("GET", self.base).json()
        run = next(item for item in bundle["runs"] if item["run_id"] == run_id)
        self.assertEqual(run["settings"]["model_id"], "qwen3.8-max-0902")
        self.assertEqual(run["settings"]["effective_effort"], "low")
        self.assertFalse(run["settings"]["web_enabled"])
        self.assertIsInstance(run["allowed_actions"], list)
        message = next(m for m in bundle["messages"] if m["role"] == "assistant")
        citation = message["metadata"]["citations"][0]
        source = self.request("GET", citation["source_url"])
        self.assertEqual(source.status_code, 200, source.text)
        self.assertEqual(source.json()["review"]["review_text"], REVIEW_TEXT)
        self.assertEqual(source.json()["source_id"], citation["source_id"])
        old_url = self.request("GET", self.base + "/sources/" + citation["source_id"])
        self.assertEqual(old_url.json(), source.json())

    def test_attachment_upload_preview_download_and_turn_use_are_owner_private(self):
        content = "坡地停机；平地正常。\n这是用户自己的记录。".encode()
        response = self.request("POST", self.base + "/attachments", content=content,
            headers={"X-Filename": quote("用户说明.md"), "Content-Type": "text/markdown"})
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()["attachment"]
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["size_bytes"], len(content))
        self.assertEqual(self.request("GET", self.base + "/attachments").json()["items"][0]["attachment_id"], item["attachment_id"])
        preview = self.request("GET", item["preview_url"])
        self.assertEqual(preview.json()["content"], content.decode())
        downloaded = self.request("GET", item["download_url"])
        self.assertEqual(downloaded.content, content)
        self.assertEqual(downloaded.headers["content-type"], "application/octet-stream")
        self.assertEqual(downloaded.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", downloaded.headers["content-security-policy"])
        admin = actor("different-owner", roles=("admin",))
        for path in (item["preview_url"], item["download_url"], self.base + "/attachments"):
            self.assertEqual(self.request("GET", path, actor_override=admin).status_code, 404)
        accepted = self.request("POST", self.base + "/turns", json={"question": "分析附件", "request_id": "with-attachment", "attachment_ids": [item["attachment_id"]]})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(self.data.calls, [])

    def test_failed_attachment_retry_returns_queued_status_without_reupload(self):
        item = self.attachments.create(self.user, self.sid, "fixture.pdf", "application/pdf", PDF)
        with self.attachments._connect() as db:
            db.execute("UPDATE research_attachments SET status='failed',error_code='attachment_parse_failed' WHERE attachment_id=?", (item["attachment_id"],))
        path = self.base + "/attachments/" + item["attachment_id"] + "/retry"
        with patch.object(self.research, "queue_attachment_parse") as queued:
            foreign = self.request("POST", path, json={}, actor_override=actor("other-owner", roles=("admin",)))
            self.assertEqual(foreign.status_code, 404)
            queued.assert_not_called()
            response = self.request("POST", path, json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["attachment"]["status"], "pending")
            self.assertEqual(response.json()["attachment"]["attachment_id"], item["attachment_id"])
            self.assertNotIn("retry_requested", response.json()["attachment"])
            repeated = self.request("POST", path, json={})
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertEqual(repeated.json()["attachment"]["status"], "pending")
            queued.assert_called_once()
            self.assertEqual(queued.call_args.kwargs, {"retry": True})
        self.assertEqual(self.attachments.download_path(self.user, self.sid, item["attachment_id"]).read_bytes(), PDF)

    def test_cross_owner_admin_cannot_read_source_trace_payload_export_or_mutate_run(self):
        run, message = self.completed_turn()
        trace_path = self.base + "/runs/" + run["run_id"] + "/trace"
        trace = self.request("GET", trace_path)
        self.assertEqual(trace.status_code, 200, trace.text)
        payload_id = trace.json()["trace"]["question"]["payload_ref"]
        source_path = message["metadata"]["citations"][0]["source_url"]
        admin = actor("different-owner", roles=("admin",))
        for path in (self.base, source_path, trace_path, trace_path + "/payloads/" + payload_id + "?include_content=true"):
            self.assertEqual(self.request("GET", path, actor_override=admin).status_code, 404, path)
            self.assertEqual(self.client.get(path).status_code, 401, path)
        for path, payload in ((self.base + "/evals/export", {"run_ids": [run["run_id"]]}),
                               (self.base + "/runs/" + run["run_id"] + "/cancel", {}),
                               (self.base + "/runs/" + run["run_id"] + "/resume", {"request_id": "foreign"})):
            self.assertEqual(self.request("POST", path, json=payload, actor_override=admin).status_code, 404, path)
        private = self.request("GET", trace_path + "/payloads/" + payload_id)
        self.assertEqual(private.status_code, 403, private.text)
        own = self.request("GET", trace_path + "/payloads/" + payload_id + "?include_content=true")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["data"]["question"], "解释这条评论")

    def test_eval_export_defaults_to_summaries_and_human_feedback_maps_to_same_run(self):
        run, message = self.completed_turn()
        feedback = self.request("POST", self.base + "/feedback", json={"message_id": message["message_id"], "run_id": run["run_id"],
            "rating": "down", "reason_codes": ["untrusted_number_or_evidence"], "comment": "遗漏了中档刀速条件"})
        self.assertEqual(feedback.status_code, 201, feedback.text)
        exported = self.request("POST", self.base + "/evals/export", json={"run_ids": [run["run_id"]]})
        self.assertEqual(exported.status_code, 200, exported.text)
        case = exported.json()["cases"][0]
        self.assertEqual(case["question"]["content"], "omitted")
        self.assertEqual(case["answer"]["content"], "omitted")
        self.assertEqual(case["human_feedback"][0]["rating"], "unhelpful")
        self.assertEqual(case["human_feedback"][0]["provenance"], "user_submitted")
        full = self.request("POST", self.base + "/evals/export", json={"run_ids": [run["run_id"]], "include_content": True})
        self.assertEqual(full.status_code, 200, full.text)
        case = full.json()["cases"][0]
        self.assertEqual(case["question"]["value"], "解释这条评论")
        self.assertIn("25 分钟", case["answer"]["value"])
        self.assertTrue(case["source_events"])
        self.assertNotIn("synthetic-never-transmitted", full.text)

    def test_native_trace_is_attempt_bound_durable_and_owner_private(self):
        from agent.agent_trace import AgentTraceStore
        run = self.accept()
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question="合成问题")
        token, cap = self.research.broker.issue(self.user, run, trace["trace_id"])
        path = "/wenquan/internal/broker/trajectory"
        headers = {"Authorization": "Bearer " + token}
        payload = {"session_id": cap.attempt_id, "event": {"type": "tool/result", "seq": 7,
            "data": {"result": "synthetic result", "api_key": "never-store-this",
                     "blocks": [{"type": "reasoning", "text": "private-provider-content"}]}}}
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        bad = self.client.post(path, headers=headers, json={**payload, "session_id": "wrong-attempt"})
        self.assertEqual(bad.status_code, 403)
        saved = self.client.post(path, headers=headers, json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        again = self.client.post(path, headers=headers, json=payload)
        self.assertEqual(again.json(), saved.json())
        self.traces.finish_trace(self.user, trace["trace_id"], status="cancelled")
        reopened = AgentTraceStore(self.traces.database_path, content_enabled=True)
        events = reopened.list_events(self.user, trace["trace_id"])["items"]
        event = next(e for e in events if e["event_type"] == "research.native")
        content = reopened.read_payload(self.user, trace["trace_id"], event["payload_ref"], include_content=True)
        self.assertIn("synthetic result", json.dumps(content))
        self.assertNotIn("never-store-this", json.dumps(content))
        self.assertNotIn("private-provider-content", json.dumps(content))
        from agent.trajectory import trace_overview, native_event
        self.assertEqual(trace_overview(reopened, self.user, trace["trace_id"])["native_events"], 1)
        with self.assertRaises(Exception):
            trace_overview(reopened, actor("other-owner"), trace["trace_id"])
        data, body, _ = native_event({"type": "assistant/attempt", "seq": 9, "data": {"opaque": "hidden-private-block"}})
        self.assertFalse(data["content_saved"])
        self.assertNotIn("hidden-private-block", json.dumps(body))
        self.assertEqual(self.client.get("/wenquan/trajectory").status_code, 401)
        page = self.request("GET", "/wenquan/trajectory")
        self.assertEqual(page.status_code, 200)
        self.assertIn("VoxQuarry", page.text)
        self.assertEqual(self.client.get("/wenquan/api/v1/trajectory").status_code, 401)
        self.assertIn("VoxQuarry", self.request("GET", "/wenquan/api/v1/trajectory").text)

    def test_native_timing_is_content_free_and_old_events_keep_owner_expiry_rules(self):
        from agent.trajectory import native_timing, trace_timing_page
        raw = {"type": "tool/call", "time": 1234, "data": {"callId": "call_one", "name": "bash",
            "arguments": {"command": "private command"}, "reasoning": "never expose"}}
        self.assertEqual(native_timing(raw), {"timing_version": 1, "native_time_ms": 1234,
            "native_call_id": "call_one", "native_tool_name": "bash"})
        self.assertNotIn("native_time_ms", native_timing({"time": float("nan")}))
        run = self.accept()
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question="synthetic")
        self.traces.append_event(self.user, trace["trace_id"], event_id="timing-old", event_type="research.native",
            data={"native_type": "tool/call"}, payload={"event": raw})
        page = trace_timing_page(self.traces, self.user, trace["trace_id"])
        item = next(e for e in page["items"] if e["event_type"] == "research.native")
        self.assertEqual(item["data"]["native_call_id"], "call_one")
        self.assertNotIn("private command", json.dumps(page))
        with self.assertRaises(Exception):
            trace_timing_page(self.traces, actor("other-owner"), trace["trace_id"])

    def test_feedback_run_message_mismatch_rejected_before_either_store_is_written(self):
        first, message = self.completed_turn("first")
        second, _ = self.completed_turn("second")
        with self.store._connect() as db:
            before = db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        response = self.request("POST", self.base + "/feedback", json={"message_id": message["message_id"], "run_id": second["run_id"],
            "rating": "up", "reason_codes": []})
        self.assertIn(response.status_code, (400, 404, 422), response.text)
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], before)
        for run in (first, second):
            self.assertEqual(self.traces.get_trace(self.user, run["trace_id"])["human_feedback"], [])

    def test_dataset_catalog_preview_is_shared_data_but_session_content_is_private(self):
        admin = actor("different-owner", roles=("admin",))
        catalog = self.request("GET", "/wenquan/api/v1/datasets", actor_override=admin)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        row = catalog.json()["items"][0]
        self.assertEqual(row["dataset_id"], "job:sample-job")
        preview = self.request("GET", row["preview_url"], actor_override=admin)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["sample"]["items"][0]["review_text"], REVIEW_TEXT)
        self.assertEqual(self.request("GET", self.base, actor_override=admin).status_code, 404)

    def test_native_tools_project_live_steps_and_keep_owner_bound_session(self):
        run = self.accept()
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question="合成问题")
        token, cap = self.research.broker.issue(self.user, run, trace["trace_id"])
        path = "/wenquan/internal/broker/trajectory"
        headers = {"Authorization": "Bearer " + token}
        call = {"type": "tool/call", "id": "event-one", "seq": 1, "data": {"name": "bash", "callId": "code-1", "arguments":
            json.dumps({"command": "python analysis.py", "description": "计算评论星级分布"})}}
        response = self.client.post(path, headers=headers, json={"session_id": cap.session_key, "event": call})
        self.assertEqual(response.status_code, 200, response.text)
        done = {"type": "tool/result", "id": "event-two", "seq": 2, "data": {"message": {
            "source": {"kind": "tool", "callId": "code-1"}, "content": [{"type": "tool-result", "isError": False}]}}}
        self.assertEqual(self.client.post(path, headers=headers, json={"session_id": cap.session_key, "event": done}).status_code, 200)
        steps = self.store.get_run(self.user, run["run_id"])["steps"]
        step = next(item for item in steps if item["tool_name"] == "bash")
        self.assertEqual(step["status"], "completed")
        self.assertEqual(step["title"], "运行分析代码")
        self.assertEqual(self.client.post(path, headers=headers, json={"session_id": "wq_" + "f" * 64, "event": done}).status_code, 403)

    def test_broker_is_capability_only_and_native_model_body_is_frozen(self):
        run = self.accept(model_key="qwen3.8-max", effort="low")
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question="合成问题")
        token, cap = self.research.broker.issue(self.user, run, trace["trace_id"])
        path = "/wenquan/internal/broker/model/v1/chat/completions"
        self.assertEqual(self.request("POST", path, json={"messages": []}).status_code, 401)
        captured = []
        def provider(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"model": "fixed-provider-id", "choices": [{"message": {"content": "合成响应"}}], "usage": {"total_tokens": 9}})
        self.research.broker.model_client = httpx.Client(transport=httpx.MockTransport(provider))
        result = self.client.post(path, headers={"Authorization": "Bearer " + token}, json={"messages": [], "model": "untrusted-model", "reasoning_effort": "max"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(captured[0]["model"], "qwen3.8-max-0902")
        self.assertEqual(captured[0]["reasoning_effort"], "low")
        self.assertNotIn("synthetic-never-transmitted", json.dumps(captured))
        self.assertEqual(cap.total_tokens, 9)


class ResearchBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, *, headers=(), messages=(), path="/wenquan/internal/broker/model/v1/chat/completions", scope_type="http", app=None):
        incoming, outgoing, calls = list(messages), [], []
        async def receive():
            calls.append("receive")
            if not incoming:
                raise AssertionError("middleware read beyond end of request")
            return incoming.pop(0)
        async def send(message):
            outgoing.append(message)
        async def downstream(scope, reader, sender):
            calls.append("app")
            if app:
                return await app(scope, reader, sender)
            raise AssertionError("oversize request reached downstream app")
        scope = {"type": scope_type, "path": path, "headers": list(headers), "method": "POST"}
        await ResearchBodyLimit(downstream, maximum=4_000_000)(scope, receive, send)
        return calls, outgoing, incoming

    async def test_header_limit_duplicate_invalid_and_huge_integer_reject_without_reading_body(self):
        for values in ((b"4000001",), (b"-1",), (b"1", b"1"), (b"not-a-number",), (b"9" * 5000,)):
            with self.subTest(header_values=values):
                calls, response, _ = await self.invoke(headers=[(b"Content-Length", value) for value in values])
                self.assertEqual(calls, [])
                self.assertEqual(response[0]["status"], 413)
                self.assertEqual(json.loads(response[1]["body"])["error"]["code"], "research_request_too_large")

    async def test_chunked_and_lying_small_content_length_stop_before_application(self):
        for headers in ([], [(b"content-length", b"1")]):
            incoming = [{"type": "http.request", "body": b"a" * 2_000_000, "more_body": True},
                        {"type": "http.request", "body": b"b" * 2_000_001, "more_body": True},
                        {"type": "http.request", "body": b"not consumed", "more_body": False}]
            calls, output, unread = await self.invoke(headers=headers, messages=incoming)
            self.assertEqual(calls, ["receive", "receive"])
            self.assertEqual(len(unread), 1)
            self.assertEqual(output[0]["status"], 413)

    async def test_exact_limit_request_and_streaming_response_are_preserved(self):
        body = b"a" * 2_000_000 + b"b" * 2_000_000
        received = []
        async def app(scope, receive, send):
            received.append(await receive())
            received.append(await receive())
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
            await send({"type": "http.response.body", "body": b"data: two\n\n", "more_body": False})
        calls, output, _ = await self.invoke(headers=[(b"content-length", b"0004000000")], messages=[
            {"type": "http.request", "body": body[:2_000_000], "more_body": True},
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": body[2_000_000:], "more_body": False},
            {"type": "http.disconnect"}], app=app)
        self.assertIn("app", calls)
        self.assertEqual(received, [{"type": "http.request", "body": body, "more_body": False}, {"type": "http.disconnect"}])
        self.assertEqual(output[1:], [{"type": "http.response.body", "body": b"data: one\n\n", "more_body": True},
                                     {"type": "http.response.body", "body": b"data: two\n\n", "more_body": False}])

    async def test_disconnect_during_upload_never_starts_downstream_work(self):
        calls, output, _ = await self.invoke(messages=[{"type": "http.request", "body": b"half", "more_body": True}, {"type": "http.disconnect"}])
        self.assertEqual(calls, ["receive", "receive"])
        self.assertEqual(output, [])

    async def test_public_attachment_and_non_http_streams_are_not_subject_to_broker_limit(self):
        observed = []
        async def downstream(scope, receive, send):
            observed.append(await receive())
        for path, scope_type, event in (("/wenquan/api/v1/sessions/s/attachments", "http", {"type": "http.request", "body": b"a" * 4_000_001, "more_body": False}),
                                       ("/wenquan/internal/broker/example", "websocket", {"type": "websocket.connect"})):
            calls, _output, _ = await self.invoke(path=path, scope_type=scope_type, headers=[(b"content-length", b"9999999")], messages=[event], app=downstream)
            self.assertEqual(calls[0], "app")
            self.assertEqual(observed[-1], event)


if __name__ == "__main__":
    unittest.main()
