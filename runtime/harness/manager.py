"""Trusted fixed-template manager. Run only against the dedicated research VM."""
from __future__ import annotations

import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tarfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

MIB = 1024 * 1024
GIB = 1024 * MIB
LABEL = "voxquarry.wenquan.managed=research-v1"
ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")


class ManagerError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, entry in items:
            if key in value:
                raise ValueError()
            value[key] = entry
        return value
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))


class DockerEngine:
    """The socket and image policy come from trusted startup configuration only."""
    def __init__(self, socket_path="/var/run/docker.sock"):
        if socket_path != "/var/run/docker.sock":
            raise ManagerError("engine_socket_rejected", 503)
        self.prefix = ["docker", "--host", "unix://" + socket_path]
        self.env = {"PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/state", "LANG": "C.UTF-8"}

    def spawn(self, args):
        return subprocess.Popen(self.prefix + args, env=self.env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def call(self, args, *, content=None, timeout=30, limit=4 * MIB, check=True):
        process = self.spawn(args)
        def feed():
            try:
                if content:
                    process.stdin.write(content)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        writer = threading.Thread(target=feed, daemon=True); writer.start()
        chunks, size = [], 0
        selector = selectors.DefaultSelector(); selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ManagerError("engine_timeout", 504)
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    size += len(chunk)
                    if size > limit:
                        raise ManagerError("engine_output_limit", 502)
                    chunks.append(chunk)
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if check and process.returncode:
                raise ManagerError("engine_operation_failed", 503)
            return b"".join(chunks)
        finally:
            selector.close()
            if process.poll() is None:
                process.kill(); process.wait()
            process.stdout.close(); writer.join(timeout=1)

    def guard(self, config):
        info = strict_json(self.call(["info", "--format", "{{json .}}"], limit=MIB))
        expected_name = config.get("expected_engine_name", "colima-voxquarry")
        if expected_name not in {"colima-voxquarry", "voxquarry"} or info.get("ID") != config["expected_engine_id"] or info.get("Name") != expected_name:
            raise ManagerError("engine_identity_mismatch", 503)
        return info


class Manager:
    def __init__(self, config, *, engine=None, clock=time.monotonic, start_watchdog=True):
        required = {"expected_engine_id", "runner_image", "analysis_image", "relay_image", "manager_image", "broker_base_url"}
        if not isinstance(config, dict) or not required <= set(config) or set(config) - required - {"expected_engine_name", "resource_profile", "research_concurrency"} or not isinstance(config["expected_engine_id"], str) or not config["expected_engine_id"]:
            raise ManagerError("manager_config_invalid", 503)
        profile = config.get("resource_profile", "local-default")
        if profile not in {"local-default", "production-single"}:
            raise ManagerError("manager_resource_profile_invalid", 503)
        if config.get("expected_engine_name") == "voxquarry" and profile != "production-single":
            raise ManagerError("production_resource_profile_required", 503)
        self.analysis_memory = "3g" if profile == "production-single" else "4g"
        self.runner_memory = "1024m" if profile == "production-single" else "1536m"
        if any(not isinstance(config[key], str) or not IMAGE.fullmatch(config[key]) for key in required if key.endswith("_image")):
            raise ManagerError("immutable_image_required", 503)
        broker = urlsplit(config["broker_base_url"])
        if broker.scheme not in {"http", "https"} or not broker.hostname or broker.username or broker.password or broker.query or broker.fragment or broker.path.rstrip("/") != "/wenquan/internal/broker":
            raise ManagerError("broker_config_invalid", 503)
        self.config, self.engine, self.clock = dict(config), engine or DockerEngine(), clock
        self.research_concurrency = config.get('research_concurrency', 1)
        if type(self.research_concurrency) is not int or not 1 <= self.research_concurrency <= 2:
            raise ManagerError('research_concurrency_invalid', 503)
        engine_info = self.engine.guard(config)
        # Per-attempt ceilings plus parser/manager/VM headroom. The current
        # 6-GiB production VM cannot safely opt in to two full-size attempts.
        required_memory = (12 if profile == 'production-single' else 16) * GIB
        if self.research_concurrency == 2 and (not engine_info or engine_info.get('MemTotal', 0) < required_memory
                                              or engine_info.get('NCPU', 0) < 4):
            raise ManagerError('research_capacity_resources_insufficient', 503)
        self.attempts, self.lock = {}, threading.RLock()
        self.closed = threading.Event()
        # A manager restart cannot adopt unverified in-memory capabilities. Only
        # resources carrying this dedicated-engine label are reclaimed.
        self.reap_orphans()
        if start_watchdog:
            threading.Thread(target=self.watchdog, daemon=True).start()

    def reap_orphans(self):
        containers = self.engine.call(["ps", "-aq", "--no-trunc", "--filter", "label=" + LABEL]).decode().split()
        for identifier in containers:
            if re.fullmatch(r"[a-f0-9]{12,64}", identifier):
                if not self.remove_resource("container", identifier):
                    raise ManagerError("cleanup_pending", 503)
        networks = self.engine.call(["network", "ls", "--filter", "label=" + LABEL, "--format", "{{.Name}}"] ).decode().split()
        for name in networks:
            if re.fullmatch(r"gtw-[a-f0-9]{20}-net", name):
                bridge = "gtw" + name[4:14]
                if not self.remove_resource("network", name):
                    raise ManagerError("cleanup_pending", 503)
                self.firewall("remove", bridge)
        volumes = self.engine.call(["volume", "ls", "--filter", "label=" + LABEL, "--format", "{{.Name}}"] ).decode().split()
        for name in volumes:
            if re.fullmatch(r"gtw-[a-f0-9]{20}-(inputs|config|work)", name):
                if not self.remove_resource("volume", name):
                    raise ManagerError("cleanup_pending", 503)

    def remove_resource(self, kind, name):
        """A failed CLI call is not proof of deletion, including timeouts."""
        removal = ["rm", "-f", name] if kind == "container" else [kind, "rm", name]
        listing = (["ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}"] if kind == "container"
                   else [kind, "ls", "--format", "{{.Name}}"])
        try:
            self.engine.call(removal, timeout=10)
            return True
        except Exception:
            try:
                remaining = self.engine.call(listing, timeout=5).decode().split()
                return name not in remaining
            except Exception:
                return False

    def firewall(self, action, bridge):
        self.engine.call(["run", "--rm", "--network", "host", "--read-only", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                          "--security-opt", "no-new-privileges", "--pids-limit", "16", "--memory", "128m", "--user", "0:0",
                          "--entrypoint", "python", self.config["manager_image"], "/opt/voxquarry/firewall.py", action, bridge])

    @staticmethod
    def container(name, *, memory="4g", cpus="2", pids="128", network="none"):
        return ["create", "--name", name, "--label", LABEL, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", pids, "--memory", memory, "--memory-swap", memory, "--cpus", cpus, "--user", "10000:10000",
                "--network", network, "--log-driver", "none", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777",
                "--env", "HOME=/workspace", "--env", "LANG=C.UTF-8", "--workdir", "/workspace"]

    def start(self, payload):
        allowed = {"attempt_id", "run_id", "model_id", "system_prompt", "prompt", "context_document", "tools", "capability", "deadline_seconds", "mode", "limits", "session_key", "reasoning_effort"}
        if not isinstance(payload, dict) or set(payload) - allowed or not ID.fullmatch(str(payload.get("attempt_id", ""))) or not ID.fullmatch(str(payload.get("run_id", ""))):
            raise ManagerError("attempt_request_invalid")
        session_key = payload.get("session_key")
        if session_key is not None and (not isinstance(session_key, str) or not re.fullmatch(r"wq_[a-f0-9]{64}", session_key)):
            raise ManagerError("session_key_invalid")
        if payload.get("reasoning_effort", "max") not in {"low", "medium", "high", "max", "minimal", "none"}:
            raise ManagerError("reasoning_effort_invalid")
        mode = payload.get("mode", "harness")
        deadline = payload.get("deadline_seconds", 600)
        if mode not in {"harness", "analysis"} or type(deadline) is not int or not 1 <= deadline <= 7200:
            raise ManagerError("attempt_request_invalid")
        limits = payload.get("limits", {"context_window": 200000, "max_output_tokens": 16384, "model_timeout_seconds": 180})
        if (not isinstance(limits, dict) or not {"context_window", "max_output_tokens", "model_timeout_seconds"} <= set(limits)
                or set(limits) - {"context_window", "max_output_tokens", "model_timeout_seconds", "research_context_tokens"}
                or any(type(limits[key]) is not int or not 1 <= limits[key] <= maximum for key, maximum in
                       (("context_window", 1_000_000), ("max_output_tokens", 393216), ("model_timeout_seconds", 1800)))):
            raise ManagerError("attempt_limits_invalid")
        if 'research_context_tokens' in limits and (type(limits['research_context_tokens']) is not int
                or not 1 <= limits['research_context_tokens'] <= limits['context_window']):
            raise ManagerError('attempt_limits_invalid')
        if mode == "harness":
            if 'context_document' in payload:
                document = payload['context_document']
                if not isinstance(document, str) or not document or len(document) > 1_000_000:
                    raise ManagerError('attempt_context_invalid')
                try:
                    if not isinstance(strict_json(document), dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ManagerError('attempt_context_invalid') from None
            for key, maximum in (("model_id", 160), ("system_prompt", 100000), ("prompt", 1_000_000), ("capability", 256)):
                if not isinstance(payload.get(key), str) or not payload[key] or len(payload[key]) > maximum:
                    raise ManagerError("attempt_request_invalid")
            if len(payload["capability"]) < 32 or any(c.isspace() for c in payload["capability"]):
                raise ManagerError("attempt_capability_invalid")
            tools = payload.get("tools")
            if not isinstance(tools, list) or not 1 <= len(tools) <= 64:
                raise ManagerError("attempt_tools_invalid")
            names = []
            for tool in tools:
                if (not isinstance(tool, dict) or not {"name", "description", "parameters"} <= set(tool)
                        or set(tool) - {"name", "description", "parameters", "concurrency_safe"}
                        or type(tool.get("concurrency_safe", False)) is not bool
                        or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(tool["name"]))
                        or not isinstance(tool["description"], str) or not isinstance(tool["parameters"], dict)):
                    raise ManagerError("attempt_tools_invalid")
                names.append(tool["name"])
            if len(set(names)) != len(names):
                raise ManagerError("attempt_tools_invalid")
        attempt_id = payload["attempt_id"]
        fingerprint = hashlib.sha256(json_bytes(payload)).hexdigest()
        with self.lock:
            existing = self.attempts.get(attempt_id)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ManagerError("attempt_conflict", 409)
                return self.status(attempt_id)
            capacity = self.research_concurrency if mode == 'harness' else 1
            if sum(a["status"] == "running" and a["mode"] == mode for a in self.attempts.values()) >= capacity:
                raise ManagerError("research_capacity_busy", 429)
            if session_key and any(a.get('session_key') == session_key and not a['purged'] for a in self.attempts.values()):
                raise ManagerError('research_session_busy', 429)
            prefix = "gtw-" + hashlib.sha256(attempt_id.encode()).hexdigest()[:20]
            state = {"attempt_id": attempt_id, "run_id": payload["run_id"], "mode": mode, "fingerprint": fingerprint,
                     "status": "running", "deadline": self.clock() + deadline, "finished": None, "prefix": prefix,
                     "events": [], "cursor": 0, "result": None, "error": None, "inputs": {}, "input_bytes": 0,
                     "containers": set(), "volumes": set(), "network": None, "bridge": None, "firewall": False,
                     "ready": threading.Event(), "operation": threading.RLock(), "initialization": threading.RLock(), "purged": False, "closing": False}
            state["session_key"] = session_key
            state["mounts"] = {suffix: (("gtw-session-" + session_key[3:] + "-" + suffix)
                if session_key and suffix != "config" else prefix + "-" + suffix) for suffix in ("inputs", "work", "state", "config")}
            self.attempts[attempt_id] = state
            self.event(state, {"kind": "manager.setup", "status": "running"})
            threading.Thread(target=self.initialize, args=(state, dict(payload)), daemon=True).start()
            return self.status(attempt_id)

    def event(self, state, value):
        with self.lock:
            if len(state["events"]) >= 1000:
                return
            state["cursor"] += 1
            state["events"].append({**value, "sequence": state["cursor"]})

    def require(self, attempt_id, *, active=False, ready=False):
        with self.lock:
            state = self.attempts.get(attempt_id)
        if not state or state["purged"] or state["closing"]:
            raise ManagerError("attempt_not_found", 404)
        if ready and not state["ready"].wait(30):
            raise ManagerError("attempt_setup_pending", 409)
        if active and (state["status"] != "running" or self.clock() >= state["deadline"]):
            raise ManagerError("attempt_inactive", 409)
        return state

    def assert_active(self, state):
        self.require(state["attempt_id"], active=True)

    def initialize(self, state, payload):
        p = state["prefix"]
        try:
            with state["initialization"]:
                for suffix in (("inputs", "config", "work", "state") if state["session_key"] else ("inputs", "config", "work")):
                    self.assert_active(state)
                    name = state["mounts"][suffix]
                    persistent = bool(state["session_key"] and suffix != "config")
                    args = ["volume", "create", "--label", "voxquarry.wenquan.persistence=session-v1" if persistent else LABEL]
                    if suffix == "work" and not persistent:
                        args += ["--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", "o=size=2147483648,uid=10000,gid=10000,mode=0700"]
                    self.engine.call(args + [name])
                    if not persistent:
                        state["volumes"].add(name)
                helper = p + "-loader"
                # Keep the bounded tmpfs volume mounted between disposable code
                # containers; the last unmount would otherwise erase outputs.
                self.engine.call(self.container(helper, memory="128m", cpus="0.5", pids="16") + [
                    "--mount", f"type=volume,src={state['mounts']['inputs']},dst=/inputs", "--mount", f"type=volume,src={state['mounts']['config']},dst=/run/voxquarry",
                    "--mount", f"type=volume,src={state['mounts']['work']},dst=/workspace",
                    "--entrypoint", "python", self.config["analysis_image"], "-c", "import time; time.sleep(604800)"])
                state["containers"].add(helper); self.engine.call(["start", helper])
                self.assert_active(state)
                if state["mode"] == "analysis":
                    state["ready"].set(); self.event(state, {"kind": "manager.ready", "status": "completed"}); return
                if 'context_document' in payload:
                    filename = 'research-context-' + payload['attempt_id'] + '.json'
                    content = payload['context_document'].encode()
                    self.copy_files(helper, '/inputs', {filename: content})
                    state['input_bytes'] += len(content)
                    state['inputs'][filename] = {'analysis_path': '/inputs/' + filename,
                        'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                native = p + "-native"
                self.engine.call(self.container(native, memory=self.analysis_memory) + [
                    "--mount", f"type=volume,src={state['mounts']['inputs']},dst=/inputs,readonly",
                    "--mount", f"type=volume,src={state['mounts']['work']},dst=/workspace",
                    "--entrypoint", "python", self.config["analysis_image"], "/opt/voxquarry/native_service.py"])
                state["containers"].add(native); self.engine.call(["start", native])
                self.wait_for_native(state, native)
                request = {key: payload[key] for key in ("attempt_id", "model_id", "prompt", "system_prompt")}
                request["reasoning_effort"] = payload.get("reasoning_effort", "max")
                if state["session_key"]:
                    request["session_key"] = state["session_key"]
                limits = payload.get("limits", {"context_window": 200000, "max_output_tokens": 16384, "model_timeout_seconds": 180})
                request.update(limits)
                request["request_timeout_seconds"] = payload.get("deadline_seconds", 600) + 30
                self.copy_files(helper, "/run/voxquarry", {"request.json": json_bytes(request), "tools.json": json_bytes(payload["tools"]),
                    "capability": payload["capability"].encode(), "relay.json": json_bytes({"broker_base_url": self.config["broker_base_url"],
                        "tool_names": sorted({tool["name"] for tool in payload["tools"]} | {"search_web", "read_web_page"}),
                        "max_concurrency": 16, "request_timeout_seconds": max(360, limits["model_timeout_seconds"] + 60),
                        "tool_idle_timeout_seconds": max(330, limits["model_timeout_seconds"] + 15), "model_idle_timeout_seconds": limits["model_timeout_seconds"] + 5,
                        "model_request_timeout_seconds": max(300, limits["model_timeout_seconds"] + 30),
                        # Containers share this dedicated VM's monotonic clock.
                        "attempt_deadline_monotonic": state["deadline"]})})
                self.assert_active(state)
                network, bridge = p + "-net", "gtw" + p[4:14]
                self.engine.call(["network", "create", "--internal", "--label", LABEL, "--opt", "com.docker.network.bridge.name=" + bridge, network])
                state["network"], state["bridge"] = network, bridge
                self.firewall("add", bridge); state["firewall"] = True
                relay = p + "-relay"
                self.engine.call(self.container(relay, memory="128m", cpus="0.5", pids="32", network="bridge") + [
                    "--mount", f"type=volume,src={state['mounts']['config']},dst=/run/voxquarry,readonly", self.config["relay_image"]])
                state["containers"].add(relay)
                self.engine.call(["network", "connect", "--alias", "relay", network, relay]); self.engine.call(["start", relay])
                networks = strict_json(self.engine.call(["inspect", "--format", "{{json .NetworkSettings.Networks}}", relay]))
                relay_ip = str(ipaddress.IPv4Address(networks[network]["IPAddress"]))
                self.wait_for_relay(state, relay)
                self.assert_active(state)
                runner = p + "-runner"
                self.engine.call(self.container(runner, memory=self.runner_memory, cpus="2", pids="128", network=network) + [
                    "--dns", "127.0.0.1", "--dns-option", "attempts:1", "--dns-option", "timeout:1", "--add-host", "relay:" + relay_ip,
                    "--mount", f"type=volume,src={state['mounts']['config']},dst=/run/voxquarry,readonly",
                    *((["--mount", f"type=volume,src={state['mounts']['state']},dst=/state"])
                      if state["session_key"] else ["--tmpfs", "/state:rw,nosuid,nodev,size=512m,uid=10000,gid=10000,mode=0700"]),
                    "--tmpfs", "/workspace:rw,nosuid,nodev,exec,size=512m,uid=10000,gid=10000,mode=0700", self.config["runner_image"]])
                state["containers"].add(runner); state["ready"].set()
                self.event(state, {"kind": "manager.ready", "status": "completed"})
            self.monitor(state, runner)
        except Exception as exc:
            self.fail(state, exc.code if isinstance(exc, ManagerError) else "attempt_setup_failed")
        finally:
            payload.clear(); state["ready"].set()

    def wait_for_native(self, state, container):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            self.assert_active(state)
            try:
                self.engine.call(["exec", container, "python", "-c",
                    "import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(1); s.connect('/tmp/voxquarry-native.sock'); s.close()"], timeout=2)
                return
            except ManagerError:
                time.sleep(0.2)
        raise ManagerError("native_provider_not_ready", 503)

    def native_io(self, attempt_id, payload):
        state = self.require(attempt_id, active=True, ready=True)
        if not isinstance(payload, dict) or payload.get("op") not in {"fs", "spawn", "poll", "read", "terminate", "resolveExecutable"}:
            raise ManagerError("native_operation_invalid")
        # Fixed executable and container: neither argv nor paths can select the
        # runner, host, another session, network or Docker operation.
        raw = self.engine.call(["exec", "-i", state["prefix"] + "-native", "python",
            "/opt/voxquarry/native_service.py", "--client"], content=json_bytes(payload), timeout=35, limit=32 * MIB)
        self.assert_active(state)
        value = strict_json(raw)
        if not isinstance(value, dict) or set(value) not in ({"result"}, {"error"}):
            raise ManagerError("native_result_invalid", 502)
        return value

    def wait_for_relay(self, state, relay):
        deadline = time.monotonic() + 10
        probe = ["exec", relay, "python", "-c",
                 "import socket; socket.create_connection(('127.0.0.1', 9000), timeout=0.5).close()"]
        while True:
            self.assert_active(state)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagerError("relay_not_ready", 503)
            try:
                self.engine.call(probe, timeout=min(1, remaining))
            except ManagerError:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            else:
                self.assert_active(state)
                return

    def copy_files(self, container, directory, files):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, content in files.items():
                if not FILE.fullmatch(name):
                    raise ManagerError("input_filename_invalid")
                entry = tarfile.TarInfo(name); entry.size = len(content); entry.mode = 0o400; entry.uid = entry.gid = 10000
                archive.addfile(entry, io.BytesIO(content))
        self.engine.call(["cp", "-", container + ":" + directory], content=output.getvalue(), timeout=180)

    def monitor(self, state, runner):
        process = self.engine.spawn(["start", "--attach", runner]); process.stdin.close()
        runner_error_type = None
        try:
            consumed = 0
            while True:
                line = process.stdout.readline(300001)
                if not line:
                    break
                consumed += len(line)
                if len(line) > 300000 or consumed > 4 * MIB:
                    raise ManagerError("runner_output_limit", 502)
                try:
                    value = strict_json(line)
                except (ValueError, TypeError):
                    continue
                self.assert_active(state)
                if isinstance(value, dict) and set(value) == {"event"} and isinstance(value["event"], dict):
                    event = value["event"]
                    if event.get("kind") in {"status", "turn/start", "turn/end", "agent/inbox/spliced", "trajectory.gap"}:
                        safe = {key: entry for key, entry in event.items() if key in {"kind", "status", "event_id"} and isinstance(entry, str) and len(entry) <= 160}
                        self.event(state, safe)
                elif isinstance(value, dict) and set(value) == {"result"}:
                    result = value["result"]
                    if not isinstance(result, dict) or set(result) - {"answer", "finish_reason", "runtime_diagnostics"} or not isinstance(result.get("answer"), str) or len(result["answer"]) > 60000 or not isinstance(result.get("finish_reason"), (str, type(None))):
                        raise ManagerError("runner_result_invalid", 502)
                    if 'runtime_diagnostics' in result:
                        timing = result['runtime_diagnostics']
                        archive = timing.get('archive') if isinstance(timing, dict) else None
                        if (not isinstance(timing, dict) or set(timing) != {'version', 'archive'} or type(timing['version']) is not int or timing['version'] != 1
                                or not isinstance(archive, dict) or set(archive) != {'events', 'attempts', 'failures', 'total_ms', 'max_ms'}
                                or any(type(archive[k]) is not int or not 0 <= archive[k] <= 10**9 for k in ('events', 'attempts', 'failures'))
                                or any(type(archive[k]) not in (int, float) or not math.isfinite(archive[k]) or not 0 <= archive[k] <= 10**9 for k in ('total_ms', 'max_ms'))):
                            raise ManagerError('runner_result_invalid', 502)
                        if not (archive['failures'] <= archive['events'] <= archive['attempts'] <= 3 * archive['events']
                                and archive['max_ms'] <= archive['total_ms'] and (archive['events'] or archive['total_ms'] == 0)):
                            raise ManagerError('runner_result_invalid', 502)
                    if result.get("finish_reason") == "error":
                        raise ManagerError("harness_failed", 502)
                    with self.lock:
                        self.assert_active(state); state["result"] = result
                elif isinstance(value, dict) and "error" in value:
                    if isinstance(value["error"], dict):
                        runner_error_type = value["error"].get("type")
                    raise ManagerError("harness_failed", 502)
            process.wait(timeout=5)
            with self.lock:
                if state["status"] == "running":
                    self.assert_active(state)
                    if process.returncode or state["result"] is None:
                        raise ManagerError("harness_failed", 502)
                    state["status"], state["finished"] = "completed", self.clock()
            self.quiesce(state)
        except Exception as exc:
            self.fail(state, exc.code if isinstance(exc, ManagerError) else "harness_failed", runner_error_type=runner_error_type)
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
            process.stdout.close()

    def fail(self, state, code, *, runner_error_type=None):
        with self.lock:
            if state["status"] == "running":
                state["status"], state["finished"], state["error"] = "failed", self.clock(), {"code": code}
                if isinstance(runner_error_type, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", runner_error_type):
                    state["error"]["runner_error_type"] = runner_error_type
                state["result"] = None
        self.quiesce(state)

    def status(self, attempt_id, after=0):
        state = self.require(attempt_id)
        if type(after) is not int or after < 0:
            raise ManagerError("event_cursor_invalid")
        with self.lock:
            events = [event for event in state["events"] if event["sequence"] > after][:200]
            return {"attempt_id": attempt_id, "status": state["status"], "ready": state["ready"].is_set(),
                    "events": events, "cursor": events[-1]["sequence"] if events else after,
                    "result": state["result"], "error": state["error"]}

    def put_input(self, attempt_id, filename, content, expected_hash=None):
        state = self.require(attempt_id, active=True, ready=True)
        if not FILE.fullmatch(filename) or not isinstance(content, bytes) or len(content) > 64 * MIB:
            raise ManagerError("input_limit_exceeded", 413)
        digest = hashlib.sha256(content).hexdigest()
        if expected_hash and expected_hash != digest:
            raise ManagerError("input_hash_mismatch", 409)
        with state["operation"]:
            self.assert_active(state)
            prior = state["inputs"].get(filename)
            if prior:
                if prior["sha256"] != digest:
                    raise ManagerError("input_immutable", 409)
                return prior
            if state["input_bytes"] + len(content) > 8 * GIB:
                raise ManagerError("input_quota_exceeded", 413)
            if len(state["inputs"]) >= 2048:
                raise ManagerError("input_file_count_limit", 413)
            # Reserve before writing: a failed archive may already have written
            # bytes. Such an attempt is stopped, never reused with a reset quota.
            state["input_bytes"] += len(content)
            try:
                self.copy_files(state["prefix"] + "-loader", "/inputs", {filename: content})
            except Exception:
                self.fail(state, "input_copy_failed")
                raise
            self.assert_active(state)
            result = {"analysis_path": "/inputs/" + filename, "bytes": len(content), "sha256": digest}
            state["inputs"][filename] = result
            return result

    def tool(self, attempt_id, name, arguments, *, output=False):
        state = self.require(attempt_id, active=not output, ready=True)
        if name not in {"run_python", "run_shell", "read_file", "write_file", "parse_attachment", "render_report", "_read_output"} or (name == "_read_output") != output or not isinstance(arguments, dict):
            raise ManagerError("analysis_tool_rejected", 403)
        timeout = arguments.get("timeout_seconds", 30 if name == "parse_attachment" else 60)
        if type(timeout) is not int or not 1 <= timeout <= (30 if name == "parse_attachment" else 300):
            raise ManagerError("analysis_timeout_invalid")
        with state["operation"]:
            if not output:
                self.assert_active(state)
                timeout = min(timeout, max(1, int(state["deadline"] - self.clock())))
            serial = hashlib.sha256(os.urandom(32)).hexdigest()[:8]
            container = state["prefix"] + "-code-" + serial
            args = self.container(container, memory="512m" if name == "parse_attachment" else self.analysis_memory) + [
                "--interactive", "--mount", f"type=volume,src={state['mounts']['inputs']},dst=/inputs,readonly",
                "--mount", f"type=volume,src={state['mounts']['work']},dst=/workspace", self.config["analysis_image"]]
            try:
                self.engine.call(args); state["containers"].add(container)
                self.require(attempt_id, active=not output)
                raw = self.engine.call(["start", "--attach", "--interactive", container], content=json_bytes({"name": name, "arguments": arguments}), timeout=timeout + 5, limit=32 * MIB if output else 4 * MIB, check=False)
                if not output:
                    self.assert_active(state)
                value = strict_json(raw)
                if not isinstance(value, dict) or set(value) not in ({"result"}, {"error"}):
                    raise ManagerError("analysis_result_invalid", 502)
                if "error" in value:
                    code = value["error"].get("code") if isinstance(value["error"], dict) else None
                    raise ManagerError(code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else "analysis_failed", 422)
                self.require(attempt_id, active=not output)
                return value["result"]
            except (ValueError, TypeError):
                raise ManagerError("analysis_result_invalid", 502) from None
            finally:
                if self.remove_resource("container", container):
                    state["containers"].discard(container)
                else:
                    self.fail(state, "cleanup_pending")
                    raise ManagerError("cleanup_pending", 503)

    def read_output(self, attempt_id, path):
        result = self.tool(attempt_id, "_read_output", {"path": path}, output=True)
        try:
            data = base64.b64decode(result["base64"], validate=True)
            if len(data) > 16 * MIB or len(data) != result["bytes"] or hashlib.sha256(data).hexdigest() != result["sha256"]:
                raise ValueError()
            return data
        except (KeyError, ValueError, TypeError):
            raise ManagerError("analysis_result_invalid", 502) from None

    def quiesce(self, state):
        # Do not wait for tool locks: cancellation must kill an executing tool.
        pending = False
        for name in tuple(state["containers"]):
            if not name.endswith("-loader"):
                if self.remove_resource("container", name):
                    state["containers"].discard(name)
                else:
                    pending = True
        state["quiesce_pending"] = pending
        return not pending

    def stop(self, attempt_id):
        with self.lock:
            state = self.attempts.get(attempt_id)
            if not state:
                return {"attempt_id": attempt_id, "status": "cancelled"}
            if state["status"] == "running":
                state["status"], state["finished"], state["result"] = "cancelled", self.clock(), None
            state["closing"] = True
        if not self.quiesce(state):
            raise ManagerError("cleanup_pending", 503)
        with state["initialization"], state["operation"]:
            self.cleanup(state)
        return {"attempt_id": attempt_id, "status": state["status"]}

    def cleanup(self, state):
        pending = False
        for name in tuple(state["containers"]):
            if self.remove_resource("container", name):
                state["containers"].discard(name)
            else:
                pending = True
        if pending:
            raise ManagerError("cleanup_pending", 503)
        if state["network"]:
            if not self.remove_resource("network", state["network"]):
                raise ManagerError("cleanup_pending", 503)
            state["network"] = None
        if state["firewall"]:
            try:
                self.firewall("remove", state["bridge"])
            except Exception:
                raise ManagerError("cleanup_pending", 503) from None
            state["firewall"] = False
        for name in tuple(state["volumes"]):
            if self.remove_resource("volume", name):
                state["volumes"].discard(name)
            else:
                pending = True
        if pending:
            raise ManagerError("cleanup_pending", 503)
        state["purged"] = True; state["inputs"].clear()

    def watchdog(self):
        while not self.closed.wait(1):
            for state in tuple(self.attempts.values()):
                try:
                    if state["purged"]:
                        continue
                    if state["status"] == "running" and self.clock() >= state["deadline"]:
                        self.fail(state, "attempt_deadline_exceeded")
                    if state["closing"] or (state["finished"] is not None and self.clock() - state["finished"] >= 7 * 86400):
                        self.stop(state["attempt_id"])
                    elif state.get("quiesce_pending"):
                        self.quiesce(state)
                except Exception:
                    # A transient engine failure must not stop later cleanup.
                    continue

    def close(self):
        self.closed.set()
        pending = False
        for attempt_id in tuple(self.attempts):
            try:
                self.stop(attempt_id)
            except Exception:
                pending = True
        if pending:
            raise ManagerError("cleanup_pending", 503)


def make_server(manager, token, address=("0.0.0.0", 9001)):
    if not isinstance(token, str) or not 32 <= len(token) <= 256 or any(c.isspace() for c in token):
        raise ManagerError("management_token_invalid", 503)
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def dispatch(self):
            self.connection.settimeout(180)
            expected = "Bearer " + token
            auth = self.headers.get("Authorization", "")
            if len(self.headers.get_all("Authorization", [])) != 1 or not hmac.compare_digest(auth.encode(), expected.encode()):
                raise ManagerError("manager_unauthorized", 401)
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                raise ManagerError("request_framing_invalid")
            parsed = urlsplit(self.path)
            request_target = self.requestline.split()[1] if len(self.requestline.split()) == 3 else ""
            if request_target != self.path or parsed.scheme or parsed.netloc or parsed.fragment or "%" in parsed.path or parsed.path.endswith("/"):
                raise ManagerError("manager_path_invalid")
            route = parsed.path
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(len(value) != 1 for value in query.values()):
                raise ManagerError("manager_query_invalid")
            parts = route.strip("/").split("/")
            is_input = len(parts) == 5 and parts[:2] == ["v1", "attempts"] and parts[3] == "inputs"
            length = self.headers.get("Content-Length", "0")
            if not length.isdecimal() or int(length) > (64 * MIB if is_input else 4 * MIB):
                raise ManagerError("request_limit", 413)
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise ManagerError("request_incomplete")
            if route == "/v1/attempts" and self.command == "POST" and not query:
                return manager.start(strict_json(raw)), False
            if len(parts) < 3 or parts[:2] != ["v1", "attempts"] or not ID.fullmatch(parts[2]):
                raise ManagerError("manager_path_invalid", 404)
            attempt_id = parts[2]
            if len(parts) == 3:
                if self.command == "DELETE" and not query:
                    return manager.stop(attempt_id), False
                if self.command == "GET" and not set(query) - {"after"}:
                    after = query.get("after", ["0"])[0]
                    if not after.isdecimal():
                        raise ManagerError("event_cursor_invalid")
                    return manager.status(attempt_id, int(after)), False
            if self.command == "POST" and not query:
                if len(parts) == 4 and parts[3] == "native":
                    return manager.native_io(attempt_id, strict_json(raw)), False
                if is_input:
                    return manager.put_input(attempt_id, parts[4], raw, self.headers.get("X-Content-SHA256")), False
                if len(parts) == 5 and parts[3] == "tools":
                    value = strict_json(raw)
                    if not isinstance(value, dict) or set(value) != {"arguments"}:
                        raise ManagerError("analysis_arguments_invalid")
                    return manager.tool(attempt_id, parts[4], value["arguments"]), False
            if self.command == "GET" and len(parts) == 4 and parts[3] == "files" and set(query) == {"path"}:
                return manager.read_output(attempt_id, query["path"][0]), True
            raise ManagerError("manager_path_invalid", 404)

        def handle_request(self):
            try:
                value, binary = self.dispatch(); status = 200
                body = value if binary else json_bytes(value)
            except Exception as exc:
                status = exc.status if isinstance(exc, ManagerError) else 400
                body, binary = json_bytes({"error": {"code": exc.code if isinstance(exc, ManagerError) else "manager_request_invalid"}}), False
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream" if binary else "application/json")
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        do_POST = do_GET = do_DELETE = do_PUT = do_PATCH = handle_request
    class BoundedServer(ThreadingHTTPServer):
        daemon_threads = True
        def __init__(self, *args):
            self.slots = threading.BoundedSemaphore(8)
            super().__init__(*args)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request); return
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release(); raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()
    server = BoundedServer(address, Handler)
    return server


def main():
    os.umask(0o077)
    config = strict_json(Path("/run/voxquarry/manager.json").read_bytes())
    token = Path("/run/voxquarry/management-token").read_text().strip()
    manager = Manager(config)
    def terminate(_number, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        make_server(manager, token).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.close()


if __name__ == "__main__":
    main()
