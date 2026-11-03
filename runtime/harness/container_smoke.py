"""Opt-in synthetic manager-container and Mac-loopback transport verification."""
from __future__ import annotations

import argparse
import json
import io
import os
from pathlib import Path
import secrets
import threading
import time
import tarfile

from vm_smoke import DedicatedTestEngine, FAKE_BROKER, HarnessManager, manager_module, wait_until


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-engine-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("WENQUAN_RUN_VM_SMOKE") != "1":
        raise SystemExit("Explicit dedicated synthetic VM smoke opt-in required")
    engine = DedicatedTestEngine(); engine.guard({"expected_engine_id": args.expected_engine_id})
    images = {kind + "_image": engine.call(["image", "inspect", "--format", "{{.Id}}", f"voxquarry-{kind}:development"]).decode().strip()
              for kind in ("analysis", "runner", "relay", "manager")}
    # This server is bound only to Mac loopback. The relay must reach it through
    # the actual Colima host.lima.internal path; there is no provider or real key.
    scope = {}
    exec(FAKE_BROKER.rsplit("ThreadingHTTPServer(", 1)[0], scope)
    broker = scope["ThreadingHTTPServer"](("127.0.0.1", 19191), scope["Handler"])
    threading.Thread(target=broker.serve_forever, daemon=True).start()
    prefix = "voxquarry-synthetic-manager"
    volume, loader, container = prefix + "-config", prefix + "-loader", prefix
    token = secrets.token_urlsafe(48)
    client = HarnessManager("http://127.0.0.1:19291", token)
    scope['native_bridge'] = lambda payload: client.native_io(attempt, payload)
    config = {**images, "expected_engine_id": args.expected_engine_id,
              "broker_base_url": "http://host.lima.internal:19191/wenquan/internal/broker"}
    evidence = {"engine_id": args.expected_engine_id, "images": images, "real_model_calls": 0}
    try:
        engine.call(["volume", "create", volume])
        create = manager_module.Manager.container(loader, memory="128m", cpus="0.5", pids="16")
        index = create.index("--label"); del create[index:index + 2]
        engine.call(create + ["--mount", f"type=volume,src={volume},dst=/run/voxquarry",
                            "--entrypoint", "python", images["analysis_image"], "-c", "import time;time.sleep(300)"])
        engine.call(["start", loader])
        archive_data = io.BytesIO()
        with tarfile.open(fileobj=archive_data, mode="w") as archive:
            for name, content in {"manager.json": manager_module.json_bytes(config), "management-token": token.encode()}.items():
                entry = tarfile.TarInfo(name); entry.size = len(content); entry.mode = 0o400
                # Trusted manager runs as root without DAC override; its private
                # config is root-owned, unlike uid 10000 runner/input files.
                entry.uid = entry.gid = 0
                archive.addfile(entry, io.BytesIO(content))
        engine.call(["cp", "-", loader + ":/run/voxquarry"], content=archive_data.getvalue())
        create = manager_module.Manager.container(container, memory="512m", cpus="1", network="bridge")
        index = create.index("--label"); del create[index:index + 2]
        engine.call(create + ["--user", "0:0", "--publish", "127.0.0.1:19291:9001",
                            "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
                            "--mount", f"type=volume,src={volume},dst=/run/voxquarry,readonly",
                            "--tmpfs", "/state:rw,nosuid,nodev,noexec,size=64m,mode=0700", images["manager_image"]])
        engine.call(["start", container])
        def reachable():
            try:
                client.status("synthetic_missing")
            except Exception as exc:
                return getattr(exc, "code", None) == "attempt_not_found"
            return False
        wait_until(reachable, timeout=30)
        identity = json.loads(engine.call(["exec", container, "docker", "--host", "unix:///var/run/docker.sock", "info", "--format", "{{json .}}"], limit=1024 * 1024))
        assert identity["ID"] == args.expected_engine_id and identity["Name"] == "colima-voxquarry"
        evidence["manager_container"] = {"engine_name": identity["Name"], "actual_image_cli": True, "loopback_api": True}
        attempt = "synthetic_container_roundtrip"
        client.start({"attempt_id": attempt, "run_id": "synthetic_run", "model_id": "deepseek-v4.1-flash", "deadline_seconds": 90,
                      "system_prompt": "Use the synthetic probe.", "prompt": "Run the probe.", "capability": "synthetic-" + "c" * 48,
                      "tools": [{"name": "probe", "description": "Synthetic no-data probe", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]})
        result = wait_until(lambda: (value if (value := client.status(attempt))["status"] != "running" else None), timeout=70)
        assert result["status"] == "completed", result
        assert result["result"]["answer"] == "隔离执行验证通过", result
        assert scope["calls"] == 2 and scope["seen_tool"] is True
        assert scope["native_events"], "native committed events were not forwarded"
        assert all(e["session_id"] == attempt and isinstance(e["event"]["type"], str) for e in scope["native_events"])
        evidence["native_trajectory"] = {"events": len(scope["native_events"]), "types": sorted({e["event"]["type"] for e in scope["native_events"]}), "attempt_bound": True}
        evidence["cross_vm_loopback"] = {"broker": "127.0.0.1:19191", "relay_host": "host.lima.internal", "status": result["status"], "synthetic_model_calls": scope["calls"], "tool_roundtrip": scope["seen_tool"]}
        client.stop(attempt)
        assert not engine.call(["ps", "-aq", "--filter", "label=" + manager_module.LABEL]).strip()
        assert not engine.call(["volume", "ls", "-q", "--filter", "label=" + manager_module.LABEL]).strip()
        terminal_cases = []
        for mode, expected_calls in (("accepted_answer", 1), ("rejected_answer", 2)):
            scope.update(mode=mode, calls=0, submitted=[])
            attempt = "synthetic_" + mode
            client.start({"attempt_id": attempt, "run_id": "synthetic_run", "model_id": "deepseek-v4.1-flash", "deadline_seconds": 45,
                          "system_prompt": "Submit the synthetic answer. Repair a rejected draft.", "prompt": "Run the terminal tool fixture.", "capability": "synthetic-" + "c" * 48,
                          "tools": [{"name": "submit_answer", "description": "Synthetic acceptance check", "parameters": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}}]})
            result = wait_until(lambda: (value if (value := client.status(attempt))["status"] != "running" else None), timeout=40)
            assert result["status"] == "completed", result
            assert scope["calls"] == expected_calls, "accepted draft triggered another model completion"
            assert len(scope["submitted"]) == expected_calls and scope["submitted"][-1] == "synthetic saved answer"
            terminal_cases.append({"case": mode, "status": result["status"], "synthetic_model_calls": scope["calls"],
                                   "accepted_draft_preserved": True, "no_redundant_completion": True})
            client.stop(attempt)
            assert not engine.call(["ps", "-aq", "--filter", "label=" + manager_module.LABEL]).strip()
            assert not engine.call(["network", "ls", "-q", "--filter", "label=" + manager_module.LABEL]).strip()
            assert not engine.call(["volume", "ls", "-q", "--filter", "label=" + manager_module.LABEL]).strip()
        evidence["native_tool_conclusion"] = terminal_cases
        session_key = "wq_" + __import__('hashlib').sha256(b'isolated-a1-container-fixture').hexdigest()
        continuity = []
        for mode in ('native', 'continuity'):
            scope.update(mode=mode, calls=0, seen_requests=[], native_events=[])
            attempt = 'synthetic_' + mode
            client.start({"attempt_id": attempt, "run_id": "synthetic_run", "session_key": session_key,
                "model_id": "deepseek-v4.1-flash", "reasoning_effort": "max", "deadline_seconds": 90,
                "system_prompt": "Synthetic native SDK verification.", "prompt": mode,
                "capability": "synthetic-" + "c" * 48,
                "tools": [{"name": "probe", "description": "Synthetic unused business tool", "parameters": {"type": "object", "properties": {}}}]})
            result = wait_until(lambda: (value if (value := client.status(attempt))["status"] != "running" else None), timeout=80)
            assert result['status'] == 'completed', result
            tool_results = [e['event']['data']['message'] for e in scope['native_events'] if e['event']['type'] == 'tool/result']
            assert len(tool_results) == (2 if mode == 'native' else 1), tool_results
            assert all(not block.get('isError') for message in tool_results for block in message['content']), tool_results
            assert ('isolation-ok' if mode == 'native' else 'continuity-ok') in json.dumps(tool_results), tool_results
            messages = json.dumps(scope['seen_requests'][-1]['messages'])
            assert ('isolation-ok' if mode == 'native' else 'continuity-ok') in messages, messages
            if mode == 'continuity':
                assert 'native-persistent-evidence' in messages, 'prior native results were not restored'
            assert all(e['session_id'] == session_key for e in scope['native_events'])
            continuity.append({'case': mode, 'status': result['status'], 'synthetic_model_calls': scope['calls']})
            client.stop(attempt)
            assert not engine.call(['ps', '-aq', '--filter', 'label=' + manager_module.LABEL]).strip()
        evidence['native_provider_continuity'] = continuity
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(json.dumps({"status": "passed", "evidence": str(args.output), "real_model_calls": 0}))
    finally:
        for attempt in ("synthetic_container_roundtrip", "synthetic_accepted_answer", "synthetic_rejected_answer", 'synthetic_native', 'synthetic_continuity'):
            try:
                client.stop(attempt)
            except Exception:
                pass
        # Delete only this deterministic synthetic session's volumes.
        import hashlib
        for suffix in ('inputs', 'work', 'state'):
            engine.call(['volume', 'rm', 'gtw-session-' + hashlib.sha256(b'isolated-a1-container-fixture').hexdigest() + '-' + suffix], check=False)
        engine.call(["rm", "-f", container, loader], check=False)
        engine.call(["volume", "rm", volume], check=False)
        broker.shutdown(); broker.server_close()


if __name__ == "__main__":
    main()
