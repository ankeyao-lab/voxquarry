from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from reporting.category_vocabulary import category_definition_by_id
from reporting.brand_persona_semantics import (
    COMPARISON_POLICY_VERSION,
    SEMANTIC_EXTRACTOR_VERSION,
    build_brand_semantics,
)
from reporting.config import MARKET_REGIONS
from reporting.ope_product_report import (
    EXTRACTOR_VERSION as OPE_EXTRACTOR_VERSION,
    OpeIndex,
    category_profile,
)
from reporting.source import UNSPECIFIED_MODEL, model_scope_value


BRAND_PERSONA_SCHEMA_VERSION = "brand-persona-workbench/v3"
BRAND_PERSONA_METHOD_VERSION = "brand-persona-deterministic/3.0.0"
BRAND_PERSONA_CACHE_VERSION = "|".join(
    (
        BRAND_PERSONA_METHOD_VERSION,
        SEMANTIC_EXTRACTOR_VERSION,
        OPE_EXTRACTOR_VERSION,
        COMPARISON_POLICY_VERSION,
    )
)
PERSONA_NARRATIVE_SCHEMA_VERSION = "brand-persona-narrative/v3"
PERSONA_NARRATIVE_POLICY_VERSION = "persona-claim-lock/3.0.0"
UNKNOWN_CATEGORY_ID = "unknown_category"
UNKNOWN_CATEGORY_NAME = "Unknown Category"

IDENTITY_DIMENSIONS = (
    "occupation",
    "operating_responsibility",
    "life_stage_ability",
    "experience_path",
    "self_reported_age",
    "self_reported_physical_constraint",
    "purchase_relationship",
    "usage_intensity",
    "brand_relationship",
    "purchase_trigger",
    "learning_support",
)

IDENTITY_COVERAGE_DIMENSIONS = (
    "occupation",
    "operating_responsibility",
    "life_stage_ability",
    "experience_path",
    "self_reported_age",
    "self_reported_physical_constraint",
    "purchase_relationship",
    "brand_relationship",
)

DIMENSION_META = {
    "occupation": ("明确职业 / 工作背景", "评论者明确自述了哪些职业或专业背景？"),
    "operating_responsibility": ("物业 / 场地责任", "评论者明确负责什么性质的场地？"),
    "life_stage_ability": ("年龄、体能与健康", "是否明确表达年龄、退休或操作能力约束？"),
    "experience_path": ("经验与迁移路径", "是否明确表达新手、经验或平台迁移路径？"),
    "self_reported_age": ("本人明确年龄段", "评论者是否以第一人称明确年龄？"),
    "self_reported_physical_constraint": ("体力与行动限制", "是否明确说明影响操作的身体条件？"),
    "purchase_relationship": ("购买与共用关系", "谁购买、谁使用、是否共同使用？"),
    "usage_intensity": ("使用频率与强度", "是否明确表达高频、长时、季节性或多场地使用？"),
    "brand_relationship": ("品牌与生态关系", "是已有平台用户、复购者、迁移者还是比较者？"),
    "purchase_trigger": ("购买触发", "什么明确事件触发购买或换机？"),
    "learning_support": ("学习与支持需求", "在哪些环节需要说明、排障或安全帮助？"),
}

FORBIDDEN_PERSONA_TERMS = (
    "收入",
    "国籍",
    "种族",
    "族裔",
    "教育程度",
    "婚姻状态",
)


SYSTEM_HYPOTHESIS_LIBRARY = {
    "runtime": (
        "验证能否用‘可完成任务量’而非空载分钟解释续航，并在选购与作业中提前暴露容量边界。",
        "任务一次完成率、补充充电次数、续航预估误差",
    ),
    "charging": (
        "验证充电状态、等待时间和多电池轮换提示能否减少任务中断与错误预期。",
        "等待时间、轮换成功率、未完成任务率",
    ),
    "weight_balance": (
        "验证重心、握持与搬运路径优化能否降低长时和重复任务中的身体负担。",
        "连续作业时长、主观疲劳、停顿和换手次数",
    ),
    "ergonomics": (
        "验证关键控制、握持姿态与调节范围能否覆盖该群组的真实身体条件和任务节奏。",
        "误操作率、调节耗时、舒适度与任务完成率",
    ),
    "assembly": (
        "验证开箱引导、免工具装配与错误预防能否缩短首次成功时间。",
        "首次成功时间、返工次数、支持请求率",
    ),
    "storage": (
        "验证折叠、壁挂、垂直收纳和搬运触点能否降低工具在非作业阶段的占用与摩擦。",
        "收纳时间、占用空间、搬运失误和损坏率",
    ),
    "battery_charger": (
        "验证电池健康透明、异常诊断和兼容性说明能否减少整套生态的连锁停机。",
        "诊断时间、误换件率、电池相关停机与退货率",
    ),
    "reliability": (
        "验证高频故障模式的设计修正与可恢复路径能否延长稳定使用周期。",
        "首次故障时间、重复故障率、恢复成功率",
    ),
    "starting": (
        "验证启动状态反馈与分步恢复指引能否把‘无法启动’从黑箱变成可操作问题。",
        "首次启动成功率、恢复时间、支持触达率",
    ),
    "maintenance_gasfree": (
        "验证低维护承诺是否覆盖清洁、耗材、季节存放和复用准备的完整成本。",
        "维护时长、耗材次数、季后恢复成功率",
    ),
    "value": (
        "验证以任务覆盖、生态复用和全生命周期成本表达价值，能否降低购买后的价值落差。",
        "购买转化、价值满意度、价格相关退货率",
    ),
    "safety": (
        "验证控制护栏、状态提示与异常停机能否提升复杂工况下的可控感。",
        "险情率、误触率、异常恢复成功率",
    ),
    "service": (
        "验证统一诊断、零件可得性和维修进度透明能否缩短从故障到恢复的链路。",
        "首次解决率、恢复时长、重复联系与退出率",
    ),
    "ecosystem": (
        "验证按任务负载推荐电池与附件组合，能否兑现跨品类复用而不制造兼容性误解。",
        "组合选择正确率、跨工具复用率、兼容相关退换率",
    ),
    "smart_connectivity": (
        "验证连接能力是否真正减少诊断、设置和维护负担，而不是新增配网步骤。",
        "功能启用率、任务节省时间、连接失败和弃用率",
    ),
}


class BrandPersonaContractError(ValueError):
    """Stable fail-closed error for the cross-category persona workbench."""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BrandPersonaContractError(f"{field} 必须是数组")
    rows = []
    for item in value:
        if not isinstance(item, str):
            raise BrandPersonaContractError(f"{field} 只能包含字符串")
        normalized = _clean(item)
        if normalized:
            rows.append(normalized)
    return sorted(set(rows))


def normalize_brand_persona_scope(raw_scope: Any) -> dict[str, Any]:
    if not isinstance(raw_scope, dict):
        raise BrandPersonaContractError("scope 必须是对象")
    brand = raw_scope.get("brand")
    if not isinstance(brand, str) or not _clean(brand):
        raise BrandPersonaContractError("品牌画像必须选择且只能选择一个品牌")
    scope = {
        "brand": _clean(brand),
        "category_ids": _string_list(
            raw_scope.get("category_ids"), "scope.category_ids"
        ),
        "models": _string_list(raw_scope.get("models"), "scope.models"),
        "channels": _string_list(raw_scope.get("channels"), "scope.channels"),
        "market_regions": _string_list(
            raw_scope.get("market_regions"), "scope.market_regions"
        ),
        "job_ids": _string_list(raw_scope.get("job_ids"), "scope.job_ids"),
        "date_range": dict(raw_scope.get("date_range") or {}),
    }
    unknown_regions = sorted(
        set(scope["market_regions"]) - set(MARKET_REGIONS)
    )
    if unknown_regions:
        raise BrandPersonaContractError(
            "scope.market_regions 只能包含 North America / Europe / Australia / Other"
        )
    for category_id in scope["category_ids"]:
        if (
            category_id != UNKNOWN_CATEGORY_ID
            and category_definition_by_id(category_id) is None
        ):
            raise BrandPersonaContractError(
                f"scope.category_ids 包含未知稳定品类 ID：{category_id}"
            )
    for boundary in ("start", "end"):
        value = _clean(scope["date_range"].get(boundary))
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise BrandPersonaContractError(
                f"scope.date_range.{boundary} 必须是 YYYY-MM-DD"
            )
        if value:
            scope["date_range"][boundary] = value
        else:
            scope["date_range"].pop(boundary, None)
    if (
        scope["date_range"].get("start")
        and scope["date_range"].get("end")
        and scope["date_range"]["start"] > scope["date_range"]["end"]
    ):
        raise BrandPersonaContractError("开始日期不能晚于结束日期")
    return scope


