"""Owner-bound projections of native Harness events and persisted diagnostics."""
from __future__ import annotations

import re
import math
from .contracts import AgentError, sha256_value


def native_timing(value):
    """Only scalar timing/identity data for the read-only overview, never content."""
    result = {"timing_version": 1}
    at = value.get("time")
    if type(at) in {int, float} and math.isfinite(at) and at >= 0:
        result["native_time_ms"] = at
    data = value.get("data")
    data = data if isinstance(data, dict) else {}
    kind = value.get("type")
    call_id = data.get("callId") if kind == "tool/call" else None
    if kind == "tool/result":
        message = data.get("message")
        message = message if isinstance(message, dict) else {}
        source = message.get("source")
        call_id = source.get("callId") if isinstance(source, dict) else None
        blocks = message.get("content")
        if isinstance(blocks, list):
            result["tool_error"] = any(isinstance(b, dict) and b.get("isError") is True for b in blocks)
    if isinstance(call_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", call_id):
        result["native_call_id"] = call_id
    if kind == "tool/call" and isinstance(data.get("name"), str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,160}", data["name"]):
        result["native_tool_name"] = data["name"]
    return result


def trace_timing_page(store, actor, trace_id, *, after_seq=0):
    """Project retained older logs without changing them or bypassing ownership/expiry."""
    page = store.list_events(actor, trace_id, after_seq=after_seq)
    for item in page["items"]:
        data = item["data"]
        if (item["event_type"] != "research.native" or data.get("timing_version")
                or data.get("native_type") not in {"tool/call", "tool/result", "user/message", "system/message"}
                or not item.get("payload_ref")):
            continue
        try:
            payload = store.read_payload(actor, trace_id, item["payload_ref"], include_content=True)
            event = payload.get("data", {}).get("event", {})
            if isinstance(event, dict):
                item["data"] = {**data, **native_timing(event)}
        except AgentError:
            # Expired or unavailable bodies must not erase the durable event ledger.
            pass
    return page


def native_event(value):
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_./-]{0,100}", value["type"]):
        raise AgentError("trajectory_event_invalid", "轨迹事件格式无效")
    kind = value["type"]
    # Native assistant streams can use compact, version-specific encodings.
    # Do not guess which encoded blocks contain private provider reasoning.
    content_allowed = kind in {"user/message", "system/message", "tool/call", "tool/result", "compaction/summary"}
    metadata = {key: item for key, item in value.items()
                if key in {"id", "seq", "type", "time", "timestamp", "createdAt", "turnId", "stepId", "parentId", "status", "reason"}
                and (item is None or type(item) in {str, int, float, bool})}
    event = value if content_allowed else metadata
    return {"native_type": kind, "native_sequence": value.get("seq"), "native_event_id": value.get("id"),
            "content_saved": content_allowed, **native_timing(value)}, {"event": event}, "native:" + sha256_value(value).split(":")[-1]


def trace_overview(store, actor, trace_id):
    """Small durable overview; never loads model/tool payload bodies."""
    import json
    with store._connect() as db:
        trace = store._owned(db, actor, trace_id)
        rows = db.execute("SELECT event_type,received_at,data,attempt_id FROM agent_trace_events WHERE trace_id=? AND (event_type LIKE 'model.%' OR event_type LIKE 'lifecycle.%' OR event_type='research.native') ORDER BY seq", (trace_id,)).fetchall()
    calls, tokens, model_ms, native_count = 0, 0, 0, 0
    started = None
    latest = None
    for row in rows:
        data = json.loads(row["data"])
        kind = row["event_type"]
        if kind == "model.started":
            calls += 1
            started = {"call": data.get("call"), "started_at": row["received_at"], "attempt_id": row["attempt_id"]}
        elif kind == "model.finished":
            tokens += (data.get("usage") or {}).get("total_tokens", 0) or 0
            model_ms += data.get("duration_ms", 0) or 0
            if started and started["attempt_id"] == row["attempt_id"] and started["call"] == data.get("call"):
                started = None
        elif kind == "research.native":
            native_count += 1
        if kind in {"model.started", "model.progress", "model.finished"}:
            latest = {"type": kind, "at": row["received_at"], **data}
    return {"model_calls": calls, "total_tokens": tokens, "model_duration_ms": model_ms,
            "native_events": native_count, "active_call": started if trace["status"] == "running" else None,
            "latest_model_activity": latest, "status": trace["status"]}
