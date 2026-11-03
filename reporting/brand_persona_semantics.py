from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from reporting.ope_product_report import (
    DimensionRule,
    LabelRule,
    OpeIndex,
    _GENERIC_COMMON_ASPECTS,
    _GENERIC_COMMON_DIMENSIONS,
)


SEMANTIC_EXTRACTOR_VERSION = "brand-persona-semantics/2.0.0"
COMPARISON_POLICY_VERSION = "brand-persona-comparison/1.0.0"


def _label(label: str, display: str, expression: str) -> LabelRule:
    return LabelRule(label, display, expression)


SELF_REPORTED_AGE = DimensionRule(
    "self_reported_age",
    "profile",
    "本人明确年龄段",
    "评论者是否用第一人称明确说明自己的年龄？亲友和邻居年龄不计入。",
    (
        _label("age_18_34", "18–34 岁", r"\b(?:i am|i['’]m|as|at age)\s+(?:an?\s+)?(?:1[89]|2\d|3[0-4])(?:\s*(?:years?|yrs?)(?:\s*old)?)?\b"),
        _label("age_35_49", "35–49 岁", r"\b(?:i am|i['’]m|as|at age)\s+(?:an?\s+)?(?:3[5-9]|4\d)(?:\s*(?:years?|yrs?)(?:\s*old)?)?\b"),
        _label("age_50_64", "50–64 岁", r"\b(?:i am|i['’]m|as|at age)\s+(?:an?\s+)?(?:5\d|6[0-4])(?:\s*(?:years?|yrs?)(?:\s*old)?)?\b"),
        _label("age_65_74", "65–74 岁", r"\b(?:i am|i['’]m|as|at age)\s+(?:an?\s+)?(?:6[5-9]|7[0-4])(?:\s*(?:years?|yrs?)(?:\s*old)?)?\b"),
        _label("age_75_plus", "75 岁及以上", r"\b(?:i am|i['’]m|as|at age)\s+(?:an?\s+)?(?:7[5-9]|[89]\d)(?:\s*(?:years?|yrs?)(?:\s*old)?)?\b"),
        _label("senior_self", "本人明确 senior / older", r"\b(?:i am|i['’]m|as a|being a)\s+(?:an?\s+)?(?:senior|older|elderly)(?:\s+(?:woman|man|person|adult|citizen))?\b"),
    ),
)


PHYSICAL_CONSTRAINT = DimensionRule(
    "self_reported_physical_constraint",
    "profile",
    "本人明确体力 / 行动限制",
    "评论者是否用第一人称明确说明影响操作的身体条件？",
    (
        _label("strength_grip", "力量 / 握力受限", r"\b(?:i have|i['’]ve got|my)\b.{0,28}\b(?:limited strength|weak (?:hands|arms|grip)|strength issues?)\b"),
        _label("mobility", "行动能力受限", r"\b(?:i have|i['’]ve got|my|i am|i['’]m)\b.{0,28}\b(?:limited mobility|wheelchair|walking difficulty|difficulty walking|disabled|disability)\b"),
        _label("joint_back", "关节 / 腰背 / 肩部问题", r"\b(?:i have|i['’]ve got|my|being older|as a senior)\b.{0,40}\b(?:arthritis|bad back|back pain|shoulder pain|knee (?:pain|problem)|joint pain)\b"),
    ),
)


USAGE_INTENSITY = DimensionRule(
    "usage_intensity",
    "profile",
    "使用频率与强度",
    "评论中是否明确表达高频、长时、季节性或多场地使用？",
    (
        _label("daily_frequent", "每日 / 高频使用", r"\b(?:every day|daily|several times a week|multiple times a week|almost every day|use it constantly|frequent use)\b"),
        _label("weekly_routine", "每周例行使用", r"\b(?:every week|weekly|once a week|twice a week|week after week)\b"),
        _label("long_session", "长时间连续作业", r"\b(?:all day|for hours|hours at a time|long jobs?|long sessions?|extended use|continuous use)\b"),
        _label("seasonal_use", "季节性任务", r"\b(?:each|every|this|last) (?:spring|summer|fall|autumn|winter|season)\b|\bseasonal(?:ly)?\b"),
        _label("multi_site_use", "多场地 / 多任务使用", r"\b(?:multiple|several) (?:properties|yards|sites|jobs|locations)\b|\bfrom job to job\b|\bfor (?:different|multiple) customers\b"),
    ),
)


