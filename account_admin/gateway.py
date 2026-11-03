from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .constants import (
    ALLOWED_USER_AUTH_SOURCES,
    IDENTITY_SCHEMA_VERSION,
    ROLE_CAPABILITIES,
    SERVICE_AUTH_SCHEMA_VERSION,
)
from .contracts import ContractError, ContractSigner


def strip_untrusted_identity_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Remove every client-supplied X-VQ-* header before trusted injection."""

    return [(name, value) for name, value in headers if not name.casefold().startswith("x-vq-")]


def capabilities_for_roles(roles: Iterable[str]) -> frozenset[str]:
    capabilities: set[str] = set()
    for role in roles:
        capabilities.update(ROLE_CAPABILITIES.get(role, frozenset()))
    return frozenset(capabilities)


@dataclass(frozen=True)
class AuthorizedIdentity:
    claims: dict[str, Any]
    capabilities: frozenset[str]


class BackendIdentityAuthorizer:
    """Verifier intended for future domain middleware; it fails closed."""

    def __init__(self, signer: ContractSigner):
        self.signer = signer

    def authorize(
        self,
        token: str | None,
        *,
        required_capability: str,
        now: datetime | None = None,
    ) -> AuthorizedIdentity:
        if not token:
            raise ContractError("missing signed identity context")
        claims = self.signer.verify(token, expected_schema=IDENTITY_SCHEMA_VERSION, now=now)
        if claims.get("auth_source") not in ALLOWED_USER_AUTH_SOURCES or claims.get("non_production") is not True:
            raise ContractError("R1 accepts only signed non-production user identity")
        capabilities = capabilities_for_roles(claims.get("roles") or [])
        if required_capability not in capabilities:
            raise PermissionError("capability denied")
        return AuthorizedIdentity(claims=claims, capabilities=capabilities)


class ServiceAuthorizer:
    def __init__(self, signer: ContractSigner, max_ttl_seconds: int = 300):
        self.signer = signer
        self.max_ttl_seconds = max_ttl_seconds

    def authorize(
        self,
        authorization: str | None,
        *,
        expected_audience: str,
        required_scope: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        prefix = "VQ-Service "
        if not authorization or not authorization.startswith(prefix):
            raise ContractError("missing service authorization")
        claims = self.signer.verify(
            authorization[len(prefix):],
            expected_schema=SERVICE_AUTH_SCHEMA_VERSION,
            expected_audience=expected_audience,
            required_scope=required_scope,
            max_ttl_seconds=self.max_ttl_seconds,
            now=now,
        )
        if claims.get("auth_source") != "local_fixture" or claims.get("non_production") is not True:
            raise ContractError("P1 accepts only synthetic local service identity")
        return claims
