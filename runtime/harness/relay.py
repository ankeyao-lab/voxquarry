"""Attempt-scoped HTTP relay; configuration and capability are read-only mounts.

Run ``python /opt/voxquarry/relay.py``. Only relay.json controls the upstream;
HTTP requests cannot select a host, proxy, tool catalog, or capability file.
"""
from __future__ import annotations

import hmac
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


CONFIG_PATH = Path("/run/voxquarry/relay.json")
CAPABILITY_PATH = Path("/run/voxquarry/capability")
MODEL_PATH = "/broker/model/v1/chat/completions"
TRAJECTORY_PATH = "/broker/trajectory"
MODEL_BODY_LIMIT = 16 * 1024 * 1024
TOOL_BODY_LIMIT = 1024 * 1024
RESPONSE_LIMIT = 16 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
TOKEN = re.compile(r"[A-Za-z0-9._~-]{16,512}\Z")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON value")


def _decode_json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_reject_constant)


def _read_file(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("invalid configuration file")
        value = handle.read(limit + 1)
        if len(value) > limit:
            raise ValueError("configuration file exceeds limit")
        return value


@dataclass(frozen=True)
class RelayConfig:
    broker_base_url: str
    tool_names: frozenset[str]
    capability: str = field(repr=False)
    max_concurrency: int = 4
    request_timeout_seconds: float = 300
    idle_timeout_seconds: float = 60
    tool_idle_timeout_seconds: float | None = None
    model_idle_timeout_seconds: float | None = None
    model_request_timeout_seconds: float = 300
    attempt_deadline_monotonic: float | None = None

    def __post_init__(self):
        url = urlsplit(self.broker_base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(char.isspace() for char in self.broker_base_url)
                or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/broker", url.path)):
            raise ValueError("invalid fixed broker URL")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("invalid broker port")
        if (not isinstance(self.tool_names, frozenset) or len(self.tool_names) > 128
                or any(not isinstance(name, str) or not TOOL_NAME.fullmatch(name) for name in self.tool_names)):
            raise ValueError("invalid tool allowlist")
        if not isinstance(self.capability, str) or not TOKEN.fullmatch(self.capability):
            raise ValueError("invalid capability")
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 16:
            raise ValueError("invalid concurrency limit")
        for value in (self.request_timeout_seconds, self.idle_timeout_seconds,
                      *(value for value in (self.tool_idle_timeout_seconds, self.model_idle_timeout_seconds) if value is not None)):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1860:
                raise ValueError("invalid timeout")
        if self.idle_timeout_seconds > self.request_timeout_seconds:
            raise ValueError("idle timeout exceeds request timeout")
        if (type(self.model_request_timeout_seconds) not in (int, float)
                or not math.isfinite(self.model_request_timeout_seconds) or not 0 < self.model_request_timeout_seconds <= 1860):
            raise ValueError("invalid model request timeout")
        if self.attempt_deadline_monotonic is not None and (type(self.attempt_deadline_monotonic) not in (int, float)
                or not math.isfinite(self.attempt_deadline_monotonic) or self.attempt_deadline_monotonic <= 0):
            raise ValueError("invalid attempt deadline")

    @classmethod
    def from_files(cls, config_path=CONFIG_PATH, capability_path=CAPABILITY_PATH):
        config = _decode_json(_read_file(config_path, 64 * 1024))
        allowed = {"broker_base_url", "tool_names", "max_concurrency", "request_timeout_seconds", "idle_timeout_seconds",
                   "tool_idle_timeout_seconds", "model_idle_timeout_seconds", "model_request_timeout_seconds", "attempt_deadline_monotonic"}
        if (not isinstance(config, dict) or set(config) - allowed or not {"broker_base_url", "tool_names"} <= set(config)
                or not isinstance(config["broker_base_url"], str) or not isinstance(config["tool_names"], list)
                or any(not isinstance(item, str) for item in config["tool_names"])):
            raise ValueError("invalid relay configuration")
        # A conventional final newline is not part of the capability. Other
        # whitespace, duplicate tools and request-provided overrides are rejected.
        token = _read_file(capability_path, 1024).decode("ascii").removesuffix("\n")
        if len(set(config["tool_names"])) != len(config["tool_names"]):
            raise ValueError("duplicate approved tool")
        return cls(**{**config, "tool_names": frozenset(config["tool_names"])}, capability=token)


@dataclass(frozen=True)
class Upstream:
    scheme: str
    hostname: str
    port: int
    path: str
    family: int
    address: tuple

    @classmethod
    def resolve(cls, base_url):
        url = urlsplit(base_url)
        port = url.port or (443 if url.scheme == "https" else 80)
        # Resolve only during trusted startup, never from request data. Pin the
        # address so per-request DNS cannot evade socket deadlines or rebind.
        addresses = socket.getaddrinfo(url.hostname, port, type=socket.SOCK_STREAM)
        family, _, _, _, address = addresses[0]
        return cls(url.scheme, url.hostname, port, url.path, family, address)


class UpstreamConnection(http.client.HTTPConnection):
    def __init__(self, upstream, timeout, expired):
        super().__init__(upstream.hostname, upstream.port, timeout=timeout)
        self.upstream = upstream
        self.expired = expired
        self.transport_socket = None

    def connect(self):
        self.sock = socket.socket(self.upstream.family, socket.SOCK_STREAM)
        self.transport_socket = self.sock
        if self.expired.is_set():
            self.sock.close()
            raise TimeoutError
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.upstream.address)
        if self.upstream.scheme == "https":
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.upstream.hostname, do_handshake_on_connect=False)
            self.transport_socket = self.sock
            if self.expired.is_set():
                self.sock.close()
                raise TimeoutError
            self.sock.do_handshake()


