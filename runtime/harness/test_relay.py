"""Offline protocol checks; every upstream is an ephemeral loopback server."""
from contextlib import contextmanager, redirect_stderr
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import relay


CAPABILITY = "only-a-local-test-attempt-capability-123456"
PRIVATE_BODY = "TEST_ONLY_BODY_MUST_NOT_APPEAR_IN_ERRORS"
TOOL = "/broker/tools/query_dataset"


def reply(handler, status=200, body=b'{"ok":true}', content_type="application/json", headers=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.received.put((self.path, dict(self.headers), body))
        try:
            self.server.behavior(self)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


@contextmanager
def running(server):
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@contextmanager
def pair(behavior=reply, **overrides):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    upstream.daemon_threads = True
    upstream.received = queue.Queue()
    upstream.behavior = behavior
    with running(upstream):
        config = relay.RelayConfig(
            broker_base_url=f"http://127.0.0.1:{upstream.server_port}/wenquan/internal/broker",
            tool_names=frozenset({"query_dataset", "read_report"}), capability=CAPABILITY,
            **{"request_timeout_seconds": 3, "idle_timeout_seconds": 1, **overrides})
        with running(relay.RelayServer(("127.0.0.1", 0), config)) as server:
            yield server, upstream


def request(server, path=TOOL, body=b"{}", method="POST", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(method, path, body=body,
                           headers={"Authorization": "Bearer " + CAPABILITY, "Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class RelayTests(unittest.TestCase):
    def test_model_and_approved_tools_use_fixed_upstream_and_only_trusted_headers(self):
        def upstream(handler):
            reply(handler, headers={"Set-Cookie": "private=secret", "X-Internal": PRIVATE_BODY})
        with pair(upstream) as (server, fake):
            for path in (TOOL, relay.MODEL_PATH, relay.TRAJECTORY_PATH):
                with self.subTest(path=path):
                    body = json.dumps({"base_url": "http://not-an-upstream.invalid", "arguments": {"text": PRIVATE_BODY}}).encode()
                    status, headers, received = request(server, path, body, headers={"Cookie": "session=PRIVATE", "Host": "evil.invalid",
                        "X-Upstream-URL": "http://evil.invalid", "Proxy-Authorization": "secret", "X-Private": PRIVATE_BODY,
                        "X-VoxQuarry-Relay-Prepare-Ms": "forged-untrusted-timing"})
                    self.assertEqual((status, received), (200, b'{"ok":true}'))
                    upstream_path, upstream_headers, upstream_body = fake.received.get(timeout=1)
                    self.assertEqual(upstream_path, "/wenquan/internal" + path)
                    self.assertEqual(upstream_body, body)
                    self.assertEqual(upstream_headers["Authorization"], "Bearer " + CAPABILITY)
                    self.assertGreaterEqual(float(upstream_headers["X-VoxQuarry-Relay-Prepare-Ms"]), 0)
                    self.assertLess(float(upstream_headers["X-VoxQuarry-Relay-Prepare-Ms"]), 3000)
                    self.assertEqual(upstream_headers["Host"], f"127.0.0.1:{fake.server_port}")
                    for key in ("Cookie", "X-Upstream-URL", "Proxy-Authorization", "X-Private"):
                        self.assertNotIn(key, upstream_headers)
                    self.assertNotIn("Set-Cookie", headers)
                    self.assertNotIn("X-Internal", headers)
                    self.assertEqual(headers["Transfer-Encoding"], "chunked")

    def test_other_methods_routes_queries_and_encoded_paths_never_reach_upstream(self):
        with pair() as (server, fake):
            for path in ("/", "/broker/model/v1/completions", "/broker/tools/unapproved", TOOL + "?url=http://evil.invalid",
                         TOOL + "#fragment", "/broker/tools/%71uery_dataset", "/broker/tools/../query_dataset",
                         "/broker//tools/query_dataset", "//broker/tools/query_dataset", "http://evil.invalid" + TOOL):
                with self.subTest(path=path):
                    self.assertEqual(request(server, path)[0], 404)
            for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE", "CUSTOM"):
                with self.subTest(method=method):
                    self.assertEqual(request(server, method=method)[0], 405)
            self.assertTrue(fake.received.empty())

    def test_capability_must_match_the_attempt_exactly_and_duplicate_auth_is_rejected(self):
        with pair() as (server, fake):
            for value in ("", "Bearer wrong-attempt-capability", "Basic " + CAPABILITY, "bearer " + CAPABILITY,
                          "Bearer " + CAPABILITY + " ", "Bearer  " + CAPABILITY):
                with self.subTest(value=value):
                    status, _, body = request(server, headers={"Authorization": value})
                    self.assertEqual(status, 401)
                    self.assertNotIn(CAPABILITY.encode(), body)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.putrequest("POST", TOOL)
                for _ in range(2):
                    connection.putheader("Authorization", "Bearer " + CAPABILITY)
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", "2")
                connection.endheaders(b"{}")
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
            finally:
                connection.close()
            self.assertTrue(fake.received.empty())

    def test_declared_body_limits_are_enforced_before_reading_or_forwarding(self):
        with pair() as (server, fake):
            for path, limit in ((TOOL, relay.TOOL_BODY_LIMIT), (relay.MODEL_PATH, relay.MODEL_BODY_LIMIT)):
                with self.subTest(path=path):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                    try:
                        connection.putrequest("POST", path)
                        connection.putheader("Authorization", "Bearer " + CAPABILITY)
                        connection.putheader("Content-Type", "application/json")
                        connection.putheader("Content-Length", str(limit + 1))
                        connection.endheaders()
                        response = connection.getresponse()
                        self.assertEqual(response.status, 413)
                        response.read()
                    finally:
                        connection.close()
            self.assertTrue(fake.received.empty())

    def test_json_and_request_framing_reject_ambiguous_or_encoded_input(self):
        with pair() as (server, fake):
            for body in (b"[]", b"null", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b"\xff", b"bad JSON"):
                with self.subTest(body=body):
                    self.assertEqual(request(server, body=body)[0], 400)
            self.assertEqual(request(server, headers={"Content-Type": "text/plain"})[0], 415)
            self.assertEqual(request(server, headers={"Content-Encoding": "gzip"})[0], 400)
            self.assertEqual(request(server, headers={"Transfer-Encoding": "chunked"})[0], 400)
            self.assertEqual(request(server, headers={"Expect": "100-continue"})[0], 417)
            self.assertTrue(fake.received.empty())

    def test_redirects_and_upstream_failures_are_not_followed_or_exposed(self):
        logs = io.StringIO()
        with redirect_stderr(logs):
            for upstream_status in (301, 302, 307, 308, 400, 401, 429, 500):
                with self.subTest(status=upstream_status):
                    def upstream(handler):
                        reply(handler, upstream_status, (PRIVATE_BODY + CAPABILITY).encode(), headers={"Location": "http://127.0.0.1:1/private"})
                    with pair(upstream) as (server, fake):
                        status, headers, body = request(server)
                        self.assertEqual(status, 502 if upstream_status < 400 else upstream_status)
                        self.assertEqual(fake.received.qsize(), 1)
                        self.assertNotIn("Location", headers)
                        self.assertNotIn(PRIVATE_BODY.encode(), body)
                        self.assertNotIn(CAPABILITY.encode(), body)
                        error = json.loads(body)["error"]
                        self.assertIn(error["code"], error["message"])
                        self.assertIn("HTTP " + str(status), error["message"])
        self.assertEqual(logs.getvalue(), "")

    def test_oversized_declared_response_is_rejected_before_success_headers(self):
        def oversized(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(relay.RESPONSE_LIMIT + 1))
            handler.send_header("Connection", "close")
            handler.end_headers()
        with pair(oversized) as (server, _fake):
            status, _, body = request(server)
            self.assertEqual(status, 502)
            self.assertEqual(json.loads(body)["error"]["code"], "response_too_large")

    def test_oversized_chunked_response_aborts_without_a_successful_stream_terminator(self):
        def oversized(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.send_header("Connection", "close")
            handler.end_headers()
            for _ in range(8):
                handler.wfile.write(b"40\r\n" + b"x" * 64 + b"\r\n")
                handler.wfile.flush()
            handler.wfile.write(b"0\r\n\r\n")
        with patch.object(relay, "RESPONSE_LIMIT", 128), pair(oversized) as (server, _fake):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("POST", TOOL, b"{}", {"Authorization": "Bearer " + CAPABILITY, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                with self.assertRaises(http.client.IncompleteRead) as failure:
                    response.read()
                self.assertLessEqual(len(failure.exception.partial), 128)
            finally:
                connection.close()

    def test_sse_first_event_arrives_before_upstream_completion(self):
        release = threading.Event()
        first = b'data: {"delta":"first"}\n\n'
        last = b"data: [DONE]\n\n"
        def stream(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(first)
            handler.wfile.flush()
            if release.wait(2):
                handler.wfile.write(last)
        with pair(stream) as (server, _fake):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
            try:
                connection.request("POST", relay.MODEL_PATH, b'{"stream":true}', {"Authorization": "Bearer " + CAPABILITY, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read1(1024), first)
                self.assertFalse(release.is_set())
                release.set()
                self.assertEqual(response.read(), last)
            finally:
                release.set()
                connection.close()

    def test_brief_connection_burst_waits_without_exceeding_active_limit(self):
        entered, release = threading.Event(), threading.Event()
        active, peak = 0, 0
        lock = threading.Lock()
        def slow_first(handler):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                entered.set()
                release.wait(1)
                reply(handler)
            finally:
                with lock:
                    active -= 1
        with pair(slow_first, max_concurrency=1, idle_timeout_seconds=2) as (server, fake):
            results = []
            first = threading.Thread(target=lambda: results.append(request(server)[0]))
            first.start()
            self.assertTrue(entered.wait(1))
            second = threading.Thread(target=lambda: results.append(request(server)[0]))
            second.start()
            try:
                time.sleep(0.15)
                self.assertTrue(second.is_alive(), "a brief burst must wait instead of returning 503")
            finally:
                release.set()
                first.join(2)
                second.join(2)
            self.assertEqual(results, [200, 200])
            self.assertEqual(peak, 1)
            self.assertEqual(fake.received.qsize(), 2)

    def test_sustained_connection_overload_is_still_bounded(self):
        with pair(max_concurrency=1, idle_timeout_seconds=3) as (server, fake):
            self.assertTrue(server.slots.acquire(blocking=False))
            try:
                started = time.monotonic()
                status, _, body = request(server)
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(body)["error"]["code"], "relay_busy")
                self.assertLess(time.monotonic() - started, 1.8)
                self.assertTrue(fake.received.empty())
            finally:
                server.slots.release()

    def test_upstream_header_timeout_is_safe(self):
        entered, release = threading.Event(), threading.Event()
        def stall(handler):
            entered.set()
            if release.wait(2):
                reply(handler)
        with pair(stall, max_concurrency=1, idle_timeout_seconds=0.25) as (server, fake):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("POST", TOOL, b"{}", {"Authorization": "Bearer " + CAPABILITY, "Content-Type": "application/json"})
                self.assertTrue(entered.wait(1))
                response = connection.getresponse()
                self.assertEqual(response.status, 504)
                self.assertNotIn(CAPABILITY.encode(), response.read())
                self.assertEqual(fake.received.qsize(), 1)
            finally:
                release.set()
                connection.close()

    def test_continuous_sse_cannot_extend_the_total_request_deadline(self):
        stop, exited = threading.Event(), threading.Event()
        def stream(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            try:
                while not stop.wait(0.02):
                    handler.wfile.write(b"data: still-running\n\n")
                    handler.wfile.flush()
            finally:
                exited.set()
        with pair(stream, request_timeout_seconds=0.3, idle_timeout_seconds=0.2) as (server, _fake):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
            try:
                started = time.monotonic()
                connection.request("POST", relay.MODEL_PATH, b'{"stream":true}', {"Authorization": "Bearer " + CAPABILITY, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                with self.assertRaises(http.client.IncompleteRead) as failure:
                    response.read()
                self.assertIn(b"data: still-running", failure.exception.partial)
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(exited.wait(1), "upstream socket must also close on the deadline")
            finally:
                stop.set()
                connection.close()

    def test_long_tool_first_byte_wait_does_not_extend_model_idle_limit(self):
        def delayed(handler):
            time.sleep(0.2)
            reply(handler)
        with pair(delayed, idle_timeout_seconds=0.05, tool_idle_timeout_seconds=0.5, request_timeout_seconds=1) as (server, fake):
            self.assertEqual(request(server)[0], 200)
            status, _, body = request(server, relay.MODEL_PATH)
            self.assertEqual(status, 504)
            self.assertEqual(json.loads(body)["error"]["code"], "upstream_timeout")
            self.assertEqual(fake.received.qsize(), 2)

    def test_model_waits_for_silent_thinking_before_headers_and_between_sse_events(self):
        for headers_first in (False, True):
            def delayed(handler):
                if headers_first:
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/event-stream")
                    handler.send_header("Connection", "close")
                    handler.end_headers()
                    handler.wfile.write(b"data: first\n\n")
                    handler.wfile.flush()
                time.sleep(0.2)
                if headers_first:
                    handler.wfile.write(b"data: [DONE]\n\n")
                else:
                    reply(handler)
            with self.subTest(headers_first=headers_first), pair(delayed, idle_timeout_seconds=0.05,
                    model_idle_timeout_seconds=0.5, request_timeout_seconds=1) as (server, _fake):
                status, _, body = request(server, relay.MODEL_PATH)
                self.assertEqual(status, 200)
                self.assertEqual(body, b"data: first\n\ndata: [DONE]\n\n" if headers_first else b'{"ok":true}')

    def test_model_wait_does_not_extend_tool_or_incoming_client_idle_limit(self):
        def delayed(handler):
            time.sleep(0.2)
            reply(handler)
        with pair(delayed, idle_timeout_seconds=0.05, model_idle_timeout_seconds=0.5,
                  request_timeout_seconds=1) as (server, fake):
            status, _, body = request(server)
            self.assertEqual(status, 504)
            self.assertEqual(json.loads(body)["error"]["code"], "upstream_timeout")
            self.assertEqual(fake.received.qsize(), 1)
        with pair(idle_timeout_seconds=0.05, model_idle_timeout_seconds=0.5,
                  request_timeout_seconds=1) as (server, fake):
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as client:
                started = time.monotonic()
                client.sendall(b"POST " + relay.MODEL_PATH.encode() + b" HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                               + CAPABILITY.encode() + b"\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
                received = client.recv(4096)
                self.assertTrue(not received or b"504" in received)
                self.assertLess(time.monotonic() - started, 0.7)
                self.assertTrue(fake.received.empty())

    def test_silent_model_wait_is_bounded_by_request_and_remaining_attempt_deadlines(self):
        for deadline_kind in ("model", "attempt"):
            stop, exited = threading.Event(), threading.Event()
            def stream(handler):
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("Connection", "close")
                handler.end_headers()
                try:
                    while not stop.wait(0.02):
                        handler.wfile.write(b"data: still-running\n\n")
                        handler.wfile.flush()
                finally:
                    exited.set()
            # A small deterministic deadline exercises the same path used by
            # the 300-second model ceiling and the VM's attempt deadline.
            bounds = {"model_request_timeout_seconds": 0.2} if deadline_kind == "model" else {
                "attempt_deadline_monotonic": time.monotonic() + 0.2}
            with self.subTest(deadline=deadline_kind), pair(stream, request_timeout_seconds=1,
                    idle_timeout_seconds=0.05, model_idle_timeout_seconds=0.8, **bounds) as (server, _fake):
                try:
                    started = time.monotonic()
                    with self.assertRaises(http.client.IncompleteRead) as failure:
                        request(server, relay.MODEL_PATH)
                    self.assertIn(b"data: still-running", failure.exception.partial)
                    self.assertLess(time.monotonic() - started, 0.8)
                    self.assertTrue(exited.wait(1), "both sockets must close at the earliest deadline")
                finally:
                    stop.set()

    def test_expired_attempt_cannot_forward_a_new_model_request(self):
        with pair(attempt_deadline_monotonic=time.monotonic() - 1) as (server, fake):
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
                request(server, relay.MODEL_PATH)
            self.assertTrue(fake.received.empty())

    def test_long_tool_wait_cannot_extend_total_deadline_or_client_read_limit(self):
        release = threading.Event()
        def stall(handler):
            if release.wait(2):
                reply(handler)
        with pair(stall, idle_timeout_seconds=0.05, tool_idle_timeout_seconds=2, request_timeout_seconds=0.2) as (server, fake):
            try:
                started = time.monotonic()
                try:
                    status, _, body = request(server)
                except (http.client.RemoteDisconnected, ConnectionResetError, http.client.IncompleteRead):
                    pass
                else:
                    # The deadline closes both sockets. The upstream thread
                    # can return its controlled failure before the client
                    # socket shutdown wins; both orders must fail closed.
                    self.assertIn(status, {502, 504})
                    self.assertIn(json.loads(body)["error"]["code"], {"relay_failed", "upstream_timeout"})
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(fake.received.qsize(), 1)
            finally:
                release.set()
        # The larger tool wait applies only after authentication and complete
        # JSON input; it must not make slow request bodies hold slots longer.
        with pair(tool_idle_timeout_seconds=2, idle_timeout_seconds=0.05, request_timeout_seconds=1) as (server, fake):
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as client:
                started = time.monotonic()
                client.sendall(b"POST /broker/tools/query_dataset HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + CAPABILITY.encode() + b"\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
                received = client.recv(4096)
                self.assertTrue(not received or b"504" in received)
                self.assertLess(time.monotonic() - started, 0.7)
                self.assertTrue(fake.received.empty())

    def test_request_deadline_closes_clients_that_never_send_complete_headers_or_body(self):
        for partial in (b"POST /broker/tools/query_dataset HTTP/1.1\r\nHost: localhost\r\n", b"POST /broker/tools/query_dataset HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + CAPABILITY.encode() + b"\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"):
            with self.subTest(partial_body=partial.endswith(b"{")):
                with pair(request_timeout_seconds=0.2, idle_timeout_seconds=0.2) as (server, fake):
                    with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as client:
                        started = time.monotonic()
                        client.sendall(partial)
                        received = client.recv(4096)
                        self.assertTrue(not received or b"504" in received)
                        self.assertLess(time.monotonic() - started, 1)
                        self.assertTrue(fake.received.empty())

    def test_truncated_upstream_and_unexpected_media_type_fail_closed(self):
        def truncated(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", "100")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(b"{}")
        with pair(truncated) as (server, _fake):
            with self.assertRaises(http.client.IncompleteRead):
                request(server)
        with pair(lambda handler: reply(handler, content_type="text/html")) as (server, _fake):
            self.assertEqual(request(server)[0], 502)

    def test_configuration_is_strict_and_capability_is_not_in_repr(self):
        with tempfile.TemporaryDirectory() as directory:
            config, token = Path(directory) / "relay.json", Path(directory) / "capability"
            original = {"broker_base_url": "http://127.0.0.1:8088/wenquan/internal/broker", "tool_names": ["query_dataset"]}
            token.write_text(CAPABILITY + "\n")
            config.write_text(json.dumps(original))
            parsed = relay.RelayConfig.from_files(config, token)
            self.assertEqual(parsed.capability, CAPABILITY)
            self.assertNotIn(CAPABILITY, repr(parsed))
            for patch_value in ({"broker_base_url": "http://user:pass@127.0.0.1/broker"}, {"broker_base_url": "http://127.0.0.1/broker?next=elsewhere"},
                                {"broker_base_url": "http://127.0.0.1/elsewhere"}, {"tool_names": ["../private"]}, {"tool_names": ["query_dataset", "query_dataset"]},
                                {"capability_file": "/private"}, {"max_concurrency": 1000}, {"request_timeout_seconds": 0},
                                {"tool_idle_timeout_seconds": -1}, {"tool_idle_timeout_seconds": 1861},
                                {"model_idle_timeout_seconds": -1}, {"model_idle_timeout_seconds": 1861},
                                {"model_request_timeout_seconds": 1861}, {"model_request_timeout_seconds": 0},
                                {"attempt_deadline_monotonic": True}, {"attempt_deadline_monotonic": -1}):
                config.write_text(json.dumps({**original, **patch_value}))
                with self.subTest(keys=list(patch_value)), self.assertRaises(ValueError):
                    relay.RelayConfig.from_files(config, token)
            config.write_text(json.dumps(original))
            symlink = Path(directory) / "capability-link"
            symlink.symlink_to(token)
            with self.assertRaises(OSError):
                relay.RelayConfig.from_files(config, symlink)


if __name__ == "__main__":
    unittest.main()
