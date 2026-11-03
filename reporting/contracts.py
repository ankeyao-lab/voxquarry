from __future__ import annotations

import re
from datetime import date
from typing import Any

from reporting.category_vocabulary import (
    canonical_category_name,
    category_definition_by_id,
    category_id_for,
)
from reporting.config import MARKET_REGIONS
from reporting.controlled_energy import ENERGY_LABELS, REPORT_ENERGY_TYPES
from reporting.product_form import FORM_CATEGORIES, FORM_LABELS, product_form_fact


SCENE_IDS = (
    "satisfaction-overview",
    "pain-and-failure",
    "product-attribute-performance",
    "needs-and-use-cases",
    "product-opportunity",
    "brand-product-comparison",
    "channel-comparison",
    "model-time-comparison",
    "user-profile",
    "smart-opportunity",
)

FACTOR_IDS = (
    "ratings",
    "recommendation",
    "written-coverage",
    "attributes",
    "pain-points",
    "use-cases",
    "explicit-age",
    "user-traits",
    "keyword-document-frequency",
    "factor-cooccurrence",
    "kano-candidates",
    "product-opportunities",
    "smart-signals",
)

UNAVAILABLE_FACTOR_IDS = ("verified-purchaser",)

SCENE_FACTORS = {
    "satisfaction-overview": (
        "ratings",
        "recommendation",
        "written-coverage",
    ),
    "pain-and-failure": ("pain-points", "factor-cooccurrence"),
    "product-attribute-performance": (
        "attributes",
        "kano-candidates",
        "factor-cooccurrence",
    ),
    "needs-and-use-cases": ("use-cases", "user-traits"),
    "product-opportunity": (
        "pain-points",
        "use-cases",
        "product-opportunities",
    ),
    "brand-product-comparison": ("ratings", "attributes", "pain-points"),
    "channel-comparison": ("ratings", "written-coverage"),
    "model-time-comparison": ("ratings", "pain-points"),
    "user-profile": ("explicit-age", "user-traits", "use-cases"),
    "smart-opportunity": ("smart-signals", "pain-points"),
}

SYNDICATION_POLICIES = {
    "include",
    "exclude_explicit",
    "only_explicit",
    "unknown_only",
}
TEXT_POLICIES = {"any", "true", "false"}
FALLBACK_POLICIES = {"include", "exclude"}
KEYWORD_MODES = {"or", "and", "exact"}
KEYWORD_FIELDS = {"title_and_body", "title", "body"}
PRODUCT_MASTER_SCHEMA_VERSION = "retail-product-master/v2"
VOLTAGE_PLATFORM_ID_RE = re.compile(r"^vpf_[0-9a-f]{20}$")
VOLTAGE_PLATFORM_KEY_RE = re.compile(
    r"^[^|\s][^|]*\|[1-9][0-9]*(?:\.[0-9]+)?V$"
)
CROSS_CATEGORY_BUNDLE_ID = "cross_category_bundle"
CROSS_CATEGORY_BUNDLE_NAME = "Cross-category Bundle"


def default_scope() -> dict[str, Any]:
    """Return a fresh default scope without depending on the API runtime."""
    return {
        "job_ids": [],
        "category_id": "",
        "categories": [],
        "exclude_categories": [],
        "channels": [],
        "brands": [],
        "models": [],
        "product_form_factors": [],
        "market_regions": [],
        "voltage_platform_keys": [],
        "include_unknown_voltage_platform": False,
        # Historical Chengmu-scoped reports retain this field unchanged. New
        # requests must use the Wotu task-fact keys above.
        "voltage_platform_ids": [],
        "energy_type": "",
        "ratings": [],
        "has_text": "any",
        "keywords": [],
        "keyword_mode": "or",
        "keyword_field": "title_and_body",
        "syndicated": "include",
        "fallback_source": "include",
        "date_range": {},
    }


def freeze_selected_jobs(
    request: dict[str, Any], selected_job_ids: list[str]
) -> dict[str, Any]:
    """Return a copy whose public scope fixes the resolved source jobs."""
    return {
        **request,
        "scope": {
            **request["scope"],
            "job_ids": list(dict.fromkeys(selected_job_ids)),
        },
    }


