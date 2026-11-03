from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .constants import (
    AUTH_SOURCE,
    IDENTITY_SCHEMA_VERSION,
    POLICY_VERSION,
    SERVICE_AUTH_SOURCE,
    SERVICE_AUTH_MAX_TTL_SECONDS,
    SERVICE_AUTH_SCHEMA_VERSION,
)


class ContractError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid token encoding") from exc


@dataclass(frozen=True)
class SignedContract:
    token: str
    claims: dict[str, Any]


class ContractSigner:
    """HMAC contract signer with explicit key IDs for bounded local rotation."""

    def __init__(self, keys: Mapping[str, bytes], active_key_id: str):
        self._keys = dict(keys)
        self.active_key_id = active_key_id
        if active_key_id not in self._keys:
            raise ContractError("active key is not in verifier keyring")
        if any(len(value) < 32 for value in self._keys.values()):
            raise ContractError("signing keys must be at least 32 bytes")

    def sign(self, claims: Mapping[str, Any]) -> SignedContract:
        payload = _b64encode(canonical_json(dict(claims)))
        signed = f"v1.{self.active_key_id}.{payload}".encode("ascii")
        signature = _b64encode(hmac.new(self._keys[self.active_key_id], signed, hashlib.sha256).digest())
        return SignedContract(token=f"v1.{self.active_key_id}.{payload}.{signature}", claims=dict(claims))

    def verify(
        self,
        token: str,
        *,
        expected_schema: str,
        now: datetime | None = None,
        expected_audience: str | None = None,
        required_scope: str | None = None,
        max_ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            raise ContractError("invalid signed contract format")
        _, key_id, payload, supplied_signature = parts
        key = self._keys.get(key_id)
        if key is None:
            raise ContractError("unknown signing key")
        signed = f"v1.{key_id}.{payload}".encode("ascii")
        expected_signature = _b64encode(hmac.new(key, signed, hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ContractError("contract integrity check failed")
        try:
            claims = json.loads(_b64decode(payload))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContractError("invalid contract payload") from exc
        if not isinstance(claims, dict) or claims.get("schema_version") != expected_schema:
            raise ContractError("unexpected contract schema")
        issued_at = parse_utc(str(claims.get("issued_at") or ""))
        expires_at = parse_utc(str(claims.get("expires_at") or ""))
        current = (now or utc_now()).astimezone(timezone.utc)
        if expires_at <= issued_at or current < issued_at - timedelta(seconds=30) or current >= expires_at:
            raise ContractError("contract is not currently valid")
        if max_ttl_seconds is not None and (expires_at - issued_at).total_seconds() > max_ttl_seconds:
            raise ContractError("contract TTL exceeds policy")
        if expected_audience is not None and claims.get("audience") != expected_audience:
            raise ContractError("service audience mismatch")
        if required_scope is not None and claims.get("scope") != required_scope:
            raise ContractError("service scope denied")
        return claims


def identity_context_claims(
    *,
    auth_source: str = AUTH_SOURCE,
    principal_id: str,
    employee_id: str,
    display_name: str,
    roles: list[str],
    session_id: str,
    request_id: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    issued = (now or utc_now()).astimezone(timezone.utc)
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "auth_source": auth_source,
        "non_production": True,
        "principal_id": principal_id,
        "employee_id": employee_id,
        "display_name": display_name,
        "roles": sorted(set(roles)),
        "session_id": session_id,
        "request_id": request_id,
        "issued_at": format_utc(issued),
        "expires_at": format_utc(issued + timedelta(seconds=ttl_seconds)),
        "policy_version": POLICY_VERSION,
    }


def service_auth_claims(
    *,
    service_principal_id: str,
    audience: str,
    scope: str,
    request_id: str,
    correlation_id: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not 1 <= ttl_seconds <= SERVICE_AUTH_MAX_TTL_SECONDS:
        raise ContractError("service credential TTL must be 1-300 seconds")
    issued = (now or utc_now()).astimezone(timezone.utc)
    return {
        "schema_version": SERVICE_AUTH_SCHEMA_VERSION,
        "auth_source": SERVICE_AUTH_SOURCE,
        "non_production": True,
        "service_principal_id": service_principal_id,
        "audience": audience,
        "scope": scope,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "issued_at": format_utc(issued),
        "expires_at": format_utc(issued + timedelta(seconds=ttl_seconds)),
        "policy_version": POLICY_VERSION,
    }
