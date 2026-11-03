from __future__ import annotations

import hashlib
import secrets
from typing import Any, Mapping

from .constants import AUDIT_SCHEMA_VERSION, POLICY_VERSION, SENSITIVE_AUDIT_KEYS
from .contracts import canonical_json, format_utc, utc_now


REDACTED = "[REDACTED]"


def redact(value: Any, key: str = "") -> Any:
    normalized = key.casefold().replace("-", "_")
    if normalized in SENSITIVE_AUDIT_KEYS or any(
        marker in normalized for marker in ("password", "secret", "authorization", "cookie")
    ):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(item_key): redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 2048:
        return value[:2048] + "…"
    return value


def build_audit_event(
    *,
    actor: Mapping[str, Any],
    session_id: str | None,
    request_id: str,
    correlation_id: str,
    module: str,
    action: str,
    resource_type: str,
    resource_id: str,
    result: str,
    reason_code: str,
    client_ip: str,
    user_agent: str,
    changes: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    policy_version: str = POLICY_VERSION,
) -> dict[str, Any]:
    if result not in {"success", "denied", "failure"}:
        raise ValueError("invalid audit result")
    event = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_id": f"aud_{secrets.token_hex(16)}",
        "occurred_at": format_utc(utc_now()),
        "actor_principal_id": str(actor.get("principal_id") or "system_local_fixture"),
        "actor_employee_id": str(actor.get("employee_id") or ""),
        "actor_display_name": str(actor.get("display_name") or "Synthetic Local System"),
        "actor_roles": sorted(set(str(role) for role in actor.get("roles") or [])),
        "session_id": session_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "module": module,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "result": result,
        "reason_code": reason_code,
        "client_ip": client_ip[:64],
        "user_agent": user_agent[:256],
        "policy_version": policy_version,
        "changes": redact(dict(changes or {})),
        "metadata": redact(dict(metadata or {})),
    }
    return event


def event_hash(event: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(dict(event))).hexdigest()