class ContractError(ValueError):
    """Stable validation error for a reporting request."""


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContractError(f"{field} 必须是数组")
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            raise ContractError(f"{field} 只能包含字符串")
        normalized = " ".join(item.split()).strip()
        if normalized:
            cleaned.append(normalized)
    return sorted(set(cleaned))


def _date_value(value: Any, field: str) -> str | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise ContractError(f"{field} 必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ContractError(f"{field} 必须是有效的 YYYY-MM-DD") from exc


def automatic_report_title(scope: dict[str, Any]) -> str:
    """Build the compact deterministic title used by newly saved reports."""
    platforms = [
        value
        for value in scope.get("voltage_platforms") or []
        if isinstance(value, dict)
    ]
    brands = sorted(
        {str(value).strip() for value in scope.get("brands") or [] if str(value).strip()}
    )
    channels = sorted(
        {str(value).strip() for value in scope.get("channels") or [] if str(value).strip()}
    )
    include_unknown = bool(scope.get("include_unknown_voltage_platform"))
    platform_scope_count = len(platforms) + (1 if include_unknown else 0)
    if len(platforms) == 1 and not include_unknown:
        platform = platforms[0]
        subject = (
            f"{platform.get('brand') or platform.get('brand_name') or (brands[0] if brands else '')} "
            f"{platform.get('canonical_voltage') or ''} 平台"
        ).strip()
    elif platform_scope_count > 1:
        subject = f"{platform_scope_count} 个电压平台"
    elif include_unknown:
        subject = (
            f"{brands[0]} 未知电压平台"
            if len(brands) == 1 else "电压平台未明确"
        )
    else:
        subject = (
            f"{brands[0]} 品牌"
            if len(brands) == 1
            else f"{len(brands)} 个品牌"
            if brands
            else "全品牌"
        )
    channel_scope = (
        f"{channels[0]} 渠道"
        if len(channels) == 1
        else f"{len(channels)} 个渠道"
        if channels
        else "全渠道"
    )
    separator = "" if channel_scope == "全渠道" else " "
    energy_type = str(scope.get("energy_type") or "").strip()
    energy_prefix = f"{ENERGY_LABELS[energy_type]} " if energy_type in ENERGY_LABELS else ""
    forms = scope.get("product_form_factors") or []
    form_prefix = " / ".join(FORM_LABELS[value] for value in forms if value in FORM_LABELS)
    return f"{form_prefix + ' ' if form_prefix else ''}{energy_prefix}{subject}{separator}{channel_scope}量化洞察"


def normalize_scope(raw_scope: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    if raw_scope is None:
        raw_scope = {}
    if not isinstance(raw_scope, dict):
        raise ContractError("scope 必须是对象")
    scope = {**defaults, **raw_scope}
    if "source_product_ids" in scope:
        scope["source_product_ids"] = _string_list(scope["source_product_ids"], "scope.source_product_ids")
    # These values are always resolved and frozen by the service from
    # immutable upstream facts. A client cannot self-assert them.
    for server_field in (
        "voltage_platforms", "voltage_platform_refs", "product_master", "model_refs",
        "energy", "energy_refs", "energy_selection",
    ):
        scope.pop(server_field, None)
    for field in (
        "job_ids",
        "channels",
        "brands",
        "categories",
        "exclude_categories",
        "models",
        "product_form_factors",
        "market_regions",
        "voltage_platform_keys",
        "voltage_platform_ids",
        "keywords",
    ):
        scope[field] = _string_list(scope.get(field), f"scope.{field}")

    unknown_regions = sorted(
        set(scope["market_regions"]) - set(MARKET_REGIONS)
    )
    if unknown_regions:
        raise ContractError(
            "scope.market_regions 只能包含 North America / Europe / "
            "Australia / Other"
        )
    scope["market_regions"] = [
        region for region in MARKET_REGIONS if region in scope["market_regions"]
    ]

    raw_category_id = str(scope.get("category_id") or "").strip()
    if (
        raw_category_id == CROSS_CATEGORY_BUNDLE_ID
        or any(
            str(value).strip().casefold()
            == CROSS_CATEGORY_BUNDLE_NAME.casefold()
            for value in scope["categories"]
        )
    ):
        raise ContractError(
            "Cross-category Bundle 当前不支持生成单品类量化报告"
        )
    category_definition = (
        category_definition_by_id(raw_category_id)
        if raw_category_id
        else None
    )
    if raw_category_id and category_definition is None:
        raise ContractError("scope.category_id 不是已注册的稳定品类 ID")
    categories = scope["categories"]
    legacy_forms = {product_form_fact({"category": value})["value"] for value in categories
                    if value.casefold() in {"handheld sprayer", "backpack sprayer", "handheld spreader"}}
    if legacy_forms:
        if scope["product_form_factors"] and set(scope["product_form_factors"]) != legacy_forms:
            raise ContractError("产品形态与原品类范围不一致")
        scope["product_form_factors"] = sorted(legacy_forms)
    if category_definition is None and len(categories) == 1:
        derived_category_id = category_id_for(categories[0])
        category_definition = (
            category_definition_by_id(derived_category_id)
            if derived_category_id
            else None
        )
    if category_definition is not None:
        conflicting_categories = [
            value
            for value in categories
            if category_id_for(value) != category_definition.category_id
        ]
        if conflicting_categories:
            raise ContractError(
                "scope.category_id 与 scope.categories 不一致"
            )
        scope["category_id"] = category_definition.category_id
        scope["categories"] = [category_definition.display_name]
    else:
        scope["category_id"] = ""
        scope["categories"] = [
            canonical_category_name(value) for value in categories
        ]

    if set(scope["product_form_factors"]) - set(FORM_LABELS):
        raise ContractError("scope.product_form_factors 包含未定义的产品形态")
    if scope["product_form_factors"] and scope["category_id"] not in FORM_CATEGORIES:
        raise ContractError("该品类不适用产品形态筛选")

    invalid_platform_keys = [
        value
        for value in scope["voltage_platform_keys"]
        if VOLTAGE_PLATFORM_KEY_RE.fullmatch(value) is None
    ]
    if invalid_platform_keys:
        raise ContractError(
            "scope.voltage_platform_keys 只能包含数据采集 brand|voltage 稳定键"
        )

    invalid_platform_ids = [
        value
        for value in scope["voltage_platform_ids"]
        if VOLTAGE_PLATFORM_ID_RE.fullmatch(value) is None
    ]
    if invalid_platform_ids:
        raise ContractError(
            "scope.voltage_platform_ids 只能包含正式 vpf_ 稳定 ID"
        )
    if scope["voltage_platform_ids"]:
        raise ContractError(
            "scope.voltage_platform_ids 仅用于历史报告；新报告请使用 voltage_platform_keys"
        )
    include_unknown = scope.get("include_unknown_voltage_platform", False)
    if not isinstance(include_unknown, bool):
        raise ContractError(
            "scope.include_unknown_voltage_platform 必须是布尔值"
        )
    scope["include_unknown_voltage_platform"] = include_unknown

    energy_type = scope.get("energy_type", "")
    if not isinstance(energy_type, str):
        raise ContractError("scope.energy_type 必须是字符串")
    energy_type = energy_type.strip()
    if energy_type and energy_type not in REPORT_ENERGY_TYPES:
        raise ContractError(
            "scope.energy_type 只能是 all / corded / battery / fuel / other / unknown"
        )
    if energy_type and scope.get("category_id") != "pressure_washer":
        raise ContractError("能源类型筛选当前仅适用于 Pressure Washer")
    scope["energy_type"] = energy_type

    ratings = scope.get("ratings") or []
    if not isinstance(ratings, list):
        raise ContractError("scope.ratings 必须是数组")
    normalized_ratings = []
    for rating in ratings:
        if isinstance(rating, bool):
            raise ContractError("scope.ratings 只能包含 1–5")
        try:
            value = int(rating)
        except (TypeError, ValueError) as exc:
            raise ContractError("scope.ratings 只能包含 1–5") from exc
        if value not in {1, 2, 3, 4, 5}:
            raise ContractError("scope.ratings 只能包含 1–5")
        normalized_ratings.append(value)
    scope["ratings"] = sorted(set(normalized_ratings), reverse=True)

    has_text = str(scope.get("has_text") or "any").lower()
    if has_text not in TEXT_POLICIES:
        raise ContractError("scope.has_text 只能是 any / true / false")
    scope["has_text"] = has_text

    syndicated = str(scope.get("syndicated") or "include").lower()
    if syndicated not in SYNDICATION_POLICIES:
        raise ContractError("scope.syndicated 策略无效")
    scope["syndicated"] = syndicated

    fallback = str(scope.get("fallback_source") or "include").lower()
    if fallback not in FALLBACK_POLICIES:
        raise ContractError("scope.fallback_source 只能是 include / exclude")
    scope["fallback_source"] = fallback

    keyword_mode = str(scope.get("keyword_mode") or "or").lower()
    if keyword_mode not in KEYWORD_MODES:
        raise ContractError("scope.keyword_mode 只能是 or / and / exact")
    scope["keyword_mode"] = keyword_mode

    keyword_field = str(scope.get("keyword_field") or "title_and_body").lower()
    if keyword_field not in KEYWORD_FIELDS:
        raise ContractError(
            "scope.keyword_field 只能是 title_and_body / title / body"
        )
    scope["keyword_field"] = keyword_field

    date_range = scope.get("date_range") or {}
    if not isinstance(date_range, dict):
        raise ContractError("scope.date_range 必须是对象")
    start = _date_value(date_range.get("start"), "scope.date_range.start")
    end = _date_value(date_range.get("end"), "scope.date_range.end")
    if start and end and start > end:
        raise ContractError("开始日期不能晚于结束日期")
    scope["date_range"] = {
        key: value for key, value in (("start", start), ("end", end)) if value
    }

    verified = scope.get("verified_purchaser")
    if verified not in {None, "any"}:
        raise ContractError(
            "verified purchaser 当前不可用：false 与 missing 无法区分"
        )
    scope.pop("verified_purchaser", None)
    return scope


def normalize_request(
    payload: Any,
    *,
    defaults: dict[str, Any],
    analysis_version: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractError("请求必须是 JSON 对象")
    scope = normalize_scope(payload.get("scope"), defaults)
    if not scope.get("category_id") or len(scope.get("categories") or []) != 1:
        raise ContractError("完整量化报告必须选择且只能选择一个已注册品类")
    if scope["category_id"] == "pressure_washer" and not scope.get("energy_type"):
        raise ContractError("Pressure Washer 报告必须且只能选择一个能源类型")
    report_name = " ".join(str(payload.get("report_name") or "").split()).strip()
    if not report_name:
        report_name = automatic_report_title(scope)
    if len(report_name) > 120:
        raise ContractError("报告名称不能超过 120 个字符")

    primary = str(payload.get("primary_direction") or "product-opportunity")
    if primary not in SCENE_IDS:
        raise ContractError("primary_direction 不是受支持的研究场景")

    modules = _string_list(payload.get("analysis_modules"), "analysis_modules")
    unknown_modules = sorted(set(modules) - set(SCENE_IDS))
    if unknown_modules:
        raise ContractError(f"未知分析模块：{', '.join(unknown_modules)}")
    modules = [scene for scene in SCENE_IDS if scene in modules and scene != primary]

    raw_factors = payload.get("factors")
    if raw_factors is None:
        # A productized category report always runs the complete deterministic
        # extraction pack. The primary direction controls emphasis and reading
        # order; it must not silently remove baseline metrics that downstream
        # product-insight assembly requires.
        factors = list(FACTOR_IDS)
    else:
        factors = _string_list(raw_factors, "factors")
        unavailable = sorted(set(factors) & set(UNAVAILABLE_FACTOR_IDS))
        if unavailable:
            raise ContractError(f"当前不可用因子：{', '.join(unavailable)}")
        unknown_factors = sorted(set(factors) - set(FACTOR_IDS))
        if unknown_factors:
            raise ContractError(f"未知分析因子：{', '.join(unknown_factors)}")
        factors = [factor for factor in FACTOR_IDS if factor in factors]
    if not factors:
        raise ContractError("至少选择一个可用分析因子")
    if not set(factors) & set(SCENE_FACTORS.get(primary, ())):
        raise ContractError("至少保留一个与主研究场景相关的分析因子")

    return {
        "report_name": report_name,
        "analysis_version": analysis_version,
        "scope": scope,
        "primary_direction": primary,
        "analysis_modules": modules,
        "factors": factors,
    }