def resolve_brand_persona_job_ids(
    tasks: Iterable[dict[str, Any]], scope: dict[str, Any]
) -> list[str]:
    selected = []
    wanted_categories = set(scope.get("category_ids") or [])
    wanted_models = set(scope.get("models") or [])
    wanted_channels = set(scope.get("channels") or [])
    wanted_regions = set(scope.get("market_regions") or [])
    explicit_jobs = set(scope.get("job_ids") or [])
    brand = _clean(scope.get("brand")).casefold()
    all_ids = {_clean(task.get("id")) for task in tasks}
    if explicit_jobs - all_ids:
        raise BrandPersonaContractError("scope.job_ids 包含当前快照不存在的任务")
    for task in tasks:
        task_id = _clean(task.get("id"))
        if not task_id or _clean(task.get("brand")).casefold() != brand:
            continue
        if explicit_jobs and task_id not in explicit_jobs:
            continue
        if wanted_categories and _clean(task.get("category_id")) not in wanted_categories:
            continue
        if wanted_models and model_scope_value(task.get("model")) not in wanted_models:
            continue
        if wanted_channels and _clean(task.get("channel")) not in wanted_channels:
            continue
        if wanted_regions and _clean(task.get("market_region")) not in wanted_regions:
            continue
        selected.append(task_id)
    if not selected:
        raise BrandPersonaContractError("当前品牌与数据范围没有可用报告任务")
    return sorted(set(selected))


def _entity_ref(fact: dict[str, Any]) -> str:
    return _clean(
        fact.get("review_entity_id") or fact.get("review_occurrence_id")
    )


