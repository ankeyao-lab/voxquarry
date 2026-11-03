from __future__ import annotations

from typing import Any, Mapping

from .constants import OWNER_SCOPE_SCHEMA_VERSION, TOKEN_USAGE_SCHEMA_VERSION


WENQUAN_RESOURCE_TYPES = frozenset({"session", "message", "memory", "decision", "tool_call"})


def owner_scope(principal_id: str) -> str:
    return f"principal:{principal_id}"


def authorize_wenquan_owner(
    identity: Mapping[str, Any],
    resource: Mapping[str, Any],
) -> bool:
    """Admin is deliberately not a content-owner bypass."""

    if resource.get("schema_version") != OWNER_SCOPE_SCHEMA_VERSION:
        return False
    if resource.get("resource_type") not in WENQUAN_RESOURCE_TYPES:
        return False
    principal_id = str(identity.get("principal_id") or "")
    return bool(
        principal_id
        and resource.get("owner_principal_id") == principal_id
        and resource.get("scope") == owner_scope(principal_id)
    )

def validate_token_usage_attribution(event: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    if event.get("schema_version") != TOKEN_USAGE_SCHEMA_VERSION:
        return False
    if event.get("principal_id") != identity.get("principal_id"):
        return False
    input_tokens = event.get("input_tokens")
    output_tokens = event.get("output_tokens")
    total_tokens = event.get("total_tokens")
    return (
        isinstance(input_tokens, int)
        and isinstance(output_tokens, int)
        and isinstance(total_tokens, int)
        and min(input_tokens, output_tokens) >= 0
        and total_tokens == input_tokens + output_tokens
    )
