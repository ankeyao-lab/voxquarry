from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Callable

from reporting.brand_persona import (
    BrandPersonaContractError,
    validate_persona_narrative,
)


JsonPost = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]
JsonGet = Callable[[str, dict[str, str], float], dict[str, Any]]
BinaryGet = Callable[
    [str, dict[str, str], float, int], tuple[bytes, str]
]


def _trusted_aliyuncs_url(url: str, *, endpoint: bool = False) -> bool:
    parsed = urllib.parse.urlparse(url)
    hostname = str(parsed.hostname or "").casefold()
    allowed_host = (
        hostname == "dashscope.aliyuncs.com"
        if endpoint
        else hostname == "aliyuncs.com" or hostname.endswith(".aliyuncs.com")
    )
    return (
        parsed.scheme == "https"
        and allowed_host
        and parsed.username is None
        and parsed.password is None
    )


def optional_env_boolean(name: str) -> bool | None:
    """Read an optional boolean without silently changing inference mode."""

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BrandPersonaContractError(
        f"{name} 必须为 true/false、1/0、yes/no 或 on/off"
    )


def _default_post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _default_get_json(
    url: str, headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _default_get_bytes(
    url: str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> tuple[bytes, str]:
    if not _trusted_aliyuncs_url(url):
        raise BrandPersonaContractError("Qwen 图片 URL 不在阿里云受信域名")
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not _trusted_aliyuncs_url(str(response.geturl() or "")):
            raise BrandPersonaContractError("Qwen 图片重定向离开阿里云受信域名")
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise BrandPersonaContractError("Qwen 图片超过允许大小")
        content_type = str(response.headers.get_content_type() or "")
    return payload, content_type


def _json_content(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrandPersonaContractError("Qwen 未返回合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise BrandPersonaContractError("Qwen Persona 输出必须是 JSON 对象")
    return parsed


class QwenPersonaClient:
    """Server-only, claim-locked Qwen adapter for derived Persona wording."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        post_json: JsonPost = _default_post_json,
        timeout: float = 90.0,
        enable_thinking: bool | None = None,
    ) -> None:
        if not api_key.strip():
            raise BrandPersonaContractError("Qwen API Key 未配置")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.post_json = post_json
        self.timeout = timeout
        self.enable_thinking = enable_thinking

    def generate(
        self, allowed_claims: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        if not allowed_claims:
            raise BrandPersonaContractError("没有可供 Persona 使用的确定性 claims")
        prompt = (
            "你是 VoxQuarry 资深产品研究编辑器。只能组织输入 claims，不能新增"
            "数字、年龄、职业、家庭、收入、国籍或未给出的业务判断。必须为输入中"
            "每一个 persona_id 输出一个 Persona，不能合并或遗漏；每个尽量引用"
            "身份、决策驱动、体验/风险和生命周期等不同类型的 claim。summary 要"
            "说明该群体的任务、选择标准与核心矛盾；first_person_statement 是明确"
            "标注为综合自述的第一人称表达，不得冒充原文；who_they_are 解释这个"
            "证据群体为何成立；day_in_life 与 journey 每个阶段必须分别绑定当前"
            "Persona 的 claim_ids。不得复述或新增数字。"
            "image_brief 分别给出人物肖像与真实任务情境的英文图库检索词，只描述"
            "安全、无品牌标识的使用情境，不指定年龄、性别、族裔或家庭关系。"
            "只输出符合以下结构的 JSON："
            '{"personas":[{"persona_id":"snake_case","name":"中文名称",'
            '"summary":"中文摘要","first_person_statement":"综合自述",'
            '"who_they_are":"群体解释","claim_ids":["claim_id"],'
            '"day_in_life":[{"stage":"阶段","narrative":"叙述",'
            '"claim_ids":["claim_id"]}],"journey":[{"stage":"阶段",'
            '"narrative":"叙述","claim_ids":["claim_id"]}],'
            '"image_brief":{"portrait_query":"English portrait search",'
            '"context_query":"English context search",'
            '"avoid":["brand logo","unsafe operation"]}}]}。'
            "\n允许使用的 claims：\n"
            + json.dumps(
                list(allowed_claims.values()),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": "严格遵守 claim-locked JSON 契约，拒绝补全未知事实。",
            },
            {"role": "user", "content": prompt},
        ]
        request_body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if self.enable_thinking is not None:
            # DashScope's OpenAI-compatible endpoint accepts this provider
            # extension at the top level of the JSON request body.
            request_body["enable_thinking"] = self.enable_thinking
        expected_personas = {
            str(claim.get("persona_id") or "")
            for claim in allowed_claims.values()
            if str(claim.get("persona_id") or "")
        }
        allowed_by_persona: dict[str, list[str]] = {}
        for claim_id, claim in allowed_claims.items():
            persona_id = str(claim.get("persona_id") or "")
            if persona_id:
                allowed_by_persona.setdefault(persona_id, []).append(claim_id)
        last_error: BrandPersonaContractError | None = None
        for attempt in range(2):
            response = self.post_json(
                f"{self.base_url}/chat/completions",
                {"Authorization": f"Bearer {self.api_key}"},
                request_body,
                self.timeout,
            )
            try:
                content = response["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise BrandPersonaContractError(
                    "Qwen 响应缺少 choices.message.content"
                ) from exc
            try:
                validated = validate_persona_narrative(
                    _json_content(content), allowed_claims
                )
                if expected_personas and {
                    row["persona_id"] for row in validated["personas"]
                } != expected_personas:
                    raise BrandPersonaContractError(
                        "Qwen 不得遗漏或新增确定性 Persona 群组"
                    )
                return validated
            except BrandPersonaContractError as exc:
                last_error = exc
                if attempt:
                    break
                # The invalid answer is never persisted. One bounded repair
                # pass handles a mistyped ID while retaining the strict lock.
                messages.extend(
                    [
                        {"role": "assistant", "content": str(content)},
                        {
                            "role": "user",
                            "content": (
                                "上一个 JSON 未通过确定性 claim-lock 校验："
                                f"{exc}。请完整重写 JSON，只能逐 Persona 使用以下"
                                " claim_id；不得新增、跨 Persona 引用或遗漏 Persona："
                                + json.dumps(
                                    allowed_by_persona,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                            ),
                        },
                    ]
                )
        raise last_error or BrandPersonaContractError("Qwen Persona 校验失败")


class QwenPersonaImageClient:
    """Disabled compatibility adapter for callers of the retired image client.

    No credential, model or endpoint setting can re-enable image generation.
    Saved images remain readable through BrandPersonaStore independently.
    """

    DEFAULT_ENDPOINT = (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    MAX_IMAGE_BYTES = 15 * 1024 * 1024

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "qwen-image-3.0-pro",
        endpoint: str = DEFAULT_ENDPOINT,
        post_json: JsonPost = _default_post_json,
        get_bytes: BinaryGet = _default_get_bytes,
        timeout: float = 120.0,
    ) -> None:
        if not api_key.strip():
            raise BrandPersonaContractError("DashScope 图片 API Key 未配置")
        self.api_key = api_key.strip()
        self.model = model.strip() or "qwen-image-3.0-pro"
        self.endpoint = endpoint.strip()
        if not _trusted_aliyuncs_url(self.endpoint, endpoint=True):
            raise BrandPersonaContractError("Qwen Image endpoint 不是 DashScope 官方地址")
        self.post_json = post_json
        self.get_bytes = get_bytes
        self.timeout = timeout

    def generate(self, prompt: str) -> dict[str, Any]:
        raise BrandPersonaContractError("Qwen 图片生成功能已关闭")


def search_pexels_image(
    query: str,
    *,
    api_key: str,
    orientation: str = "landscape",
    get_json: JsonGet = _default_get_json,
    timeout: float = 20.0,
) -> dict[str, Any] | None:
    """Return one attributed illustrative image without exposing the API key."""
    if not api_key.strip() or not query.strip():
        return None
    normalized_orientation = (
        orientation if orientation in {"landscape", "portrait", "square"} else "landscape"
    )
    url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(
        {
            "query": query.strip(),
            "orientation": normalized_orientation,
            "per_page": 8,
        }
    )
    payload = get_json(
        url,
        {
            "Authorization": api_key.strip(),
            "Accept": "application/json",
            "User-Agent": "VoxQuarry-Reporting/1.0",
        },
        timeout,
    )
    photos = payload.get("photos") if isinstance(payload, dict) else None
    if not isinstance(photos, list) or not photos:
        return None
    photo = photos[0]
    src = photo.get("src") if isinstance(photo, dict) else {}
    image_url = str(
        (src or {}).get("portrait" if normalized_orientation == "portrait" else "landscape")
        or (src or {}).get("medium")
        or ""
    )
    page_url = str(photo.get("url") or "")
    photographer_url = str(photo.get("photographer_url") or "")
    if not image_url.startswith("https://images.pexels.com/"):
        raise BrandPersonaContractError("Pexels 图片 URL 不在允许域名")
    if not page_url.startswith("https://www.pexels.com/"):
        raise BrandPersonaContractError("Pexels 来源页面 URL 不在允许域名")
    if photographer_url and not photographer_url.startswith("https://www.pexels.com/"):
        raise BrandPersonaContractError("Pexels 摄影师 URL 不在允许域名")
    return {
        "provider": "Pexels",
        "photo_id": photo.get("id"),
        "image_url": image_url,
        "source_url": page_url,
        "photographer": str(photo.get("photographer") or ""),
        "photographer_url": photographer_url,
        "alt": str(photo.get("alt") or "品牌用户情境示意图"),
        "query": query.strip(),
        "illustrative_only": True,
        "disclosure": "情境示意图 · 非真实评论者",
    }