DECISION_DRIVER = DimensionRule(
    "decision_driver",
    "profile",
    "购买与选型驱动",
    "评论中明确强调了哪些购买、选型或留用标准？",
    (
        _label("performance_power", "性能、动力与任务结果", r"\b(?:more|enough|plenty of) power\b|\bpowerful\b|\bperformance\b|\btorque\b|\bcutting power\b|\bsuction power\b|\bgets? the job done\b"),
        _label("ease_convenience", "简单、省事与快速上手", r"\b(?:easy|simple|quick) to (?:use|set up|setup|start|operate|assemble)\b|\bconvenien(?:t|ce)\b|\bgrab and go\b|\bno hassle\b"),
        _label("ergonomics_mobility", "轻量、人机与可操控性", r"\blightweight\b|\beasy to (?:carry|handle|maneuver|move)\b|\bergonomic\b|\bcomfortable (?:to use|grip|handle)\b|\bbalance[ds]? well\b"),
        _label("runtime_completion", "续航与一次完成任务", r"\bruntime\b|\brun time\b|\bone charge\b|\bsingle charge\b|\bfinish(?:ed)? (?:the )?(?:whole|entire)? ?(?:job|yard|task|driveway)\b"),
        _label("reliability_durability", "可靠性与耐久", r"\breliab(?:le|ility)\b|\bdurab(?:le|ility)\b|\bbuilt to last\b|\blong[- ]lasting\b|\bquality build\b"),
        _label("value_price", "价格与性价比", r"\b(?:good|great|excellent|best) value\b|\bvalue for (?:the )?money\b|\bprice(?:d)? (?:well|right)\b|\baffordable\b|\bworth (?:the|every)\b|\btoo expensive\b|\boverpriced\b"),
        _label("ecosystem_compatibility", "电池生态与兼容", r"\bsame batter(?:y|ies)\b|\bbattery (?:system|platform|compatib)\b|\bcompatible with\b|\buse (?:my|the) existing batter(?:y|ies)\b|\b(?:one\+|power\+|18v|20v|40v|56v) (?:system|platform|line|tools?)\b"),
        _label("maintenance_noise", "低维护、低噪与清洁能源", r"\bno (?:gas|oil|cord)\b|\blow maintenance\b|\bmaintenance[- ]free\b|\bquiet(?:er)?\b|\bless noise\b|\bno fumes\b"),
        _label("service_warranty", "售后、保修与可维修性", r"\bwarrant(?:y|ies)\b|\bcustomer service\b|\bsupport\b|\breplacement (?:part|unit|tool)\b|\brepair(?:able|ability)?\b"),
        _label("setup_learning", "装配、学习与可理解性", r"\bmanual\b|\binstructions?\b|\bquick start\b|\beasy to learn\b|\bintuitive\b|\bset ?up\b|\bassembl(?:e|ed|y|ing)\b"),
        _label("portability_storage", "搬运、运输与收纳", r"\bportable\b|\beasy to (?:transport|store|load)\b|\bcompact\b|\bfold(?:s|ed|ing)?\b|\bvertical stor(?:e|age)\b|\bwall mount(?:ed)?\b"),
        _label("safety_control", "安全、控制与信心", r"\bsafe(?:ty)?\b|\bmore control\b|\beasy to control\b|\bfeel(?:s)? secure\b|\bauto(?:matic)? shut ?off\b|\bbrake\b|\bguard\b"),
    ),
)


BRAND_RELATIONSHIP = DimensionRule(
    "brand_relationship",
    "profile",
    "品牌与生态关系",
    "用户与该品牌、电池平台和竞品是什么关系？",
    (
        _label("existing_brand_owner", "已有同品牌工具 / 电池", r"\b(?:already|also|currently) (?:own|have|had).{0,65}\b(?:ego|ryobi|skil|dewalt|milwaukee|greenworks|craftsman)\b|\bmy (?:other |existing )?(?:ego|ryobi|skil|dewalt|milwaukee|greenworks|craftsman) (?:tool|tools|battery|batteries)\b"),
        _label("repeat_brand_buyer", "复购 / 长期品牌用户", r"\b(?:always|only) buy (?:ego|ryobi|skil|dewalt|milwaukee|greenworks|craftsman)\b|\blong[- ]time (?:ego|ryobi|skil|dewalt|milwaukee|greenworks|craftsman) (?:user|owner)\b|\bbought (?:ego|ryobi|skil|dewalt|milwaukee|greenworks|craftsman) again\b"),
        _label("competitor_switcher", "从其他品牌迁移", r"\b(?:replaced|replacing|switched from|came from|instead of).{0,55}\b(?:dewalt|ryobi|ego|milwaukee|makita|craftsman|greenworks|bosch|black\s*\+?\s*decker|kobalt|skil)\b"),
        _label("cross_brand_comparer", "明确跨品牌比较", r"\b(?:compared|comparing|versus|vs\.?|better than|worse than).{0,55}\b(?:dewalt|ryobi|ego|milwaukee|makita|craftsman|greenworks|bosch|black\s*\+?\s*decker|kobalt|skil)\b"),
        _label("recommendation_led", "受推荐 / 评论影响", r"\b(?:friend|family|coworker|contractor|neighbor) recommended\b|\brecommended (?:it|this|one) to me\b|\bbased on (?:the )?reviews\b"),
    ),
)


