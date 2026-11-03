from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import AgentError

DEEPSEEK_MODEL_ID = "deepseek-v4.1-flash-1017"


def upgrade_retired_deepseek_model(model_id: str) -> str:
    """Replace retired deployment settings; never use an old model as fallback."""
    if model_id in {"deepseek-v4-flash-0731", "deepseek-v4.1-flash"}:
        return DEEPSEEK_MODEL_ID
    return model_id


def exact_bool(raw: str | None, *, name: str, default: bool | None = None) -> bool:
    if raw is None:
        if default is None:
            raise AgentError("dependency_config_invalid", f"{name} 未配置", http_status=503)
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise AgentError(
        "dependency_config_invalid",
        f"{name} 必须精确为 true 或 false",
        http_status=503,
    )


def validate_service_url(value: str, *, name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentError("dependency_config_invalid", f"{name} 无效", http_status=503)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AgentError("dependency_config_invalid", f"{name} 包含禁止部分", http_status=503)
    return value.rstrip("/")


def validate_private_service_url(value: str, *, name: str) -> str:
    normalized = validate_service_url(value, name=name)
    hostname = urlsplit(normalized).hostname or ""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        private = (
            "." not in hostname
            or hostname == "localhost"
            or hostname.endswith((".localhost", ".internal", ".local"))
        )
    else:
        private = address.is_loopback or address.is_private or address.is_link_local
    if not private:
        raise AgentError(
            "dependency_config_invalid",
            f"{name} 必须位于 loopback/private-only 网络",
            http_status=503,
        )
    return normalized


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str
    model: str
    enable_thinking: bool
    reasoning_effort: str | None = None

    @property
    def is_glm_53_flash(self) -> bool:
        return self.model.casefold() == "zhipu/glm-5.3-flash"

    @classmethod
    def from_environment(cls) -> "LLMSettings | None":
        api_key = os.environ.get("VOXQUARRY_LLM_API_KEY", "")
        base_url = os.environ.get("VOXQUARRY_LLM_BASE_URL", "")
        model = upgrade_retired_deepseek_model(os.environ.get("VOXQUARRY_LLM_MODEL", ""))
        if not api_key and not base_url and not model:
            return None
        if not api_key or not base_url or not model:
            raise AgentError(
                "dependency_config_invalid",
                "LLM 配置不完整",
                http_status=503,
            )
        is_glm = model.casefold() == "zhipu/glm-5.3-flash"
        reasoning_effort = os.environ.get("VOXQUARRY_LLM_REASONING_EFFORT") or None
        allowed_efforts = {"low", "high", "max"} if is_glm else {"low", "medium", "high", "max"}
        if reasoning_effort is not None and reasoning_effort not in allowed_efforts:
            raise AgentError(
                "dependency_config_invalid",
                "VOXQUARRY_LLM_REASONING_EFFORT 不受当前模型支持",
                http_status=503,
            )
        enable_thinking = exact_bool(
            os.environ.get("VOXQUARRY_LLM_ENABLE_THINKING"),
            name="VOXQUARRY_LLM_ENABLE_THINKING",
            default=True if is_glm else False,
        )
        return cls(
            api_key=api_key,
            base_url=validate_service_url(base_url, name="LLM base URL"),
            model=model,
            # GLM-5.3 Flash always reasons; an old deployment's false flag must
            # not send an unsupported request after switching models.
            enable_thinking=True if is_glm else enable_thinking,
            reasoning_effort=reasoning_effort or ("high" if is_glm else None),
        )

    def __repr__(self) -> str:
        return (
            "LLMSettings(api_key=<redacted>, "
            f"base_url={self.base_url!r}, model={self.model!r}, "
            f"enable_thinking={self.enable_thinking!r}, "
            f"reasoning_effort={self.reasoning_effort!r})"
        )


@dataclass(frozen=True)
class WenquanSettings:
    data_dir: Path
    identity_hmac_secret: str
    identity_provider_mode: str
    account_admin_identity_signing_key: str
    account_admin_identity_key_id: str
    identity_dependency_status: str
    zhuojian_base_url: str | None
    zhuojian_gateway_token: str | None
    llm: LLMSettings | None

    @classmethod
    def from_environment(cls) -> "WenquanSettings":
        raw_data_dir = os.environ.get("VOXQUARRY_WENQUAN_DATA_DIR", "")
        if not raw_data_dir:
            raise AgentError(
                "dependency_config_invalid",
                "VOXQUARRY_WENQUAN_DATA_DIR 未配置",
                http_status=503,
            )
        status = os.environ.get("VOXQUARRY_IDENTITY_DEPENDENCY_STATUS", "unavailable")
        if status not in {"ready", "unavailable", "synthetic_test_only"}:
            raise AgentError("dependency_config_invalid", "身份依赖状态无效", http_status=503)
        identity_provider_mode = os.environ.get(
            "VOXQUARRY_WENQUAN_IDENTITY_PROVIDER", "candidate_hmac"
        )
        if identity_provider_mode not in {"candidate_hmac", "account_admin_v1"}:
            raise AgentError("dependency_config_invalid", "身份适配器模式无效", http_status=503)
        zhuojian_url = os.environ.get("VOXQUARRY_ZHUOJIAN_BASE_URL") or None
        if zhuojian_url:
            zhuojian_url = validate_private_service_url(
                zhuojian_url, name="分析报告 base URL"
            )
        return cls(
            data_dir=Path(raw_data_dir).expanduser().resolve(),
            identity_hmac_secret=os.environ.get("VOXQUARRY_IDENTITY_HMAC_SECRET", ""),
            identity_provider_mode=identity_provider_mode,
            account_admin_identity_signing_key=os.environ.get(
                "VQ_LOCAL_IDENTITY_SIGNING_KEY", ""
            ),
            account_admin_identity_key_id=os.environ.get(
                "VQ_LOCAL_IDENTITY_SIGNING_KEY_ID", "local-identity-v1"
            ),
            identity_dependency_status=status,
            zhuojian_base_url=zhuojian_url,
            zhuojian_gateway_token=os.environ.get("VOXQUARRY_ZHUOJIAN_GATEWAY_TOKEN") or None,
            llm=LLMSettings.from_environment(),
        )
