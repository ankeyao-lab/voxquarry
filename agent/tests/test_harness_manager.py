import json
import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpcore
import httpx

from agent.contracts import AgentError
from agent.harness_manager import HarnessManager


class HarnessManagerClientTests(unittest.TestCase):
    def client(self, callback):
        return HarnessManager("http://research.test:9001", "m" * 48, transport=httpx.MockTransport(callback))

    def test_protocol_and_binary_inputs_do_not_send_docker_options(self):
        requests = []
        def callback(request):
            requests.append(request)
            return httpx.Response(200, json={"status": "running"})
        client = self.client(callback)
        client.start({"attempt_id": "a_1", "run_id": "r_1", "mode": "analysis"})
        client.status("a_1", 5)
        client.tool("a_1", "run_python", {"code": "print(2+3)"})
        client.put_input("a_1", "part-001.jsonl", b'{"rating":5}\n')
        client.stop("a_1")
        self.assertEqual([request.method for request in requests], ["POST", "GET", "POST", "POST", "DELETE"])
        self.assertEqual(str(requests[1].url), "http://research.test:9001/v1/attempts/a_1?after=5")
        self.assertEqual(json.loads(requests[2].content), {"arguments": {"code": "print(2+3)"}})
        self.assertEqual(requests[3].content, b'{"rating":5}\n')
        self.assertEqual(requests[3].headers["content-type"], "application/octet-stream")
        self.assertEqual(requests[3].headers["authorization"], "Bearer " + "m" * 48)
        self.assertNotIn(b"docker", b"".join(request.content for request in requests))

    def test_output_is_exact_binary_and_path_is_encoded(self):
        seen = []
        def callback(request):
            seen.append(request)
            return httpx.Response(200, content=b"\x00PNG\xff", headers={"content-type": "application/octet-stream"})
        client = self.client(callback)
        self.assertEqual(client.read_output("a_1", "/workspace/result chart.png"), b"\x00PNG\xff")
        self.assertEqual(seen[0].url.params["path"], "/workspace/result chart.png")

    def test_report_render_uses_the_isolated_tool_route_and_preserves_markdown(self):
        seen = []
        def callback(request):
            seen.append(request)
            return httpx.Response(200, json={"files": [{"path": "/workspace/报告.pdf"}]})
        arguments = {"title": "报告", "filename": "报告", "markdown": "|体验|来源|\n|---|---|\n|干草|[1]|", "formats": ["md", "pdf"]}
        result = self.client(callback).tool("a_1", "render_report", arguments)
        self.assertEqual(result["files"][0]["path"], "/workspace/报告.pdf")
        self.assertEqual(str(seen[0].url), "http://research.test:9001/v1/attempts/a_1/tools/render_report")
        self.assertEqual(json.loads(seen[0].content), {"arguments": arguments})

    def test_bad_ids_paths_tools_and_oversized_shards_never_leave_client(self):
        client = self.client(lambda _request: self.fail("should not issue a request"))
        for action in [lambda: client.start([]), lambda: client.status("../../other"), lambda: client.status("a", True),
                       lambda: client.read_output("a", "/etc/passwd"), lambda: client.read_output("a", "/workspace/../secret"),
                       lambda: client.put_input("a", "../bad", b"x"),
                       lambda: client.put_input("a", "part", b"x" * (64 * 1024 * 1024 + 1)),
                       lambda: client.tool("a", "docker", {})]:
            with self.assertRaises(AgentError):
                action()

    def test_http_failures_do_not_surface_upstream_body_or_token(self):
        client = self.client(lambda _request: httpx.Response(403, json={"error": {"code": "manager_unauthorized", "message": "sensitive-token internal-source"}}))
        with self.assertRaises(AgentError) as raised:
            client.status("a")
        self.assertEqual(raised.exception.code, "manager_unauthorized")
        self.assertNotIn("sensitive", str(raised.exception))

    def test_status_recovers_transient_transport_failure_with_same_cursor(self):
        calls = []
        def callback(request):
            calls.append(request)
            if len(calls) < 3:
                raise httpx.ConnectTimeout("private-upstream-token", request=request)
            return httpx.Response(200, json={"status": "running", "cursor": 8, "events": [{"seq": 8}]})
        with patch("agent.harness_manager.time.sleep"):
            result = self.client(callback).status("a_1", after=7)
        self.assertEqual(result["cursor"], 8)
        self.assertEqual(result["transport_recovery"], {"retries": 2, "failures": ["connect_timeout", "connect_timeout"]})
        self.assertEqual({(request.method, str(request.url)) for request in calls},
                         {("GET", "http://research.test:9001/v1/attempts/a_1?after=7")})
        self.assertNotIn("private", json.dumps(result))

    def test_persistent_status_failure_is_bounded_and_post_is_never_replayed(self):
        calls = []
        def callback(request):
            calls.append(request)
            raise httpx.ReadError("private-source", request=request)
        client = self.client(callback)
        with patch("agent.harness_manager.time.sleep"), self.assertRaises(AgentError) as raised:
            client.status("a_1")
        self.assertEqual(len(calls), 3)
        self.assertEqual(raised.exception.transport_kind, "read_error")
        self.assertNotIn("private", str(raised.exception))
        with self.assertRaises(AgentError):
            client.tool("a_1", "run_python", {"code": "print(1)"})
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[-1].method, "POST")

    def test_native_connect_timeout_recovers_before_sending_request(self):
        written = []
        class Stream:
            def read(self, _max_bytes, timeout=None):
                body = b'{"result":{"id":"one-process"}}'
                return b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body

            def write(self, data, timeout=None):
                written.append(data)

            def close(self):
                pass

            def get_extra_info(self, _name):
                return None

        client = HarnessManager('http://research.test:9001', 'm' * 48)
        payload = {'op': 'spawn', 'spec': {'command': 'synthetic'}}
        # Exercise HTTPX's actual transport retry, without opening a socket.
        with patch('httpcore._backends.sync.SyncBackend.connect_tcp',
                   side_effect=[httpcore.ConnectTimeout('synthetic timeout'), Stream()]) as connect:
            result = client.native_io('a_1', payload)
        self.assertEqual(result, {'result': {'id': 'one-process'}})
        self.assertEqual(connect.call_count, 2)
        raw = b''.join(written)
        self.assertEqual(raw.count(b'POST /v1/attempts/a_1/native'), 1)
        self.assertEqual(json.loads(raw.split(b'\r\n\r\n', 1)[1]), payload)

    def test_native_unknown_outcome_and_http_rejection_are_not_replayed(self):
        for failure in ('read', '503', '403'):
            with self.subTest(failure=failure):
                calls = []
                def callback(request):
                    calls.append(request)
                    if failure == 'read':
                        raise httpx.ReadError('uncertain native outcome', request=request)
                    return httpx.Response(int(failure), json={'error': {'code': 'native_unavailable'}})
                with self.assertRaises(AgentError):
                    self.client(callback).native_io('a_1', {'op': 'spawn', 'spec': {}})
                self.assertEqual(len(calls), 1)

    def test_redirects_are_not_followed_and_unknown_json_is_rejected(self):
        calls = []
        def callback(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "http://another.test/credentials"})
        with self.assertRaises(AgentError):
            self.client(callback).status("a")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(AgentError) as raised:
            self.client(lambda _request: httpx.Response(200, json=["not-a-contract"])).status("a")
        self.assertEqual(raised.exception.code, "harness_response_invalid")

    def test_configuration_must_not_embed_credentials_or_arbitrary_base_path(self):
        for url in ["file:///var/run/docker.sock", "http://user:pass@research.test", "http://research.test/api", "http://research.test?q=1"]:
            with self.assertRaises(AgentError):
                HarnessManager(url, "m" * 48)
        with self.assertRaises(AgentError):
            HarnessManager("http://research.test", "short")

    def stop_clock(self):
        clock = SimpleNamespace(now=0.0, sleeps=[])
        def sleep(seconds):
            clock.sleeps.append(seconds)
            clock.now += seconds
        clock.monotonic, clock.sleep = lambda: clock.now, sleep
        return clock

    def test_stop_retries_only_explicit_cleanup_pending_until_confirmed(self):
        clock, calls = self.stop_clock(), []
        def callback(request):
            calls.append(request)
            return (httpx.Response(503, json={"error": {"code": "cleanup_pending"}})
                    if len(calls) < 3 else httpx.Response(200, json={"status": "stopped"}))
        with patch("agent.harness_manager.time", clock, create=True):
            result = self.client(callback).stop("a_1")
        self.assertEqual(result, {"status": "stopped"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(clock.sleeps), 2)
        self.assertTrue(all(0 < delay <= 1 for delay in clock.sleeps))
        self.assertEqual({str(request.url) for request in calls}, {"http://research.test:9001/v1/attempts/a_1"})

    def test_stop_persistent_pending_obeys_total_deadline_and_request_remaining_time(self):
        clock, calls = self.stop_clock(), []
        def callback(request):
            remaining = 45 - clock.now
            timeouts = request.extensions["timeout"]
            self.assertTrue(all(value <= remaining for value in timeouts.values()))
            calls.append(remaining)
            clock.now += min(timeouts["read"], 10)
            return httpx.Response(503, json={"error": {"code": "cleanup_pending"}})
        with patch("agent.harness_manager.time", clock, create=True), self.assertRaises(AgentError) as raised:
            self.client(callback).stop("a_1")
        self.assertEqual(raised.exception.code, "cleanup_pending")
        self.assertEqual(clock.now, 45)
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(remaining > 0 for remaining in calls))

    def test_stop_other_failures_are_not_retried_after_pending(self):
        clock, calls = self.stop_clock(), []
        def callback(request):
            calls.append(request)
            code = "cleanup_pending" if len(calls) == 1 else "manager_unauthorized"
            return httpx.Response(503 if len(calls) == 1 else 403, json={"error": {"code": code}})
        with patch("agent.harness_manager.time", clock, create=True), self.assertRaises(AgentError) as raised:
            self.client(callback).stop("a_1")
        self.assertEqual(raised.exception.code, "manager_unauthorized")
        self.assertEqual(len(calls), 2)

    def test_stop_does_not_publish_a_late_success_after_the_deadline(self):
        clock = self.stop_clock()
        def callback(request):
            clock.now = 46
            return httpx.Response(200, json={"status": "stopped"})
        with patch("agent.harness_manager.time", clock, create=True), self.assertRaises(AgentError) as raised:
            self.client(callback).stop("a_1")
        self.assertEqual(raised.exception.code, "cleanup_pending")

    def test_stop_caller_deadline_is_bounded_even_when_http_phase_is_still_running(self):
        released, finished = threading.Event(), threading.Event()
        calls = []
        def callback(request):
            calls.append(request)
            try:
                released.wait(timeout=2)
                return httpx.Response(200, json={"status": "stopped"})
            finally:
                finished.set()
        started = time.monotonic()
        try:
            with patch("agent.harness_manager.STOP_TIMEOUT_SECONDS", 0.05), self.assertRaises(AgentError) as raised:
                self.client(callback).stop("a_1")
            self.assertEqual(raised.exception.code, "cleanup_pending")
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            released.set()
        self.assertTrue(finished.wait(timeout=1))
        self.assertEqual(len(calls), 1)
