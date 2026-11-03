"""Manager boundary tests: fake Docker engine only, optional loopback HTTP."""
from contextlib import contextmanager
import base64
import hashlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import queue
import sys
import tarfile
import threading
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("wenquan_manager_under_test", Path(__file__).with_name("manager.py"))
manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manager
SPEC.loader.exec_module(manager)

CONFIG = {"expected_engine_id": "TEST-DEDICATED-ENGINE-ID", "runner_image": "sha256:" + "a" * 64,
          "analysis_image": "sha256:" + "b" * 64, "relay_image": "sha256:" + "c" * 64,
          "manager_image": "sha256:" + "d" * 64, "broker_base_url": "http://127.0.0.1:8088/wenquan/internal/broker"}
TOKEN = "ONLY-TEST-MANAGEMENT-CAPABILITY-1234567890"
CAPABILITY = "ONLY-TEST-ATTEMPT-CAPABILITY-1234567890"


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class FakeProcess:
    def __init__(self, content, wait_hook=None):
        self.stdout = io.BytesIO(content)
        self.stdin = io.BytesIO()
        self.returncode = None
        self.wait_hook = wait_hook

    def wait(self, timeout=None):
        if self.wait_hook:
            hook, self.wait_hook = self.wait_hook, None
            hook()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeEngine(manager.DockerEngine):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.info = {"ID": CONFIG["expected_engine_id"], "Name": "colima-voxquarry"}
        self.setup_entered, self.setup_release = threading.Event(), threading.Event()
        self.setup_release.set()
        self.tool_entered, self.tool_release = threading.Event(), threading.Event()
        self.tool_release.set()
        self.runner_removed = threading.Event()
        self.worker_result = {"result": {"stdout": "isolated output", "exit_code": 0}}
        self.runner_result = b'{"result":{"answer":"A fixed answer","finish_reason":"stop"}}\n'
        self.wait_hook = None
        self.resources = {"container": set(), "network": set(), "volume": set()}
        self.removal_failures = {}
        self.listing_failure = False
        self.probe_failures = 0

    def call(self, args, **options):
        args = list(args)
        self.calls.append((args, options))
        if args[0] == "info":
            return manager.json_bytes(self.info)
        if args[0] == "exec" and "socket.create_connection" in args[-1] and self.probe_failures:
            self.probe_failures -= 1
            raise manager.ManagerError("engine_operation_failed", 503)
        kind = "container" if args[:2] == ["rm", "-f"] else args[0] if args[:2] in (["network", "rm"], ["volume", "rm"]) else None
        if kind:
            failure = self.removal_failures.get((kind, args[-1]))
            if failure == "gone":
                self.resources[kind].discard(args[-1])
                raise manager.ManagerError("engine_operation_failed", 503)
            if failure:
                if failure == "timeout" or options.get("check", True):
                    raise manager.ManagerError("engine_timeout" if failure == "timeout" else "engine_operation_failed", 503)
                return b""  # Real DockerEngine.call(check=False) hides nonzero exit.
            self.resources[kind].discard(args[-1])
        if args[0] == "ps" or args[:2] in (["network", "ls"], ["volume", "ls"]):
            if self.listing_failure:
                raise manager.ManagerError("engine_timeout", 504)
            kind = "container" if args[0] == "ps" else args[0]
            return "\n".join(sorted(self.resources[kind])).encode()
        if args[0] == "create":
            self.resources["container"].add(args[args.index("--name") + 1])
        if args[:2] == ["network", "create"]:
            self.resources["network"].add(args[-1])
        if args[:3] == ["inspect", "--format", "{{json .NetworkSettings.Networks}}"]:
            prefix = args[-1].removesuffix("-relay")
            return manager.json_bytes({"bridge": {"IPAddress": "172.17.0.9"}, prefix + "-net": {"IPAddress": "172.31.100.2"}})
        if args[:2] == ["volume", "create"]:
            self.setup_entered.set()
            if not self.setup_release.wait(2):
                raise manager.ManagerError("fake_setup_timeout")
            self.resources["volume"].add(args[-1])
        if args[:3] == ["start", "--attach", "--interactive"]:
            self.tool_entered.set()
            if not self.tool_release.wait(2):
                raise manager.ManagerError("fake_tool_timeout")
            return manager.json_bytes(self.worker_result)
        if args[:2] == ["rm", "-f"]:
            self.tool_release.set()
            if args[2].endswith("-runner"):
                self.runner_removed.set()
        return b""

    def spawn(self, args):
        self.calls.append((list(args), {"fake_spawn": True}))
        return FakeProcess(self.runner_result, self.wait_hook)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.deny_process = patch.object(manager.subprocess, "Popen", side_effect=AssertionError("No external Docker or subprocess is permitted"))
        self.deny_process.start()
        self.addCleanup(self.deny_process.stop)
        self.engine = FakeEngine()
        self.clock = Clock()
        self.service = manager.Manager(CONFIG, engine=self.engine, clock=self.clock, start_watchdog=False)
        self.addCleanup(self.finish_attempts)

    def finish_attempts(self):
        self.engine.setup_release.set()
        self.engine.tool_release.set()
        self.service.closed.set()
        for state in list(self.service.attempts.values()):
            state["ready"].wait(2)
            self.service.stop(state["attempt_id"])

    def test_production_engine_requires_exact_id_name_and_resource_profile(self):
        config = {**CONFIG, "expected_engine_name": "voxquarry", "resource_profile": "production-single"}
        self.engine.info["Name"] = "voxquarry"
        production = manager.Manager(config, engine=self.engine, clock=self.clock, start_watchdog=False)
        self.assertEqual(production.analysis_memory, "3g")
        self.assertEqual(production.runner_memory, "1024m")
        for invalid in ({**config, "expected_engine_id": "wrong"},
                        {**config, "expected_engine_name": "production-default-docker"},
                        {**config, "resource_profile": "unlimited"},
                        {**config, "resource_profile": "local-default"}):
            with self.subTest(config=invalid):
                with self.assertRaises(manager.ManagerError):
                    manager.Manager(invalid, engine=self.engine, start_watchdog=False)

    def test_two_researches_require_resource_headroom_and_keep_sessions_separate(self):
        config = {**CONFIG, 'research_concurrency': 2}
        self.engine.info.update(MemTotal=6 * manager.GIB, NCPU=2)
        self.assert_error('research_capacity_resources_insufficient', manager.Manager, config, engine=self.engine, start_watchdog=False)
        self.engine.info.update(MemTotal=12 * manager.GIB, NCPU=4)
        self.assert_error('research_capacity_resources_insufficient', manager.Manager, config, engine=self.engine, start_watchdog=False)
        self.engine.info.update(MemTotal=16 * manager.GIB)
        self.service = manager.Manager(config, engine=self.engine, clock=self.clock, start_watchdog=False)
        payload = {'attempt_id': 'pair_1', 'run_id': 'run_pair_1', 'mode': 'harness', 'model_id': 'configured-model',
            'system_prompt': 'Synthetic only', 'prompt': 'Independent research', 'capability': CAPABILITY,
            'tools': [{'name': 'read_one', 'description': 'Synthetic read', 'parameters': {}, 'concurrency_safe': True}],
            'session_key': 'wq_' + 'a' * 64}
        # Hold setup so both attempts are live during admission. No containers
        # or real models are used by this fake engine boundary test.
        self.engine.setup_release.clear()
        self.service.start(payload)
        self.assert_error('research_session_busy', self.service.start, {**payload, 'attempt_id': 'pair_same'})
        second = {**payload, 'attempt_id': 'pair_2', 'run_id': 'run_pair_2', 'session_key': 'wq_' + 'b' * 64}
        self.service.start(second)
        self.assert_error('research_capacity_busy', self.service.start, {**payload, 'attempt_id': 'pair_3', 'session_key': 'wq_' + 'c' * 64})
        self.assertNotEqual(self.service.attempts['pair_1']['mounts']['work'], self.service.attempts['pair_2']['mounts']['work'])
        self.engine.setup_release.set()

    def test_production_analysis_template_preserves_isolation_with_smaller_limit(self):
        config = {**CONFIG, "expected_engine_name": "voxquarry", "resource_profile": "production-single"}
        self.engine.info["Name"] = "voxquarry"
        self.service = manager.Manager(config, engine=self.engine, clock=self.clock, start_watchdog=False)
        self.start_analysis()
        self.service.tool("analysis_1", "run_python", {"code": "print(1)", "timeout_seconds": 20})
        args = next(a for a, _ in self.engine.calls if a[0] == "create" and "-code-" in a[a.index("--name") + 1])
        for option, expected in (("--memory", "3g"), ("--memory-swap", "3g"), ("--network", "none"),
                                 ("--user", "10000:10000"), ("--cap-drop", "ALL")):
            self.assertEqual(args[args.index(option)+1], expected)

    def start_analysis(self, attempt="analysis_1", **extra):
        value = self.service.start({"attempt_id": attempt, "run_id": "run_1", "mode": "analysis", **extra})
        self.assertEqual(value["attempt_id"], attempt)
        state = self.service.attempts[attempt]
        self.assertTrue(state["ready"].wait(2))
        self.assertEqual(state["status"], "running", state.get("error"))
        return state

    def test_session_files_survive_attempt_cleanup_but_credentials_do_not(self):
        key = "wq_" + "a" * 64
        first = self.start_analysis("persist_1", session_key=key)
        mounts = dict(first["mounts"])
        self.service.stop("persist_1")
        self.assertNotIn(mounts["config"], self.engine.resources["volume"])
        self.assertTrue({mounts[k] for k in ("inputs", "work", "state")} <= self.engine.resources["volume"])
        again = self.start_analysis("persist_2", session_key=key)
        self.assertEqual(again["mounts"]["work"], mounts["work"])
        self.assertNotEqual(again["mounts"]["config"], mounts["config"])
        self.service.stop("persist_2")
        other = self.start_analysis("persist_3", session_key="wq_" + "b" * 64)
        self.assertNotEqual(other["mounts"]["work"], mounts["work"])

    def test_session_key_cannot_be_used_as_arbitrary_docker_mount(self):
        for key in ("../../var/run/docker.sock", "wq_" + "a" * 63, "wq_" + "a" * 64 + "-config"):
            with self.assertRaises(manager.ManagerError) as error:
                self.service.start({"attempt_id": "rejected", "run_id": "run", "mode": "analysis", "session_key": key})
            self.assertEqual(error.exception.code, "session_key_invalid")
        self.assertFalse(self.service.attempts)

    def test_shutdown_attempts_every_stop_before_reporting_pending_cleanup(self):
        self.service.attempts = {"first": {}, "second": {}}
        try:
            with patch.object(self.service, "stop", side_effect=[manager.ManagerError("cleanup_pending", 503), {}]) as stop:
                with self.assertRaises(manager.ManagerError) as raised:
                    self.service.close()
                self.assertEqual(raised.exception.code, "cleanup_pending")
                self.assertEqual([call.args[0] for call in stop.call_args_list], ["first", "second"])
                self.assertTrue(self.service.closed.is_set())
        finally:
            self.service.attempts = {}

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(manager.ManagerError) as failure:
            function(*args, **kwargs)
        self.assertEqual(failure.exception.code, code)

    def test_exact_engine_id_and_name_are_required_before_any_cleanup(self):
        for info in ({"ID": "other-engine", "Name": "colima-voxquarry"},
                     {"ID": CONFIG["expected_engine_id"], "Name": "colima-business"}):
            with self.subTest(info=info):
                engine = FakeEngine()
                engine.info = info
                self.assert_error("engine_identity_mismatch", manager.Manager, CONFIG, engine=engine, start_watchdog=False)
                self.assertEqual([call[0] for call in engine.calls], [["info", "--format", "{{json .}}"]])
        self.assertEqual(self.engine.prefix, ["docker", "--host", "unix:///var/run/docker.sock"])
        self.assert_error("engine_socket_rejected", manager.DockerEngine, "/home/example/.docker/run/docker.sock")

    def test_startup_rejects_uncertain_orphan_cleanup_and_keeps_its_isolation(self):
        engine = FakeEngine()
        container, prefix = "a" * 64, "gtw-" + "b" * 20
        engine.resources = {"container": {container}, "network": {prefix + "-net"}, "volume": {prefix + "-work"}}
        engine.removal_failures["container", container] = "nonzero"
        self.assert_error("cleanup_pending", manager.Manager, CONFIG, engine=engine, start_watchdog=False)
        self.assertEqual(engine.resources["container"], {container})
        self.assertEqual(engine.resources["network"], {prefix + "-net"})
        self.assertFalse(any(args[0] == "run" or args[:2] == ["network", "rm"] for args, _ in engine.calls))
        engine.removal_failures.clear()
        manager.Manager(CONFIG, engine=engine, start_watchdog=False)
        self.assertFalse(any(engine.resources.values()))

    def test_only_immutable_image_ids_and_trusted_fixed_configuration_are_accepted(self):
        for image in ("python:latest", "repo/image@sha256:" + "a" * 64, "sha256:" + "A" * 64, "sha256:short"):
            for key in ("runner_image", "analysis_image", "relay_image", "manager_image"):
                with self.subTest(image=image, key=key):
                    engine = FakeEngine()
                    self.assert_error("immutable_image_required", manager.Manager, {**CONFIG, key: image}, engine=engine, start_watchdog=False)
                    self.assertEqual(engine.calls, [])
        self.assert_error("manager_config_invalid", manager.Manager, {**CONFIG, "docker_host": "ssh://business"}, engine=FakeEngine())

    def test_create_returns_while_setup_is_blocked_and_retries_are_idempotent(self):
        self.engine.setup_release.clear()
        received = queue.Queue()
        payload = {"attempt_id": "analysis_async", "run_id": "run_async", "mode": "analysis"}
        thread = threading.Thread(target=lambda: received.put(self.service.start(payload)), daemon=True)
        thread.start()
        value = received.get(timeout=1)
        self.assertEqual(value["status"], "running")
        self.assertFalse(value["ready"])
        self.assertTrue(self.engine.setup_entered.wait(1))
        self.assertFalse(self.engine.setup_release.is_set())
        self.assertEqual(self.service.start(payload)["attempt_id"], "analysis_async")
        self.assert_error("attempt_conflict", self.service.start, {**payload, "run_id": "different_run"})
        self.assert_error("research_capacity_busy", self.service.start, {**payload, "attempt_id": "second_analysis"})
        self.engine.setup_release.set()
        self.assertTrue(self.service.attempts["analysis_async"]["ready"].wait(1))
        thread.join(1)

    def test_analysis_template_has_no_network_no_capability_and_bounded_writable_work(self):
        state = self.start_analysis()
        result = self.service.tool("analysis_1", "run_python", {"code": "print('untrusted code remains stdin')", "timeout_seconds": 20})
        self.assertEqual(result["stdout"], "isolated output")
        code_create = next(args for args, _ in self.engine.calls if args[0] == "create" and "-code-" in args[args.index("--name") + 1])
        for option, expected in (("--network", "none"), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                                 ("--user", "10000:10000"), ("--memory", "4g"), ("--memory-swap", "4g"), ("--pids-limit", "128")):
            self.assertEqual(code_create[code_create.index(option) + 1], expected)
        self.assertIn("--read-only", code_create)
        self.assertEqual(code_create[-1], CONFIG["analysis_image"])
        mounts = [code_create[index + 1] for index, value in enumerate(code_create) if value == "--mount"]
        self.assertEqual(mounts, [f"type=volume,src={state['prefix']}-inputs,dst=/inputs,readonly", f"type=volume,src={state['prefix']}-work,dst=/workspace"])
        self.assertNotIn("capability", " ".join(code_create))
        self.assertNotIn("/run/voxquarry", " ".join(code_create))
        self.assertFalse(any(value in code_create for value in ("--privileged", "--cap-add", "--device", "--pid")))
        work = next(args for args, _ in self.engine.calls if args[:2] == ["volume", "create"] and args[-1].endswith("-work"))
        self.assertIn("type=tmpfs", work)
        self.assertIn("o=size=2147483648,uid=10000,gid=10000,mode=0700", work)
        start, options = next((args, options) for args, options in self.engine.calls if args[:3] == ["start", "--attach", "--interactive"])
        self.assertNotIn("untrusted code", " ".join(start))
        self.assertEqual(manager.strict_json(options["content"])["arguments"]["code"], "print('untrusted code remains stdin')")
        loader = next(args for args, _ in self.engine.calls if args[0] == "create" and args[args.index("--name") + 1].endswith("-loader"))
        loader_mounts = [loader[index + 1] for index, value in enumerate(loader) if value == "--mount"]
        self.assertIn(f"type=volume,src={state['prefix']}-work,dst=/workspace", loader_mounts)
        self.assertEqual(loader[loader.index("--network") + 1], "none")
        self.assertEqual(loader[-4:], ["python", CONFIG["analysis_image"], "-c", "import time; time.sleep(604800)"])
        self.assertIn(state["prefix"] + "-loader", self.engine.resources["container"])

    def test_quiesce_keeps_failed_removals_tracked_and_preserves_checkpoint_loader(self):
        state = self.start_analysis()
        runner = state["prefix"] + "-runner"
        for failure in ("timeout", "nonzero"):
            with self.subTest(failure=failure):
                state["containers"].add(runner)
                self.engine.resources["container"].add(runner)
                self.engine.removal_failures["container", runner] = failure
                try:
                    self.assertFalse(self.service.quiesce(state))
                    self.assertIn(runner, state["containers"])
                    self.assertIn(runner, self.engine.resources["container"])
                    self.assertTrue(state["quiesce_pending"])
                    self.assertIn(state["prefix"] + "-loader", state["containers"])
                finally:
                    self.engine.removal_failures.clear()
                self.assertTrue(self.service.quiesce(state))
                self.assertNotIn(runner, state["containers"])
                self.assertNotIn(runner, self.engine.resources["container"])
                self.assertIn(state["prefix"] + "-loader", self.engine.resources["container"])

    def test_stop_reports_cleanup_pending_without_removing_isolation_or_forgetting_live_runner(self):
        state = self.start_analysis()
        runner, network = state["prefix"] + "-runner", state["prefix"] + "-net"
        state["containers"].add(runner)
        self.engine.resources["container"].add(runner)
        state["network"], state["bridge"], state["firewall"] = network, "gtw0123456789", True
        self.engine.resources["network"].add(network)
        self.engine.removal_failures["container", runner] = "nonzero"
        try:
            self.assert_error("cleanup_pending", self.service.stop, "analysis_1")
            self.assertTrue(state["closing"])
            self.assertFalse(state["purged"])
            self.assertIn(runner, state["containers"])
            self.assertTrue(state["firewall"])
            self.assertEqual(state["network"], network)
            self.assertTrue(state["volumes"])
            self.assertFalse(any(args[:2] in (["network", "rm"], ["volume", "rm"]) or (args[0] == "run" and "remove" in args) for args, _ in self.engine.calls))
            self.assert_error("attempt_not_found", self.service.read_output, "analysis_1", "/workspace/checkpoint.json")
        finally:
            self.engine.removal_failures.clear()
        self.assertEqual(self.service.stop("analysis_1")["status"], "cancelled")
        self.assertTrue(state["purged"])
        self.assertFalse(state["firewall"])
        self.assertFalse(any(self.engine.resources.values()))

    def test_network_and_volume_removal_failures_remain_retryable(self):
        for kind in ("network", "volume"):
            with self.subTest(kind=kind):
                state = self.start_analysis("cleanup_" + kind)
                name = state["prefix"] + ("-net" if kind == "network" else "-work")
                if kind == "network":
                    state["network"], state["bridge"], state["firewall"] = name, "gtw0123456789", True
                    self.engine.resources[kind].add(name)
                self.engine.removal_failures[kind, name] = "nonzero"
                try:
                    self.assert_error("cleanup_pending", self.service.stop, state["attempt_id"])
                    self.assertFalse(state["purged"])
                    self.assertIn(name, self.engine.resources[kind])
                    if kind == "network":
                        self.assertEqual(state["network"], name)
                        self.assertTrue(state["firewall"])
                    else:
                        self.assertIn(name, state["volumes"])
                finally:
                    self.engine.removal_failures.clear()
                self.service.stop(state["attempt_id"])
                self.assertTrue(state["purged"])
                self.assertFalse(any(self.engine.resources.values()))

    def test_failed_remove_is_idempotent_only_after_successful_exact_absence_check(self):
        state = self.start_analysis()
        target = state["prefix"] + "-code-12345678"
        neighbor = target + "-different"
        self.engine.resources["container"].update((target, neighbor))
        self.engine.removal_failures["container", target] = "gone"
        try:
            self.engine.listing_failure = True
            self.assertFalse(self.service.remove_resource("container", target))
            self.engine.listing_failure = False
            self.assertTrue(self.service.remove_resource("container", target))
            self.assertIn(neighbor, self.engine.resources["container"])
        finally:
            self.engine.listing_failure = False
            self.engine.removal_failures.clear()
            self.engine.resources["container"].discard(neighbor)

    def test_tool_cleanup_failure_does_not_publish_and_watchdog_keeps_saved_work(self):
        state = self.start_analysis()
        original_call = self.engine.call
        def fail_code_removal(args, **options):
            if args[0] == "create":
                name = args[args.index("--name") + 1]
                if "-code-" in name:
                    self.engine.removal_failures["container", name] = "nonzero"
            return original_call(args, **options)
        try:
            with patch.object(self.engine, "call", side_effect=fail_code_removal):
                self.assert_error("cleanup_pending", self.service.tool, "analysis_1", "run_python", {"code": "print('must not publish')"})
            self.assertEqual(state["status"], "failed")
            self.assertIsNone(state["result"])
            self.assertTrue(state["quiesce_pending"])
            self.assertTrue(any("-code-" in name for name in state["containers"]))
        finally:
            self.engine.removal_failures.clear()
        with patch.object(self.service.closed, "wait", side_effect=[False, True]):
            self.service.watchdog()
        self.assertFalse(any("-code-" in name for name in state["containers"]))
        self.assertIn(state["prefix"] + "-loader", self.engine.resources["container"])
        self.assertIn(state["prefix"] + "-work", self.engine.resources["volume"])
        self.assertFalse(state["purged"])

    def test_watchdog_retries_cancelled_cleanup_and_survives_transient_failure(self):
        state = self.start_analysis()
        runner = state["prefix"] + "-runner"
        state["containers"].add(runner)
        self.engine.resources["container"].add(runner)
        state["closing"] = True
        state["status"], state["finished"] = "cancelled", self.clock()
        self.engine.removal_failures["container", runner] = "timeout"
        rounds = []
        def tick(_timeout):
            rounds.append(len(rounds))
            if len(rounds) == 2:
                self.assertFalse(state["purged"])
                self.assertIn(runner, state["containers"])
                self.engine.removal_failures.clear()
            return len(rounds) > 2
        try:
            with patch.object(self.service.closed, "wait", side_effect=tick):
                self.service.watchdog()
        finally:
            self.engine.removal_failures.clear()
        self.assertEqual(len(rounds), 3)
        self.assertTrue(state["purged"])
        self.assertNotIn(runner, self.engine.resources["container"])

    def test_relay_probe_retries_only_local_socket_before_runner_start(self):
        self.engine.probe_failures = 2
        payload = {"attempt_id": "harness_ready", "run_id": "run_1", "mode": "harness", "model_id": "configured-model",
                   "system_prompt": "fixed instructions", "prompt": "question", "capability": CAPABILITY,
                   "tools": [{"name": "query_dataset", "description": "Read data", "parameters": {"type": "object"}}]}
        with patch.object(manager.time, "sleep"):
            self.service.start(payload)
            self.assertTrue(self.engine.runner_removed.wait(2))
        probes = [(index, args, opts) for index, (args, opts) in enumerate(self.engine.calls) if args[0] == "exec" and args[1].endswith("-relay")]
        self.assertEqual(len(probes), 3)
        for _, args, opts in probes:
            self.assertEqual(args[1], self.service.attempts["harness_ready"]["prefix"] + "-relay")
            self.assertEqual(args[2:4], ["python", "-c"])
            self.assertEqual(args[4], "import socket; socket.create_connection(('127.0.0.1', 9000), timeout=0.5).close()")
            self.assertLessEqual(opts["timeout"], 1)
        runner_create = next(index for index, (args, _) in enumerate(self.engine.calls) if args[0] == "create" and args[args.index("--name") + 1].endswith("-runner"))
        self.assertLess(probes[-1][0], runner_create)
        self.assertEqual(self.service.status("harness_ready")["status"], "completed")

    def test_relay_probe_has_ten_second_bound_and_rechecks_cancellation(self):
        state = self.start_analysis()
        self.engine.probe_failures = 100
        with patch.object(manager.time, "monotonic", side_effect=[0, 0, 9.9, 10.1]), patch.object(manager.time, "sleep"):
            self.assert_error("relay_not_ready", self.service.wait_for_relay, state, state["prefix"] + "-relay")
        count = sum(args[0] == "exec" for args, _ in self.engine.calls)
        self.assertEqual(count, 1)
        self.engine.probe_failures = 1
        with patch.object(manager.time, "sleep", side_effect=lambda _: state.update(closing=True)):
            self.assert_error("attempt_not_found", self.service.wait_for_relay, state, state["prefix"] + "-relay")
        self.assertEqual(sum(args[0] == "exec" for args, _ in self.engine.calls), count + 1)

    def test_inputs_are_hash_checked_immutable_and_copied_as_unprivileged_regular_files(self):
        state = self.start_analysis()
        raw = b'{"review":"whole original record"}\n'
        digest = hashlib.sha256(raw).hexdigest()
        result = self.service.put_input("analysis_1", "part-001.jsonl", raw, digest)
        copies = [(args, options) for args, options in self.engine.calls if args[0] == "cp"]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0][0], ["cp", "-", state["prefix"] + "-loader:/inputs"])
        with tarfile.open(fileobj=io.BytesIO(copies[0][1]["content"])) as archive:
            entries = archive.getmembers()
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertTrue(entry.isfile())
            self.assertEqual((entry.name, entry.uid, entry.gid, entry.mode), ("part-001.jsonl", 10000, 10000, 0o400))
            self.assertEqual(archive.extractfile(entry).read(), raw)
        self.assertEqual(result, {"analysis_path": "/inputs/part-001.jsonl", "bytes": len(raw), "sha256": digest})
        self.assertEqual(self.service.put_input("analysis_1", "part-001.jsonl", raw, digest), result)
        self.assertEqual(sum(args[0] == "cp" for args, _ in self.engine.calls), 1)
        self.assert_error("input_hash_mismatch", self.service.put_input, "analysis_1", "other.jsonl", raw, "0" * 64)
        self.assert_error("input_immutable", self.service.put_input, "analysis_1", "part-001.jsonl", b"changed")
        self.assertEqual(state["input_bytes"], len(raw))

    def test_input_per_file_total_size_and_paths_are_bounded(self):
        state = self.start_analysis()
        for name in ("../outside", "/absolute", ".hidden", "nested/file.json", "file\\escape", "a" * 161):
            with self.subTest(name=name), self.assertRaises(manager.ManagerError):
                self.service.put_input("analysis_1", name, b"x")
        self.assert_error("input_limit_exceeded", self.service.put_input, "analysis_1", "oversized", b"x" * (64 * manager.MIB + 1))
        state["input_bytes"] = 8 * manager.GIB - 3
        self.assert_error("input_quota_exceeded", self.service.put_input, "analysis_1", "overflow", b"1234")
        self.service.put_input("analysis_1", "last", b"123")
        self.assertEqual(state["input_bytes"], 8 * manager.GIB)
        self.assert_error("input_quota_exceeded", self.service.put_input, "analysis_1", "one_more", b"x")

    def test_cancellation_kills_running_tool_and_never_returns_late_output(self):
        self.start_analysis()
        self.engine.tool_release.clear()
        outcome = queue.Queue()
        def work():
            try:
                outcome.put(self.service.tool("analysis_1", "run_shell", {"command": "echo should-not-publish"}))
            except manager.ManagerError as error:
                outcome.put(error)
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self.assertTrue(self.engine.tool_entered.wait(1))
        stopped = self.service.stop("analysis_1")
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(stopped["status"], "cancelled")
        self.assertIsInstance(outcome.get(timeout=1), manager.ManagerError)
        with self.assertRaises(manager.ManagerError):
            self.service.read_output("analysis_1", "/workspace/result.json")

    def test_sdk_error_result_cannot_become_completed(self):
        state = self.start_analysis("sdk_error")
        self.engine.runner_result = manager.json_bytes({"result": {"answer": "Unfinished draft", "finish_reason": "error"}})
        self.service.monitor(state, state["prefix"] + "-runner")
        result = self.service.status("sdk_error")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "harness_failed")
        self.assertIsNone(result["result"])

    def test_tool_cannot_publish_if_deadline_passes_while_decoding_result(self):
        state = self.start_analysis(deadline_seconds=2)
        decode = manager.strict_json
        def expire_after_execution(raw):
            self.clock.value = state["deadline"] + 1
            return decode(raw)
        with patch.object(manager, "strict_json", side_effect=expire_after_execution):
            with self.assertRaises(manager.ManagerError):
                self.service.tool("analysis_1", "run_python", {"code": "print(1)"})

    def test_harness_result_before_deadline_cannot_complete_after_process_wait_expires(self):
        self.engine.wait_hook = lambda: setattr(self.clock, "value", 102.0)
        payload = {"attempt_id": "harness_1", "run_id": "run_1", "mode": "harness", "model_id": "configured-model",
                   "system_prompt": "fixed instructions", "prompt": "question", "capability": CAPABILITY, "deadline_seconds": 1,
                   "tools": [{"name": "query_dataset", "description": "Read data", "parameters": {"type": "object"}}]}
        self.service.start(payload)
        self.assertTrue(self.engine.runner_removed.wait(2))
        result = self.service.status("harness_1")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])
        runner = next(args for args, _ in self.engine.calls if args[0] == "create" and args[args.index("--name") + 1].endswith("-runner"))
        self.assertEqual(runner[runner.index("--dns") + 1], "127.0.0.1")
        dns_options = [runner[index + 1] for index, value in enumerate(runner) if value == "--dns-option"]
        self.assertEqual(set(dns_options), {"timeout:1", "attempts:1"})
        self.assertEqual(runner[runner.index("--add-host") + 1], "relay:172.31.100.2")
        self.assertEqual(runner[-1], CONFIG["runner_image"])
        copied = next(options["content"] for args, options in self.engine.calls if args[0] == "cp")
        with tarfile.open(fileobj=io.BytesIO(copied)) as archive:
            relay_config = json.load(archive.extractfile("relay.json"))
            self.assertEqual(relay_config["broker_base_url"], CONFIG["broker_base_url"])
            self.assertEqual(relay_config["tool_names"], ["query_dataset", "read_web_page", "search_web"])
            self.assertEqual(relay_config['max_concurrency'], 16)
            self.assertEqual(relay_config["tool_idle_timeout_seconds"], 330)
            self.assertEqual(relay_config["request_timeout_seconds"], 360)
            self.assertEqual(relay_config["model_idle_timeout_seconds"], 185)
            self.assertEqual(relay_config["model_request_timeout_seconds"], 300)
            self.assertEqual(relay_config["attempt_deadline_monotonic"], 101.0)

    def test_trusted_large_allowances_reach_runner_and_relay_together(self):
        payload = {"attempt_id": "wide_1", "run_id": "run_1", "mode": "harness", "model_id": "configured-model",
            "system_prompt": "fixed instructions", "prompt": "question", "capability": CAPABILITY, "deadline_seconds": 3600,
            "limits": {"context_window": 1_000_000, "max_output_tokens": 131072, "model_timeout_seconds": 1200},
            "tools": [{"name": "query_dataset", "description": "Read data", "parameters": {"type": "object"}}]}
        self.service.start(payload)
        self.assertTrue(self.engine.runner_removed.wait(2))
        copied = next(options["content"] for args, options in self.engine.calls if args[0] == "cp")
        with tarfile.open(fileobj=io.BytesIO(copied)) as archive:
            request = json.load(archive.extractfile("request.json"))
            relay_config = json.load(archive.extractfile("relay.json"))
        self.assertEqual((request["context_window"], request["max_output_tokens"], request["request_timeout_seconds"]), (1_000_000, 131072, 3630))
        self.assertEqual((relay_config["request_timeout_seconds"], relay_config["model_idle_timeout_seconds"], relay_config["model_request_timeout_seconds"]), (1260, 1205, 1230))

    def test_research_context_policy_and_original_document_reach_runner_without_changing_capacity(self):
        document = json.dumps({"user_question": "保留全部约束", "conversation": [
            {"role": "user", "content": "不采样"}, {"role": "assistant", "content": "完整旧答复" * 1000}]}, ensure_ascii=False)
        payload = {"attempt_id": "context_1", "run_id": "run_1", "mode": "harness", "model_id": "configured-model",
            "system_prompt": "fixed instructions", "prompt": "question with file index", "context_document": document,
            "capability": CAPABILITY, "deadline_seconds": 3600,
            "limits": {"context_window": 1_000_000, "research_context_tokens": 200000, "max_output_tokens": 131072, "model_timeout_seconds": 1200},
            "tools": [{"name": "query_dataset", "description": "Read data", "parameters": {"type": "object"}}]}
        self.service.start(payload)
        self.assertTrue(self.engine.runner_removed.wait(2))
        copies = [(index, args[-1], options["content"]) for index, (args, options) in enumerate(self.engine.calls) if args[0] == "cp"]
        input_index, _, raw = next(item for item in copies if item[1].endswith(":/inputs"))
        filename = "research-context-context_1.json"
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            self.assertEqual(archive.getnames(), [filename])
            self.assertEqual(archive.extractfile(filename).read(), document.encode())
        _, _, raw = next(item for item in copies if item[1].endswith(":/run/voxquarry"))
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            request = json.load(archive.extractfile("request.json"))
        self.assertEqual((request["context_window"], request["research_context_tokens"]), (1000000, 200000))
        runner_index = next(index for index, (args, _) in enumerate(self.engine.calls)
                            if args[0] == "create" and args[args.index("--name") + 1].endswith("-runner"))
        self.assertLess(input_index, runner_index)
        state = self.service.attempts["context_1"]
        self.assertEqual(state["input_bytes"], len(document.encode()))
        self.assertEqual(state["inputs"][filename], {"analysis_path": "/inputs/" + filename,
            "bytes": len(document.encode()), "sha256": hashlib.sha256(document.encode()).hexdigest()})

    def test_research_context_allowance_and_document_reject_invalid_values_before_setup(self):
        base = {"context_window": 200000, "max_output_tokens": 16384, "model_timeout_seconds": 180}
        for value in (True, False, None, "200000", 0, -1, 200001, 1.5):
            with self.subTest(value=value):
                self.assert_error("attempt_limits_invalid", self.service.start, {"attempt_id": "invalid_1", "run_id": "run_1",
                    "mode": "analysis", "limits": {**base, "research_context_tokens": value}})
        self.assert_error("attempt_limits_invalid", self.service.start, {"attempt_id": "invalid_1", "run_id": "run_1",
            "mode": "analysis", "limits": {**base, "unknown": 1}})
        for value in (None, {}, [], "", "x" * 1000001, "null", "[]", '{"duplicate":1,"duplicate":2}', '{"value":NaN}'):
            with self.subTest(value_type=type(value).__name__):
                self.assert_error("attempt_context_invalid", self.service.start, {"attempt_id": "invalid_1", "run_id": "run_1",
                    "mode": "harness", "context_document": value})
        self.assertFalse(self.service.attempts)

    def test_runtime_diagnostics_accept_only_bounded_counts_and_consistent_archive_totals(self):
        good = {"version": 1, "archive": {"events": 3, "attempts": 4, "failures": 1, "total_ms": 9.5, "max_ms": 8.0}}
        variants = [good, {"version": 1, "archive": {"events": 0, "attempts": 0, "failures": 0, "total_ms": 0, "max_ms": 0}}]
        variants += [{**good, "version": value} for value in (True, 1.0, 2, "1")]
        variants += [{**good, "private": "PRIVATE_BODY"}, {"version": 1}, {"version": 1, "archive": []}]
        for key, value in (("events", True), ("attempts", 1.0), ("failures", -1), ("events", 10**9 + 1),
                           ("total_ms", True), ("total_ms", "PRIVATE_BODY"), ("max_ms", 10**9 + 1),
                           ("attempts", 2), ("attempts", 10), ("failures", 4), ("max_ms", 10)):
            variants.append({"version": 1, "archive": {**good["archive"], key: value}})
        variants.append({"version": 1, "archive": {**good["archive"], "unexpected": "PRIVATE_BODY"}})
        variants.append({"version": 1, "archive": {key: value for key, value in good["archive"].items() if key != "events"}})
        for index, timing in enumerate(variants):
            with self.subTest(index=index):
                state = self.start_analysis(attempt="diagnostic_" + str(index))
                self.engine.runner_result = manager.json_bytes({"result": {"answer": "synthetic", "finish_reason": "completed",
                    "runtime_diagnostics": timing}}) + b"\n"
                self.service.monitor(state, state["prefix"] + "-runner")
                result = self.service.status(state["attempt_id"])
                if index < 2:
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["result"]["runtime_diagnostics"], timing)
                else:
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["error"]["code"], "runner_result_invalid")
                    self.assertIsNone(result["result"])
                    self.assertNotIn("PRIVATE_BODY", json.dumps(result))

    def test_runner_error_type_is_preserved_without_exception_content(self):
        for index, error_type in enumerate(("RequestTimeoutError", "APIError", "bad type /private/key", "x" * 65, None, {})):
            with self.subTest(error_type=error_type):
                state = self.start_analysis(attempt="runner_error_" + str(index))
                self.engine.runner_result = manager.json_bytes({"error": {"type": error_type,
                    "message": "PRIVATE_EXCEPTION_BODY", "payload": {"key": "PRIVATE_SECRET"}}}) + b"\n"
                self.service.monitor(state, state["prefix"] + "-runner")
                result = self.service.status(state["attempt_id"])
                expected = {"code": "harness_failed"}
                if index < 2:
                    expected["runner_error_type"] = error_type
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"], expected)
                self.assertIsNone(result["result"])
                self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_failed_checkpoint_can_be_recovered_but_stopped_attempt_cannot_be_read(self):
        state = self.start_analysis()
        self.service.fail(state, "attempt_deadline_exceeded")
        content = b"saved checkpoint"
        self.engine.worker_result = {"result": {"base64": base64.b64encode(content).decode(), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}}
        self.assertEqual(self.service.read_output("analysis_1", "/workspace/checkpoint.json"), content)
        self.assert_error("attempt_inactive", self.service.tool, "analysis_1", "run_python", {"code": "print(1)"})
        self.engine.worker_result["result"]["sha256"] = "0" * 64
        self.assert_error("analysis_result_invalid", self.service.read_output, "analysis_1", "/workspace/checkpoint.json")
        self.service.stop("analysis_1")
        with self.assertRaises(manager.ManagerError):
            self.service.read_output("analysis_1", "/workspace/checkpoint.json")

    def test_request_cannot_choose_engine_image_mount_network_or_private_tool(self):
        payload = {"attempt_id": "analysis_bad", "run_id": "run_1", "mode": "analysis"}
        for field in ("docker_host", "image", "mounts", "network", "entrypoint"):
            with self.subTest(field=field):
                self.assert_error("attempt_request_invalid", self.service.start, {**payload, field: "untrusted"})
        self.start_analysis()
        for name in ("docker", "../run_shell", "_read_output", "run_shell;id"):
            with self.subTest(name=name):
                self.assert_error("analysis_tool_rejected", self.service.tool, "analysis_1", name, {})
        self.assertFalse(any(args[0] == "create" and "-code-" in " ".join(args) for args, _ in self.engine.calls))


