from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Mapping

from account_admin.constants import IDENTITY_SCHEMA_VERSION
from account_admin.contracts import ContractError, ContractSigner
from account_admin.gateway import BackendIdentityAuthorizer
from account_admin.ownership import owner_scope

from .contracts import (
    IDENTITY_CONTRACT,
    AgentError,
    IdentityContext,
    require_exact_fields,
)


IDENTITY_HEADER = "x-voxquarry-identity-context"
SIGNATURE_HEADER = "x-voxquarry-identity-signature"
ACCOUNT_ADMIN_IDENTITY_HEADER = "x-vq-identity-context"
MAX_IDENTITY_HEADER_BYTES = 8192


class TrustedHeaderIdentityProvider:
    """Verify an identity envelope created by a private trusted gateway.

    Account-admin owns the final identity contract. This adapter is deliberately
    narrow and will remain unavailable unless an out-of-repository HMAC secret is
    supplied. Browser/user JSON never becomes identity input.
    """

    def __init__(self, secret: str | bytes | None) -> None:
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._secret = bytes(raw or b"")

    @property
    def configured(self) -> bool:
        return len(self._secret) >= 32

    def resolve(self, headers: Mapping[str, str]) -> IdentityContext:
        if not self.configured:
            raise AgentError(
                "identity_unavailable",
                "可信身份适配器尚未配置",
                http_status=503,
                retryable=True,
            )
        encoded = str(headers.get(IDENTITY_HEADER) or "")
        supplied = str(headers.get(SIGNATURE_HEADER) or "")
        if not encoded or len(encoded.encode("ascii", "ignore")) > MAX_IDENTITY_HEADER_BYTES:
            raise AgentError("identity_invalid", "身份上下文缺失或过大", http_status=401)
        expected = "sha256=" + hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise AgentError("identity_invalid", "身份签名无效", http_status=401)
        try:
            padding = "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(encoded + padding)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentError("identity_invalid", "身份上下文无法解析", http_status=401) from exc
        value = require_exact_fields(
            payload,
            allowed={
                "contract",
                "actor_id",
                "owner_user_id",
                "scope_id",
                "authority_id",
                "roles",
                "allowed_report_ids",
                "allow_archived_reports",
                "mode",
            },
            required={
                "contract",
                "actor_id",
                "owner_user_id",
                "scope_id",
                "authority_id",
                "roles",
                "allowed_report_ids",
                "allow_archived_reports",
                "mode",
            },
            label="identity context",
        )
        if value["contract"] != IDENTITY_CONTRACT:
            raise AgentError("identity_invalid", "身份合同版本不受支持", http_status=401)
        roles = value["roles"]
        if not isinstance(roles, list):
            raise AgentError("identity_invalid", "roles 必须是数组", http_status=401)
        raw_reports = value["allowed_report_ids"]
        if raw_reports == "*":
            reports = None
        elif isinstance(raw_reports, list):
            reports = frozenset(raw_reports)
        else:
            raise AgentError("identity_invalid", "报告允许范围无效", http_status=401)
        if not isinstance(value["allow_archived_reports"], bool):
            raise AgentError("identity_invalid", "归档报告权限无效", http_status=401)
        return IdentityContext(
            actor_id=value["actor_id"],
            owner_user_id=value["owner_user_id"],
            scope_id=value["scope_id"],
            authority_id=value["authority_id"],
            roles=tuple(roles),
            allowed_report_ids=reports,
            allow_archived_reports=value["allow_archived_reports"],
            mode=value["mode"],
        )


class AccountAdminIdentityProvider:
    """Adapt the formal account-admin identity contract to Wenquan ownership.

    The signed token remains the authority.  Browser-supplied owner, scope,
    report allowlist, and legacy candidate identity headers are ignored.  R1
    grants authenticated local users access to active local reports while
    keeping archived reports outside the default scope.
    """

    _EXPECTED_FIELDS = {
        "schema_version",
        "auth_source",
        "non_production",
        "principal_id",
        "employee_id",
        "display_name",
        "roles",
        "session_id",
        "request_id",
        "issued_at",
        "expires_at",
        "policy_version",
    }

    def __init__(
        self,
        secret: str | bytes | None,
        *,
        key_id: str = "local-identity-v1",
    ) -> None:
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._secret = bytes(raw or b"")
        self._key_id = key_id
        self._authorizer = None
        if self.configured:
            signer = ContractSigner({key_id: self._secret}, key_id)
            self._authorizer = BackendIdentityAuthorizer(signer)

    @property
    def configured(self) -> bool:
        return len(self._secret) >= 32

    def resolve(self, headers: Mapping[str, str]) -> IdentityContext:
        if not self.configured or self._authorizer is None:
            raise AgentError(
                "identity_unavailable",
                "正式账户身份适配器尚未配置",
                http_status=503,
                retryable=True,
            )
        token = str(headers.get(ACCOUNT_ADMIN_IDENTITY_HEADER) or "")
        if not token or len(token.encode("ascii", "ignore")) > MAX_IDENTITY_HEADER_BYTES:
            raise AgentError("identity_invalid", "正式账户身份上下文缺失或过大", http_status=401)
        try:
            authorized = self._authorizer.authorize(
                token,
                required_capability="chat.session.own",
            )
        except PermissionError as exc:
            raise AgentError("identity_forbidden", "AI 研究会话权限不足", http_status=403) from exc
        except ContractError as exc:
            raise AgentError("identity_invalid", "正式账户身份校验失败", http_status=401) from exc
        claims = authorized.claims
        if set(claims) != self._EXPECTED_FIELDS or claims.get("schema_version") != IDENTITY_SCHEMA_VERSION:
            raise AgentError("identity_invalid", "正式账户身份字段集合无效", http_status=401)
        principal_id = str(claims.get("principal_id") or "")
        roles = claims.get("roles")
        if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
            raise AgentError("identity_invalid", "正式账户 roles 无效", http_status=401)
        policy_version = str(claims.get("policy_version") or "")
        return IdentityContext(
            actor_id=principal_id,
            owner_user_id=principal_id,
            scope_id=owner_scope(principal_id),
            authority_id=policy_version,
            roles=tuple(roles),
            allowed_report_ids=None,
            allow_archived_reports=False,
            mode="authenticated",
        )