PURCHASE_TRIGGER = DimensionRule(
    "purchase_trigger",
    "journey",
    "购买触发",
    "哪些明确事件触发购买或更换？",
    (
        _label("replace_failed_tool", "旧机故障 / 寿命结束", r"\breplac(?:e|ed|ing) (?:my|our|an?) (?:old|broken|dead|failed)\b|\bmy old .{0,35}(?:broke|died|failed|quit)\b"),
        _label("upgrade_trigger", "升级性能 / 容量", r"\bupgrade(?:d|ing)? (?:from|to)\b|\bneeded (?:more|better) (?:power|runtime|capacity|performance)\b"),
        _label("first_category_purchase", "首次进入品类 / 平台", r"\b(?:my|our) first (?:ever )?(?:cordless |electric |battery )?(?:tool|mower|blower|trimmer|saw|vacuum|washer|product)\b|\bnew to (?:this|the) (?:tool|category|platform)\b"),
        _label("promotion_discount", "促销 / 折扣", r"\bon sale\b|\bdiscount(?:ed)?\b|\bclearance\b|\bcoupon\b|\bspecial (?:offer|deal)\b|\bpromotion\b"),
        _label("tool_only_battery_reuse", "裸机复用已有电池", r"\btool only\b|\bbare tool\b|\bwithout (?:a )?batter(?:y|ies)\b|\bdidn['’]t need (?:another |a )?batter(?:y|ies)\b"),
    ),
)


LIFECYCLE_STAGE = DimensionRule(
    "lifecycle_stage",
    "journey",
    "拥有与使用生命周期",
    "评论明确发生在购买、上手、使用、故障恢复还是留用阶段？",
    (
        _label("research_selection", "研究与选型", r"\b(?:researched|looked at|considered|compared|reviews convinced|decided to buy|chose this)\b"),
        _label("purchase_unboxing", "购买与开箱", r"\b(?:just|recently) (?:bought|purchased|received|got)\b|\bout of the box\b|\bunbox(?:ed|ing)?\b"),
        _label("setup_learning", "装配与首次上手", r"\bassembl(?:e|ed|y|ing)\b|\bset ?up\b|\bfirst (?:use|time|run|charge)\b|\bmanual\b|\binstructions?\b"),
        _label("routine_operation", "常规任务执行", r"\b(?:every|each) (?:week|weekend|day|season)\b|\broutine\b|\bregular(?:ly)?\b|\bmy usual\b"),
        _label("failure_diagnosis", "故障与诊断", r"\b(?:stopped|quit|failed|broke|dead|won['’]t|wouldn['’]t|error code|troubleshoot|diagnos)\b"),
        _label("service_recovery", "售后、维修与换货", r"\b(?:contacted|called|emailed).{0,28}(?:support|service|warranty)\b|\brepair(?:ed|ing)?\b|\breplacement (?:part|unit|tool)\b|\bexchang(?:e|ed|ing)\b"),
        _label("retention_advocacy", "留用、复购与推荐", r"\bwould buy again\b|\bwill buy (?:another|more)\b|\brecommend(?:ed|ing)? (?:it|this|one|to)\b|\blong[- ]time (?:user|owner)\b"),
        _label("return_exit", "退货与流失", r"\breturn(?:ed|ing)? (?:it|this|the|for)\b|\bsent (?:it|this) back\b|\bnever buy (?:again|another)\b|\bswitched (?:back|away)\b"),
    ),
)