def _fact_content_hash(fact: dict[str, Any]) -> str:
    stored = _clean(fact.get("row_content_sha256"))
    if stored:
        return stored
    material = {
        key: fact.get(key)
        for key in (
            "review_entity_id",
            "review_occurrence_id",
            "job_id",
            "brand",
            "category_id",
            "model",
            "rating",
            "review_title",
            "review_text",
            "submission_date",
        )
    }
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical_facts(facts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        fact
        for fact in facts
        if fact.get("business_key_status") == "incomplete"
        or int(fact.get("canonical_entity_rank") or 1) == 1
    ]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _distribution(
    facts: Iterable[dict[str, Any]], field: str, *, text_only: bool = False
) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for fact in facts:
        if text_only and not fact.get("has_text"):
            continue
        value = _clean(fact.get(field)) or (
            UNSPECIFIED_MODEL if field == "model" else "未提供"
        )
        counter[value] += 1
    total = sum(counter.values())
    return [
        {field: key, "review_entities": value, "share": _ratio(value, total)}
        for key, value in sorted(
            counter.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def _rating_summary(facts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    ratings = [int(fact["rating"]) for fact in facts if fact.get("rating")]
    histogram = Counter(ratings)
    return {
        "valid_rating_entities": len(ratings),
        "average_rating": round(sum(ratings) / len(ratings), 4) if ratings else None,
        "positive_entities": sum(histogram[value] for value in (4, 5)),
        "positive_rate": _ratio(
            sum(histogram[value] for value in (4, 5)), len(ratings)
        ),
        "negative_entities": sum(histogram[value] for value in (1, 2)),
        "negative_rate": _ratio(
            sum(histogram[value] for value in (1, 2)), len(ratings)
        ),
        "histogram": {str(value): histogram[value] for value in range(1, 6)},
    }


def _representative_ids(
    indexes: list[OpeIndex], dimension_id: str, label: str, limit: int = 12
) -> list[str]:
    candidates: list[str] = []
    for index in indexes:
        for entity in sorted(index.dimension_entities.get(dimension_id, {}).get(label, set())):
            candidates.extend(
                index.dimension_evidence.get(dimension_id, {})
                .get(label, {})
                .get(entity, [])
            )
    return sorted(set(candidates))[:limit]


def _group_id(parts: Iterable[str]) -> str:
    material = "|".join(str(value) for value in parts)
    return "bpg_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _dimension_rows(indexes: list[OpeIndex], denominator: int) -> list[dict[str, Any]]:
    rows = []
    for dimension_id in IDENTITY_DIMENSIONS:
        title, question = DIMENSION_META[dimension_id]
        label_entities: dict[str, set[str]] = defaultdict(set)
        label_display: dict[str, str] = {}
        category_entities: dict[str, set[str]] = defaultdict(set)
        for index in indexes:
            for dimension in index.dimensions:
                if dimension.dimension_id == dimension_id:
                    label_display.update(
                        {rule.label: rule.display for rule in dimension.rules}
                    )
            for label, entities in index.dimension_entities.get(
                dimension_id, {}
            ).items():
                label_entities[label].update(entities)
                category_entities[index.profile_id].update(entities)
        matched = set().union(*label_entities.values()) if label_entities else set()
        values = []
        for label, entities in sorted(
            label_entities.items(), key=lambda item: (-len(item[1]), item[0])
        ):
            values.append(
                {
                    "value_id": label,
                    "label": label_display.get(label, label.replace("_", " ")),
                    "review_entities": len(entities),
                    "coverage": _ratio(len(entities), denominator),
                    "evidence_group_id": _group_id((dimension_id, label)),
                    "evidence_ids": _representative_ids(
                        indexes, dimension_id, label
                    ),
                }
            )
        rows.append(
            {
                "dimension_id": dimension_id,
                "title": title,
                "question": question,
                "review_entities": len(matched),
                "coverage": _ratio(len(matched), denominator),
                "unknown_entities": max(0, denominator - len(matched)),
                "unknown_rate": _ratio(max(0, denominator - len(matched)), denominator),
                "values": values,
                "boundary": (
                    "仅统计评论或来源字段中明确表达；未知不表示没有该特征，"
                    "也不允许从任务、材料或共用关系补写身份。"
                ),
            }
        )
    return rows


def _cohort_entities(
    indexes: list[OpeIndex],
    dimensions: Iterable[tuple[str, set[str]]],
) -> set[str]:
    entities: set[str] = set()
    for index in indexes:
        for dimension_id, labels in dimensions:
            entities.update(index.entities_for(dimension_id, labels))
    return entities


def _cohort_evidence(
    indexes: list[OpeIndex], entities: set[str], dimension_ids: Iterable[str]
) -> list[str]:
    evidence = []
    for index in indexes:
        local = entities & index.all_entities
        if local:
            evidence.extend(
                index.evidence_for_entities(
                    local, dimension_ids=tuple(dimension_ids), limit=8
                )
            )
    return sorted(set(evidence))[:18]


def _deterministic_personas(
    indexes: list[OpeIndex],
    facts: list[dict[str, Any]],
    denominator: int,
    semantic: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
]:
    specs = (
        (
            "professional_operator",
            "跨场地专业作业者",
            "明确自述相关职业，或明确承担客户、商业或工作任务。",
            (
                ("occupation", {
                    "landscape_tree_professional",
                    "facilities_grounds", "agriculture_operator",
                    "cleaning_detailing_professional", "snow_removal_professional",
                }),
                ("operating_responsibility", {"commercial_service_operator"}),
            ),
            "以稳定效率和低停机完成多个场地或客户任务。",
        ),
        (
            "residential_self_maintainer",
            "住宅与庭院自维护者",
            "明确说明自己承担住宅、庭院、车道或居家清洁维护。",
            (("operating_responsibility", {
                "residential_self_maintainer", "homeowner_owner_operator",
            }),),
            "用可控时间与身体负担独立完成自有物业维护。",
        ),
        (
            "multi_property_caretaker",
            "多物业 / 机构场地维护者",
            "明确负责出租物业、机构公共场地或农牧果园。",
            (("operating_responsibility", {
                "landlord_property_manager", "institutional_site_caretaker",
                "farm_ranch_property",
            }),),
            "跨场地复用工具、电池与维护流程，并减少切换和停机。",
        ),
        (
            "older_or_retired_operator",
            "明确年长 / 体力受限作业者",
            "正文第一人称明确为 65+、退休或存在身体限制；不使用来源 age 字段，也不从产品偏好推断年龄。",
            (
                ("self_reported_age", {"age_65_74", "age_75_plus", "senior_self"}),
                ("life_stage_ability", {"senior", "retired"}),
                ("self_reported_physical_constraint", {"strength_grip", "mobility", "joint_back"}),
            ),
            "在体能与安全边界内降低启动、搬运和长时间操作负担。",
        ),
        (
            "first_time_or_switcher",
            "首次使用 / 平台迁移者",
            "明确自述新手，或从燃油、有线及其他平台迁移。",
            (("experience_path", {
                "first_time_user", "gas_switcher", "corded_switcher",
                "platform_switcher",
            }),),
            "快速建立正确预期，减少首次上手和生态迁移成本。",
        ),
        (
            "battery_ecosystem_owner",
            "电池生态复用者",
            "明确拥有同平台电池或工具，并把兼容性作为购买与使用上下文。",
            (("experience_path", {"battery_ecosystem_owner"}),),
            "跨品类复用电池、充电和收纳体系，同时看清容量与负载边界。",
        ),
        (
            "high_intensity_operator",
            "高频 / 长时作业者",
            "明确表达每日、每周、长时间、季节性高强度或多场地使用。",
            (("usage_intensity", {"daily_frequent", "weekly_routine", "long_session", "seasonal_use", "multi_site_use"}),),
            "在重复与长时任务中维持稳定完成度，并降低疲劳、等待和维护中断。",
        ),
        (
            "experienced_brand_switcher",
            "经验型换机与跨品牌比较者",
            "明确从燃油、有线、旧机或竞品迁移，并用既有经验评价新平台。",
            (
                ("experience_path", {"gas_switcher", "corded_switcher", "previous_product_owner", "long_time_user"}),
                ("brand_relationship", {"competitor_switcher", "cross_brand_comparer"}),
            ),
            "用熟悉的任务标准验证新平台是否真正降低操作成本而不牺牲结果。",
        ),
    )
    fact_by_entity = {_entity_ref(fact): fact for fact in facts}
    driver_rows = semantic.get("driver_rows_internal") or []
    experience_rows = semantic.get("experience_rows_internal") or []
    lifecycle_rows = semantic.get("lifecycle_rows_internal") or []
    semantic_index = semantic["index"]

    def related_rows(
        entities: set[str],
        rows: list[dict[str, Any]],
        *,
        source_type: str,
        polarity: str | None = None,
    ) -> list[dict[str, Any]]:
        ranked = []
        minimum_support = max(2, round(len(entities) * 0.03))
        for row in rows:
            if polarity == "negative":
                source = set(row.get("negative_entity_ids") or set())
            elif polarity == "positive":
                source = set(row.get("positive_entity_ids") or set())
            else:
                source = set(row.get("entity_ids") or set())
            overlap = entities & source
            if len(overlap) < minimum_support:
                continue
            cohort_rate = len(overlap) / len(entities) if entities else 0.0
            baseline_rate = len(source) / denominator if denominator else 0.0
            index_vs_brand = cohort_rate / baseline_rate if baseline_rate else None
            if source_type in {"decision_driver", "lifecycle_stage"}:
                evidence_ids = semantic_index.evidence_for_entities(
                    overlap,
                    dimension_ids=(source_type,),
                    dimension_label_filters={source_type: {row["item_id"]}},
                    limit=12,
                    prefer_low=polarity == "negative",
                )
            else:
                evidence_ids = semantic_index.evidence_for_entities(
                    overlap,
                    aspects=(row["item_id"],),
                    polarity=polarity,
                    limit=12,
                    prefer_low=polarity == "negative",
                )
            ranked.append(
                {
                    "item_id": row["item_id"],
                    "title": row["title"],
                    "review_entities": len(overlap),
                    "cohort_rate": round(cohort_rate, 6),
                    "brand_rate": round(baseline_rate, 6),
                    "index_vs_brand": round(index_vs_brand, 3)
                    if index_vs_brand is not None
                    else None,
                    "strength_score": round(
                        math.sqrt(len(overlap))
                        * math.sqrt(max(0.25, min(index_vs_brand or 1.0, 6.0))),
                        4,
                    ),
                    "evidence_ids": evidence_ids,
                }
            )
        ranked.sort(
            key=lambda item: (
                -item["strength_score"],
                -item["review_entities"],
                item["item_id"],
            )
        )
        return ranked

    personas = []
    claims: dict[str, dict[str, Any]] = {}
    persona_memberships: list[dict[str, Any]] = []
    for persona_id, title, definition, dimensions, jtbd in specs:
        entities = _cohort_entities(indexes, dimensions)
        if not entities:
            continue
        dimension_ids = tuple(dimension_id for dimension_id, _ in dimensions)
        evidence_ids = _cohort_evidence(indexes, entities, dimension_ids)
        for entity in sorted(entities):
            fact = fact_by_entity.get(entity)
            if fact is None:
                continue
            quote = "\n".join(
                value
                for value in (
                    _clean(fact.get("review_title")),
                    _clean(fact.get("review_text")),
                )
                if value
            )
            persona_memberships.append(
                {
                    "evidence_id": "ev_bp_"
                    + hashlib.sha256(
                        f"{persona_id}|{entity}".encode("utf-8")
                    ).hexdigest()[:24],
                    "review_entity_id": entity,
                    "kind": "persona_membership",
                    "label": persona_id,
                    "persona_id": persona_id,
                    "persona_title": title,
                    "quote": quote,
                    "matched_text": quote,
                    "rating": fact.get("rating"),
                    "job_id": _clean(fact.get("job_id")),
                    "brand": _clean(fact.get("brand")),
                    "category_id": _clean(fact.get("category_id")),
                    "category": _clean(fact.get("category")),
                    "model": _clean(fact.get("model")),
                    "channel": _clean(fact.get("channel")),
                    "market_region": _clean(fact.get("market_region")),
                    "submission_date": _clean(fact.get("submission_date")),
                    "product_url": _clean(fact.get("product_url")),
                    "boundary": (
                        "该原文只证明评论命中此 Persona 的显式规则，不证明 Persona "
                        "之间互斥，也不代表品牌人口占比。"
                    ),
                }
            )
        categories = Counter(
            _clean(fact_by_entity[entity].get("category"))
            for entity in entities
            if entity in fact_by_entity
        )
        models = Counter(
            model_scope_value(fact_by_entity[entity].get("model"))
            for entity in entities
            if entity in fact_by_entity
        )
        identity_claim_id = f"claim_{persona_id}_identity"
        identity_claim = {
            "claim_id": identity_claim_id,
            "persona_id": persona_id,
            "claim_type": "deterministic_cohort",
            "title": title,
            "definition": definition,
            "review_entities": len(entities),
            "coverage": _ratio(len(entities), denominator),
            "category_count": len(categories),
            "model_count": len(models),
            "top_categories": [
                {"category": key, "review_entities": value}
                for key, value in categories.most_common(4)
            ],
            "top_models": [
                {"model": key, "review_entities": value}
                for key, value in models.most_common(4)
            ],
            "evidence_group_id": _group_id(("persona", persona_id)),
            "evidence_ids": evidence_ids,
            "method": "explicit-rule-union",
        }
        claims[identity_claim_id] = identity_claim
        claim_ids = [identity_claim_id]
        driver_signals = related_rows(
            entities, driver_rows, source_type="decision_driver"
        )[:4]
        experience_signals = related_rows(
            entities, experience_rows, source_type="aspect"
        )[:4]
        positive_signals = related_rows(
            entities,
            experience_rows,
            source_type="aspect",
            polarity="positive",
        )[:4]
        pain_signals = related_rows(
            entities,
            experience_rows,
            source_type="aspect",
            polarity="negative",
        )[:4]
        lifecycle_signals = related_rows(
            entities, lifecycle_rows, source_type="lifecycle_stage"
        )[:4]

        for claim_type, rows in (
            ("decision_driver", driver_signals[:4]),
            ("experience_priority", experience_signals[:4]),
            ("pain_point", pain_signals[:4]),
            ("lifecycle_stage", lifecycle_signals[:4]),
        ):
            for row in rows:
                claim_id = f"claim_{persona_id}_{claim_type}_{row['item_id']}"
                claims[claim_id] = {
                    "claim_id": claim_id,
                    "persona_id": persona_id,
                    "claim_type": claim_type,
                    "title": row["title"],
                    "review_entities": row["review_entities"],
                    "cohort_rate": row["cohort_rate"],
                    "brand_rate": row["brand_rate"],
                    "index_vs_brand": row["index_vs_brand"],
                    "evidence_ids": row["evidence_ids"],
                    "method": "cohort-intersection-and-lift",
                }
                claim_ids.append(claim_id)

        success_criteria = [
            {
                "title": row["title"],
                "review_entities": row["review_entities"],
                "evidence_ids": row["evidence_ids"],
            }
            for row in positive_signals[:3]
        ]
        product_hypotheses = []
        for row in pain_signals[:3]:
            hypothesis, success_metrics = SYSTEM_HYPOTHESIS_LIBRARY.get(
                row["item_id"],
                (
                    f"验证能否系统性降低“{row['title']}”相关摩擦。",
                    "任务完成率、恢复时间和明确差评率",
                ),
            )
            hypothesis_claim_id = (
                f"claim_{persona_id}_product_hypothesis_{row['item_id']}"
            )
            claims[hypothesis_claim_id] = {
                "claim_id": hypothesis_claim_id,
                "persona_id": persona_id,
                "claim_type": "product_hypothesis_basis",
                "title": row["title"],
                "review_entities": row["review_entities"],
                "cohort_rate": row["cohort_rate"],
                "brand_rate": row["brand_rate"],
                "index_vs_brand": row["index_vs_brand"],
                "evidence_ids": row["evidence_ids"],
                "method": "negative-cohort-signal-to-testable-hypothesis",
            }
            claim_ids.append(hypothesis_claim_id)
            product_hypotheses.append(
                {
                    "claim_id": hypothesis_claim_id,
                    "system_id": row["item_id"],
                    "problem": f"该群组有 {row['review_entities']:,} 条正文明确表达“{row['title']}”负向体验。",
                    "hypothesis": hypothesis,
                    "success_metrics": success_metrics,
                    "basis": {
                        "review_entities": row["review_entities"],
                        "cohort_rate": row["cohort_rate"],
                        "index_vs_brand": row["index_vs_brand"],
                    },
                    "evidence_ids": row["evidence_ids"],
                    "status": "evidence_backed_hypothesis",
                }
            )
        category_count = len(categories)
        evidence_strength = (
            "high"
            if len(entities) >= 100 and category_count >= 3 and len(claim_ids) >= 5
            else "medium"
            if len(entities) >= 30 and category_count >= 2 and len(claim_ids) >= 4
            else "limited"
        )
        personas.append(
            {
                "persona_id": persona_id,
                "title": title,
                "definition": definition,
                "jtbd": jtbd,
                "claim_ids": claim_ids,
                **{
                    key: value
                    for key, value in identity_claim.items()
                    if key not in {"claim_id", "persona_id", "title", "definition"}
                },
                "decision_drivers": driver_signals,
                "experience_priorities": experience_signals,
                "pain_points": pain_signals,
                "lifecycle_stages": lifecycle_signals,
                "success_criteria": success_criteria,
                "product_hypotheses": product_hypotheses,
                "evidence_strength": {
                    "grade": evidence_strength,
                    "explicit_identity_entities": len(entities),
                    "category_count": category_count,
                    "model_count": len(models),
                    "independent_claim_count": len(claim_ids),
                    "representative_evidence_count": len(
                        set(
                            evidence_ids
                            + [
                                evidence_id
                                for signal in (
                                    driver_signals
                                    + experience_signals
                                    + pain_signals
                                    + lifecycle_signals
                                )
                                for evidence_id in signal.get("evidence_ids") or []
                            ]
                        )
                    ),
                    "rule": (
                        "high: ≥100 显式样本、≥3 品类、≥5 claims；medium: ≥30 样本、"
                        "≥2 品类、≥4 claims；其余 limited。"
                    ),
                },
                "research_questions": [
                    f"该群组如何定义‘{jtbd}’已经完成？",
                    "现有产品在哪个任务阶段最容易打断工作流？",
                    "评论中没有表达、但必须通过访谈验证的购买与使用约束是什么？",
                ],
                "recruiting_criteria": definition,
                "cannot_infer": [
                    "收入、教育、国籍与未明确表达的家庭结构",
                    "群组在品牌全部购买者中的真实人口占比",
                    "同一评论者是否跨多个来源或品类重复出现",
                ],
                "narrative_origin": "deterministic_fallback",
                "image": None,
                "evidence_count": len(entities),
                "boundary": "规则群组可重叠，不代表品牌总体用户人口占比。",
            }
        )
    # UI and Agent consumers rely on the most evidenced cohort appearing first.
    # Claim count is a quality annotation, never the ordering denominator.
    personas.sort(key=lambda row: (-row["review_entities"], row["persona_id"]))
    personas = personas[:8]
    kept = {row["persona_id"] for row in personas}
    claims = {
        claim_id: claim
        for claim_id, claim in claims.items()
        if claim.get("persona_id") in kept
    }
    return (
        personas,
        claims,
        [
            row
            for row in persona_memberships
            if row.get("persona_id") in kept
        ],
    )


def _task_context_rows(
    indexes: list[OpeIndex],
    denominator: int,
    category_ids_by_profile: dict[str, str],
) -> list[dict[str, Any]]:
    rows = []
    for index in indexes:
        dimensions = []
        for dimension in index.dimensions:
            if dimension.dimension_id in IDENTITY_DIMENSIONS:
                continue
            values = index.dimension_entities.get(dimension.dimension_id, {})
            matched = set().union(*values.values()) if values else set()
            if not matched:
                continue
            dimensions.append(
                {
                    "dimension_id": dimension.dimension_id,
                    "title": dimension.title,
                    "review_entities": len(matched),
                    "coverage": _ratio(len(matched), len(index.all_entities)),
                    "values": [
                        {
                            "value_id": label,
                            "label": next(
                                (
                                    rule.display
                                    for rule in dimension.rules
                                    if rule.label == label
                                ),
                                label,
                            ),
                            "review_entities": len(entities),
                            "evidence_ids": _representative_ids(
                                [index], dimension.dimension_id, label, 6
                            ),
                        }
                        for label, entities in sorted(
                            values.items(), key=lambda item: (-len(item[1]), item[0])
                        )[:6]
                    ],
                }
            )
        rows.append(
            {
                "category_id": category_ids_by_profile[index.profile_id],
                "profile_id": index.profile_id,
                "category": index.category_label,
                "written_review_entities": len(index.all_entities),
                "share_of_written": _ratio(len(index.all_entities), denominator),
                "dimensions": dimensions,
            }
        )
    return sorted(rows, key=lambda row: (-row["written_review_entities"], row["category"]))


def _data_quality_summary(
    input_facts: list[dict[str, Any]],
    canonical: list[dict[str, Any]],
    text_facts: list[dict[str, Any]],
    *,
    supported_category_ids: set[str],
) -> dict[str, Any]:
    unique_complete_entities = len(
        {
            _clean(fact.get("review_entity_id"))
            for fact in input_facts
            if fact.get("business_key_status") == "complete"
            and _clean(fact.get("review_entity_id"))
        }
    )
    incomplete_identity_occurrences = sum(
        fact.get("business_key_status") == "incomplete" for fact in input_facts
    )
    duplicate_occurrences_excluded = sum(
        bool(fact.get("is_duplicate_occurrence")) for fact in input_facts
    )
    dated = sorted(
        _clean(fact.get("submission_date"))
        for fact in canonical
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", _clean(fact.get("submission_date")))
    )
    ratings = sum(fact.get("rating") is not None for fact in canonical)
    syndicated_known = sum(
        isinstance(fact.get("is_syndicated"), bool) for fact in canonical
    )
    syndicated_true = sum(fact.get("is_syndicated") is True for fact in canonical)
    supported_written = sum(
        _clean(fact.get("category_id")) in supported_category_ids
        for fact in text_facts
    )
    category_counts = Counter(_clean(fact.get("category_id")) for fact in canonical)
    total = len(canonical)
    if unique_complete_entities + incomplete_identity_occurrences != total:
        raise BrandPersonaContractError(
            "品牌画像实体总体无法由完整实体与不完整身份出现记录对账"
        )
    hhi = sum((count / total) ** 2 for count in category_counts.values()) if total else 0
    largest_category, largest_count = (
        category_counts.most_common(1)[0] if category_counts else ("", 0)
    )
    return {
        "input_fact_rows": len(input_facts),
        "analysis_review_entities": total,
        "canonical_review_entities": unique_complete_entities,
        "unique_complete_review_entities": unique_complete_entities,
        "incomplete_identity_occurrences": incomplete_identity_occurrences,
        "duplicate_occurrences_excluded": duplicate_occurrences_excluded,
        "noncanonical_occurrences_excluded": duplicate_occurrences_excluded,
        "written_review_entities": len(text_facts),
        "written_coverage": _ratio(len(text_facts), total),
        "rating_coverage": _ratio(ratings, total),
        "date_parse_coverage": _ratio(len(dated), total),
        "date_range": {
            "start": dated[0] if dated else None,
            "end": dated[-1] if dated else None,
        },
        "syndication_known_entities": syndicated_known,
        "syndication_known_coverage": _ratio(syndicated_known, total),
        "syndicated_entities": syndicated_true,
        "ambiguous_source_age_fields_ignored": sum(
            bool(_clean(fact.get("age"))) for fact in text_facts
        ),
        "supported_profile_written_entities": supported_written,
        "supported_profile_written_coverage": _ratio(supported_written, len(text_facts)),
        "category_concentration": {
            "largest_category_id": largest_category,
            "largest_category_entities": largest_count,
            "largest_category_share": _ratio(largest_count, total),
            "hhi": round(hhi, 6),
            "warning": (
                "样本高度集中于单一品类，品牌级 Persona 必须结合品类修正阅读。"
                if total and largest_count / total >= 0.6
                else None
            ),
        },
        "population_rules": {
            "rating_population": (
                "完整业务键去重实体 + 无法形成跨任务实体键但仍合格的独立出现记录"
            ),
            "semantic_population": "其中所有非空正文；不设置最少词数",
            "identity_population": "正文中明确自述身份、责任、经验或身体条件的实体",
        },
    }


def build_brand_persona_analysis(
    facts: list[dict[str, Any]],
    scope: dict[str, Any],
    *,
    snapshot_id: str,
) -> dict[str, Any]:
    normalized_scope = normalize_brand_persona_scope(scope)
    brand = normalized_scope["brand"].casefold()
    input_facts = [
        fact for fact in facts if _clean(fact.get("brand")).casefold() == brand
    ]
    canonical = _canonical_facts(input_facts)
    canonical = [
        fact for fact in canonical if _clean(fact.get("brand")).casefold() == brand
    ]
    if not canonical:
        raise BrandPersonaContractError("当前范围没有该品牌的合规评论实体")
    text_facts = [fact for fact in canonical if fact.get("has_text")]
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in text_facts:
        by_category[_clean(fact.get("category_id"))].append(fact)
    indexes = []
    category_ids_by_profile: dict[str, str] = {}
    unsupported_categories = []
    for category_id, category_facts in sorted(by_category.items()):
        profile = category_profile(category_id)
        if profile is None:
            unsupported_categories.append(category_id or "unknown")
            continue
        context_dimensions = tuple(
            dimension
            for dimension in profile.dimensions
            if dimension.dimension_id not in IDENTITY_DIMENSIONS
        )
        index = OpeIndex(
            category_facts,
            profile_id=profile.profile_id,
            category_label=profile.category_label,
            dimensions=context_dimensions,
            numeric_dimension_ids=profile.numeric_dimension_ids,
            aspect_rules=(),
        )
        index.run()
        indexes.append(index)
        category_ids_by_profile[index.profile_id] = category_id
    written_entities = len({_entity_ref(fact) for fact in text_facts})
    semantic = build_brand_semantics(text_facts, normalized_scope["brand"])
    semantic_index = semantic["index"]
    dimensions = _dimension_rows([semantic_index], written_entities)
    deterministic_personas, claim_pool, persona_memberships = _deterministic_personas(
        [semantic_index], text_facts, written_entities, semantic
    )
    evidence_by_id: dict[str, dict[str, Any]] = {}
    fact_by_entity = {_entity_ref(fact): fact for fact in text_facts}
    for row in semantic["evidence"]:
        evidence_by_id[row["evidence_id"]] = row
    for row in persona_memberships:
        evidence_by_id[row["evidence_id"]] = row
    for index in indexes:
        for evidence_id, row in index.evidence.rows.items():
            fact = fact_by_entity.get(_clean(row.get("review_entity_id")), {})
            evidence_by_id.setdefault(
                evidence_id,
                {
                    **row,
                    "job_id": _clean(fact.get("job_id")),
                    "category_id": _clean(fact.get("category_id")),
                    "category": _clean(fact.get("category")),
                    "model": _clean(fact.get("model")),
                    "channel": _clean(fact.get("channel")),
                    "market_region": _clean(fact.get("market_region")),
                    "submission_date": _clean(fact.get("submission_date")),
                },
            )
    identity_entities: set[str] = set()
    for dimension_id in IDENTITY_COVERAGE_DIMENSIONS:
        identity_entities.update(semantic_index.entities_for(dimension_id))
    ratings = _rating_summary(canonical)
    all_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in canonical:
        all_by_category[_clean(fact.get("category_id"))].append(fact)
    category_rows = []
    for category_id, category_facts in sorted(all_by_category.items()):
        definition = category_definition_by_id(category_id)
        category_label = (
            definition.display_name
            if definition
            else UNKNOWN_CATEGORY_NAME
            if category_id == UNKNOWN_CATEGORY_ID
            else _clean(category_facts[0].get("category")) or "未提供"
        )
        category_rows.append(
            {
                "category_id": category_id,
                "category": category_label,
                "review_entities": len(category_facts),
                "written_review_entities": sum(bool(fact.get("has_text")) for fact in category_facts),
                **_rating_summary(category_facts),
            }
        )
    identity_material = {
        "schema_version": BRAND_PERSONA_SCHEMA_VERSION,
        "method_version": BRAND_PERSONA_METHOD_VERSION,
        "cache_version": BRAND_PERSONA_CACHE_VERSION,
        "semantic_versions": {
            **semantic["versions"],
            "ope_extractor": OPE_EXTRACTOR_VERSION,
        },
        "snapshot_id": snapshot_id,
        "scope": normalized_scope,
        "fact_hashes": sorted(_fact_content_hash(fact) for fact in canonical),
    }
    content_hash = hashlib.sha256(
        json.dumps(identity_material, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    modules = semantic["modules"]
    task_context = _task_context_rows(
        indexes, written_entities, category_ids_by_profile
    )
    top_driver = next(iter(modules["decision_drivers"]), None)
    top_experience = next(iter(modules["experience_systems"]), None)
    top_failure = next(iter(modules["failure_chains"]), None)
    top_persona = next(iter(deterministic_personas), None)
    top_negative_experience = next(
        iter(
            sorted(
                (
                    row
                    for row in modules["experience_systems"]
                    if row.get("review_entities", 0) >= max(30, round(written_entities * 0.005))
                ),
                key=lambda row: (
                    -row.get("explicit_negative_rate", 0),
                    -row.get("negative_entities", 0),
                ),
            )
        ),
        None,
    )
    top_lifecycle = next(iter(modules["lifecycle_stages"]), None)
    top_cross_category = next(
        (
            row
            for row in modules["cross_category_signals"]
            if row.get("brand_level_signal")
        ),
        None,
    )
    top_cooccurrence = next(iter(modules["cooccurrence_signals"]), None)
    temporal = modules.get("temporal_trends") or {}
    top_temporal = next(iter(temporal.get("signals") or []), None)
    executive_readout = [
        {
            "readout_id": "sample_boundary",
            "title": "先看样本边界",
            "statement": (
                f"当前范围含 {len(canonical):,} 条唯一评论，其中 {written_entities:,} 条有正文；"
                "评分总体与语义总体使用不同分母。"
            ),
            "metric_refs": ["sample.review_entities", "sample.written_review_entities"],
            "product_use": "先决定哪些判断可以代表全部评分、哪些只能代表主动写正文的用户。",
            "boundary": "ratings-only 进入评分总体，但不进入语义提取。",
        },
        *(
            [
                {
                    "readout_id": "leading_driver",
                    "title": "最常被明确表达的选型标准",
                    "statement": (
                        f"“{top_driver['title']}”由 {top_driver['review_entities']:,} 条正文明确支持，"
                        f"覆盖 {top_driver['coverage']:.1%}。"
                    ),
                    "metric_refs": [f"decision_drivers.{top_driver['item_id']}"],
                    "evidence_ids": top_driver.get("evidence_ids") or [],
                    "product_use": "作为概念测试、商品页信息架构和访谈追问的首要选择标准，而不是直接等同购买因果。",
                    "boundary": "覆盖率表示评论中明确提及，不表示未提及者不在意。",
                }
            ]
            if top_driver
            else []
        ),
        *(
            [
                {
                    "readout_id": "leading_experience",
                    "title": "最广泛的系统体验",
                    "statement": (
                        f"“{top_experience['title']}”出现在 {top_experience['review_entities']:,} 条正文中；"
                        "需同时查看明确正负体验与品类构成。"
                    ),
                    "metric_refs": [f"experience_systems.{top_experience['item_id']}"],
                    "evidence_ids": (top_experience.get("positive_evidence_ids") or [])[:8],
                    "product_use": "拆分其正向守护点与负向问题，不因高提及量就自动判定为优势或痛点。",
                    "boundary": "同一系统可同时有正负体验，需结合品类、型号和原文。",
                }
            ]
            if top_experience
            else []
        ),
        *(
            [
                {
                    "readout_id": "highest_negative_system",
                    "title": "负向表达最集中的体验系统",
                    "statement": (
                        f"“{top_negative_experience['title']}”在 {top_negative_experience['review_entities']:,} 条相关正文中，"
                        f"有 {top_negative_experience['negative_entities']:,} 条明确负向表达（"
                        f"{top_negative_experience['explicit_negative_rate']:.1%}）。"
                    ),
                    "metric_refs": [f"experience_systems.{top_negative_experience['item_id']}"],
                    "evidence_ids": top_negative_experience.get("negative_evidence_ids") or [],
                    "product_use": "进入问题分型、工程复现和服务恢复设计；先找发生工况，再谈解决方案。",
                    "boundary": "负向表达率以提及该系统的正文为分母，不是产品故障率。",
                }
            ]
            if top_negative_experience
            else []
        ),
        *(
            [
                {
                    "readout_id": "largest_failure_corridor",
                    "title": "最大的故障—恢复观察链",
                    "statement": (
                        f"“{top_failure['title']}”含 {top_failure['negative_entities']:,} 条明确负向正文；"
                        "该链是同评论共现，不能解释为因果。"
                    ),
                    "metric_refs": [f"failure_chains.{top_failure['chain_id']}"],
                    "evidence_ids": top_failure.get("evidence_ids") or [],
                    "product_use": "把故障发现、用户诊断、售后恢复和退出作为一条完整体验链验证。",
                    "boundary": top_failure.get("boundary"),
                }
            ]
            if top_failure
            else []
        ),
        *(
            [
                {
                    "readout_id": "largest_evidence_cohort",
                    "title": "证据最完整的 Persona",
                    "statement": (
                        f"“{top_persona['title']}”由 {top_persona['review_entities']:,} 条显式样本和 "
                        f"{len(top_persona['claim_ids'])} 条独立 claim 组成。"
                    ),
                    "metric_refs": top_persona["claim_ids"],
                    "evidence_ids": top_persona.get("evidence_ids") or [],
                    "product_use": "用于招募与任务测试；先验证该群组的任务成功标准和关键摩擦。",
                    "boundary": "Persona 群组可重叠，显式样本数不是品牌人口占比。",
                }
            ]
            if top_persona
            else []
        ),
        *(
            [
                {
                    "readout_id": "leading_lifecycle_stage",
                    "title": "评论最集中出现的生命周期阶段",
                    "statement": (
                        f"“{top_lifecycle['title']}”由 {top_lifecycle['review_entities']:,} 条正文明确支持，"
                        f"覆盖语义总体 {top_lifecycle['coverage']:.1%}。"
                    ),
                    "metric_refs": [f"lifecycle_stages.{top_lifecycle['item_id']}"],
                    "evidence_ids": top_lifecycle.get("evidence_ids") or [],
                    "product_use": "判断网评更擅长解释哪一段旅程，并据此补访谈、客服或行为数据。",
                    "boundary": "评论天然偏向购买后体验，不代表完整漏斗构成。",
                }
            ]
            if top_lifecycle
            else []
        ),
        *(
            [
                {
                    "readout_id": "brand_level_cross_category_signal",
                    "title": "跨品类共同出现的品牌级信号",
                    "statement": (
                        f"“{top_cross_category['title']}”在 {top_cross_category['supported_category_count']} 个品类达到支持门槛，"
                        "可作为跨品类体验系统候选。"
                    ),
                    "metric_refs": [f"cross_category_signals.{top_cross_category['item_id']}"],
                    "product_use": "评估是否值得建立品牌级规范、平台能力或统一研究量表。",
                    "boundary": top_cross_category.get("boundary"),
                }
            ]
            if top_cross_category
            else []
        ),
        *(
            [
                {
                    "readout_id": "strongest_choice_cooccurrence",
                    "title": "最强的选择标准组合",
                    "statement": (
                        f"“{top_cooccurrence['left_title']}”与“{top_cooccurrence['right_title']}”"
                        f"在 {top_cooccurrence['joint_entities']:,} 条正文共同出现，Lift {top_cooccurrence['lift']:.2f}×。"
                    ),
                    "metric_refs": [
                        f"decision_drivers.{top_cooccurrence['left_id']}",
                        f"decision_drivers.{top_cooccurrence['right_id']}",
                    ],
                    "product_use": "用于构造取舍题、信息组合与概念包，不把两个标准拆成孤立卖点。",
                    "boundary": top_cooccurrence.get("boundary"),
                }
            ]
            if top_cooccurrence and top_cooccurrence.get("lift") is not None
            else []
        ),
        *(
            [
                {
                    "readout_id": "largest_same_period_change",
                    "title": "同周期变化最大的观察信号",
                    "statement": (
                        f"“{top_temporal['title']}”从 {top_temporal['baseline_year']} 年同周期的 "
                        f"{top_temporal['baseline_rate']:.1%} 变为 {top_temporal['current_year']} 年同周期的 "
                        f"{top_temporal['current_rate']:.1%}（{top_temporal['percentage_point_change']:+.2f}pp）。"
                    ),
                    "metric_refs": [f"temporal_trends.{top_temporal['item_id']}"],
                    "product_use": "作为下一轮原因诊断入口，先拆分品类、渠道、型号和来源构成。",
                    "boundary": temporal.get("boundary"),
                }
            ]
            if top_temporal and temporal.get("status") == "available"
            else []
        ),
        {
            "readout_id": "identity_unknown_boundary",
            "title": "人口属性仍是高未知信息",
            "statement": (
                f"只有 {len(identity_entities):,} / {written_entities:,} 条正文明确表达至少一种身份、责任、经验或关系；"
                "其余保持未知。"
            ),
            "metric_refs": ["quality.identity_coverage"],
            "product_use": "把网评画像作为招募假设和问题地图，用定向访谈/问卷补齐未表达的人口与动机信息。",
            "boundary": "未知不是没有；不得从产品、场景、语气或来源市场反推职业、年龄、收入和家庭结构。",
        },
    ]
    richness_checks = {
        "analysis_module_count": 14,
        "decision_driver_count": len(modules["decision_drivers"]),
        "experience_system_count": len(modules["experience_systems"]),
        "lifecycle_stage_count": len(modules["lifecycle_stages"]),
        "failure_chain_count": len(modules["failure_chains"]),
        "cross_category_signal_count": len(modules["cross_category_signals"]),
        "cooccurrence_signal_count": len(modules["cooccurrence_signals"]),
        "persona_count": len(deterministic_personas),
        "persona_claim_count": len(claim_pool),
        "evidence_rows": len(evidence_by_id),
        "cross_category_count": len(category_rows),
        "available_comparison_lens_count": sum(
            row.get("status") == "available"
            for row in modules.get("comparison_lenses") or []
        ),
        "task_context_dimension_count": sum(
            len(row.get("dimensions") or [])
            for row in task_context
        ),
        "persona_average_claim_count": round(
            len(claim_pool) / len(deterministic_personas), 3
        ) if deterministic_personas else 0,
    }
    richness_pass = (
        written_entities >= 300
        and (
            richness_checks["cross_category_count"] >= 3
            and richness_checks["decision_driver_count"] >= 10
            and richness_checks["experience_system_count"] >= 12
            and richness_checks["lifecycle_stage_count"] >= 7
            and richness_checks["persona_count"] >= 5
            and richness_checks["persona_average_claim_count"] >= 5
            and richness_checks["available_comparison_lens_count"] >= 2
            and semantic["execution_ledger"]["failed_extractors"] == 0
        )
    )
    experience_risk_matrix = [
        {
            "system_id": row.get("item_id"),
            "title": row.get("title"),
            "review_entities": row.get("review_entities", 0),
            "mention_rate": row.get("mention_rate", row.get("coverage", 0)),
            "positive_entities": row.get("positive_entities", 0),
            "negative_entities": row.get("negative_entities", 0),
            "explicit_negative_rate": row.get("explicit_negative_rate", 0),
            "negative_evidence_ids": row.get("negative_evidence_ids") or [],
            "quadrant": (
                "high_reach_high_risk"
                if row.get("review_entities", 0) >= max(30, round(written_entities * 0.02))
                and row.get("explicit_negative_rate", 0) >= 0.2
                else "high_reach_guardrail"
                if row.get("review_entities", 0) >= max(30, round(written_entities * 0.02))
                else "focused_risk"
                if row.get("explicit_negative_rate", 0) >= 0.2
                else "monitor"
            ),
        }
        for row in modules.get("experience_systems") or []
    ]
    battery_system_ids = {
        "runtime",
        "charging",
        "battery_charger",
        "ecosystem",
        "reliability",
        "service",
    }
    battery_chain = [
        row
        for row in experience_risk_matrix
        if row.get("system_id") in battery_system_ids
        and int(row.get("review_entities") or 0) > 0
    ]
    deterministic_report = {
        "llm_used": False,
        "population": {
            "rating": len(canonical),
            "semantic": written_entities,
            "identity_explicit": len(identity_entities),
        },
        "chapters": [
            {"chapter_id": "summary", "title": "管理摘要", "status": "available"},
            {"chapter_id": "sample", "title": "样本结构", "status": "available"},
            {"chapter_id": "trend", "title": "时间趋势", "status": temporal.get("status", "unavailable")},
            {"chapter_id": "drivers", "title": "决策驱动", "status": "available" if modules.get("decision_drivers") else "unavailable"},
            {"chapter_id": "risk", "title": "体验风险矩阵", "status": "available" if experience_risk_matrix else "unavailable"},
            {"chapter_id": "battery", "title": "电池与服务链路", "status": "available" if battery_chain else "unavailable"},
            {"chapter_id": "identity", "title": "身份与未知", "status": "available"},
            {"chapter_id": "journey", "title": "生命周期与恢复", "status": "available" if modules.get("lifecycle_stages") else "unavailable"},
            {"chapter_id": "comparison", "title": "渠道与范围对照", "status": "available" if any(row.get("status") == "available" for row in modules.get("comparison_lenses") or []) else "unavailable"},
            {"chapter_id": "context", "title": "任务与场景", "status": "available" if task_context else "unavailable"},
            {"chapter_id": "method", "title": "方法与边界", "status": "available"},
        ],
        "experience_risk_matrix": experience_risk_matrix,
        "battery_chain": battery_chain,
        "interpretation_policy": (
            "全部数字、排序、矩阵象限和模块可用性由固定脚本生成；品牌报告不调用 LLM。"
        ),
    }
    return {
        "schema_version": BRAND_PERSONA_SCHEMA_VERSION,
        "method_version": BRAND_PERSONA_METHOD_VERSION,
        "cache_version": BRAND_PERSONA_CACHE_VERSION,
        "snapshot_id": snapshot_id,
        "analysis_id": f"bpa_{content_hash[:24]}",
        "content_hash": f"sha256:{content_hash}",
        "scope": normalized_scope,
        "sample": {
            "review_entities": len(canonical),
            "written_review_entities": written_entities,
            "ratings_only_entities": len(canonical) - written_entities,
            "written_coverage": _ratio(written_entities, len(canonical)),
            **ratings,
            "category_count": len(category_rows),
            "model_count": len({_clean(fact.get("model")) for fact in canonical}),
            "channel_count": len({_clean(fact.get("channel")) for fact in canonical}),
            "market_region_count": len({_clean(fact.get("market_region")) for fact in canonical if _clean(fact.get("market_region"))}),
        },
        "sample_composition": {
            "categories": category_rows,
            "models": _distribution(canonical, "model"),
            "channels": _distribution(canonical, "channel"),
            "market_regions": _distribution(canonical, "market_region"),
        },
        "data_quality": _data_quality_summary(
            input_facts,
            canonical,
            text_facts,
            supported_category_ids=set(category_ids_by_profile.values()),
        ),
        "executive_readout": executive_readout,
        "deterministic_personas": deterministic_personas,
        "personas": deterministic_personas,
        "claim_pool": list(claim_pool.values()),
        "dimensions": dimensions,
        **modules,
        "task_context": task_context,
        "execution_ledger": semantic["execution_ledger"],
        "semantic_versions": {
            **semantic["versions"],
            "ope_extractor": OPE_EXTRACTOR_VERSION,
        },
        "evidence": [evidence_by_id[key] for key in sorted(evidence_by_id)],
        "quality": {
            "identity_matched_entities": len(identity_entities),
            "identity_unknown_entities": max(0, written_entities - len(identity_entities)),
            "identity_coverage": _ratio(len(identity_entities), written_entities),
            "unsupported_profile_categories": unsupported_categories,
            "information_richness": {
                "status": (
                    "insufficient_for_benchmark"
                    if written_entities < 300
                    else "scope_limited"
                    if len(category_rows) < 3
                    else "pass"
                    if richness_pass
                    else "below_minimum"
                ),
                "reference_standard": "SKIL-cross-category-persona-20260807",
                "checks": richness_checks,
                "acceptance": (
                    "跨品类大样本必须覆盖至少 3 个品类、10 个决策驱动、12 个体验系统、"
                    "7 个生命周期阶段、5 个 Persona、平均 5 条独立 claim、2 个可用比较视角，"
                    "且所有确定性提取器零失败。"
                ),
                "comparison_to_reference": {
                    "reference": "SKIL 手工用户画像报告",
                    "reference_strengths_preserved": [
                        "样本与分母审计",
                        "决策驱动、体验风险、身份场景和渠道下钻",
                        "Persona、JTBD、证据与不能推断边界",
                    ],
                    "required_productized_extensions": [
                        "全量实体 × 全部脚本执行账本",
                        "跨品类共同信号与品类修正",
                        "生命周期和故障—恢复—退出链",
                        "Persona 多 claim 锁定、研究问题和成功指标",
                        "Evidence SQLite 全量分页与不可变保存",
                    ],
                },
            },
            "limitations": [
                "画像只反映评论中主动表达身份或责任的用户，不代表品牌总体人口分布。",
                "跨品类群组可重叠；同一评论者的跨产品身份无法在缺少稳定用户主键时合并。",
                "来源市场不是评论者国籍；不推断收入、教育、家庭结构或未表达职业。",
                "来源字段 age 可能表示评论时长，品牌画像不使用该字段推断年龄；年龄只接受正文第一人称明确自述。",
                "跨品类指数、共现和年度变化是观察信号，不是市场规模、因果关系或用户人口占比。",
            ],
        },
        "persona_narrative": {
            "status": "deterministic_fallback",
            "schema_version": PERSONA_NARRATIVE_SCHEMA_VERSION,
            "policy_version": PERSONA_NARRATIVE_POLICY_VERSION,
            "llm_used": False,
            "model": None,
        },
        "deterministic_report": deterministic_report,
    }


def validate_persona_narrative(
    payload: Any, allowed_claims: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("personas"), list):
        raise BrandPersonaContractError("Persona 派生文本必须包含 personas 数组")
    personas = payload["personas"]
    if not 1 <= len(personas) <= 8:
        raise BrandPersonaContractError("Persona 数量必须为 1–8 个")
    validated = []
    seen_ids = set()

    def validate_text(value: Any, field: str) -> str:
        text = _clean(value)
        if not text:
            raise BrandPersonaContractError(f"{field} 不得为空")
        if "%" in text or re.search(r"\b\d+(?:\.\d+)?\b", text):
            raise BrandPersonaContractError("Persona 派生文本不得新增或复述数字")
        if any(term in text for term in FORBIDDEN_PERSONA_TERMS):
            raise BrandPersonaContractError("Persona 包含当前证据契约不支持的人口属性")
        return text

    def validate_claim_ids(
        values: Any, persona_id: str, field: str, *, minimum: int = 1
    ) -> list[str]:
        claim_ids = _string_list(values, field)
        if len(claim_ids) < minimum or any(
            claim_id not in allowed_claims for claim_id in claim_ids
        ):
            raise BrandPersonaContractError(f"{field} 引用了不存在的确定性 claim_id")
        if any(
            _clean(allowed_claims[claim_id].get("persona_id"))
            and _clean(allowed_claims[claim_id].get("persona_id")) != persona_id
            for claim_id in claim_ids
        ):
            raise BrandPersonaContractError("Persona 不得引用其他群组的确定性 claim")
        return claim_ids

    def validate_story_rows(
        rows: Any, persona_id: str, field: str
    ) -> list[dict[str, Any]]:
        if not isinstance(rows, list) or not 2 <= len(rows) <= 6:
            raise BrandPersonaContractError(f"{field} 必须包含 2–6 个阶段")
        result = []
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                raise BrandPersonaContractError(f"{field} 阶段必须是对象")
            result.append(
                {
                    "stage": validate_text(item.get("stage"), f"{field}[{index}].stage"),
                    "narrative": validate_text(
                        item.get("narrative"), f"{field}[{index}].narrative"
                    ),
                    "claim_ids": validate_claim_ids(
                        item.get("claim_ids"), persona_id, f"{field}[{index}].claim_ids"
                    ),
                }
            )
        return result

    for row in personas:
        if not isinstance(row, dict):
            raise BrandPersonaContractError("Persona 必须是对象")
        persona_id = _clean(row.get("persona_id"))
        if not persona_id or persona_id in seen_ids:
            raise BrandPersonaContractError("Persona ID、名称和摘要必须完整且唯一")
        name = validate_text(row.get("name"), "persona.name")
        summary = validate_text(row.get("summary"), "persona.summary")
        first_person_statement = validate_text(
            row.get("first_person_statement"), "persona.first_person_statement"
        )
        who_they_are = validate_text(row.get("who_they_are"), "persona.who_they_are")
        claim_ids = validate_claim_ids(row.get("claim_ids"), persona_id, "persona.claim_ids")
        day_in_life = validate_story_rows(
            row.get("day_in_life"), persona_id, "persona.day_in_life"
        )
        journey = validate_story_rows(row.get("journey"), persona_id, "persona.journey")
        image_brief = row.get("image_brief") or {}
        if not isinstance(image_brief, dict):
            raise BrandPersonaContractError("Persona 必须提供受控图片检索 brief")
        portrait_query = _clean(
            image_brief.get("portrait_query") or image_brief.get("query")
        )
        context_query = _clean(
            image_brief.get("context_query") or image_brief.get("query")
        )
        if not portrait_query or not context_query:
            raise BrandPersonaContractError("Persona 必须提供人物与情境图片检索 brief")
        avoid = _string_list(image_brief.get("avoid"), "persona.image_brief.avoid")
        validated.append(
            {
                "persona_id": persona_id,
                "name": name,
                "summary": summary,
                "first_person_statement": first_person_statement,
                "who_they_are": who_they_are,
                "day_in_life": day_in_life,
                "journey": journey,
                "claim_ids": claim_ids,
                "image_brief": {
                    "portrait_query": portrait_query,
                    "context_query": context_query,
                    "avoid": avoid,
                },
            }
        )
        seen_ids.add(persona_id)
    return {
        "schema_version": PERSONA_NARRATIVE_SCHEMA_VERSION,
        "policy_version": PERSONA_NARRATIVE_POLICY_VERSION,
        "personas": validated,
    }
