from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Mapping


REPORT_DISPLAY_NAME_PERMISSION = "report_manage"
REPORT_MANAGEMENT_TOKEN_ENV = "VOXQUARRY_REPORT_MANAGEMENT_GATEWAY_TOKEN"
REPORT_MANAGEMENT_TOKEN_HEADER = "x-voxquarry-report-management-token"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


class ReportManagementAuthorityError(ValueError):
    def __init__(self, code: str, message: str, *, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class ReportManagementAuthority:
    authority_id: str
    principal_id: str
    permissions: frozenset[str]
    request_id: str

    def actor(self) -> dict[str, str]:
        return {
            "authority_id": self.authority_id,
            "principal_id": self.principal_id,
            "request_id": self.request_id,
        }


def _clean_identifier(value: object, field: str) -> str:
    cleaned = str(value or "").strip()
    if not IDENTIFIER_PATTERN.fullmatch(cleaned):
        raise ReportManagementAuthorityError(
            "invalid_report_management_authority",
            f"可信身份上下文中的 {field} 无效",
            http_status=403,
        )
    return cleaned


def authority_from_report_management_gateway(
    headers: Mapping[str, str], expected_token: str | None
) -> ReportManagementAuthority:
    """Read actor and permission only from a trusted identity gateway.

    The browser request body cannot choose its actor or permissions.  Local
    assembly must strip client-supplied copies of these headers and inject the
    verified values after authentication.
    """

    expected = str(expected_token or "")
    if not expected:
        raise ReportManagementAuthorityError(
            "report_management_gateway_unconfigured",
            "报告管理身份网关尚未配置",
            http_status=503,
        )
    supplied = str(headers.get(REPORT_MANAGEMENT_TOKEN_HEADER) or "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise ReportManagementAuthorityError(
            "report_management_unauthorized",
            "请求未通过可信报告管理身份网关",
            http_status=401,
        )
    authority_id = _clean_identifier(
        headers.get("x-voxquarry-authority-id"), "authority_id"
    )
    principal_id = _clean_identifier(
        headers.get("x-voxquarry-principal-id"), "principal_id"
    )
    raw_permissions = str(headers.get("x-voxquarry-permissions") or "")
    permissions = frozenset(
        value.strip() for value in raw_permissions.split(",") if value.strip()
    )
    if REPORT_DISPLAY_NAME_PERMISSION not in permissions:
        raise ReportManagementAuthorityError(
            "report_manage_forbidden",
            "当前账号没有报告管理权限",
            http_status=403,
        )
    if any(not IDENTIFIER_PATTERN.fullmatch(value) for value in permissions):
        raise ReportManagementAuthorityError(
            "invalid_report_management_authority",
            "可信身份上下文中的 permissions 无效",
            http_status=403,
        )
    request_id_value = headers.get("x-voxquarry-request-id")
    request_id = (
        _clean_identifier(request_id_value, "request_id")
        if request_id_value
        else f"rrq_{secrets.token_hex(12)}"
    )
    return ReportManagementAuthority(
        authority_id=authority_id,
        principal_id=principal_id,
        permissions=permissions,
        request_id=request_id,
    )
