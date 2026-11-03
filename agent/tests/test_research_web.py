"""Network-boundary tests with synthetic HTTP/DNS; no real credentials or I/O."""
from __future__ import annotations

import copy
import json
import socket
import unittest
from unittest.mock import Mock, patch

import httpx

from agent.contracts import AgentError
from agent.research_web import ALIYUN_SEARCH_MCP_URL, ResearchWeb, _PinnedConnection, public_addresses, public_url


def address(ip="93.184.216.34", port=443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port))


class PageResponse:
    def __init__(self, status=200, headers=None, body=b"public text"):
        self.status = status
        self.headers = {"Content-Type": "text/plain", **(headers or {})}
        self.body, self.read_sizes = body, []
        self.offset = 0

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, amount):
        self.read_sizes.append(amount)
        part = self.body[self.offset:self.offset + amount]
        self.offset += len(part)
        return part


class PageConnection:
    def __init__(self, response):
        self.response = response
        self.requests, self.closed = [], False

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ResearchWebTests(unittest.TestCase):
    def assert_code(self, code, call, *args, **kwargs):
        with self.assertRaises(AgentError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_url_rejects_local_metadata_credentials_ports_and_controls(self):
        urls = ("http://localhost/", "https://api.local/", "https://api.internal/", "http://127.0.0.1/", "http://10.2.3.4/",
                "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                "http://user:password@example.com/", "https://example.com:8443/", "file:///etc/passwd",
                "https://example.com/\r\nHost:localhost", 123, None, [])
        for url in urls:
            with self.subTest(url=url):
                self.assert_code("web_url_invalid", public_url, url)
        self.assertEqual(public_url("https://example.com?search=grass#part"), "https://example.com/?search=grass")

    def test_dns_rejects_any_private_answer_even_when_another_answer_is_public(self):
        for results in ([], [address("127.0.0.1")], [address(), address("10.0.0.2")], [address("2606:4700:4700::1111"), address("::1")]):
            with patch("agent.research_web.socket.getaddrinfo", return_value=results):
                self.assert_code("web_address_blocked", public_addresses, "public-looking.example", 443)
        with patch("agent.research_web.socket.getaddrinfo", side_effect=socket.gaierror("private diagnostics")):
            error = self.assert_code("web_address_blocked", public_addresses, "unknown.example", 443)
            self.assertNotIn("private diagnostics", error.message)

    def test_tls_connect_uses_pinned_public_address_and_original_hostname_without_redns(self):
        sock, wrapped = Mock(), Mock()
        context = Mock()
        context.wrap_socket.return_value = wrapped
        with patch("agent.research_web.socket.socket", return_value=sock), patch("agent.research_web.ssl.create_default_context", return_value=context), \
             patch("agent.research_web.socket.getaddrinfo", side_effect=AssertionError("must not resolve again")):
            connection = _PinnedConnection("public-looking.example", 443, tls=True, addresses=[address()])
            connection.connect()
        sock.connect.assert_called_once_with(("93.184.216.34", 443))
        context.wrap_socket.assert_called_once_with(sock, server_hostname="public-looking.example")
        self.assertIs(connection.sock, wrapped)

    def test_every_redirect_rechecks_url_and_dns_before_any_next_connection(self):
        web = ResearchWeb("")
        for location, expected in (("http://127.0.0.1/admin", "web_url_invalid"), ("http://other.example/admin", "web_address_blocked")):
            connection = PageConnection(PageResponse(302, {"Location": location}))
            dns = [[address()]] + ([[address("127.0.0.1", 80)]] if "other" in location else [])
            with patch("agent.research_web.socket.getaddrinfo", side_effect=dns), patch("agent.research_web._PinnedConnection", return_value=connection) as factory:
                self.assert_code(expected, web.fetch, "https://example.com/start")
            self.assertEqual(factory.call_count, 1)
            self.assertTrue(connection.closed)

    def test_relative_redirect_returns_final_full_text_and_closes_every_connection(self):
        redirect = PageConnection(PageResponse(302, {"Location": "/article?x=1#section"}))
        page = PageConnection(PageResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=b"<title>Field notes</title><h1>Grass</h1><script>do not show()</script><p>Medium speed only.</p>"))
        with patch("agent.research_web.socket.getaddrinfo", return_value=[address()]) as dns, \
             patch("agent.research_web._PinnedConnection", side_effect=[redirect, page]):
            result = ResearchWeb("").fetch("https://example.com/start")
        self.assertEqual(result["url"], "https://example.com/article?x=1")
        self.assertEqual(result["title"], "Field notes")
        self.assertIn("Medium speed only.", result["text"])
        self.assertNotIn("do not show", result["text"])
        self.assertEqual(page.requests[0][0], ("GET", "/article?x=1"))
        self.assertEqual(dns.call_count, 2)
        self.assertTrue(redirect.closed and page.closed)

    def test_redirect_loop_oversize_and_nontext_never_return_partial_source(self):
        loops = [PageConnection(PageResponse(302, {"Location": "/again"})) for _ in range(5)]
        with patch("agent.research_web.socket.getaddrinfo", return_value=[address()]), patch("agent.research_web._PinnedConnection", side_effect=loops):
            self.assert_code("web_redirect_limit", ResearchWeb("").fetch, "https://example.com/")
        self.assertTrue(all(item.closed for item in loops))
        for response, code in ((PageResponse(body=b"x" * 2_000_001), "web_page_too_large"),
                               (PageResponse(headers={"Content-Type": "application/pdf"}), "web_type_unsupported"),
                               (PageResponse(headers={"Content-Encoding": "gzip"}), "web_type_unsupported")):
            connection = PageConnection(response)
            with patch("agent.research_web.socket.getaddrinfo", return_value=[address()]), patch("agent.research_web._PinnedConnection", return_value=connection):
                self.assert_code(code, ResearchWeb("").fetch, "https://example.com/")
            self.assertTrue(connection.closed)
            if code == "web_page_too_large":
                self.assertTrue(all(amount <= 64 * 1024 for amount in response.read_sizes))
                self.assertLessEqual(response.offset, 2_000_000 + 64 * 1024)
            else:
                self.assertEqual(response.read_sizes, [])

    def mcp(self, *, overrides=None, handler=None):
        calls = []
        responses = {"initialize": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "synthetic", "version": "1"}},
            "tools/list": {"tools": [{"name": "search_pro", "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 500}}, "required": ["query"], "additionalProperties": False}}]},
            "tools/call": {"content": [{"type": "text", "text": "Synthetic public search result"}], "isError": False}}
        responses.update(overrides or {})
        def provider(request):
            payload = json.loads(request.content)
            calls.append((request, payload))
            if handler:
                value = handler(request, payload)
                if value is not None:
                    return value
            if payload["method"] == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(200, headers={"Mcp-Session-Id": "synthetic-session"}, json={"jsonrpc": "2.0", "id": payload["id"], "result": copy.deepcopy(responses[payload["method"]])})
        return ResearchWeb("synthetic-search-secret", client=httpx.Client(transport=httpx.MockTransport(provider), follow_redirects=False)), calls

    def test_search_initializes_session_and_calls_only_advertised_query_schema(self):
        web, calls = self.mcp()
        result = web.search("battery runtime")
        self.assertEqual([body["method"] for _, body in calls], ["initialize", "notifications/initialized", "tools/list", "tools/call"])
        self.assertNotIn("id", calls[1][1])
        self.assertEqual(calls[1][0].headers["mcp-session-id"], "synthetic-session")
        self.assertEqual(calls[-1][1]["params"], {"name": "search_pro", "arguments": {"query": "battery runtime"}})
        self.assertEqual(result["content"][0]["text"], "Synthetic public search result")
        self.assertEqual(result["provider"], "aliyun-websearch")
        self.assertEqual(result["service"], "EnhancedSearch")
        self.assertNotIn("synthetic-search-secret", json.dumps(result))
        self.assertTrue(all(request.headers["accept-encoding"] == "identity" for request, _ in calls))
        self.assertTrue(all(str(request.url) == ALIYUN_SEARCH_MCP_URL for request, _ in calls))
        self.assertTrue(all(request.headers["authorization"] == "Bearer synthetic-search-secret" for request, _ in calls))
        self.assertTrue(all(request.headers["mcp-protocol-version"] == "2024-11-05" for request, _ in calls[1:]))

    def test_search_never_redirects_credentials_to_an_arbitrary_or_retired_service(self):
        for url in ("http://127.0.0.1/mcp", "https://example.com/mcp", ALIYUN_SEARCH_MCP_URL + "?token=other",
                    "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp"):
            self.assert_code("dependency_config_invalid", ResearchWeb, "synthetic-search-secret", mcp_url=url)

    def test_search_query_limits_are_checked_before_any_provider_request(self):
        for query in ("", " ", "x", "x" * 501, " " * 500 + "xx", None, []):
            with self.subTest(query_type=type(query).__name__):
                web, calls = self.mcp()
                self.assert_code("invalid_request", web.search, query)
                self.assertEqual(calls, [])
        for query in ("草坪", "x" * 500):
            web, calls = self.mcp()
            web.search(query)
            self.assertEqual(calls[-1][1]["params"]["arguments"], {"query": query})

    def test_enhanced_search_preserves_full_pages_json_for_source_registration(self):
        text = json.dumps({"pages": [{"title": "Synthetic product", "url": "https://example.com/product",
                                     "snippet": "Full public snippet", "hostname": "example.com"}],
                           "request_id": "synthetic-request", "tools": [], "status": 0})
        web, _ = self.mcp(overrides={"tools/call": {"content": [{"type": "text", "text": text}], "isError": False}})
        result = web.search("EGO synthetic mower")
        self.assertEqual(result["content"], [{"type": "text", "text": text}])

    def test_unrecognized_protocol_or_tool_is_rejected_without_searching(self):
        for overrides in ({"initialize": {"protocolVersion": "unknown"}},
                          {"initialize": {"protocolVersion": {"malformed": "value"}}},
                          {"tools/list": {"tools": [{"name": "web_search"}]}}):
            web, calls = self.mcp(overrides=overrides)
            self.assert_code("search_contract_changed", web.search, "query")
            self.assertNotIn("tools/call", [body["method"] for _, body in calls])

    def test_changed_query_type_required_fields_or_bounds_stop_before_search(self):
        for query_schema, required in (({"type": "integer"}, ["query"]), ({"type": "string"}, ["query", "account"]),
                                       ({"type": "string", "maxLength": 2}, ["query"]),
                                       ({"type": "string", "minLength": "2"}, ["query"]),
                                       ({"type": "string", "maxLength": False}, ["query"]),
                                       ({"type": "string"}, [])):
            web, calls = self.mcp(overrides={"tools/list": {"tools": [{"name": "search_pro", "inputSchema": {
                "type": "object", "properties": {"query": query_schema}, "required": required}}]}})
            self.assert_code("search_contract_changed", web.search, "query")
            self.assertNotIn("tools/call", [body["method"] for _, body in calls])

    def test_sse_multiline_reply_finishes_without_waiting_for_connection_eof(self):
        class OpenStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b':keepalive\n\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
                yield b'data: {"jsonrpc":"2.0",\ndata: "id":3,"result":{"content":[{"type":"text","text":"result"}]}}\n\n'
                raise AssertionError("RPC must close after its own response")
        def handler(_request, payload):
            if payload["method"] == "tools/call":
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=OpenStream())
        web, _ = self.mcp(handler=handler)
        self.assertEqual(web.search("query")["content"][0]["text"], "result")

    def test_rpc_wrong_id_version_and_nonobject_results_raise_safe_domain_errors(self):
        invalid = ([1], None, {"jsonrpc": "2.0", "id": 99, "result": {}}, {"jsonrpc": "1.0", "id": 1, "result": {}},
                   {"jsonrpc": "2.0", "id": True, "result": {}}, {"jsonrpc": "2.0", "id": 1, "result": []},
                   {"jsonrpc": "2.0", "id": 1, "error": {"message": "secret-reflection"}})
        for value in invalid:
            with self.subTest(value=value):
                web, _ = self.mcp(handler=lambda *_: httpx.Response(200, json=value))
                error = self.assert_code("search_response_invalid", web.search, "query")
                self.assertNotIn("secret-reflection", error.message)

    def test_malformed_tools_and_text_blocks_cannot_escape_as_attribute_errors(self):
        cases = (({"tools/list": {"tools": [1]}}, "search_response_invalid"),
                 ({"tools/list": {"tools": [{"name": "search_pro", "inputSchema": None}]}}, "search_contract_changed"),
                 ({"tools/list": {"tools": [{"name": "search_pro", "inputSchema": {"properties": {"query": {}}, "required": ["query", "account"]}}]}}, "search_contract_changed"),
                 ({"tools/call": {"content": [1]}}, "search_response_invalid"),
                 ({"tools/call": {"content": [{"type": "text", "text": {"unexpected": "object"}}]}}, "search_response_invalid"))
        for overrides, code in cases:
            web, calls = self.mcp(overrides=overrides)
            self.assert_code(code, web.search, "query")
            if "tools/list" in overrides:
                self.assertNotIn("tools/call", [body["method"] for _, body in calls])

    def test_mcp_permission_redirect_and_notification_failures_do_not_send_next_request(self):
        for status, expected in ((401, "search_not_enabled"), (403, "search_not_enabled"), (404, "search_not_enabled"), (302, "search_unavailable")):
            web, calls = self.mcp(handler=lambda *_: httpx.Response(status, headers={"Location": "http://127.0.0.1/secret"}, text="private-provider-error"))
            error = self.assert_code(expected, web.search, "query")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("private-provider-error", error.message)
        web, calls = self.mcp(handler=lambda _r, body: httpx.Response(401) if body["method"] == "notifications/initialized" else None)
        self.assert_code("search_not_enabled", web.search, "query")
        self.assertEqual(len(calls), 2)

    def test_search_stops_reading_oversize_body_before_unbounded_stream_is_consumed(self):
        class HugeStream(httpx.SyncByteStream):
            consumed = 0
            def __iter__(self):
                for _ in range(20):
                    self.consumed += 1
                    yield b"x" * 800_000
        stream = HugeStream()
        web, _ = self.mcp(handler=lambda *_: httpx.Response(200, stream=stream))
        self.assert_code("search_response_too_large", web.search, "query")
        self.assertEqual(stream.consumed, 3)

    def test_tool_error_remains_retryable_and_next_search_can_use_repaired_query(self):
        first, _ = self.mcp(overrides={"tools/call": {"isError": True, "content": [{"type": "text", "text": "provider details not public"}]}})
        error = self.assert_code("search_unavailable", first.search, "unsupported query")
        self.assertTrue(error.retryable)
        self.assertNotIn("provider details", error.message)
        repaired, calls = self.mcp()
        result = repaired.search("specific product battery runtime")
        self.assertTrue(result["content"])
        self.assertEqual(calls[-1][1]["params"]["arguments"]["query"], "specific product battery runtime")

    def test_slow_drip_page_checks_total_deadline_between_available_byte_reads(self):
        now = [100.0]
        class DripResponse(PageResponse):
            def read1(self, amount):
                self.read_sizes.append(amount)
                now[0] += 20
                return b"x"
        response = DripResponse()
        connection = PageConnection(response)
        with patch("agent.research_web.time.monotonic", side_effect=lambda: now[0]), \
             patch("agent.research_web.socket.getaddrinfo", return_value=[address()]), \
             patch("agent.research_web._PinnedConnection", return_value=connection):
            self.assert_code("web_timeout", ResearchWeb("").fetch, "https://example.com/")
        self.assertEqual(len(response.read_sizes), 3)
        self.assertTrue(connection.closed)

    def test_redirects_share_one_deadline_and_cancellation_is_checked_after_blocked_read(self):
        now = [0.0]
        class SlowHeaders(PageConnection):
            def getresponse(self):
                now[0] += 31
                return super().getresponse()
        responses = [SlowHeaders(PageResponse(302, {"Location": "/second"})), SlowHeaders(PageResponse())]
        with patch("agent.research_web.time.monotonic", side_effect=lambda: now[0]), \
             patch("agent.research_web.socket.getaddrinfo", return_value=[address()]), \
             patch("agent.research_web._PinnedConnection", side_effect=responses):
            self.assert_code("web_timeout", ResearchWeb("").fetch, "https://example.com/")
        self.assertTrue(all(connection.closed for connection in responses))
        self.assertEqual(responses[-1].response.read_sizes, [])
        cancelled = [False]
        class CancelResponse(PageResponse):
            def read1(self, amount):
                cancelled[0] = True
                return b"late content"
        def check():
            if cancelled[0]:
                raise AgentError("research_cancelled", "已取消", http_status=409)
        connection = PageConnection(CancelResponse())
        with patch("agent.research_web.socket.getaddrinfo", return_value=[address()]), patch("agent.research_web._PinnedConnection", return_value=connection):
            self.assert_code("research_cancelled", ResearchWeb("").fetch, "https://example.com/", check_cancel=check)
        self.assertTrue(connection.closed)

    def test_search_deadline_is_shared_across_rpc_requests_and_streams_are_cancellable(self):
        now = [0.0]
        def slow(_request, _payload):
            now[0] += 21
        web, calls = self.mcp(handler=slow)
        with patch("agent.research_web.time.monotonic", side_effect=lambda: now[0]):
            self.assert_code("search_timeout", web.search, "query")
        self.assertEqual(len(calls), 3)
        cancelled = [False]
        class CancelStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b":progress\n\n"
                cancelled[0] = True
                yield b'data: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"late"}]}}\n\n'
        def check():
            if cancelled[0]:
                raise AgentError("research_cancelled", "已取消", http_status=409)
        web, _ = self.mcp(handler=lambda _r, p: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=CancelStream()) if p["method"] == "tools/call" else None)
        self.assert_code("research_cancelled", web.search, "query", check_cancel=check)

    def test_search_slow_stream_stops_after_deadline_even_without_idle_timeout(self):
        now = [0.0]
        class DripStream(httpx.SyncByteStream):
            count = 0
            def __iter__(self):
                while True:
                    now[0] += 15
                    self.count += 1
                    yield b":progress\n\n"
        stream = DripStream()
        web, _ = self.mcp(handler=lambda _r, p: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream) if p["method"] == "tools/call" else None)
        with patch("agent.research_web.time.monotonic", side_effect=lambda: now[0]):
            self.assert_code("search_timeout", web.search, "query")
        self.assertEqual(stream.count, 4)


if __name__ == "__main__":
    unittest.main()
