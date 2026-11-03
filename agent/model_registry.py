"""Server-owned model capabilities; a browser chooses keys, never credentials or URLs."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Mapping
from urllib.parse import urlsplit
import re

from .config import DEEPSEEK_MODEL_ID, LLMSettings, upgrade_retired_deepseek_model, validate_service_url
from .contracts import AgentError, sha256_value

DEFAULT_MODEL = "deepseek-v4.1-flash"
VISION_MODEL = "qwen3.8-flash"
PRICING_VERSION = "dashscope-beijing-public-2026-10-08/v3"
PRICING_CHECKED_AT = "2026-10-08"
PRICING_SOURCE = "https://help.aliyun.com/zh/model-studio/model-pricing"
DEEPSEEK_PRICING_CHECKED_AT = "2026-10-23"
DEEPSEEK_PRICING_VERSION = "qianwen-beijing-deepseek-1017-2026-10-23/v1"
DEEPSEEK_SOURCE = "https://www.qianwenai.com/models/deepseek-v4.1-flash-1017"
# Observed provider receipts use these route spellings. This is deliberately
# an explicit allowlist: namespaced models and other revisions are not aliases.
_RECEIPT_ALIASES = {
    "ZHIPU/GLM-5.3": frozenset({"ZHIPU/GLM-5.3", "zhipu/glm-5.3"}),
    "ZHIPU/GLM-5.3-Flash": frozenset({"ZHIPU/GLM-5.3-Flash", "zhipu/glm-5.3-flash"}),
}


def matches_model_receipt(requested, actual):
    return actual == requested or actual in _RECEIPT_ALIASES.get(requested, ())


# Last verified Beijing list prices; DeepSeek has a separate verification date.
# Estimates keep their version until replaced, without a calendar-based shutdown.
# CNY per million tokens; peak prices and no
# cache discount for reservation. No batch/explicit-cache API is enabled here.
# Output ceilings for supplier-direct models include their full published
# thinking allowance plus the separately limited answer, not max_tokens alone.
_PRICES = {
    DEEPSEEK_MODEL_ID: ("2", "8", "0.2", 0, DEEPSEEK_SOURCE),
    "qwen3.8-max-0902": ("12", "36", "1.5", 0, "qwen3-8-max"),
    "qwen3.8-max": ("12", "36", "1.5", 0, "qwen3-8-max"),
    "qwen3.8-flash": ("0.8", "2.7", "0.1", 0, "qwen3-8-flash"),
    "kimi-k3": ("20", "100", "2", 1048576, "https://www.qianwenai.com/models/kimi-k3"),
    "kimi/kimi-k3": ("20", "100", "2", 1048576, "kimi-k3-by-moonshot"),
    # Current official public pages verify input/output, but do not establish
    # supplier-direct implicit-cache rates. Never copy Aliyun glm-* discounts.
    "ZHIPU/GLM-5.3": ("8", "28", None, 131072, "glm-5-3-by-zhipu"),
    "ZHIPU/GLM-5.3-Flash": ("0.8", "2.8", None, 131072, "glm-5-3-flash-by-zhipu"),
}


@dataclass(frozen=True)
class ResearchModel:
    key: str
    label: str
    model_id: str
    efforts: tuple[str, ...]
    default_effort: str
    can_disable_thinking: bool = False
    image_input: bool = False


MODELS = (
    ResearchModel(DEFAULT_MODEL, "DeepSeek V4.1 Flash 1017", DEEPSEEK_MODEL_ID, ("low", "high", "max"), "low"),
    # The pinned Harness provider accepts only low for these Qwen models.
    ResearchModel("qwen3.8-max", "Qwen3.8-max", "qwen3.8-max-0902", ("low",), "low", True),
    ResearchModel("qwen3.8-flash", "Qwen3.8-flash", "qwen3.8-flash", ("low",), "low", True, True),
    # Aliyun deployment: low + 16384 total output passed a real tool roundtrip.
    # Other efforts have no verified compatible output allowance yet.
    ResearchModel("kimi-k3", "Kimi K3", "kimi-k3", ("low",), "low", image_input=True),
    ResearchModel("glm-5.3", "GLM-5.3", "ZHIPU/GLM-5.3", ("low", "high", "max"), "max"),
    ResearchModel("glm-5.3-flash", "GLM-5.3 Flash", "ZHIPU/GLM-5.3-Flash", ("low", "high", "max"), "max", image_input=True),
)


class ModelRegistry:
    """Configuration is loaded once per process and pinned in each accepted run."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        try:
            overrides = json.loads(env.get("VOXQUARRY_WENQUAN_MODELS_JSON", "{}"))
        except (TypeError, ValueError) as exc:
            raise AgentError("dependency_config_invalid", "模型配置不是有效 JSON", http_status=503) from exc
        if not isinstance(overrides, dict) or set(overrides) - {m.key for m in MODELS}:
            raise AgentError("dependency_config_invalid", "模型配置包含未知模型", http_status=503)
        if env.get("VOXQUARRY_LLM_MODEL"):
            overrides = {**overrides, DEFAULT_MODEL: {**overrides.get(DEFAULT_MODEL, {}), "model_id": env["VOXQUARRY_LLM_MODEL"]}}
        self._models: dict[str, ResearchModel] = {}
        self._settings: dict[str, LLMSettings | None] = {}
        self._status: dict[str, str] = {}
        default_url = env.get("VOXQUARRY_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        vision_model = env.get("VOXQUARRY_WENQUAN_VISION_MODEL", VISION_MODEL)
        if vision_model not in {VISION_MODEL, "disabled"}:
            raise AgentError("dependency_config_invalid", "图片模型请选择 qwen3.8-flash 或 disabled", http_status=503)
        self._vision = LLMSettings(env.get("VOXQUARRY_LLM_API_KEY", ""), validate_service_url(default_url, name="图片模型服务地址"), VISION_MODEL, False, "low") if vision_model != "disabled" else None
        for model in MODELS:
            override = overrides.get(model.key, {})
            if not isinstance(override, dict) or set(override) - {"model_id", "base_url", "api_key_env", "enabled", "verification"}:
                raise AgentError("dependency_config_invalid", "模型配置字段无效", http_status=503)
            model_id = override.get("model_id", model.model_id)
            if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 160:
                raise AgentError("dependency_config_invalid", "模型 ID 无效", http_status=503)
            configured_model_id = model_id
            model_id = upgrade_retired_deepseek_model(model_id)
            model = replace(model, model_id=model_id, label=model_id if model.key == DEFAULT_MODEL and env.get("VOXQUARRY_LLM_MODEL") else model.label)
            if model.key == "kimi-k3" and model_id == "kimi/kimi-k3":
                # An explicit legacy supplier route is never treated as an alias.
                model = replace(model, efforts=("max",), default_effort="max")
            self._models[model.key] = model
            key_name = override.get("api_key_env", "VOXQUARRY_LLM_API_KEY")
            if not isinstance(key_name, str) or not key_name.replace("_", "").isalnum():
                raise AgentError("dependency_config_invalid", "模型凭据引用无效", http_status=503)
            enabled = override.get("enabled", True)
            if not isinstance(enabled, bool):
                raise AgentError("dependency_config_invalid", "模型启用状态无效", http_status=503)
            verification = override.get("verification", "configured")
            if not isinstance(verification, str) or verification not in {"configured", "verified", "permission_denied"}:
                raise AgentError("dependency_config_invalid", "模型权限验证状态无效", http_status=503)
            if configured_model_id != model_id or (model.key == DEFAULT_MODEL and "model_id" not in override and verification == "verified"):
                # Permissions observed for a retired revision do not verify its replacement.
                verification = "configured"
            key = env.get(key_name, "") if enabled else ""
            base_url = validate_service_url(override.get("base_url", default_url), name="模型服务地址")
            self._settings[model.key] = LLMSettings(key, base_url, model_id, True, model.default_effort) if key else None
            self._status[model.key] = "disabled" if not enabled else (verification if key else "unconfigured")
        config = {key: {"model_id": value.model_id, "base_url": self._settings[key].base_url if self._settings[key] else None,
                                                 "verification": self._status[key], "available": self._unavailable_reason(value) is None,
                                                 "efforts": value.efforts, "default_effort": value.default_effort}
                                           for key, value in self._models.items()}
        self.config_version = sha256_value(config)
        # Recognize only known previous effort catalogs. Provider, permissions
        # and model IDs must still match; runtime also validates the frozen
        # effective effort against today's supported set.
        self._effort_versions = {}
        for previous_qwen in (False, True):
            for effort in ("max", "high", self._models[DEFAULT_MODEL].default_effort):
                previous = {key: dict(item) for key, item in config.items()}
                previous[DEFAULT_MODEL]["default_effort"] = effort
                if previous_qwen:
                    for key in ("qwen3.8-max", "qwen3.8-flash"):
                        previous[key].update(efforts=("low", "medium", "xhigh"), default_effort="xhigh")
                self._effort_versions[sha256_value(previous)] = {key: item["default_effort"] for key, item in previous.items()}

    def catalog(self) -> dict[str, Any]:
        labels = {"low": "轻量", "medium": "均衡", "high": "深入", "max": "最高", "xhigh": "最高"}
        return {"contract": "voxquarry-wenquan-models/v1", "default_model_key": DEFAULT_MODEL,
                "config_version": self.config_version, "items": [
                    {"key": model.key, "label": model.label, "model_id": model.model_id,
                     "available": self._unavailable_reason(model) is None,
                     "verification": self._status[model.key],
                     "unavailable_reason": self._unavailable_reason(model),
                     "default_effort": model.default_effort,
                     "efforts": [{"value": value, "label": labels[value]} for value in model.efforts],
                     "thinking": {"supported": True, "can_disable": model.can_disable_thinking, "default": True},
                     "image_input": model.image_input,
                     "capability_note": "当前仅开放兼容的轻量思考强度" if model.efforts == ("low",) else None,
                     "pricing": self.pricing({"model_key": model.key})} for model in self._models.values()]}

    def pricing(self, frozen: Mapping[str, Any], *, today: date | None = None) -> dict[str, Any]:
        key = frozen.get("model_key")
        model = self._models.get(key) if isinstance(key, str) else None
        settings = self._settings.get(key) if isinstance(key, str) else None
        return self._pricing(settings, model.model_id if model else None, today=today)

    def vision_runtime(self) -> LLMSettings:
        if not self._vision or not self._vision.api_key:
            raise AgentError("vision_unavailable", "图片识别模型尚未配置", http_status=503)
        return self._vision

    def vision_pricing(self) -> dict[str, Any]:
        return self._pricing(self._vision, VISION_MODEL)

    @staticmethod
    def _pricing(settings, model_id, *, today=None):
        current = today or date.today()
        checked_at = DEEPSEEK_PRICING_CHECKED_AT if model_id == DEEPSEEK_MODEL_ID else PRICING_CHECKED_AT
        version = DEEPSEEK_PRICING_VERSION if model_id == DEEPSEEK_MODEL_ID else PRICING_VERSION
        unknown = {"status": "unknown", "currency": "CNY", "version": version,
                   "checked_at": checked_at}
        if current.isoformat() < checked_at:
            return {**unknown, "reason": "pricing_not_yet_verified"}
        if not settings or model_id not in _PRICES:
            return {**unknown, "reason": "model_price_unverified"}
        parsed = urlsplit(settings.base_url)
        if (parsed.scheme != "https" or parsed.path.rstrip("/") != "/compatible-mode/v1" or parsed.port not in {None, 443}
                or not (parsed.hostname in {"dashscope.aliyuncs.com", "cn-beijing.maas.qianwenaiapi.com"} or re.fullmatch(r"[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com", parsed.hostname or ""))):
            return {**unknown, "reason": "provider_region_price_unverified"}
        incoming, outgoing, cached, thinking, source = _PRICES[model_id]
        return {**unknown, "status": "verified_list_price", "region": "cn-beijing",
                "input_per_million": incoming, "output_per_million": outgoing,
                "cached_input_per_million": cached, "maximum_thinking_tokens": thinking,
                "cache_discount_verified": cached is not None,
                "output_limit_includes_thinking": thinking == 0 or model_id == "kimi-k3",
                "source_url": source if source.startswith("https://") else (PRICING_SOURCE if source.startswith("deepseek") else "https://help.aliyun.com/zh/model-studio/" + source),
                "output_limit_source_url": "https://platform.qianwenai.com/docs/developer-guides/getting-started/text-generation-models" if model_id == DEEPSEEK_MODEL_ID else "https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions",
                "basis": "on_demand_peak_no_cache_discount"}

    @staticmethod
    def _budget_unavailable_reason(model: ResearchModel) -> str | None:
        if model.model_id == "kimi/kimi-k3":
            return "此 Kimi K3 调用方式尚未验证总输出限额，请使用阿里云部署的 Kimi K3"
        return None

    def _unavailable_reason(self, model: ResearchModel) -> str | None:
        reasons = {"disabled": "此模型已停用", "unconfigured": "此模型尚未配置",
                   "permission_denied": "云账号尚未开通此模型"}
        return reasons.get(self._status[model.key]) or self._budget_unavailable_reason(model)

    def saved_preferences(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Retire known invalid preferences for future questions only, never runs."""
        result = dict(value)
        retired = lambda key, effort: key in {"qwen3.8-max", "qwen3.8-flash"} and effort in ("medium", "xhigh")
        if retired(result.get("model_key"), result.get("effort")):
            result["effort"] = "default"
        remembered = result.get("effort_by_model")
        if isinstance(remembered, dict):
            result["effort_by_model"] = {key: "default" if retired(key, effort) else effort for key, effort in remembered.items()}
        return self.selection(result)

    def selection(self, value: Mapping[str, Any] | None = None, *, require_available: bool = False) -> dict[str, Any]:
        if value is not None and not isinstance(value, Mapping):
            raise AgentError("invalid_request", "模型设置必须是对象")
        value = dict(value or {})
        allowed = {"model_key", "effort", "web_enabled", "thinking_enabled", "effort_by_model"}
        if set(value) - allowed:
            raise AgentError("invalid_request", "模型设置包含未知选项")
        key = value.get("model_key", DEFAULT_MODEL)
        if not isinstance(key, str) or key not in self._models:
            raise AgentError("invalid_request", "请选择可用模型")
        model = self._models[key]
        effort = value.get("effort", "default")
        if effort != "default" and effort not in model.efforts:
            raise AgentError("invalid_request", "此模型不支持所选思考强度")
        web = value.get("web_enabled", False)
        thinking = value.get("thinking_enabled", True)
        if not isinstance(web, bool) or not isinstance(thinking, bool):
            raise AgentError("invalid_request", "联网和思考设置必须为开关")
        if not thinking and not model.can_disable_thinking:
            raise AgentError("invalid_request", "此模型需要开启思考")
        remembered = value.get("effort_by_model", {})
        if not isinstance(remembered, dict) or any(k not in self._models or (v != "default" and v not in self._models[k].efforts) for k, v in remembered.items()):
            raise AgentError("invalid_request", "保存的模型强度无效")
        if require_available and self._status[key] == "permission_denied":
            raise AgentError("model_permission_denied", self._unavailable_reason(model), http_status=503)
        if require_available and self._settings[key] is None:
            raise AgentError("model_unavailable", self._unavailable_reason(model), http_status=503)
        if require_available and self._budget_unavailable_reason(model):
            raise AgentError("model_budget_unavailable", self._budget_unavailable_reason(model), http_status=409)
        return {"model_key": key, "effort": effort, "web_enabled": web,
                "thinking_enabled": thinking, "effort_by_model": {**remembered, key: effort}}

    def freeze(self, value: Mapping[str, Any]) -> dict[str, Any]:
        selection = self.selection(value, require_available=True)
        model = self._models[selection["model_key"]]
        return {**selection, "model_id": model.model_id, "model_label": model.label,
                "effective_effort": model.default_effort if selection["effort"] == "default" else selection["effort"],
                "model_config_version": self.config_version, "image_input": model.image_input}

    def runtime(self, frozen: Mapping[str, Any]) -> LLMSettings:
        if not isinstance(frozen, Mapping):
            raise AgentError("model_config_changed", "研究模型配置不完整", http_status=409)
        key = frozen.get("model_key")
        version = frozen.get("model_config_version")
        if not isinstance(key, str) or key not in self._models or not isinstance(version, str) or version not in self._effort_versions or frozen.get("model_id") != self._models[key].model_id:
            raise AgentError("model_config_changed", "模型配置已更新，请用当前配置重新分析", http_status=409)
        fields = ("model_key", "effort", "web_enabled", "thinking_enabled")
        if any(field not in frozen for field in fields):
            raise AgentError("model_config_changed", "研究模型配置不完整", http_status=409)
        expected_effort = self._effort_versions[version][key] if frozen["effort"] == "default" else frozen["effort"]
        if expected_effort not in self._models[key].efforts:
            raise AgentError("model_config_changed", "此研究固定的思考强度已停用，请用当前配置重新分析", http_status=409)
        selected = self.selection({k: frozen[k] for k in fields}, require_available=True)
        if frozen.get("effective_effort") != expected_effort:
            raise AgentError("model_config_changed", "研究思考强度与固定设置不一致", http_status=409)
        settings = self._settings[key]
        assert settings is not None
        return replace(settings, enable_thinking=selected["thinking_enabled"], reasoning_effort=frozen["effective_effort"])