def _error_body(code, status):
    # The SDK retains the message but may normalize the machine code to
    # UNKNOWN. Keep only our bounded category and HTTP status for diagnosis;
    # upstream response bodies, request data and credentials stay private.
    return json.dumps({"error": {"code": code,
        "message": f"Relay request could not be completed ({code}; HTTP {status})."}}, separators=(",", ":")).encode()


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address, config):
        self.config = config
        self.upstream = Upstream.resolve(config.broker_base_url)
        self.slots = threading.BoundedSemaphore(config.max_concurrency)
        self.request_queue_size = config.max_concurrency
        super().__init__(address, RelayHandler)

    def process_request(self, request, client_address):
        # Admission happens before allocating a handler thread, including for
        # clients that never finish their headers or authenticate.
        # Native tools and trajectory writes can briefly overlap in one turn.
        # Wait for a retiring connection without increasing active handlers.
        if not self.slots.acquire(timeout=1):
            try:
                body = _error_body("relay_busy", 503)
                request.settimeout(min(1, self.config.idle_timeout_seconds))
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                                + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # BaseServer's traceback can include request/transport data.
        pass


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VoxQuarryRelay"
    sys_version = ""

    def setup(self):
        self.request.settimeout(self.server.config.idle_timeout_seconds)
        super().setup()
        self._started = False
        self._upstream = None
        self._upstream_response = None
        self._expired = threading.Event()
        self._request_started = time.monotonic()
        self._deadline_at = self._request_started + self.server.config.request_timeout_seconds
        if self.server.config.attempt_deadline_monotonic is not None:
            self._deadline_at = min(self._deadline_at, self.server.config.attempt_deadline_monotonic)
        self._set_deadline()

    def _set_deadline(self):
        self._deadline = threading.Timer(max(0, self._deadline_at - time.monotonic()), self._expire)
        self._deadline.daemon = True
        self._deadline.start()

    def finish(self):
        self._deadline.cancel()
        self._close_upstream()
        try:
            super().finish()
        except OSError:
            pass

    def _expire(self):
        self._expired.set()
        self._abort()

    def _abort(self):
        sockets = [self.connection, self._upstream.transport_socket if self._upstream else None]
        for connection in sockets:
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                if connection is not self.connection:
                    connection.close()

    def _close_upstream(self):
        if self._upstream_response is not None:
            self._upstream_response.close()
        if self._upstream is not None:
            self._upstream.close()

    def log_message(self, _format, *_args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._error(405 if code == 501 else code, "request_rejected")

    def handle_expect_100(self):
        self._error(417, "expectation_rejected")
        return False

    def _error(self, status, code):
        if self._started:
            self._abort()
            return
        self.close_connection = True
        self._started = True
        body = _error_body(code, status)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def do_POST(self):
        self.close_connection = True
        try:
            self._relay()
        except (TimeoutError, socket.timeout):
            self._error(504, "upstream_timeout")
        except Exception:
            self._error(502, "relay_failed")

    def _relay(self):
        target = self.requestline.split()[1]
        match = re.fullmatch(r"/broker/tools/([a-z][a-z0-9_]{0,63})", target)
        if target != self.path or target not in {MODEL_PATH, TRAJECTORY_PATH, "/broker/native"} and (not match or match[1] not in self.server.config.tool_names):
            self._error(404, "route_rejected")
            return
        authorizations = self.headers.get_all("Authorization", [])
        expected = ("Bearer " + self.server.config.capability).encode("ascii")
        supplied = (authorizations[0] if len(authorizations) == 1 else "").encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            self._error(401, "capability_rejected")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or self.headers.get("Content-Encoding") is not None:
            self._error(400, "request_encoding_rejected")
            return
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            self._error(411, "content_length_required")
            return
        maximum = MODEL_BODY_LIMIT if target in {MODEL_PATH, TRAJECTORY_PATH, "/broker/native"} else TOOL_BODY_LIMIT
        size = int(lengths[0])
        if size > maximum:
            self._error(413, "request_too_large")
            return
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._error(415, "json_required")
            return
        if self.headers.get("Expect") is not None:
            self._error(417, "expectation_rejected")
            return
        body = self.rfile.read(size)
        if len(body) != size:
            self._error(400, "incomplete_request")
            return
        try:
            value = _decode_json(body)
            if not isinstance(value, dict):
                raise ValueError("object required")
        except (ValueError, UnicodeError, RecursionError):
            self._error(400, "invalid_json")
            return
        if self._expired.is_set():
            return
        config = self.server.config
        # Upstream models may be silent while thinking, and code tools return
        # only after execution. Longer waits apply only after authentication
        # and complete JSON input; incoming clients keep their 60-second limit.
        idle = config.tool_idle_timeout_seconds if match else config.model_idle_timeout_seconds
        if idle is None:
            idle = config.idle_timeout_seconds
        if target == MODEL_PATH:
            self._deadline.cancel()
            self._deadline_at = min(self._deadline_at, self._request_started + config.model_request_timeout_seconds)
            self._set_deadline()
        remaining = self._deadline_at - time.monotonic()
        if self._expired.is_set() or remaining <= 0:
            self._expire()
            return
        connection = self._upstream = UpstreamConnection(self.server.upstream, min(idle, remaining), self._expired)
        connection.request("POST", self.server.upstream.path + target[len("/broker"):], body=body,
                           headers={"Authorization": "Bearer " + config.capability, "Content-Type": "application/json",
                                    "X-VoxQuarry-Relay-Prepare-Ms": str(round((time.monotonic() - self._request_started) * 1000, 3)),
                                    "Accept": "application/json, text/event-stream", "Connection": "close"})
        response = self._upstream_response = connection.getresponse()
        if 300 <= response.status < 400:
            self._error(502, "upstream_redirect_rejected")
            return
        if not 200 <= response.status < 300:
            self._error(response.status if 400 <= response.status <= 599 else 502, "upstream_rejected")
            return
        types = response.headers.get_all("Content-Type", [])
        media_type = types[0].split(";", 1)[0].strip().lower() if len(types) == 1 else ""
        if media_type not in {"application/json", "text/event-stream"} or response.headers.get("Content-Encoding") is not None:
            self._error(502, "upstream_content_rejected")
            return
        lengths = response.headers.get_all("Content-Length", [])
        encodings = response.headers.get_all("Transfer-Encoding", [])
        if (len(lengths) > 1 or lengths and not re.fullmatch(r"[0-9]{1,10}", lengths[0])
                or encodings and (len(encodings) != 1 or encodings[0].lower() != "chunked" or lengths)):
            self._error(502, "upstream_framing_rejected")
            return
        declared = int(lengths[0]) if lengths else None
        if declared is not None and declared > RESPONSE_LIMIT:
            self._error(502, "response_too_large")
            return
        self._started = True
        self.send_response(response.status)
        self.send_header("Content-Type", media_type + ("; charset=utf-8" if media_type == "text/event-stream" else ""))
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        count = 0
        while not self._expired.is_set():
            # read1 returns available bytes instead of waiting to fill a 64 KiB
            # buffer, so the first SSE event reaches the client immediately.
            chunk = response.read1(min(CHUNK_SIZE, RESPONSE_LIMIT - count + 1))
            if not chunk:
                if declared is not None and declared != count:
                    self._abort()
                    return
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            count += len(chunk)
            if count > RESPONSE_LIMIT:
                # Headers may already be sent: leave chunked framing incomplete
                # so clients cannot mistake a truncated stream for success.
                self._abort()
                return
            self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()


def main():
    try:
        config = RelayConfig.from_files()
        with RelayServer(("0.0.0.0", 9000), config) as server:
            server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("relay_startup_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
