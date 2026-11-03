from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

from agent.agent_trace import AgentTraceStore
from agent.contracts import AgentError
from agent.harness_broker import HarnessBroker
from agent.research_limits import ResearchLimits
from agent.harness_tools import research_tools
from agent.model_registry import ModelRegistry
from agent.research_state import ResearchState
from agent.store import WenquanStore
from agent.tests.test_agent_trace import actor


class HarnessBrokerTests(unittest.TestCase):
    def test_known_provider_case_alias_settles_but_unrelated_model_does_not(self):
        # Exercise the frozen per-call ledger, including a supplier alias and
        # a nearby model revision that must not release its reservation.
        from agent.model_registry import matches_model_receipt
        self.assertTrue(matches_model_receipt('ZHIPU/GLM-5.3', 'zhipu/glm-5.3'))
        self.assertFalse(matches_model_receipt('kimi-k3', 'kimi/kimi-k3'))
        self.assertFalse(matches_model_receipt('ZHIPU/GLM-5.3', 'zhipu/glm-5.3-flash'))
        for actual, expected_settled in [('zhipu/glm-5.3', True), ('zhipu/glm-5.3-flash', False)]:
            number = self.broker.prepare_model(self.cap, {'messages': []}, self.tools)[2]
            with self.broker._budget_connection() as db:
                row = db.execute('SELECT pricing_json FROM research_model_charges WHERE call_number=?', (number,)).fetchone()
                pricing = json.loads(row['pricing_json'])
                pricing['requested_model'] = 'ZHIPU/GLM-5.3'
                db.execute('UPDATE research_model_charges SET pricing_json=? WHERE call_number=?', (json.dumps(pricing), number))
            self.broker._record_usage(self.cap, {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120},
                                     number=number, complete=True, actual_model=actual)
            with self.broker._budget_connection() as db:
                row = db.execute('SELECT charged_nano FROM research_model_charges WHERE call_number=?', (number,)).fetchone()
            self.assertEqual(row['charged_nano'] is not None, expected_settled)

    def setUp(self):
        price_clock = patch('agent.model_registry.date', wraps=date)
        price_clock.start().today.return_value = date(2026, 10, 23)
        self.addCleanup(price_clock.stop)
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WenquanStore(root / "wenquan.sqlite3")
        self.store.initialize()
        self.registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-provider-private-key"})
        self.state = ResearchState(self.store, self.registry)
        self.state.initialize()
        self.traces = AgentTraceStore(root / "traces" / "traces.sqlite3", content_enabled=True)
        self.traces.initialize()
        self.user = actor()
        sid = self.store.create_session(self.user, "broker test")["session_id"]
        self.frozen = self.registry.freeze({"model_key": "qwen3.8-max", "effort": "low", "web_enabled": False})
        self.run = self.state.accept(self.user, sid, {"question": "按完整评论回答", "request_id": "request1"}, self.frozen)["run"]
        self.trace_id = self.traces.start_trace(self.user, session_id=sid, run_id=self.run["run_id"], question="按完整评论回答")["trace_id"]
        self.broker = HarnessBroker(self.state, self.registry, self.traces, limits=ResearchLimits(model_calls=40, total_tokens=1_500_000, active_seconds=600, model_budget_cny=10, tool_calls=300, search_calls=10, fetch_calls=20, deepseek_output_tokens=16384, model_request_bytes=4_000_000))
        self.token, self.cap = self.broker.issue(self.user, self.run, self.trace_id)
        self.tools = research_tools()

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def test_owned_http_pool_reuses_connections_without_cookies_or_default_credentials(self):
        received = []
        def provider(request):
            received.append(request)
            return httpx.Response(200, json={"choices": []}, headers={"Set-Cookie": "session=private; Path=/"})
        real_client = httpx.Client
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(provider), **kwargs)
        with patch("agent.harness_broker.httpx.Client", side_effect=client_factory) as factory:
            settings, body, number = self.broker.prepare_model(self.cap, {"messages": []}, [])
            list(self.broker._model_stream(self.cap, settings, body, number))
            first = self.broker._provider_client()
            settings, body, number = self.broker.prepare_model(self.cap, {"messages": []}, [])
            list(self.broker._model_stream(self.cap, replace(settings, api_key="second-request-only-key"), body, number))
            self.assertIs(first, self.broker._provider_client())
            factory.assert_called_once()
            self.assertFalse(first.is_closed)
            self.assertNotIn("authorization", first.headers)
            self.assertFalse(list(first.cookies.jar))
            self.assertNotIn("cookie", received[1].headers)
            self.assertEqual(received[0].headers["authorization"], "Bearer synthetic-provider-private-key")
            self.assertEqual(received[1].headers["authorization"], "Bearer second-request-only-key")
            self.assertEqual(received[1].extensions["timeout"]["connect"], 15)
            self.broker.close()
            self.assertTrue(first.is_closed)
            self.broker.close()  # Idempotent shutdown must not allocate a pool.
            self.assert_code("research_inactive", self.broker._provider_client)

    def test_borrowed_client_is_reusable_and_owned_by_caller(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": []}))) as client:
            self.broker.model_client = client
            for _ in range(2):
                list(self.broker.model_stream(self.cap, {"messages": []}, []))
            self.broker.close()
            self.assertFalse(client.is_closed)

    def test_http_pool_shutdown_drains_existing_stream_and_rejects_new_leases(self):
        real_client = httpx.Client
        with patch("agent.harness_broker.httpx.Client", side_effect=lambda **kwargs: real_client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200)), **kwargs)):
            with self.broker._provider_session() as client:
                self.broker.close()
                self.assertFalse(client.is_closed)
                with self.assertRaises(AgentError):
                    with self.broker._provider_session():
                        self.fail("Closed broker acquired another stream")
            self.assertTrue(client.is_closed)

    def test_phase_timings_context_counts_and_native_handoff_contain_no_content(self):
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b'data: [DONE]\n\n')))
        clock = [1000.0]
        with patch("agent.harness_broker.time.monotonic", side_effect=lambda: clock[0]):
            self.broker.event(self.cap, "research.native", data={"native_type": "tool/result"})
            clock[0] = 1004.125
            list(self.broker.model_stream(self.cap, {"messages": [
                {"role": "system", "content": "system-private"}, {"role": "user", "content": "用户约束-private"},
                {"role": "tool", "content": "tool-private"}], "stream": True}, [], relay_prepare_ms="2.5"))
        events = self.traces.list_events(self.user, self.trace_id)["items"]
        started = next(e["data"] for e in events if e["event_type"] == "model.started")
        ended = next(e["data"] for e in events if e["event_type"] == "model.finished")
        self.assertEqual(started["phase_timings"]["handoff_wait_ms"], 4125)
        self.assertEqual(started["phase_timings"]["relay_prepare_ms"], 2.5)
        self.assertEqual(started["context_profile"]["roles"]["user"]["message_count"], 1)
        self.assertGreater(started["context_profile"]["roles"]["user"]["json_bytes"], len("用户约束-private"))
        self.assertNotIn("private", json.dumps(started))
        self.assertEqual(ended["phase_timings"]["identity_reads"], 2)
        self.assertIn("provider_headers_ms", ended["phase_timings"])
        self.assertIn("settlement_ms", ended["phase_timings"])
        self.assertFalse(self.cap.prepared_models)

    def assert_code(self, code, method, *args, **kwargs):
        with self.assertRaises(AgentError) as caught:
            method(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_functional_allowances_reach_provider_reservation_and_runtime(self):
        limits = ResearchLimits.from_environment({})
        broker = HarnessBroker(self.state, self.registry, self.traces, limits=limits)
        sid = self.store.create_session(self.user, "functional validation")["session_id"]
        frozen = self.registry.freeze({"model_key": "deepseek-v4.1-flash", "effort": "default", "web_enabled": False})
        run = self.state.accept(self.user, sid, {"question": "生成完整报告", "request_id": "wide"}, frozen)["run"]
        trace = self.traces.start_trace(self.user, session_id=sid, run_id=run["run_id"], question="生成完整报告")["trace_id"]
        _, cap = broker.issue(self.user, run, trace)
        _, body, _ = broker.prepare_model(cap, {"messages": [{"role": "user", "content": "test"}]}, [])
        self.assertEqual(body["model"], "deepseek-v4.1-flash-1017")
        self.assertEqual(body["max_tokens"], 131072)
        self.assertEqual(limits.runtime(body["model"])["context_window"], 1_000_000)
        budget = broker.budget_status(cap)
        self.assertEqual((budget["model_call_limit"], budget["token_limit"], budget["active_second_limit"], budget["model_cost"]["limit"]), (160, 200_000_000, 3600, 100))
        self.assertGreater(broker._cost_reservation(cap, body)["reserved_nano"], 1_000_000_000)
        self.assertGreater(cap.expires_at - __import__("time").monotonic(), 3600)

    def test_compaction_output_budget_and_native_tool_schema_are_preserved(self):
        tool = {"type": "function", "function": {"name": "read", "description": "Official read", "parameters": {"type": "object"}}}
        _, body, _ = self.broker.prepare_model(self.cap, {"messages": [], "max_tokens": 8192,
            "temperature": 0.3, "tools": [tool]}, [])
        self.assertEqual(body.get("max_tokens", body.get("max_completion_tokens")), 8192)
        self.assertEqual(body["temperature"], 0.3)
        self.assertEqual(body["tools"], [tool])
        tool["function"]["name"] = "arbitrary_host_tool"
        self.assert_code("model_tools_rejected", self.broker.prepare_model, self.cap, {"messages": [], "tools": [tool]}, [])

    def test_token_resolves_only_active_same_owner_attempt_and_is_not_logged(self):
        self.assertIs(self.broker.resolve("Bearer " + self.token), self.cap)
        self.assert_code("broker_unauthorized", self.broker.resolve, "Bearer wrong")
        self.assert_code("run_not_found", self.broker.issue, actor("admin", roles=("admin",)), self.run, self.trace_id)
        self.assertNotIn(self.token, self.traces.database_path.read_bytes().decode("latin-1"))
        self.state.cancel(self.user, self.run["run_id"])
        self.assert_code("research_inactive", self.broker.resolve, "Bearer " + self.token)
        resumed = self.state.resume(self.user, self.run["run_id"])["run"]
        self.assert_code("research_inactive", self.broker.resolve, "Bearer " + self.token)
        self.traces.resume_trace(self.user, self.trace_id, attempt_id=resumed["attempt_id"])
        token, cap = self.broker.issue(self.user, resumed, self.trace_id)
        self.assertIs(self.broker.resolve("Bearer " + token), cap)

    def test_frozen_body_ignores_sdk_model_url_key_effort_and_caller_cannot_mutate_cap(self):
        supplied = copy.deepcopy(self.run)
        supplied["settings"]["model_id"] = "not-the-selected-model"
        _token, cap = self.broker.issue(self.user, supplied, self.trace_id)
        self.assertEqual(cap.settings["model_id"], "qwen3.8-max-0902")
        incoming = {"model": "different", "base_url": "https://forbidden.test", "api_key": "untrusted-key", "reasoning_effort": "max",
                    "max_tokens": 99999999, "messages": [{"role": "user", "content": "hello"}]}
        settings, body, count = self.broker.prepare_model(cap, incoming, self.tools)
        self.assertEqual(count, 1)
        self.assertEqual(body["model"], "qwen3.8-max-0902")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["max_completion_tokens"], 16384)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("api_key", body)
        self.assertNotIn("base_url", body)
        self.assertEqual(settings.api_key, "synthetic-provider-private-key")
        incoming["messages"][0]["content"] = "changed after prepare"
        self.assertEqual(body["messages"][0]["content"], "hello")

    def test_native_tools_are_server_schemas_not_model_invented_parameters(self):
        incoming = {"messages": [], "tools": [{"type": "function", "function": {"name": "query_dataset", "description": "bad", "parameters": {"anything": True}}}]}
        _, body, _ = self.broker.prepare_model(self.cap, incoming, self.tools)
        expected = next(tool for tool in self.tools if tool["name"] == "query_dataset")
        self.assertEqual(body["tools"], [{"type": "function", "function": expected}])
        for tools in ([{"function": {"name": "read_host_secrets"}}], [{"function": "bad"}], incoming["tools"] * 2):
            before = self.cap.model_calls
            self.assert_code("model_tools_rejected", self.broker.prepare_model, self.cap, {"messages": [], "tools": tools}, self.tools)
            self.assertEqual(self.cap.model_calls, before)

    def test_kimi_uses_fixed_total_output_allowance_and_retains_ten_yuan_guard(self):
        sid = self.store.create_session(self.user, "Kimi budget test")["session_id"]
        frozen = self.registry.freeze({"model_key": "kimi-k3", "web_enabled": False})
        run = self.state.accept(self.user, sid, {"question": "工具预算测试", "request_id": "kimi-budget"}, frozen)["run"]
        trace_id = self.traces.start_trace(self.user, session_id=sid, run_id=run["run_id"], question="工具预算测试")["trace_id"]
        _, cap = self.broker.issue(self.user, run, trace_id)
        incoming = {"messages": [{"role": "user", "content": "hello"}], "model": "kimi/kimi-k3",
                    "reasoning_effort": "max", "max_completion_tokens": 1048576, "thinking_budget": 1048576}
        _, body, _ = self.broker.prepare_model(cap, incoming, self.tools)
        self.assertEqual((body["model"], body["reasoning_effort"], body["max_completion_tokens"]), ("kimi-k3", "low", 16384))
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("thinking_budget", body)
        estimate = self.broker._cost_reservation(cap, body)
        self.assertGreater(estimate["reserved_nano"], 1_600_000_000)
        self.assertLess(estimate["reserved_nano"], 2_000_000_000)
        # No usage receipt means no refund: accumulated reservations still stop
        # the next call before the existing 10 CNY research budget is exceeded.
        for _ in range(4):
            self.broker.prepare_model(cap, incoming, self.tools)
        self.assert_code("research_cost_budget_reached", self.broker.prepare_model, cap, incoming, self.tools)

    def test_model_call_and_token_budget_are_enforced_before_next_provider_call(self):
        self.broker.max_model_calls = 1
        self.broker.prepare_model(self.cap, {"messages": []}, self.tools)
        self.assert_code("research_budget_reached", self.broker.prepare_model, self.cap, {"messages": []}, self.tools)
        self.broker.max_model_calls = 5
        self.broker._record_usage(self.cap, {"total_tokens": self.broker.max_tokens})
        self.assert_code("research_budget_reached", self.broker.prepare_model, self.cap, {"messages": []}, self.tools)

    def test_budget_survives_resume_and_new_broker_and_late_usage_is_not_erased(self):
        self.broker.prepare_model(self.cap, {"messages": []}, self.tools)
        self.broker.reserve_tool(self.cap)
        self.state.cancel(self.user, self.run["run_id"])
        resumed = self.state.resume(self.user, self.run["run_id"])["run"]
        self.traces.resume_trace(self.user, self.trace_id, attempt_id=resumed["attempt_id"])
        restarted = HarnessBroker(self.state, self.registry, self.traces, limits=self.broker.limits, max_model_calls=2, max_tokens=50)
        _token, cap = restarted.issue(self.user, resumed, self.trace_id)
        self.assertEqual((cap.model_calls, cap.tool_calls, cap.total_tokens), (1, 1, 0))
        # A response incurred before cancellation may report its cost later.
        self.broker._record_usage(self.cap, {"total_tokens": 30})
        self.assertEqual(restarted.prepare_model(cap, {"messages": []}, self.tools)[2], 2)
        self.assertEqual(cap.total_tokens, 30)
        self.assert_code("research_budget_reached", restarted.prepare_model, cap, {"messages": []}, self.tools)
        restarted._record_usage(cap, {"total_tokens": 20})
        self.assert_code("research_budget_reached", restarted.reserve_tool, cap)

    def test_concurrent_capabilities_cannot_each_spend_the_same_remaining_call(self):
        caps = [self.cap] + [self.broker.issue(self.user, self.run, self.trace_id)[1] for _ in range(7)]
        self.broker.max_model_calls = 3
        def reserve(cap):
            try:
                return self.broker.prepare_model(cap, {"messages": []}, self.tools)[2]
            except AgentError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, caps))
        self.assertEqual(sorted(x for x in results if type(x) is int), [1, 2, 3])
        self.assertEqual(results.count("research_budget_reached"), 5)
        _token, restored = self.broker.issue(self.user, self.run, self.trace_id)
        self.assertEqual(restored.model_calls, 3)

    def test_tool_limit_is_persisted_and_authorization_recheck_precedes_reservation(self):
        with self.broker._budget_connection() as db:
            db.execute("UPDATE research_budget SET tool_calls=299 WHERE run_id=?", (self.run["run_id"],))
        self.assertEqual(self.broker.reserve_tool(self.cap), 300)
        self.assert_code("research_budget_reached", self.broker.reserve_tool, self.cap)
        calls = []
        def reject(actor, run_id):
            calls.append((actor.owner_user_id, run_id))
            raise AgentError("identity_expired", "身份需要重新验证", http_status=401)
        self.broker.authorize = reject
        self.assert_code("identity_expired", self.broker.prepare_model, self.cap, {"messages": []}, self.tools)
        self.assertEqual(calls, [(self.user.owner_user_id, self.run["run_id"])])
        self.assertEqual(self.cap.model_calls, 0)

    def test_stream_keeps_native_tool_calls_and_actual_model_usage_without_secret_logging(self):
        captured = []
        events = [{"model": "provider-fixed-snapshot", "choices": [{"delta": {"reasoning_content": "do-not-log-reasoning"}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "tool-id", "type": "function", "function": {"name": "query_dataset", "arguments": "{}"}}]}}]},
                  {"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}, "choices": []}]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        def handler(request):
            captured.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(handler))
        actual = b"".join(self.broker.model_stream(self.cap, {"messages": [{"role": "user", "content": "question"}], "stream": True}, self.tools))
        self.assertEqual(actual.decode(), raw)
        self.assertEqual(captured[0].headers["authorization"], "Bearer synthetic-provider-private-key")
        body = json.loads(captured[0].content)
        self.assertNotIn("synthetic-provider-private-key", captured[0].content.decode())
        self.assertTrue(body["stream"])
        self.assertEqual(self.cap.total_tokens, 18)
        logged = self.traces.list_events(self.user, self.trace_id)["items"]
        finished = next(event for event in logged if event["event_type"] == "model.finished")
        self.assertEqual(finished["data"]["actual_model"], "provider-fixed-snapshot")
        self.assertEqual(finished["data"]["usage"]["total_tokens"], 18)
        progress = next(event for event in logged if event["event_type"] == "model.progress")
        self.assertEqual(progress["data"]["phase"], "receiving")
        self.assertGreaterEqual(progress["data"]["data_packets"], 1)
        self.assertLess(progress["seq"], finished["seq"])
        steps = self.state.get(self.user, self.run["run_id"])["steps"]
        model_step = next(step for step in steps if step["tool_name"] == "model")
        self.assertEqual(model_step["status"], "completed")
        from agent.trajectory import trace_overview
        overview = trace_overview(self.traces, self.user, self.trace_id)
        self.assertEqual(overview["model_calls"], 1)
        self.assertEqual(overview["total_tokens"], 18)
        self.assertIsNone(overview["active_call"])
        persisted = self.traces.database_path.read_bytes() + b"".join(path.read_bytes() for path in self.traces.payload_dir.iterdir())
        self.assertNotIn(b"synthetic-provider-private-key", persisted)
        self.assertNotIn(b"do-not-log-reasoning", persisted)

    def test_nonstream_provider_output_records_actual_model_and_redirect_is_rejected(self):
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"model": "actual-id", "choices": [{"message": {"content": "answer"}}], "usage": {"total_tokens": 5}})))
        value = json.loads(b"".join(self.broker.model_stream(self.cap, {"messages": []}, self.tools)))
        self.assertEqual(value["model"], "actual-id")
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://unexpected.test"}, text="secret-reflection")))
        self.assert_code("model_unavailable", lambda: list(self.broker.model_stream(self.cap, {"messages": []}, self.tools)))

    def test_interrupted_stream_diagnostics_keep_counts_and_timing_without_content(self):
        clock = [1000.0]
        visible, hidden, arguments = "实际正文", "private-deliberation-never-log", "private-tool-arguments-never-log"
        packets = [b": heartbeat-private-text\r\n\r\n",
                   ("data: " + json.dumps({"choices": [{"delta": {"content": visible, "reasoning_content": hidden}}]}, ensure_ascii=False) + "\r\n\r\n").encode(),
                   ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                       {"index": 0, "function": {"arguments": arguments}}, {"index": 0, "function": {"arguments": "more"}}]},
                       "finish_reason": "tool_calls"}]}) + "\r\n\r\n").encode(),
                   b'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":4,"total_tokens":12}}\r\n\r\n']
        class InterruptedStream(httpx.SyncByteStream):
            def __iter__(self):
                for stamp, packet in zip((1000.125, 1000.25, 1001.0, 1001.5), packets):
                    clock[0] = stamp
                    yield packet
                clock[0] = 1004.0  # EOF, despite a tool_calls finish_reason.
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=InterruptedStream())))
        with patch("agent.harness_broker.time.monotonic", side_effect=lambda: clock[0]):
            self.assert_code("model_stream_interrupted", lambda: list(self.broker.model_stream(
                self.cap, {"messages": [], "stream": True}, self.tools)))
        finished = next(e for e in self.traces.list_events(self.user, self.trace_id)["items"] if e["event_type"] == "model.finished")
        self.assertFalse(finished["data"]["complete"])
        self.assertEqual(finished["data"]["usage"]["total_tokens"], 12)
        self.assertEqual(finished["data"]["stream_diagnostics"], {
            "data_packets": 3, "received_bytes": sum(map(len, packets)),
            "first_packet_wait_ms": 250, "last_data_idle_ms": 2500,
            "finish_reasons": ["tool_calls"], "unrecognized_finish_reason_count": 0,
            "visible_text_chars": len(visible), "hidden_text_chars": len(hidden), "tool_call_blocks": 2})
        persisted = self.traces.database_path.read_bytes() + b"".join(p.read_bytes() for p in self.traces.payload_dir.iterdir())
        for secret in (visible, hidden, arguments, "heartbeat-private-text"):
            self.assertNotIn(secret.encode(), persisted)

    def test_stream_finish_reasons_allowlist_drops_arbitrary_provider_values(self):
        secret = "provider-private-finish-reason"
        reasons = ["stop", "length", "tool_calls", "content_filter", "function_call",
                   secret * 100, {"message": secret}, [secret], 123, None, "stop"]
        events = [{"choices": [{"finish_reason": reason, "delta": {}}]} for reason in reasons]
        events += [{"choices": [], "usage": {"total_tokens": 3}}]
        raw = ": keepalive\n\n" + "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw)))
        output = b"".join(self.broker.model_stream(self.cap, {"messages": [], "stream": True}, self.tools))
        self.assertEqual(output.decode(), raw)  # Diagnostics do not rewrite SSE.
        finished = next(e for e in self.traces.list_events(self.user, self.trace_id)["items"] if e["event_type"] == "model.finished")
        diagnostic = finished["data"]["stream_diagnostics"]
        self.assertTrue(finished["data"]["complete"])
        self.assertEqual(diagnostic["finish_reasons"], ["content_filter", "function_call", "length", "stop", "tool_calls"])
        self.assertEqual(diagnostic["unrecognized_finish_reason_count"], 4)
        self.assertEqual(diagnostic["data_packets"], len(events) + 1)  # Includes usage and DONE, never comments.
        self.assertEqual(diagnostic["received_bytes"], len(raw.encode()))
        self.assertEqual((diagnostic["visible_text_chars"], diagnostic["hidden_text_chars"], diagnostic["tool_call_blocks"]), (0, 0, 0))
        self.assertNotIn(secret, json.dumps(finished))
        self.assertNotIn(secret.encode(), self.traces.database_path.read_bytes())

    def test_comment_only_eof_has_no_data_packet_timestamps(self):
        raw = b": keepalive\r\n\r\n: another\r\n\r\n"
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw)))
        self.assert_code("model_stream_interrupted", lambda: list(self.broker.model_stream(self.cap, {"messages": [], "stream": True}, self.tools)))
        diagnostic = self.traces.list_events(self.user, self.trace_id)["items"][-1]["data"]["stream_diagnostics"]
        self.assertEqual(diagnostic["data_packets"], 0)
        self.assertEqual(diagnostic["received_bytes"], len(raw))
        self.assertIsNone(diagnostic["first_packet_wait_ms"])
        self.assertIsNone(diagnostic["last_data_idle_ms"])

    def test_transport_failure_retains_partial_diagnostics_without_retrying(self):
        requests = []
        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"reasoning":"never-store-this-text"}}]}\n\n'
                raise httpx.ReadError("private-provider-transport-details")
        def handler(request):
            requests.append(request)
            return httpx.Response(200, stream=BrokenStream())
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(handler))
        self.assert_code("model_unavailable", lambda: list(self.broker.model_stream(self.cap, {"messages": [], "stream": True}, self.tools)))
        self.assertEqual(len(requests), 1)
        finished = self.traces.list_events(self.user, self.trace_id)["items"][-1]
        self.assertFalse(finished["data"]["complete"])
        self.assertEqual(finished["data"]["stream_diagnostics"]["data_packets"], 1)
        self.assertEqual(finished["data"]["stream_diagnostics"]["hidden_text_chars"], len("never-store-this-text"))
        for secret in (b"never-store-this-text", b"private-provider-transport-details"):
            self.assertNotIn(secret, self.traces.database_path.read_bytes())

    def test_provider_auth_and_rate_limit_statuses_reach_native_retry_classifier(self):
        for status, code, public_status, retryable in (
            (400, "model_unavailable", 400, False), (422, "model_unavailable", 422, False),
            (401, "model_auth_failed", 401, False), (403, "model_access_denied", 403, False),
            (404, "model_not_found", 404, False), (429, "model_rate_limited", 429, True),
            (500, "model_unavailable", 502, True), (503, "model_unavailable", 502, True)):
            with self.subTest(status=status):
                self.broker.model_client = httpx.Client(transport=httpx.MockTransport(
                    lambda _, status=status: httpx.Response(status, text="provider-private-error")))
                with self.assertRaises(AgentError) as caught:
                    list(self.broker.model_stream(self.cap, {"messages": [], "stream": True}, self.tools))
                self.assertEqual((caught.exception.code, caught.exception.http_status, caught.exception.retryable),
                                 (code, public_status, retryable))

    def test_provider_error_bodies_never_become_harness_output_or_logs(self):
        for streaming in (False, True):
            secret = "provider-reflected-secret"
            data = {"error": {"message": secret}}
            content = ("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode() if streaming else json.dumps(data).encode()
            self.broker.model_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
            received = []
            with self.assertRaises(AgentError) as caught:
                for chunk in self.broker.model_stream(self.cap, {"messages": [], "stream": streaming}, self.tools):
                    received.append(chunk)
            self.assertNotIn(secret, caught.exception.message)
            self.assertEqual(received, [])
            self.assertNotIn(secret.encode(), self.traces.database_path.read_bytes())

    def test_cancellation_between_stream_packets_prevents_late_output(self):
        state, user, run_id = self.state, self.user, self.run["run_id"]
        class CancellingStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
                state.cancel(user, run_id)
                yield b'data: {"choices":[{"delta":{"content":"late"}}]}\n\n'
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=CancellingStream())))
        received = []
        with self.assertRaises(AgentError) as caught:
            for chunk in self.broker.model_stream(self.cap, {"messages": [], "stream": True}, self.tools):
                received.append(chunk)
        self.assertEqual(caught.exception.code, "research_inactive")
        self.assertIn(b"first", b"".join(received))
        self.assertNotIn(b"late", b"".join(received))

    def test_stream_started_by_one_asgi_worker_can_continue_and_close_on_another(self):
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            content=b'data: {"choices":[{"delta":{"content":"first"}}]}\n\ndata: [DONE]\n\n')))
        stream = self.broker.model_stream(self.cap, {"messages": [], "stream": True}, self.tools)
        with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
            first_id, chunk = first.submit(lambda: (threading.get_ident(), next(stream))).result()
            second_id, remainder = second.submit(lambda: (threading.get_ident(), list(stream))).result()
        self.assertNotEqual(first_id, second_id)
        self.assertIn(b"first", chunk)
        self.assertIn(b"[DONE]", b"".join(remainder))
        finished = next(e for e in self.traces.list_events(self.user, self.trace_id)["items"] if e["event_type"] == "model.finished")
        self.assertTrue(finished["data"]["complete"])

    def test_web_budgets_are_named_persisted_and_do_not_charge_rejected_calls(self):
        for _ in range(10):
            self.broker.reserve_tool(self.cap, "search_web")
        for _ in range(20):
            self.broker.reserve_tool(self.cap, "read_web_page")
        self.assert_code("research_web_budget_reached", self.broker.reserve_tool, self.cap, "search_web")
        self.assert_code("research_web_budget_reached", self.broker.reserve_tool, self.cap, "read_web_page")
        self.broker.reserve_tool(self.cap, "query_dataset")
        self.broker.reserve_tool(self.cap)
        restarted = HarnessBroker(self.state, self.registry, self.traces, limits=self.broker.limits)
        _, cap = restarted.issue(self.user, self.run, self.trace_id)
        status = restarted.budget_status(cap)
        self.assertEqual((status["search_calls"], status["fetch_calls"], status["tool_calls"]), (10, 20, 32))
        self.assertEqual((status["remaining_search_calls"], status["remaining_fetch_calls"]), (0, 0))
        self.assert_code("research_web_budget_reached", restarted.reserve_tool, cap, "search_web")

    def test_calls_after_former_price_deadline_keep_estimates_and_budget_enforcement(self):
        with patch("agent.model_registry.date", wraps=date) as clock:
            clock.today.return_value = date(2026, 10, 16)
            _, body, number = self.broker.prepare_model(self.cap, {"messages": []}, self.tools)
            with self.broker._budget_connection() as db:
                charge = db.execute("SELECT reserved_nano,pricing_json FROM research_model_charges WHERE run_id=? AND call_number=?",
                                    (self.cap.run_id, number)).fetchone()
            self.assertGreater(charge["reserved_nano"], 0)
            self.assertEqual(json.loads(charge["pricing_json"])["checked_at"], "2026-10-08")
            self.assertEqual(self.registry.vision_pricing()["status"], "verified_list_price")
            self.broker._record_usage(self.cap, {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                                      number=number, complete=True, actual_model=body["model"])
            cost = self.broker.budget_status(self.cap)["model_cost"]
            self.assertEqual(cost["enforcement"], "conservative_preflight")
            self.assertEqual(cost["estimate_status"], "estimated_from_usage")
            self.assertGreater(float(cost["estimated_cny"]), 0)
            with patch.object(self.broker, "model_budget", 1):
                self.assert_code("research_cost_budget_reached", self.broker.prepare_model, self.cap, {"messages": []}, self.tools)

    def test_unknown_price_blocks_before_dispatch_and_keeps_budget_unchanged(self):
        with patch.object(self.registry, "pricing", return_value={"status": "unknown", "reason": "provider_region_price_unverified"}):
            self.assert_code("model_pricing_unavailable", self.broker.prepare_model, self.cap, {"messages": []}, self.tools)
            budget = self.broker.budget_status(self.cap)
            self.assertEqual(budget["remaining_model_calls"], 40)
            self.assertEqual(budget["model_cost"]["enforcement"], "blocked_unverified_pricing")
            self.assertEqual(budget["model_cost"]["estimate_status"], "unknown")
            self.assertIsNone(budget["model_cost"]["remaining_cny"])
            self.assertEqual(budget['model_cost']['unpriced_calls'], 0)
        with self.broker._budget_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM research_model_charges').fetchone()[0], 0)

    def test_provider_400_preserves_only_allowlisted_reason_without_retry(self):
        calls = []
        secret = 'private-user-text-or-key'
        def reject(request):
            calls.append(request)
            return httpx.Response(400, json={'error': {'code': 'InvalidParameter.UnsupportedParameter',
                'param': 'reasoning_effort', 'message': secret, 'body': secret}})
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(reject))
        with self.assertRaises(AgentError) as raised:
            list(self.broker.model_stream(self.cap, {'messages': []}, self.tools))
        self.assertEqual(raised.exception.http_status, 400)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(calls), 1)
        event = next(e for e in self.traces.list_events(self.user, self.trace_id)['items'] if e['event_type'] == 'error.model')
        self.assertEqual(event['data']['provider_error_category'], 'unsupported_parameter')
        self.assertEqual(event['data']['provider_parameter'], 'reasoning_effort')
        self.assertNotIn(secret.encode(), self.traces.database_path.read_bytes())
        for payload in ({'error': {'code': secret, 'param': secret}}, {'error': [secret]}):
            self.assertEqual(self.broker._provider_rejection(httpx.Response(400, json=payload)),
                             {'provider_error_category': 'unclassified'})
        self.assertEqual(self.broker._provider_rejection(httpx.Response(400, content=b'x' * 10000)),
                         {'provider_error_category': 'unclassified'})

    def test_historical_unpriced_calls_block_new_money_reservations(self):
        with self.broker._budget_connection() as db:
            db.execute('UPDATE research_budget SET unpriced_model_calls=1 WHERE run_id=?', (self.cap.run_id,))
        self.assert_code('research_cost_unreconciled', self.broker.prepare_model, self.cap, {'messages': []}, self.tools)
        self.assertEqual(self.broker.budget_status(self.cap)['model_cost']['enforcement'], 'blocked_unpriced_history')

    def test_unverified_cache_discount_keeps_upper_bound_without_inventing_estimate(self):
        number = self.broker.prepare_model(self.cap, {'messages': []}, self.tools)[2]
        with self.broker._budget_connection() as db:
            pricing = json.loads(db.execute('SELECT pricing_json FROM research_model_charges WHERE call_number=?', (number,)).fetchone()[0])
            pricing['cached_input_per_million'] = None
            db.execute('UPDATE research_model_charges SET pricing_json=? WHERE call_number=?', (json.dumps(pricing), number))
        self.broker._record_usage(self.cap, {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
            'prompt_tokens_details': {'cached_tokens': 80}}, number=number, complete=True, actual_model=self.cap.settings['model_id'])
        with self.broker._budget_connection() as db:
            row = db.execute('SELECT charged_nano,estimated_nano,settlement_basis FROM research_model_charges WHERE call_number=?', (number,)).fetchone()
        self.assertGreater(row['charged_nano'], 0)
        self.assertIsNone(row['estimated_nano'])
        self.assertEqual(row['settlement_basis'], 'unverified_cache_discount_upper_bound')

    def test_cost_reservation_is_atomic_and_not_refunded_on_restart_or_incomplete_usage(self):
        number = self.broker.prepare_model(self.cap, {"messages": []}, self.tools)[2]
        self.broker._record_usage(self.cap, {}, number=number, complete=False)
        with self.broker._budget_connection() as db:
            db.execute("UPDATE research_model_charges SET charged_nano=9000000000,estimated_nano=9000000000 WHERE run_id=?", (self.run["run_id"],))
        caps = [self.cap] + [self.broker.issue(self.user, self.run, self.trace_id)[1] for _ in range(5)]
        def reserve(cap):
            try:
                return self.broker.prepare_model(cap, {"messages": []}, self.tools)[2]
            except AgentError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=6) as pool:
            result = list(pool.map(reserve, caps))
        self.assertEqual(result.count(2), 1)
        self.assertEqual(result.count("research_cost_budget_reached"), 5)
        restarted = HarnessBroker(self.state, self.registry, self.traces, limits=self.broker.limits)
        _, cap = restarted.issue(self.user, self.run, self.trace_id)
        self.assert_code("research_cost_budget_reached", restarted.prepare_model, cap, {"messages": []}, self.tools)
        status = restarted.budget_status(cap)
        self.assertEqual(status["model_calls"], 2)
        self.assertEqual(status["model_cost"]["estimate_status"], "unknown")
        self.assertGreater(float(status["model_cost"]["pending_reserved_cny"]), 0)

    def test_verified_usage_settles_confirmed_cache_discount_and_keeps_safe_receipt(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
                 "prompt_tokens_details": {"cached_tokens": 500, "secret": "must-not-persist"},
                 "completion_tokens_details": {"reasoning_tokens": 50, "reasoning": "must-not-persist"},
                 "unknown": "must-not-persist", "reasoning_content": "must-not-persist"}
        raw = {"model": self.cap.settings["model_id"], "choices": [{"message": {"content": "answer"}}], "usage": usage}
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)))
        list(self.broker.model_stream(self.cap, {"messages": []}, self.tools))
        status = self.broker.budget_status(self.cap)
        self.assertEqual(status["model_cost"]["guarded_spend_cny"], "0.010350000")
        self.assertEqual(status["model_cost"]["estimated_cny"], "0.010350000")
        self.assertEqual(status["model_cost"]["pending_reserved_cny"], "0.000000000")
        finished = self.traces.list_events(self.user, self.trace_id)["items"][-1]
        self.assertEqual(finished["data"]["usage"]["prompt_tokens_details"], {"cached_tokens": 500})
        self.assertEqual(finished["data"]["usage"]["completion_tokens_details"], {"reasoning_tokens": 50})
        with self.broker._budget_connection() as db:
            receipt = db.execute("SELECT * FROM research_model_charges").fetchone()
            persisted = receipt["usage_json"]
            self.assertEqual(receipt["response_complete"], 1)
            self.assertEqual(receipt["actual_model"], self.cap.settings["model_id"])
            self.assertEqual(receipt["settlement_basis"], "verified_implicit_cache_usage")
        self.assertNotIn("must-not-persist", persisted)
        self.assertNotIn("must-not-persist", self.traces.database_path.read_bytes().decode("latin-1"))

    def test_late_usage_after_cancel_is_monotonic_and_settled_once(self):
        number = self.broker.prepare_model(self.cap, {"messages": []}, self.tools)[2]
        self.broker._record_usage(self.cap, {"total_tokens": 100}, number=number)
        self.broker._record_usage(self.cap, {"total_tokens": 50}, number=number)
        self.state.cancel(self.user, self.run["run_id"])
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        self.broker._record_usage(self.cap, usage, number=number, complete=True, actual_model=self.cap.settings["model_id"])
        self.broker._record_usage(self.cap, usage, number=number, complete=True, actual_model=self.cap.settings["model_id"])
        self.assertEqual(self.broker.budget_status(self.cap)["total_tokens"], 110)

    def test_unverifiable_input_cannot_bypass_known_price_guard(self):
        for content in ({"image": "https://example.test/large.png"}, [{"type": "image_url", "image_url": "https://example.test/large.png"}], [{"type": "text", "text": []}]):
            self.assert_code("model_cost_unverifiable", self.broker.prepare_model, self.cap, {"messages": [{"role": "user", "content": content}]}, self.tools)
        self.assertEqual(self.cap.model_calls, 0)
        estimate = self.broker._cost_reservation(self.cap, {"messages": [{"role": "user", "content": "x" * 80000}], "enable_thinking": True})
        self.assertGreater(estimate["reserved_nano"], 1_000_000_000)

    def test_legacy_budget_migration_does_not_reset_unknown_web_or_cost_spend(self):
        with self.broker._budget_connection() as db:
            db.execute("DROP TABLE research_model_charges")
            db.execute("DROP TABLE research_budget")
            db.execute("CREATE TABLE research_budget(run_id TEXT PRIMARY KEY,model_calls INTEGER,tool_calls INTEGER,total_tokens INTEGER,started_at REAL)")
            db.execute("INSERT INTO research_budget VALUES(?,2,7,123,0)", (self.run["run_id"],))
        restarted = HarnessBroker(self.state, self.registry, self.traces, limits=self.broker.limits)
        _, cap = restarted.issue(self.user, self.run, self.trace_id)
        status = restarted.budget_status(cap)
        self.assertEqual((status["search_calls"], status["fetch_calls"], status["legacy_unclassified_tool_calls"]), (7, 7, 7))
        self.assertEqual(status["model_cost"]["unpriced_calls"], 2)
        self.assertIsNone(status["model_cost"]["remaining_cny"])

    def test_active_budget_excludes_waiting_and_is_shared_across_resumed_attempts(self):
        with self.broker._budget_connection() as db:
            db.execute("UPDATE research_active_windows SET started_at=100 WHERE run_id=?", (self.run["run_id"],))
        self.state.checkpoint(self.user, self.run["run_id"], {}, status="waiting_collection", attempt_id=self.cap.attempt_id)
        with patch("agent.harness_broker.time.time", return_value=200):
            self.broker.pause(self.cap)
        with patch("agent.harness_broker.time.time", return_value=10000):
            self.assertEqual(self.broker.budget_status(self.cap)["active_seconds"], 100)
            resumed = self.state.resume(self.user, self.run["run_id"])["run"]
            self.traces.resume_trace(self.user, self.trace_id, attempt_id=resumed["attempt_id"])
            restarted = HarnessBroker(self.state, self.registry, self.traces, limits=self.broker.limits)
            _, cap = restarted.issue(self.user, resumed, self.trace_id)
            restarted.issue(self.user, resumed, self.trace_id)  # A second capability cannot duplicate a window.
        with patch("agent.harness_broker.time.time", return_value=10499):
            self.assertEqual(restarted.budget_status(cap)["remaining_active_seconds"], 1)
            restarted.reserve_tool(cap, "query_dataset")
        with patch("agent.harness_broker.time.time", return_value=10500):
            self.assert_code("research_active_budget_reached", restarted.prepare_model, cap, {"messages": []}, self.tools)
            restarted.revoke(cap.run_id, cap.attempt_id)
        with patch("agent.harness_broker.time.time", return_value=20000):
            status = restarted.budget_status(cap)
            self.assertEqual(status["active_seconds"], 600)
            self.assertEqual(status["model_calls"], 0)

    def test_recovery_closes_window_at_committed_wait_transition_without_pause(self):
        self.state.checkpoint(self.user, self.run["run_id"], {}, status="waiting_collection", attempt_id=self.cap.attempt_id)
        with self.broker._budget_connection() as db:
            db.execute("UPDATE research_active_windows SET started_at=100 WHERE run_id=?", (self.run["run_id"],))
            db.execute("UPDATE runs SET updated_at='1970-01-01T00:03:20Z' WHERE run_id=?", (self.run["run_id"],))
        with patch("agent.harness_broker.time.time", return_value=10000):
            restarted = HarnessBroker(self.state, self.registry, self.traces, limits=self.broker.limits)
            status = restarted.budget_status(self.cap)
        self.assertEqual(status["active_seconds"], 100)
        self.assertEqual(status["remaining_active_seconds"], 500)

    def test_parallel_web_calls_cannot_reuse_last_search_slots(self):
        with self.broker._budget_connection() as db:
            db.execute("UPDATE research_budget SET search_calls=8,tool_calls=8 WHERE run_id=?", (self.run["run_id"],))
        caps = [self.broker.issue(self.user, self.run, self.trace_id)[1] for _ in range(6)]
        def reserve(cap):
            try:
                return self.broker.reserve_tool(cap, "search_web")
            except AgentError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(reserve, caps))
        self.assertEqual(sorted(x for x in results if type(x) is int), [9, 10])
        self.assertEqual(results.count("research_web_budget_reached"), 4)

    def test_invalid_or_mismatched_usage_keeps_reservation_and_cannot_release_more_budget(self):
        number = self.broker.prepare_model(self.cap, {"messages": []}, self.tools)[2]
        before = self.broker.budget_status(self.cap)["model_cost"]["guarded_spend_cny"]
        for usage, model in (({"prompt_tokens": True, "completion_tokens": -1}, self.cap.settings["model_id"]),
                             ({"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 120}, self.cap.settings["model_id"]),
                             ({"prompt_tokens": 150, "completion_tokens": 10, "total_tokens": 160}, "different-model")):
            self.broker._record_usage(self.cap, usage, number=number, complete=True, actual_model=model)
            cost = self.broker.budget_status(self.cap)["model_cost"]
            self.assertEqual(cost["guarded_spend_cny"], before)
            self.assertEqual(cost["estimate_status"], "unknown")

    def test_confirmed_cache_releases_old_spend_but_next_call_still_reserves_no_cache(self):
        incoming = {"messages": [{"role": "user", "content": "x" * 400000}]}
        _, body, number = self.broker.prepare_model(self.cap, incoming, self.tools)
        reserve = self.broker._cost_reservation(self.cap, body)["reserved_nano"]
        usage = {"prompt_tokens": 400000, "completion_tokens": 100, "total_tokens": 400100,
                 "prompt_tokens_details": {"cached_tokens": 390000}}
        self.broker._record_usage(self.cap, usage, number=number, complete=True, actual_model=self.cap.settings["model_id"])
        cost = self.broker.budget_status(self.cap)["model_cost"]
        self.assertEqual(cost["guarded_spend_cny"], "0.708600000")
        self.assertGreater(reserve + 4_803_600_000, 10_000_000_000)  # Old undiscounted settlement would falsely block.
        self.assertEqual(self.broker.prepare_model(self.cap, incoming, self.tools)[2], 2)
        with self.broker._budget_connection() as db:
            self.assertEqual(db.execute("SELECT reserved_nano FROM research_model_charges WHERE call_number=2").fetchone()[0], reserve)
        self.assert_code("research_cost_budget_reached", self.broker.prepare_model, self.cap, incoming, self.tools)

    def test_cache_discount_requires_full_receipt_exact_model_and_consistent_cache_counts(self):
        base = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}
        cases = [({**base, "prompt_tokens_details": {"cached_tokens": 500}}, None),
                 ({"prompt_tokens": 1000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 500}}, self.cap.settings["model_id"]),
                 ({**base, "prompt_tokens_details": {"cached_tokens": 500}, "prompt_cache_hit_tokens": 600}, self.cap.settings["model_id"]),
                 ({**base, "prompt_tokens_details": {"cached_tokens": -1}}, self.cap.settings["model_id"]),
                 ({**base, "prompt_tokens_details": {"cached_tokens": 1001}}, self.cap.settings["model_id"]),
                 ({**base, "prompt_cache_hit_tokens": 500, "prompt_cache_miss_tokens": 1}, self.cap.settings["model_id"])]
        for usage, actual in cases:
            number = self.broker.prepare_model(self.cap, {"messages": []}, self.tools)[2]
            self.broker._record_usage(self.cap, usage, number=number, complete=True, actual_model=actual)
            with self.broker._budget_connection() as db:
                row = db.execute("SELECT * FROM research_model_charges WHERE call_number=?", (number,)).fetchone()
            self.assertEqual(row["charged_nano"], 15_600_000)
            self.assertEqual(row["settlement_basis"], "uncached_usage_upper_bound")

    def test_explicit_cache_controls_are_removed_recursively_without_editing_source_text(self):
        literal = '{"cache_control":{"type":"ephemeral"}}'
        incoming = {"messages": [{"role": "user", "cache_control": {"type": "ephemeral"}, "content": [
            {"type": "text", "text": literal, "cache_control": {"type": "ephemeral"}, "metadata": {"nested": [{"cache_control": {"type": "ephemeral"}}]}}]}]}
        original = copy.deepcopy(incoming)
        _, body, _ = self.broker.prepare_model(self.cap, incoming, self.tools)
        self.assertEqual(incoming, original)
        self.assertNotIn("cache_control", body["messages"][0])
        part = body["messages"][0]["content"][0]
        self.assertNotIn("cache_control", part)
        self.assertEqual(part["metadata"]["nested"], [{}])
        self.assertEqual(part["text"], literal)

    def test_unexpected_cache_creation_cannot_be_discounted_or_silently_ignored(self):
        number = self.broker.prepare_model(self.cap, {"messages": []}, self.tools)[2]
        reserve = self.broker.budget_status(self.cap)["model_cost"]["guarded_spend_cny"]
        usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
                 "prompt_tokens_details": {"cached_tokens": 500, "cache_creation_input_tokens": 200}}
        self.broker._record_usage(self.cap, usage, number=number, complete=True, actual_model=self.cap.settings["model_id"])
        restarted = HarnessBroker(self.state, self.registry, self.traces, limits=self.broker.limits)
        _, cap = restarted.issue(self.user, self.run, self.trace_id)
        cost = restarted.budget_status(cap)["model_cost"]
        self.assertEqual(cost["guarded_spend_cny"], reserve)
        self.assertEqual(cost["unverified_cache_creation_calls"], 1)
        self.assertEqual(cost["enforcement"], "blocked_unverified_cache_creation")
        self.assertEqual(cost["estimate_status"], "unknown")
        self.assertIsNone(cost["remaining_cny"])
        self.assert_code("research_cost_budget_reached", restarted.prepare_model, cap, {"messages": []}, self.tools)
        # An inconsistent later receipt cannot erase the already reported fee.
        for _ in range(2):
            self.broker._record_usage(self.cap, {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
                                      number=number, complete=True, actual_model=self.cap.settings["model_id"])
        self.assertEqual(restarted.budget_status(cap)["model_cost"]["unverified_cache_creation_calls"], 1)

    def test_stream_does_not_lose_cache_creation_signal_before_final_usage(self):
        items = [{"model": self.cap.settings["model_id"], "usage": {"cache_creation_input_tokens": "unverified-private-value"}},
                 {"usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100, "prompt_tokens_details": {"cached_tokens": 500}}}]
        raw = "".join("data: " + json.dumps(item) + "\n\n" for item in items) + "data: [DONE]\n\n"
        self.broker.model_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)))
        list(self.broker.model_stream(self.cap, {"messages": [], "stream": True}, self.tools))
        self.assertEqual(self.broker.budget_status(self.cap)["model_cost"]["unverified_cache_creation_calls"], 1)
        with self.broker._budget_connection() as db:
            self.assertNotIn("unverified-private-value", db.execute("SELECT usage_json FROM research_model_charges").fetchone()[0])
        self.assertNotIn("unverified-private-value", self.traces.database_path.read_bytes().decode("latin-1"))

    def test_existing_settled_rows_are_not_blindly_discounted_on_schema_upgrade(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100, "prompt_tokens_details": {"cached_tokens": 500}}
        pricing = self.registry.pricing(self.cap.settings)
        with self.broker._budget_connection() as db:
            db.execute("DROP TABLE research_model_charges")
            db.execute("CREATE TABLE research_model_charges(run_id TEXT,call_number INTEGER,pricing_json TEXT,reserved_nano INTEGER,charged_nano INTEGER,estimated_nano INTEGER,usage_json TEXT,PRIMARY KEY(run_id,call_number))")
            db.execute("INSERT INTO research_model_charges VALUES(?,?,?,?,?,?,?)", (self.run["run_id"], 1, json.dumps(pricing), 700000000, 15600000, 10350000, json.dumps(usage)))
            db.execute("UPDATE research_budget SET model_calls=1 WHERE run_id=?", (self.run["run_id"],))
        restarted = HarnessBroker(self.state, self.registry, self.traces, limits=self.broker.limits)
        _, cap = restarted.issue(self.user, self.run, self.trace_id)
        self.assertEqual(restarted.budget_status(cap)["model_cost"]["guarded_spend_cny"], "0.015600000")
        # Replaying a new receipt cannot rewrite an old settled call either.
        restarted._record_usage(cap, usage, number=1, complete=True, actual_model=cap.settings["model_id"])
        with restarted._budget_connection() as db:
            row = db.execute("SELECT * FROM research_model_charges").fetchone()
        self.assertEqual(row["charged_nano"], 15600000)
        self.assertEqual(row["settlement_basis"], "legacy_unverified")
        self.assertEqual(row["response_complete"], 0)
        self.assertIsNone(row["actual_model"])


if __name__ == "__main__":
    unittest.main()