class APIStub:
    def __init__(self):
        self.calls = []

    def status(self, attempt_id, after=0):
        self.calls.append(("status", attempt_id, after))
        return {"attempt_id": attempt_id, "status": "running", "events": [], "cursor": after}

    def start(self, value):
        self.calls.append(("start", value))
        return {"attempt_id": value["attempt_id"], "status": "running"}


class ManagerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.deny_process = patch.object(manager.subprocess, "Popen", side_effect=AssertionError("No subprocess is permitted"))
        self.deny_process.start()
        self.addCleanup(self.deny_process.stop)
        self.api = APIStub()
        self.server = manager.make_server(self.api, TOKEN, ("127.0.0.1", 0))
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(1)

    def request(self, path="/v1/attempts/attempt_1", *, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_valid_management_identity_and_cursor_are_forwarded(self):
        status, body = self.request("/v1/attempts/attempt_1?after=10")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["cursor"], 10)
        self.assertEqual(self.api.calls, [("status", "attempt_1", 10)])

    def test_wrong_or_duplicate_management_authorization_never_dispatches(self):
        for token in ("", "Bearer different-management-token", "Basic " + TOKEN, "Bearer " + TOKEN + " "):
            with self.subTest(token=token):
                status, body = self.request(headers={"Authorization": token})
                self.assertEqual(status, 401)
                self.assertNotIn(TOKEN.encode(), body)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.putrequest("GET", "/v1/attempts/attempt_1")
            connection.putheader("Authorization", "Bearer " + TOKEN)
            connection.putheader("Authorization", "Bearer different-attempt")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            connection.close()
        self.assertEqual(self.api.calls, [])

    def test_untrusted_paths_and_queries_never_dispatch(self):
        for path in ("/v1/attempts/attempt_1?after=1&after=2", "/v1/attempts/attempt_1?after=-1", "/v1/attempts/attempt_1?engine=business",
                     "/v1/attempts/%2e%2e", "/v1/attempts/../files", "http://evil.invalid/v1/attempts/attempt_1", "/v1/attempts/attempt_1#fragment",
                     "/v1/attempts/attempt_1/", "//v1/attempts/attempt_1"):
            with self.subTest(path=path):
                self.assertGreaterEqual(self.request(path)[0], 400)
        self.assertEqual(self.api.calls, [])

    def test_oversized_and_ambiguous_requests_are_rejected_before_body_read(self):
        status, _ = self.request("/v1/attempts", method="POST", body=b"", headers={"Content-Length": str(4 * manager.MIB + 1)})
        self.assertEqual(status, 413)
        status, _ = self.request("/v1/attempts/attempt_1/inputs/part.jsonl", method="POST", body=b"", headers={"Content-Length": str(64 * manager.MIB + 1)})
        self.assertEqual(status, 413)
        status, _ = self.request(method="GET", headers={"Transfer-Encoding": "chunked"})
        self.assertGreaterEqual(status, 400)
        self.assertEqual(self.api.calls, [])


class DockerInvocationTests(unittest.TestCase):
    def test_spawn_ignores_user_docker_context_and_never_invokes_a_shell(self):
        engine = manager.DockerEngine()
        with patch.dict(manager.os.environ, {"DOCKER_HOST": "ssh://business", "DOCKER_CONTEXT": "business", "PRIVATE_KEY": "not-forwarded"}), patch.object(manager.subprocess, "Popen") as spawn:
            engine.spawn(["info"])
        args, kwargs = spawn.call_args
        self.assertEqual(args[0], ["docker", "--host", "unix:///var/run/docker.sock", "info"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["env"], {"PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/state", "LANG": "C.UTF-8"})
        self.assertEqual(kwargs["stderr"], manager.subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
