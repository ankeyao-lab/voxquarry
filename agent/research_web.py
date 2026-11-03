"""Aliyun MCP search and a bounded, public-only page reader for the broker."""
from __future__ import annotations

import http.client
import codecs
import ipaddress
import json
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .contracts import AgentError

ALIYUN_SEARCH_MCP_URL = "https://dashscope.aliyuncs.com/api/v1/mcps/EnhancedSearch/mcp"


def public_url(value: str) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or
                parts.username or parts.password or parts.port not in {None, 80, 443} or
                len(value) > 4000 or any(ord(c) < 33 for c in value)):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError
        except ValueError:
            # Distinguish a hostname from an invalid/non-public IP literal.
            if ":" in host or host.replace(".", "").isdigit():
                raise
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    except (TypeError, UnicodeError, ValueError) as exc:
        raise AgentError("web_url_invalid", "只能读取公开的网页链接") from exc


def public_addresses(host: str, port: int) -> list[tuple]:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError
        return addresses
    except (OSError, ValueError) as exc:
        raise AgentError("web_address_blocked", "该链接不是可访问的公开网页", http_status=422) from exc


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, *, tls, addresses):
        super().__init__(host, port, timeout=15)
        self.tls, self.addresses = tls, addresses

    def connect(self):
        # Use exactly the addresses validated above. A second DNS lookup could
        # turn a public hostname into an intranet request (DNS rebinding).
        last = None
        for family, kind, proto, _canon, address in self.addresses:
            sock = socket.socket(family, kind, proto)
            sock.settimeout(self.timeout)
            try:
                sock.connect(address)
                self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host) if self.tls else sock
                return
            except OSError as exc:
                last = exc
                sock.close()
        raise last or OSError("unreachable")


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []
        self.title = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "li", "h1", "h2", "h3", "br", "tr", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
        if self.in_title:
            self.title.append(data)


