"""Trusted client for the dedicated research VM, never a model-facing Docker API."""
from __future__ import annotations

import hashlib
import json
import re
import time
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from urllib.parse import quote, urlsplit

import httpx

from .contracts import AgentError

STOP_TIMEOUT_SECONDS = 45
TRANSPORT_KINDS = frozenset({"connect_timeout", "read_timeout", "write_timeout", "pool_timeout", "connect_error",
                             "read_error", "write_error", "remote_protocol_error", "transport_error"})


def transport_kind(error):
    for cls, kind in ((httpx.ConnectTimeout, "connect_timeout"), (httpx.ReadTimeout, "read_timeout"),
                      (httpx.WriteTimeout, "write_timeout"), (httpx.PoolTimeout, "pool_timeout"),
                      (httpx.ConnectError, "connect_error"), (httpx.ReadError, "read_error"),
                      (httpx.WriteError, "write_error"), (httpx.RemoteProtocolError, "remote_protocol_error")):
        if isinstance(error, cls):
            return kind
    return "transport_error"


class HarnessManager:
    def __init__(self, base_url, token, *, transport=None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise AgentError("harness_config_invalid", "研究执行服务地址无效", http_status=503)
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or any(c.isspace() for c in token):
            raise AgentError("harness_config_invalid", "研究执行服务凭证无效", http_status=503)
        self.base_url, self._token, self._transport = base_url.rstrip("/"), token, transport

    @staticmethod
    def _id(attempt_id):
        if not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", attempt_id):
            raise AgentError("harness_request_invalid", "研究执行编号无效")
        return attempt_id

    def _request(self, method, path, *, value=None, content=None, timeout=30, binary=False, deadline=None, connect_retries=0):
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/octet-stream" if binary else "application/json"}
        if content is not None:
            headers.update({"Content-Type": "application/octet-stream", "X-Content-SHA256": hashlib.sha256(content).hexdigest()})
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise AgentError("cleanup_pending", "研究执行环境尚未确认清理完成", http_status=503, retryable=True)
            transport = self._transport if self._transport is not None else httpx.HTTPTransport(retries=connect_retries, trust_env=False)
            with httpx.Client(transport=transport, timeout=httpx.Timeout(timeout, connect=min(10, timeout)), follow_redirects=False, trust_env=False) as client:
                with client.stream(method, self.base_url + path, json=value, content=content, headers=headers) as response:
                    parts, size = [], 0
                    for part in response.iter_bytes():
                        if deadline is not None and time.monotonic() >= deadline:
                            raise AgentError("cleanup_pending", "研究执行环境尚未确认清理完成", http_status=503, retryable=True)
                        size += len(part)
                        if size > (16 * 1024 * 1024 if binary or path.endswith("/native") else 4 * 1024 * 1024):
                            raise AgentError("harness_output_limit", "研究输出超过单次读取限制", http_status=502)
                        parts.append(part)
                    raw = b"".join(parts)
                    if response.status_code >= 300:
                        # Do not copy manager, container, or reverse-proxy bodies
                        # into user-visible errors: they may contain source text.
                        code = "harness_unavailable"
                        try:
                            body = json.loads(raw)
                            proposed = body.get("error", {}).get("code")
                            if isinstance(proposed, str) and re.fullmatch(r"[a-z_]{1,64}", proposed):
                                code = proposed
                        except (ValueError, AttributeError, TypeError):
                            pass
                        raise AgentError(code, "研究执行未完成，请检查执行状态后重试", http_status=503, retryable=response.status_code in {408, 429, 502, 503, 504})
                    if binary:
                        return raw
                    try:
                        value = json.loads(raw)
                        if not isinstance(value, dict):
                            raise ValueError()
                        return value
                    except (ValueError, TypeError):
                        raise AgentError("harness_response_invalid", "研究执行服务返回格式无效", http_status=502) from None
        except httpx.HTTPError as exc:
            error = AgentError("harness_unavailable", "研究执行服务暂时无法连接", http_status=503, retryable=True)
            error.transport_kind = transport_kind(exc)
            raise error from exc

    def start(self, payload):
        if not isinstance(payload, dict):
            raise AgentError("harness_request_invalid", "研究执行请求无效")
        self._id(payload.get("attempt_id"))
        return self._request("POST", "/v1/attempts", value=payload)

    def status(self, attempt_id, after=0):
        if type(after) is not int or after < 0:
            raise AgentError("harness_request_invalid", "研究事件位置无效")
        path = f"/v1/attempts/{self._id(attempt_id)}?after={after}"
        failures = []
        for attempt in range(3):
            try:
                result = self._request("GET", path, timeout=10)
                if failures:
                    result["transport_recovery"] = {"retries": len(failures), "failures": failures}
                return result
            except AgentError as error:
                # Only this read-only query may be repeated. The same cursor
                # keeps events recoverable; model and tool POSTs are never replayed.
                if not isinstance(error.__cause__, httpx.TransportError) or attempt == 2:
                    raise
                failures.append(transport_kind(error.__cause__))
                time.sleep(0.25)

    def stop(self, attempt_id):
        path = f"/v1/attempts/{self._id(attempt_id)}"
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        pending = AgentError("cleanup_pending", "研究执行环境尚未确认清理完成", http_status=503, retryable=True)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise pending
            request = Future()
            def dispatch(result=request, timeout=remaining):
                if not result.set_running_or_notify_cancel():
                    return
                try:
                    value = self._request("DELETE", path, timeout=timeout, deadline=deadline)
                except BaseException as error:
                    result.set_exception(error)
                else:
                    result.set_result(value)
            # At most one DELETE is in flight. If its I/O outlasts our deadline,
            # this daemon can finish that DELETE only: it has no retry, model or
            # code-tool path and does not hold up process exit.
            threading.Thread(target=dispatch, name="wenquan-cleanup", daemon=True).start()
            try:
                # HTTPX timeouts apply per I/O phase, not to the whole operation.
                result = request.result(timeout=max(0, deadline - time.monotonic()))
            except FutureTimeout:
                request.cancel()
                raise pending from None
            except AgentError as error:
                # Concurrent cancel/watchdog cleanup can temporarily retain
                # this attempt. Retry only this explicit manager response.
                if error.code != "cleanup_pending" or not error.retryable:
                    raise
                pending = error
            else:
                if time.monotonic() >= deadline:
                    raise pending
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise pending
            time.sleep(min(0.25, remaining))

    def tool(self, attempt_id, name, arguments):
        if name not in {"run_python", "run_shell", "read_file", "write_file", "parse_attachment", "render_report"}:
            raise AgentError("harness_tool_rejected", "此工具不属于隔离分析能力", http_status=403)
        return self._request("POST", f"/v1/attempts/{self._id(attempt_id)}/tools/{name}", value={"arguments": arguments}, timeout=330)

    def native_io(self, attempt_id, payload):
        # HTTPX retries only ConnectError/ConnectTimeout, before request bytes
        # are sent. Never replay a native write/spawn after an uncertain result.
        return self._request("POST", f"/v1/attempts/{self._id(attempt_id)}/native", value=payload, timeout=40, connect_retries=2)

    def put_input(self, attempt_id, filename, content):
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", filename):
            raise AgentError("harness_input_invalid", "研究输入文件名无效")
        if not isinstance(content, bytes) or len(content) > 64 * 1024 * 1024:
            raise AgentError("harness_input_limit", "研究输入分片不能超过 64 MiB")
        return self._request("POST", f"/v1/attempts/{self._id(attempt_id)}/inputs/{filename}", content=content, timeout=180)

    def read_output(self, attempt_id, path):
        if not isinstance(path, str) or len(path) > 500 or not path.startswith("/workspace/") or any(part in {"", ".", ".."} for part in path.split("/")[2:]):
            raise AgentError("harness_path_invalid", "只能读取本次研究工作目录中的输出")
        return self._request("GET", f"/v1/attempts/{self._id(attempt_id)}/files?path={quote(path, safe='')}", binary=True, timeout=60)