LEARNING_SUPPORT = DimensionRule(
    "learning_support",
    "journey",
    "学习、设置与支持需求",
    "用户在哪些环节需要说明、排障或安全帮助？",
    (
        _label("manual_instruction", "说明书 / 指引", r"\bmanual\b|\binstructions?\b|\buser guide\b|\bquick start\b|\bdiagram\b"),
        _label("assembly_setup_help", "装配 / 初始设置", r"\bassembl(?:e|ed|y|ing)\b|\bset ?up\b|\bput together\b|\bout of the box\b"),
        _label("troubleshooting_help", "故障诊断 / 排障", r"\btroubleshoot(?:ing)?\b|\berror code\b|\bdiagnos(?:e|is|tic)\b|\bcan['’]t figure out\b|\bwon['’]t (?:start|charge|run|work)\b"),
        _label("safety_guidance", "安全操作指引", r"\bsafety (?:instruction|warning|guard|feature)\b|\bhow to (?:use|operate) safely\b|\bprotective (?:gear|equipment)\b"),
    ),
)


BRAND_PERSONA_DIMENSIONS: tuple[DimensionRule, ...] = (
    *_GENERIC_COMMON_DIMENSIONS,
    SELF_REPORTED_AGE,
    PHYSICAL_CONSTRAINT,
    USAGE_INTENSITY,
    DECISION_DRIVER,
    BRAND_RELATIONSHIP,
    PURCHASE_TRIGGER,
    LIFECYCLE_STAGE,
    LEARNING_SUPPORT,
)


DRIVER_META = {rule.label: rule.display for rule in DECISION_DRIVER.rules}
LIFECYCLE_META = {rule.label: rule.display for rule in LIFECYCLE_STAGE.rules}
ASPECT_META = {
    "runtime": "续航与任务完成",
    "charging": "充电速度与流程",
    "weight_balance": "重量与平衡",
    "ergonomics": "操作舒适与人机",
    "noise": "噪音与邻里友好",
    "assembly": "装配与首次设置",
    "storage": "收纳与运输",
    "battery_charger": "电池与充电器体验",
    "reliability": "整机可靠性与耐久",
    "starting": "启动与恢复",
    "maintenance_gasfree": "维护与无油气体验",
    "value": "价格与价值感",
    "safety": "安全与控制",
    "service": "售后与保修",
    "ecosystem": "平台生态与兼容",
    "smart_connectivity": "连接、状态与智能功能",
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _entity_ref(fact: dict[str, Any]) -> str:
    return _clean(fact.get("review_entity_id") or fact.get("review_occurrence_id"))


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _wilson(numerator: int, denominator: int, z: float = 1.96) -> list[float] | None:
    if not denominator:
        return None
    p = numerator / denominator
    base = 1 + z * z / denominator
    centre = p + z * z / (2 * denominator)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * denominator)) / denominator)
    return [round((centre - margin) / base, 6), round((centre + margin) / base, 6)]