class ResearchWeb:
    def __init__(self, api_key: str, *, mcp_url=ALIYUN_SEARCH_MCP_URL, client=None):
        # The URL is server-owned configuration, never a model tool argument.
        if mcp_url != ALIYUN_SEARCH_MCP_URL:
            raise AgentError("dependency_config_invalid", "搜索服务地址不在已启用服务列表", http_status=503)
        self._key, self._url, self._client = api_key, mcp_url, client

    @staticmethod
    def _guard(deadline, check_cancel, *, search=False):
        if check_cancel is not None:
            check_cancel()
        if deadline is not None and time.monotonic() >= deadline:
            raise AgentError("search_timeout" if search else "web_timeout",
                "联网搜索超过本次等待时间，可以改用其他资料" if search else "网页读取超过本次等待时间，可以改用其他来源",
                http_status=504, retryable=True)

    @staticmethod
    def _rpc(client, url, headers, method, params, identifier, *, deadline=None, check_cancel=None):
        ResearchWeb._guard(deadline, check_cancel, search=True)
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        if identifier is not None:
            request["id"] = identifier
        # post().content buffers an unbounded body before a subsequent size
        # check can reject it. Stop reading as soon as this operation's cap is
        # exceeded, including for notification acknowledgements.
        with client.stream("POST", url, headers={**headers, "Accept-Encoding": "identity"}, json=request) as response:
            ResearchWeb._guard(deadline, check_cancel, search=True)
            if response.status_code in {401, 403, 404}:
                raise AgentError("search_not_enabled", "阿里云搜索尚未开通或当前账号无权使用", http_status=503)
            if response.status_code >= 300:
                raise AgentError("search_unavailable", "联网搜索暂时不可用", http_status=503, retryable=response.status_code in {408, 429, 500, 502, 503, 504})
            if response.headers.get("content-encoding", "identity").strip().lower() not in {"identity", ""}:
                raise AgentError("search_response_invalid", "搜索服务没有返回可用结果", http_status=502)
            try:
                length = response.headers.get("content-length")
                if length is not None:
                    if int(length) < 0:
                        raise ValueError
                    if int(length) > 2_000_000:
                        raise AgentError("search_response_too_large", "搜索结果超过处理容量", http_status=502)
            except ValueError:
                raise AgentError("search_response_invalid", "搜索服务没有返回可用结果", http_status=502) from None
            parts, size = [], 0
            streaming = identifier is not None and "text/event-stream" in response.headers.get("content-type", "").lower()
            decoder, pending, data_lines, value = codecs.getincrementaldecoder("utf-8")(), "", [], None
            for part in response.iter_bytes():
                ResearchWeb._guard(deadline, check_cancel, search=True)
                size += len(part)
                if size > 2_000_000:
                    raise AgentError("search_response_too_large", "搜索结果超过处理容量", http_status=502)
                parts.append(part)
                if streaming:
                    try:
                        pending += decoder.decode(part)
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            line = line.removesuffix("\r")
                            if not line and data_lines:
                                event = "\n".join(data_lines)
                                data_lines = []
                                if event == "[DONE]":
                                    continue
                                item = json.loads(event)
                                if not isinstance(item, dict):
                                    raise ValueError
                                if type(item.get("id")) is type(identifier) and item.get("id") == identifier:
                                    value = item
                                    break
                            elif line.startswith("data:"):
                                data_lines.append(line[5:].removeprefix(" "))
                        if value is not None:
                            # A Streamable HTTP response can keep its SSE
                            # connection open. This RPC is done once its own
                            # result arrives; do not wait for unrelated events.
                            break
                    except (ValueError, UnicodeError, RecursionError):
                        raise AgentError("search_response_invalid", "搜索服务没有返回可用结果", http_status=502) from None
            raw = b"".join(parts)
            response_headers = response.headers
            ResearchWeb._guard(deadline, check_cancel, search=True)
        if identifier is None:
            return None
        try:
            body = "" if streaming and value is not None else raw.decode("utf-8")
            if streaming and value is not None:
                pass
            elif "text/event-stream" in response_headers.get("content-type", "").lower():
                # SSE permits one JSON value to span several data lines and
                # permits notifications/keepalives ahead of the actual reply.
                items, data_lines = [], []
                for line in body.splitlines() + [""]:
                    if line == "":
                        if data_lines:
                            text = "\n".join(data_lines)
                            if text != "[DONE]":
                                item = json.loads(text)
                                if not isinstance(item, dict):
                                    raise ValueError
                                items.append(item)
                            data_lines = []
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].removeprefix(" "))
                replies = [item for item in items if type(item.get("id")) is type(identifier) and item.get("id") == identifier]
                if len(replies) != 1:
                    raise ValueError
                value = replies[0]
            else:
                value = json.loads(body)
            if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
                    or type(value.get("id")) is not type(identifier) or value.get("id") != identifier
                    or "error" in value or not isinstance(value.get("result"), dict)):
                raise ValueError
            session = response_headers.get("mcp-session-id")
            if session:
                if len(session) > 512 or not session.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in session):
                    raise ValueError
                headers["Mcp-Session-Id"] = session
            return value["result"]
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
            raise AgentError("search_response_invalid", "搜索服务没有返回可用结果", http_status=502) from exc

    def search(self, query: str, *, check_cancel=None) -> dict:
        """One 60-second budget across the complete MCP exchange.

        Deadline/cancellation checks run between network operations and stream
        chunks. An in-progress blocking read can additionally consume its socket
        idle timeout; this method creates no helper threads to interrupt it.
        """
        if not self._key:
            raise AgentError("search_not_enabled", "阿里云搜索尚未配置", http_status=503)
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 500 or len(query) > 500:
            raise AgentError("invalid_request", "搜索内容应为 2–500 个字符")
        headers = {"Authorization": "Bearer " + self._key, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26"}
        deadline = time.monotonic() + 60
        progress = {"deadline": deadline, "check_cancel": check_cancel}
        try:
            with self._client or httpx.Client(timeout=35, follow_redirects=False, trust_env=False) as client:
                initialized = self._rpc(client, self._url, headers, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "wenquan-search", "version": "1.0"}}, 1, **progress)
                protocol = initialized.get("protocolVersion")
                if not isinstance(protocol, str) or protocol not in {"2024-11-05", "2025-03-26"}:
                    raise AgentError("search_contract_changed", "阿里云搜索协议已变化，需要更新配置", http_status=503)
                headers["MCP-Protocol-Version"] = protocol
                self._rpc(client, self._url, headers, "notifications/initialized", {}, None, **progress)
                listed = self._rpc(client, self._url, headers, "tools/list", {}, 2, **progress)
                tools = listed.get("tools")
                if not isinstance(tools, list) or any(not isinstance(item, dict) for item in tools):
                    raise AgentError("search_response_invalid", "搜索服务没有返回可用结果", http_status=502)
                tool = next((item for item in tools if item.get("name") == "search_pro"), None)
                if tool is None:
                    raise AgentError("search_contract_changed", "阿里云搜索接口已变化，需要更新配置", http_status=503)
                schema = tool.get("inputSchema", {})
                if (not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict)
                        or "query" not in schema["properties"] or not isinstance(schema.get("required", []), list)
                        or not isinstance(schema["properties"]["query"], dict)
                        or schema["properties"]["query"].get("type") != "string"
                        or schema.get("required") != ["query"]):
                    raise AgentError("search_contract_changed", "阿里云搜索参数已变化，需要更新配置", http_status=503)
                query_schema = schema["properties"]["query"]
                minimum, maximum = query_schema.get("minLength", 2), query_schema.get("maxLength", 500)
                if (type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum
                        or not minimum <= len(query) <= maximum):
                    raise AgentError("search_contract_changed", "阿里云搜索参数已变化，需要更新配置", http_status=503)
                result = self._rpc(client, self._url, headers, "tools/call", {"name": "search_pro", "arguments": {"query": query}}, 3, **progress)
                if result.get("isError"):
                    raise AgentError("search_unavailable", "阿里云搜索未能完成，请稍后重试", http_status=503, retryable=True)
                content = result.get("content")
                if (not isinstance(content, list) or any(not isinstance(item, dict) for item in content)
                        or any(not isinstance(item.get("text"), str) for item in content if item.get("type") == "text")):
                    raise AgentError("search_response_invalid", "搜索服务没有返回可用结果", http_status=502)
                text = [item for item in content if item.get("type") == "text"]
                if not text:
                    raise AgentError("search_response_invalid", "搜索服务没有返回可用结果", http_status=502)
                return {"provider": "aliyun-websearch", "service": "EnhancedSearch", "query": query, "content": text}
        except httpx.HTTPError as exc:
            raise AgentError("search_unavailable", "联网搜索暂时不可用", http_status=503, retryable=True) from exc

    def fetch(self, url: str, *, check_cancel=None) -> dict:
        """Read a complete page within one 60-second budget across redirects.

        read1 returns available bytes instead of waiting to fill a large read.
        Checks run between reads, so one socket idle timeout may extend the
        observed cutoff. No partial page is returned after timeout/cancellation.
        """
        current = public_url(url)
        deadline = time.monotonic() + 60
        for _ in range(5):
            self._guard(deadline, check_cancel)
            parts = urlsplit(current)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            addresses = public_addresses(parts.hostname, port)
            self._guard(deadline, check_cancel)
            connection = _PinnedConnection(parts.hostname, port, tls=parts.scheme == "https", addresses=addresses)
            try:
                connection.request("GET", urlunsplit(("", "", parts.path, parts.query, "")), headers={"User-Agent": "VoxQuarry-Wenquan/1.0", "Accept": "text/html,text/plain,application/json", "Accept-Encoding": "identity"})
                response = connection.getresponse()
                self._guard(deadline, check_cancel)
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        break
                    current = public_url(urljoin(current, location))
                    continue
                if response.status != 200:
                    raise AgentError("web_page_unavailable", "该网页暂时无法读取", http_status=502)
                kind = response.getheader("Content-Type", "").split(";", 1)[0].lower()
                if kind not in {"text/html", "text/plain", "application/json", "application/xhtml+xml"} or response.getheader("Content-Encoding", "identity") not in {"identity", ""}:
                    raise AgentError("web_type_unsupported", "此链接不是可读取的文字网页")
                chunks, size = [], 0
                while True:
                    self._guard(deadline, check_cancel)
                    part = response.read1(64 * 1024)
                    self._guard(deadline, check_cancel)
                    if not part:
                        break
                    size += len(part)
                    if size > 2_000_000:
                        raise AgentError("web_page_too_large", "网页超过处理容量，未将截断内容作为完整原文")
                    chunks.append(part)
                raw = b"".join(chunks)
                body = raw.decode("utf-8", errors="replace")
                title = current
                if "html" in kind:
                    parser = _PageText()
                    parser.feed(body)
                    body = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
                    title = " ".join(parser.title).strip() or current
                return {"url": current, "title": title, "text": body, "media_type": kind}
            except (OSError, http.client.HTTPException) as exc:
                raise AgentError("web_page_unavailable", "网页读取失败，可以尝试其他来源", http_status=502, retryable=True) from exc
            finally:
                connection.close()
        raise AgentError("web_redirect_limit", "网页跳转次数过多，已停止读取")
