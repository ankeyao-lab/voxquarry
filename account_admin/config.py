from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .constants import (
    AUTH_SOURCE,
    IDENTITY_CONTEXT_TTL_SECONDS,
    MODE,
    REMEMBER_SESSION_LIFETIME_SECONDS,
    SERVICE_AUTH_MAX_TTL_SECONDS,
    STANDARD_SESSION_LIFETIME_SECONDS,
)


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class LocalDevConfig:
    data_dir: Path
    identity_signing_keys: dict[str, bytes]
    service_signing_keys: dict[str, bytes]
    active_identity_key_id: str = "local-identity-v1"
    active_service_key_id: str = "local-service-v1"
    mode: str = MODE
    auth_source: str = AUTH_SOURCE
    native_bind_host: str = "127.0.0.1"
    allow_self_registration: bool = True
    allow_fixture_login: bool = False
    ad_enabled: bool = False
    standard_session_lifetime_seconds: int = STANDARD_SESSION_LIFETIME_SECONDS
    remember_session_lifetime_seconds: int = REMEMBER_SESSION_LIFETIME_SECONDS
    identity_context_ttl_seconds: int = IDENTITY_CONTEXT_TTL_SECONDS
    service_auth_max_ttl_seconds: int = SERVICE_AUTH_MAX_TTL_SECONDS
    allowed_origins: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "http://127.0.0.1:18081",
                "http://localhost:18081",
            }
        )
    )

    def __post_init__(self) -> None:
        if self.mode != MODE or self.auth_source != "local_account":
            raise ConfigurationError("account-admin R1 only supports local_dev_account_admin/local_account")
        if self.ad_enabled:
            raise ConfigurationError("real AD authentication is blocked in the local scaffold")
        if self.native_bind_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ConfigurationError("native local-dev server must bind loopback")
        if self.active_identity_key_id not in self.identity_signing_keys:
            raise ConfigurationError("active identity signing key is missing")
        if self.active_service_key_id not in self.service_signing_keys:
            raise ConfigurationError("active service signing key is missing")
        if min(
            self.standard_session_lifetime_seconds,
            self.remember_session_lifetime_seconds,
            self.identity_context_ttl_seconds,
        ) <= 0:
            raise ConfigurationError("session and identity TTLs must be positive")
        if self.remember_session_lifetime_seconds > REMEMBER_SESSION_LIFETIME_SECONDS:
            raise ConfigurationError("remembered session may not exceed 90 days")
        if not 1 <= self.service_auth_max_ttl_seconds <= SERVICE_AUTH_MAX_TTL_SECONDS:
            raise ConfigurationError("service token TTL exceeds the P1 maximum")

    @classmethod
    def from_env(cls) -> "LocalDevConfig":
        mode = os.environ.get("VQ_ACCOUNT_ADMIN_MODE", "")
        identity_key = os.environ.get("VQ_LOCAL_IDENTITY_SIGNING_KEY", "")
        service_key = os.environ.get("VQ_LOCAL_SERVICE_SIGNING_KEY", "")
        if mode != MODE:
            raise ConfigurationError(
                f"VQ_ACCOUNT_ADMIN_MODE must be {MODE}; production/AD modes are disabled"
            )
        if len(identity_key.encode("utf-8")) < 32 or len(service_key.encode("utf-8")) < 32:
            raise ConfigurationError("local signing keys must be injected and at least 32 bytes")
        data_dir = Path(os.environ.get("VQ_ACCOUNT_ADMIN_DATA_DIR", "account_admin/var"))
        bind_host = os.environ.get("VQ_ACCOUNT_ADMIN_BIND_HOST", "127.0.0.1")
        origins = frozenset(
            item.strip()
            for item in os.environ.get(
                "VQ_ACCOUNT_ADMIN_ALLOWED_ORIGINS",
                "http://127.0.0.1:18081,http://localhost:18081",
            ).split(",")
            if item.strip()
        )
        return cls(
            data_dir=data_dir,
            identity_signing_keys={"local-identity-v1": identity_key.encode("utf-8")},
            service_signing_keys={"local-service-v1": service_key.encode("utf-8")},
            mode=mode,
            native_bind_host=bind_host,
            allow_self_registration=os.environ.get("VQ_ACCOUNT_ADMIN_ALLOW_REGISTRATION", "1") == "1",
            allow_fixture_login=os.environ.get("VQ_ACCOUNT_ADMIN_ENABLE_FIXTURES", "0") == "1",
            allowed_origins=origins,
        )