def _rating_context(entities: set[str], facts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ratings = [int(facts[entity]["rating"]) for entity in entities if entity in facts and facts[entity].get("rating")]
    return {
        "valid_rating_entities": len(ratings),
        "average_rating": round(sum(ratings) / len(ratings), 4) if ratings else None,
        "positive_rate": _ratio(sum(value >= 4 for value in ratings), len(ratings)),
        "negative_rate": _ratio(sum(value <= 2 for value in ratings), len(ratings)),
    }


def _top_mix(entities: set[str], facts: dict[str, dict[str, Any]], field: str, limit: int = 5) -> list[dict[str, Any]]:
    counts = Counter(_clean(facts[entity].get(field)) or "未提供" for entity in entities if entity in facts)
    return [{field: key, "review_entities": value, "share": _ratio(value, len(entities))} for key, value in counts.most_common(limit)]


def _dimension_evidence(index: OpeIndex, dimension_id: str, label: str, entities: set[str], limit: int = 16) -> list[str]:
    return index.evidence_for_entities(
        entities,
        dimension_ids=(dimension_id,),
        dimension_label_filters={dimension_id: {label}},
        limit=limit,
    )


def _aspect_evidence(index: OpeIndex, aspect: str, entities: set[str], polarity: str | None = None, limit: int = 16) -> list[str]:
    return index.evidence_for_entities(entities, aspects=(aspect,), polarity=polarity, limit=limit, prefer_low=polarity == "negative")


def build_semantic_index(text_facts: list[dict[str, Any]], brand: str) -> OpeIndex:
    # Several source exports use ``age`` for the age of the review (for
    # example "2 years"), not the reviewer.  A cross-channel persona must
    # never reinterpret it as demographics.  Demographic age is accepted
    # only from first-person text rules above.
    semantic_facts = [{**fact, "age": ""} for fact in text_facts]
    index = OpeIndex(
        semantic_facts,
        profile_id=f"brand-persona/{SEMANTIC_EXTRACTOR_VERSION.split('/')[-1]}",
        category_label=f"{brand} 跨品类",
        dimensions=BRAND_PERSONA_DIMENSIONS,
        numeric_dimension_ids=(),
        aspect_rules=_GENERIC_COMMON_ASPECTS,
    )
    index.run()
    return index


def _dimension_module(index: OpeIndex, dimension_id: str, meta: dict[str, str], facts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    denominator = len(index.all_entities)
    rows = []
    for label, entities in sorted(index.dimension_entities.get(dimension_id, {}).items(), key=lambda item: (-len(item[1]), item[0])):
        evidence_ids = _dimension_evidence(index, dimension_id, label, entities)
        rows.append({
            "item_id": label,
            "title": meta.get(label, label.replace("_", " ")),
            "review_entities": len(entities),
            "coverage": _ratio(len(entities), denominator),
            "ci95": _wilson(len(entities), denominator),
            "category_count": len({_clean(facts[entity].get("category_id")) for entity in entities if entity in facts}),
            "model_count": len({_clean(facts[entity].get("model")) for entity in entities if entity in facts}),
            "top_categories": _top_mix(entities, facts, "category", 5),
            "top_models": _top_mix(entities, facts, "model", 5),
            **_rating_context(entities, facts),
            "evidence_count": sum(len(index.dimension_evidence[dimension_id][label].get(entity, [])) for entity in entities),
            "evidence_ids": evidence_ids,
            "entity_ids": entities,
        })
    return rows


def _experience_module(index: OpeIndex, facts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    denominator = len(index.all_entities)
    rows = []
    for rule in index.aspect_rules:
        aspect = rule.label
        entities = set(index.aspect_entities.get(aspect, set()))
        if not entities:
            continue
        negative = set(index.aspect_negative.get(aspect, set()))
        positive = set(index.aspect_positive.get(aspect, set()))
        rows.append({
            "item_id": aspect,
            "title": ASPECT_META.get(aspect, rule.display),
            "review_entities": len(entities),
            "mention_rate": _ratio(len(entities), denominator),
            "positive_entities": len(positive),
            "negative_entities": len(negative),
            "explicit_positive_rate": _ratio(len(positive), len(entities)),
            "explicit_negative_rate": _ratio(len(negative), len(entities)),
            "category_count": len({_clean(facts[entity].get("category_id")) for entity in entities if entity in facts}),
            "top_categories": _top_mix(entities, facts, "category", 5),
            **_rating_context(entities, facts),
            "positive_evidence_ids": _aspect_evidence(index, aspect, positive, "positive"),
            "negative_evidence_ids": _aspect_evidence(index, aspect, negative, "negative"),
            "evidence_count": sum(len(rows) for polarity in index.aspect_evidence.get(aspect, {}).values() for rows in polarity.values()),
            "entity_ids": entities,
            "negative_entity_ids": negative,
            "positive_entity_ids": positive,
        })
    rows.sort(key=lambda row: (-row["review_entities"], row["item_id"]))
    return rows


def _category_signals(module_rows: list[dict[str, Any]], facts: dict[str, dict[str, Any]], denominator_by_category: Counter[str]) -> list[dict[str, Any]]:
    output = []
    total = sum(denominator_by_category.values())
    minimum_cell = max(30, round(total * 0.0005))
    category_labels = {_clean(fact.get("category_id")): _clean(fact.get("category")) for fact in facts.values()}
    for row in module_rows:
        entities = set(row.get("entity_ids") or set())
        overall = len(entities) / total if total else 0
        per_category = Counter(_clean(facts[entity].get("category_id")) for entity in entities if entity in facts)
        comparisons = []
        for category_id, denominator in denominator_by_category.items():
            if denominator < minimum_cell or not overall:
                continue
            count = per_category[category_id]
            rate = count / denominator
            comparisons.append({
                "category_id": category_id,
                "category": category_labels.get(category_id, category_id),
                "review_entities": count,
                "denominator": denominator,
                "mention_rate": round(rate, 6),
                "index_vs_brand": round(rate / overall, 3),
            })
        comparisons.sort(key=lambda item: (-item["index_vs_brand"], -item["review_entities"], item["category_id"]))
        supported = [item for item in comparisons if item["review_entities"] >= max(8, round(minimum_cell * 0.1))]
        output.append({
            "item_id": row["item_id"],
            "title": row["title"],
            "brand_mention_rate": round(overall, 6),
            "minimum_category_denominator": minimum_cell,
            "comparable_category_count": len(comparisons),
            "supported_category_count": len(supported),
            "brand_level_signal": len(supported) >= 3,
            "top_over_indexed": supported[:5],
            "boundary": "相对指数描述评论语言集中度，不等于市场渗透率或因果关系。",
        })
    return sorted(output, key=lambda row: (-row["supported_category_count"], -row["brand_mention_rate"], row["item_id"]))


def _cooccurrences(rows: list[dict[str, Any]], denominator: int) -> list[dict[str, Any]]:
    minimum = max(8, round(denominator * 0.001))
    output = []
    for index, left in enumerate(rows):
        left_entities = set(left.get("entity_ids") or set())
        for right in rows[index + 1:]:
            right_entities = set(right.get("entity_ids") or set())
            joint = left_entities & right_entities
            if len(joint) < minimum:
                continue
            expected = (len(left_entities) / denominator) * (len(right_entities) / denominator) if denominator else 0
            observed = len(joint) / denominator if denominator else 0
            output.append({
                "left_id": left["item_id"], "left_title": left["title"],
                "right_id": right["item_id"], "right_title": right["title"],
                "joint_entities": len(joint),
                "joint_rate": round(observed, 6),
                "lift": round(observed / expected, 3) if expected else None,
                "minimum_support": minimum,
                "boundary": "共现只表示同一评论同时提及，不证明需求之间存在因果。",
            })
    output.sort(key=lambda row: (-(row["lift"] or 0), -row["joint_entities"], row["left_id"], row["right_id"]))
    return output[:30]


def _temporal(rows: list[dict[str, Any]], facts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_year: dict[int, set[str]] = defaultdict(set)
    dated: list[tuple[str, str]] = []
    for entity, fact in facts.items():
        date = _clean(fact.get("submission_date"))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            by_year[int(date[:4])].add(entity)
            dated.append((entity, date))
    if not dated:
        return {
            "status": "unavailable",
            "reason": "当前没有可解析正文日期，不能计算时间变化。",
            "yearly_sample": [],
            "signals": [],
        }
    latest_date = max(date for _entity, date in dated)
    current_year = int(latest_date[:4])
    baseline_year = current_year - 1
    cutoff_month_day = latest_date[4:]
    current_entities = {
        entity
        for entity, date in dated
        if int(date[:4]) == current_year and date[4:] <= cutoff_month_day
    }
    baseline_entities = {
        entity
        for entity, date in dated
        if int(date[:4]) == baseline_year and date[4:] <= cutoff_month_day
    }
    if len(baseline_entities) < 100 or len(current_entities) < 100:
        return {
            "status": "unavailable",
            "reason": (
                "趋势仅比较最近数据年与上一年的同月同日窗口，且每个窗口至少需要 "
                "100 条正文；当前不具备条件。"
            ),
            "comparison_window": {
                "baseline_year": baseline_year,
                "current_year": current_year,
                "through_month_day": cutoff_month_day.removeprefix("-"),
                "baseline_written": len(baseline_entities),
                "current_written": len(current_entities),
            },
            "yearly_sample": [{"year": year, "written_review_entities": len(entities)} for year, entities in sorted(by_year.items())],
            "signals": [],
        }
    signals = []
    for row in rows:
        entities = set(row.get("entity_ids") or set())
        prior = len(entities & baseline_entities)
        current = len(entities & current_entities)
        prior_rate = prior / len(baseline_entities)
        current_rate = current / len(current_entities)
        signals.append({
            "item_id": row["item_id"], "title": row["title"],
            "baseline_year": baseline_year, "current_year": current_year,
            "baseline_entities": prior, "current_entities": current,
            "baseline_rate": round(prior_rate, 6), "current_rate": round(current_rate, 6),
            "percentage_point_change": round((current_rate - prior_rate) * 100, 3),
            "direction": "up" if current_rate - prior_rate >= 0.005 else "down" if prior_rate - current_rate >= 0.005 else "stable",
        })
    signals.sort(key=lambda row: (-abs(row["percentage_point_change"]), row["item_id"]))
    return {
        "status": "available",
        "comparison": {
            "baseline_year": baseline_year,
            "current_year": current_year,
            "through_month_day": cutoff_month_day.removeprefix("-"),
            "baseline_written": len(baseline_entities),
            "current_written": len(current_entities),
            "window_policy": "same-month-day-window",
        },
        "yearly_sample": [{"year": year, "written_review_entities": len(entities)} for year, entities in sorted(by_year.items())],
        "signals": signals,
        "boundary": "同周期年度变化仍未控制品类、渠道、型号和来源构成，属于观察信号而非品牌表现因果结论。",
    }


def _comparison_lenses(
    driver_rows: list[dict[str, Any]],
    experience_rows: list[dict[str, Any]],
    facts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build descriptive, gated lenses without inventing causal differences."""
    total = len(facts)
    minimum = max(100, round(total * 0.01))
    lenses = []
    for field, title, limit in (
        ("category_id", "品类", 20),
        ("channel", "渠道", 12),
        ("market_region", "来源市场", 8),
        ("model", "型号", 20),
    ):
        groups: dict[str, set[str]] = defaultdict(set)
        labels: dict[str, str] = {}
        for entity, fact in facts.items():
            value = _clean(fact.get(field)) or "未提供"
            groups[value].add(entity)
            labels[value] = (
                _clean(fact.get("category")) if field == "category_id" else value
            ) or value
        comparable = [
            (value, entities)
            for value, entities in groups.items()
            if len(entities) >= minimum
        ]
        comparable.sort(key=lambda item: (-len(item[1]), item[0]))
        group_rows = []
        for value, entities in comparable[:limit]:
            signals = []
            for source, kind in (
                (driver_rows, "decision_driver"),
                (experience_rows, "experience_system"),
            ):
                for row in source:
                    overlap = entities & set(row.get("entity_ids") or set())
                    if len(overlap) < max(8, round(len(entities) * 0.005)):
                        continue
                    group_rate = len(overlap) / len(entities)
                    brand_rate = len(row.get("entity_ids") or set()) / total if total else 0
                    signals.append({
                        "signal_type": kind,
                        "item_id": row["item_id"],
                        "title": row["title"],
                        "review_entities": len(overlap),
                        "mention_rate": round(group_rate, 6),
                        "index_vs_brand": round(group_rate / brand_rate, 3) if brand_rate else None,
                    })
            signals.sort(
                key=lambda row: (
                    -(row["index_vs_brand"] or 0),
                    -row["review_entities"],
                    row["item_id"],
                )
            )
            dates = sorted(
                _clean(facts[entity].get("submission_date"))
                for entity in entities
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", _clean(facts[entity].get("submission_date")))
            )
            group_rows.append({
                "value": value,
                "label": labels[value],
                "written_review_entities": len(entities),
                "share_of_written": _ratio(len(entities), total),
                "date_range": {
                    "start": dates[0] if dates else None,
                    "end": dates[-1] if dates else None,
                },
                "top_signals": signals[:8],
            })
        lenses.append({
            "lens_id": field,
            "title": title,
            "status": "available" if len(group_rows) >= 2 else "unavailable",
            "minimum_written_per_group": minimum,
            "eligible_group_count": len(group_rows),
            "groups": group_rows,
            "boundary": (
                "仅展示满足正文门槛的描述性构成和语言指数；不同时间、来源与型号构成未校正，"
                "不得写成渠道、地区、品类或型号的因果优劣。"
            ),
        })
    return lenses


def _failure_chains(index: OpeIndex, experience_rows: list[dict[str, Any]], lifecycle_rows: list[dict[str, Any]], facts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    recovery = next((set(row["entity_ids"]) for row in lifecycle_rows if row["item_id"] == "service_recovery"), set())
    exit_entities = next((set(row["entity_ids"]) for row in lifecycle_rows if row["item_id"] == "return_exit"), set())
    output = []
    minimum = (
        1
        if len(index.all_entities) < 300
        else max(5, round(len(index.all_entities) * 0.0005))
    )
    for row in experience_rows:
        negative = set(row.get("negative_entity_ids") or set())
        if len(negative) < minimum:
            continue
        recovered = negative & recovery
        exited = negative & exit_entities
        evidence = _aspect_evidence(index, row["item_id"], negative, "negative", 18)
        output.append({
            "chain_id": f"failure_{row['item_id']}",
            "system_id": row["item_id"],
            "title": f"{row['title']}问题 → 恢复 / 退出",
            "negative_entities": len(negative),
            "negative_share_of_text": _ratio(len(negative), len(index.all_entities)),
            "service_recovery_entities": len(recovered),
            "return_exit_entities": len(exited),
            "recovery_observed_rate": _ratio(len(recovered), len(negative)),
            "exit_observed_rate": _ratio(len(exited), len(negative)),
            "top_categories": _top_mix(negative, facts, "category", 5),
            "top_models": _top_mix(negative, facts, "model", 5),
            "evidence_ids": evidence,
            "evidence_count": row.get("evidence_count", 0),
            "boundary": "同评论中的故障与恢复/退货表达构成观察链，不证明售后动作由该问题唯一导致。",
        })
    output.sort(key=lambda row: (-row["negative_entities"], row["system_id"]))
    return output


def _execution_ledger(index: OpeIndex) -> dict[str, Any]:
    denominator = len(index.all_entities)
    rows = []
    for dimension in index.dimensions:
        matched = index.entities_for(dimension.dimension_id)
        labels = index.dimension_entities.get(dimension.dimension_id, {})
        material = "\n".join(f"{entity}|{','.join(sorted(label for label, entities in labels.items() if entity in entities))}" for entity in sorted(matched))
        rows.append({
            "extractor_id": f"dimension:{dimension.dimension_id}",
            "population": "written_review_entities",
            "processed_entities": denominator,
            "matched_entities": len(matched),
            "no_match_entities": denominator - len(matched),
            "failed_entities": 0,
            "match_set_sha256": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
        })
    for rule in index.aspect_rules:
        matched = set(index.aspect_entities.get(rule.label, set()))
        material = "\n".join(sorted(matched))
        rows.append({
            "extractor_id": f"aspect:{rule.label}",
            "population": "written_review_entities",
            "processed_entities": denominator,
            "matched_entities": len(matched),
            "no_match_entities": denominator - len(matched),
            "failed_entities": 0,
            "match_set_sha256": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
        })
    return {
        "schema_version": "brand-persona-execution-ledger/v1",
        "extractor_version": SEMANTIC_EXTRACTOR_VERSION,
        "extractor_count": len(rows),
        "completed_extractors": len(rows),
        "failed_extractors": 0,
        "expected_entity_executions": denominator * len(rows),
        "completed_entity_executions": denominator * len(rows),
        "extractors": rows,
    }


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.endswith("entity_ids") and key != "entity_ids"}


def build_brand_semantics(text_facts: list[dict[str, Any]], brand: str) -> dict[str, Any]:
    index = build_semantic_index(text_facts, brand)
    facts = {_entity_ref(fact): fact for fact in text_facts}
    driver_rows = _dimension_module(index, "decision_driver", DRIVER_META, facts)
    lifecycle_rows = _dimension_module(index, "lifecycle_stage", LIFECYCLE_META, facts)
    experience_rows = _experience_module(index, facts)
    denominator_by_category = Counter(_clean(fact.get("category_id")) for fact in text_facts)
    evidence = []
    for row in index.evidence.rows.values():
        fact = facts.get(_clean(row.get("review_entity_id")), {})
        evidence.append({
            **row,
            "job_id": _clean(fact.get("job_id")),
            "category_id": _clean(fact.get("category_id")),
            "category": _clean(fact.get("category")),
            "model": _clean(fact.get("model")),
            "channel": _clean(fact.get("channel")),
            "market_region": _clean(fact.get("market_region")),
            "submission_date": _clean(fact.get("submission_date")),
        })
    modules = {
        "decision_drivers": [public_row(row) for row in driver_rows],
        "experience_systems": [public_row(row) for row in experience_rows],
        "lifecycle_stages": [public_row(row) for row in lifecycle_rows],
        "cross_category_signals": _category_signals(driver_rows + experience_rows, facts, denominator_by_category),
        "cooccurrence_signals": _cooccurrences(driver_rows, len(text_facts)),
        "temporal_trends": _temporal(driver_rows + experience_rows, facts),
        "failure_chains": _failure_chains(index, experience_rows, lifecycle_rows, facts),
        "comparison_lenses": _comparison_lenses(driver_rows, experience_rows, facts),
    }
    return {
        "index": index,
        "fact_by_entity": facts,
        "driver_rows_internal": driver_rows,
        "experience_rows_internal": experience_rows,
        "lifecycle_rows_internal": lifecycle_rows,
        "modules": modules,
        "evidence": evidence,
        "execution_ledger": _execution_ledger(index),
        "versions": {
            "semantic_extractor": SEMANTIC_EXTRACTOR_VERSION,
            "comparison_policy": COMPARISON_POLICY_VERSION,
        },
    }
