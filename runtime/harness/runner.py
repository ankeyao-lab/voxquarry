"""Container-only adapter for the pinned official DeepSeek Harness SDK."""
from __future__ import annotations

import json
import os
import sys
import http.client
import time
from pathlib import Path


def archive_event(notification, token, session_id, *, diagnostics=None):
    """Persist each committed event outside the disposable runtime home."""
    if notification.method != "session.event" or notification.payload.get("sessionId") != session_id:
        return
    event = notification.payload.get("event")
    if not isinstance(event, dict):
        return
    started_at = time.monotonic()
    diagnostics = {} if diagnostics is None else diagnostics
    diagnostics["events"] = diagnostics.get("events", 0) + 1
    body = json.dumps({"session_id": session_id, "event": event}, ensure_ascii=False).encode()
    saved = False
    for attempt in range(3):
        diagnostics["attempts"] = diagnostics.get("attempts", 0) + 1
        connection = http.client.HTTPConnection("relay", 9000, timeout=5)
        try:
            connection.request("POST", "/broker/trajectory", body=body,
                               headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read(4096))
            if response.status == 200 and result.get("saved") is True:
                saved = True
                break
            if response.status in {401, 403, 409}:
                break  # Stopped/replaced executions cannot keep writing.
        except Exception:
            pass
        finally:
            connection.close()
        if attempt < 2:
            time.sleep(0.1 * (attempt + 1))
    elapsed_ms = round((time.monotonic() - started_at) * 1000, 3)
    diagnostics["total_ms"] = round(diagnostics.get("total_ms", 0) + elapsed_ms, 3)
    diagnostics["max_ms"] = max(diagnostics.get("max_ms", 0), elapsed_ms)
    if not saved:
        diagnostics["failures"] = diagnostics.get("failures", 0) + 1
        print(json.dumps({"event": {"kind": "trajectory.gap", "status": "archive_failed"}}), flush=True)


def public_event(notification):
    """Keep lifecycle data; never copy provider reasoning or whole SDK logs."""
    value = notification.payload
    if notification.method == "session.status":
        return {"kind": "status", "status": value.get("status")}
    if notification.method != "session.event":
        return None
    event = value.get("event", {})
    kind = event.get("type", "")
    if kind in {"turn/start", "turn/end", "agent/inbox/spliced"}:
        return {"kind": kind, "event_id": event.get("id"), "sequence": event.get("seq")}
    return None


def continuation_prompt(text, restoring):
    if not restoring:
        return text
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(value, dict):
        value.pop("conversation", None)
        return json.dumps(value, ensure_ascii=False)
    return text


def main():
    # The manager constructs this file; none of these values come from model
    # arguments. Real provider credentials never enter this container.
    config = json.loads(Path("/run/voxquarry/request.json").read_text())
    from deepseek_harness import DeepSeekHarness
    os.umask(0o077)
    token = Path("/run/voxquarry/capability").read_text().strip()
    os.environ["DSH_SYSTEM_PROMPT"] = config["system_prompt"]
    os.environ["DSH_CONTEXT_WINDOW"] = str(config.get("context_window", 200000))
    os.environ["DSH_RESEARCH_CONTEXT_TOKENS"] = str(config.get("research_context_tokens", 200000))
    allowed = {"PATH", "LANG", "HOME", "DSH_SYSTEM_PROMPT", "DSH_CONTEXT_WINDOW", "DSH_RESEARCH_CONTEXT_TOKENS"}
    for key in tuple(os.environ):
        if key not in allowed:
            del os.environ[key]

    archive_diagnostics = {"events": 0, "attempts": 0, "failures": 0, "total_ms": 0, "max_ms": 0}
    def notify(item):
        archive_event(item, token, config.get("session_key", config["attempt_id"]), diagnostics=archive_diagnostics)
        event = public_event(item)
        if event:
            print(json.dumps({"event": event}, ensure_ascii=False), flush=True)

    try:
        with DeepSeekHarness(
            provider="deepseek-official", model=config["model_id"],
            reasoning_effort=config.get("reasoning_effort", "max"), max_tokens=config.get("max_output_tokens", 16384),
            cwd="/workspace", dsh_home="/state/dsh", profile="sdk",
            patches=("/opt/voxquarry/research.patch.yml",),
            base_url="http://relay:9000/broker/model/v1", api_key=token,
            initialize_timeout_seconds=30, request_timeout_seconds=config.get("request_timeout_seconds", 600),
        ) as harness:
            from session_resume import resume_session, session_exists
            session_id = config.get("session_key", config["attempt_id"])
            restoring = session_exists(session_id)
            text = continuation_prompt(config["prompt"], restoring)
            if restoring:
                result = resume_session(harness, session_id, text, on_notification=notify)
            else:
                result = harness.run(text, session_id=session_id, on_notification=notify)
        print(json.dumps({"result": {"answer": result.final_response, "finish_reason": result.finish_reason,
                         "runtime_diagnostics": {"version": 1, "archive": archive_diagnostics}}}, ensure_ascii=False), flush=True)
    except Exception as exc:
        # Runtime diagnostics can contain provider payloads. The manager stores
        # error type, while trusted broker trace holds the safe detailed cause.
        print(json.dumps({"error": {"code": "harness_failed", "type": type(exc).__name__}}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
