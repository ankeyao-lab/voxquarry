from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping


CORE_CONTRACT = "voxquarry-wenquan-core/v1"
RESPONSE_CONTRACT = "voxquarry-agent-response/v1"
TOOL_REGISTRY_CONTRACT = "voxquarry-agent-tools/v1"
IDENTITY_CONTRACT = "identity-context/v1"

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._:@/-]{0,159}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|authorization)\s*[:=]", re.I),
    re.compile(r"\b(?:bearer)\s+[A-Za-z0-9._~+/-]{12,}", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:password|passwd)\s*[:=]", re.I),
)


class AgentError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 422,
        retryable: bool = False,
        safe_next_step: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.safe_next_step = safe_next_step


@dataclass(frozen=True)
class IdentityContext:
    actor_id: str
    owner_user_id: str
    scope_id: str
    authority_id: str
    roles: tuple[str, ...] = ()
    allowed_report_ids: frozenset[str] | None = frozenset()
    allow_archived_reports: bool = False
    mode: str = "authenticated"

    def __post_init__(self) -> None:
        for name, value in (
            ("actor_id", self.actor_id),
            ("owner_user_id", self.owner_user_id),
            ("scope_id", self.scope_id),
            ("authority_id", self.authority_id),
        ):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise AgentError("identity_invalid", f"{name} 无效", http_status=401)
        if self.mode not in {"authenticated", "synthetic"}:
            raise AgentError("identity_invalid", "identity mode 无效", http_status=401)
        if any(not isinstance(role, str) or not _ID.fullmatch(role) for role in self.roles):
            raise AgentError("identity_invalid", "identity roles 无效", http_status=401)
        if self.allowed_report_ids is not None and any(
            not isinstance(value, str) or not value.startswith("rpt_")
            for value in self.allowed_report_ids
        ):
            raise AgentError("identity_invalid", "report allowlist 无效", http_status=401)

    def public(self) -> dict[str, Any]:
        return {
            "contract": IDENTITY_CONTRACT,
            "actor_id": self.actor_id,
            "owner_user_id": self.owner_user_id,
            "scope_id": self.scope_id,
            "authority_id": self.authority_id,
            "roles": list(self.roles),
            "report_scope_mode": (
                "all_reports" if self.allowed_report_ids is None else "allowlist"
            ),
            "allowed_report_count": (
                None if self.allowed_report_ids is None else len(self.allowed_report_ids)
            ),
            "allow_archived_reports": self.allow_archived_reports,
            "mode": self.mode,
        }


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9]{1,15}", prefix):
        raise ValueError("invalid id prefix")
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_sha256(value: str, field: str = "hash") -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise AgentError("invalid_request", f"{field} 必须是 sha256 内容哈希")
    return value


def normalize_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise AgentError("invalid_request", f"{field} 必须是字符串")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise AgentError("invalid_request", f"{field} 长度必须在 1–{maximum}")
    return normalized


def reject_sensitive_text(value: str, *, field: str) -> None:
    if any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS):
        raise AgentError(
            "sensitive_content_rejected",
            f"{field} 可能包含凭据或敏感认证内容，未保存",
            http_status=422,
        )


def require_exact_fields(
    value: object,
    *,
    allowed: Iterable[str],
    required: Iterable[str] = (),
    label: str = "对象",
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise AgentError("invalid_request", f"{label}必须是 JSON 对象")
    allowed_set = set(allowed)
    extras = sorted(set(value) - allowed_set)
    if extras:
        raise AgentError("invalid_request", f"{label}包含未知字段：{', '.join(extras)}")
    missing = sorted(set(required) - set(value))
    if missing:
        raise AgentError("invalid_request", f"{label}缺少字段：{', '.join(missing)}")
    return value


def success_envelope(
    *,
    request_id: str,
    audit_id: str,
    data: Any,
    citations: list[dict[str, Any]] | None = None,
    limitations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "contract": RESPONSE_CONTRACT,
        "request_id": request_id,
        "status": "ok",
        "data": data,
        "citations": citations or [],
        "limitations": limitations or [],
        "error": None,
        "audit_id": audit_id,
    }


def error_envelope(
    *, request_id: str, audit_id: str | None, error: AgentError
) -> dict[str, Any]:
    return {
        "contract": RESPONSE_CONTRACT,
        "request_id": request_id,
        "status": "error",
        "data": None,
        "citations": [],
        "limitations": [],
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "safe_next_step": error.safe_next_step,
        },
        "audit_id": audit_id,
    }
