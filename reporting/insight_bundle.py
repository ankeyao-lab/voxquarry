from __future__ import annotations

import hashlib
from typing import Any

from reporting.source import canonical_json


def _row(
    rows: list[dict[str, Any]],
    key: str,
    value: str,
) -> dict[str, Any]:
    return next(row for row in rows if row[key] == value)


def _metric(report: dict[str, Any], metric_id: str) -> dict[str, Any]:
    return _row(report["metrics"], "metric_id", metric_id)


def _ref(
    source: str,
    collection: str,
    row_id: str,
    field: str,
    value: Any,
) -> dict[str, Any]:
    return {
        "source": source,
        "collection": collection,
        "row_id": row_id,
        "field": field,
        "value": value,
    }


def _scene_ref(
    collection: str,
    row_id: str,
    field: str,
    value: Any,
) -> dict[str, Any]:
    return _ref(
        "scene_analysis",
        collection,
        row_id,
        field,
        value,
    )


def _report_ref(
    collection: str,
    row_id: str,
    field: str,
    value: Any,
) -> dict[str, Any]:
    return _ref(
        "core_report",
        collection,
        row_id,
        field,
        value,
    )


def _insight(
    insight_id: str,
    headline: str,
    body: str,
    *,
    claim_type: str,
    confidence: str,
    metric_refs: list[dict[str, Any]],
    evidence_refs: list[str],
    limitations: list[str],
) -> dict[str, Any]:
    return {
        "insight_id": insight_id,
        "headline": headline,
        "body": body,
        "claim_type": claim_type,
        "confidence": confidence,
        "metric_refs": metric_refs,
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "limitations": limitations,
    }


def build_insight_bundle(
    report: dict[str, Any],
    scene: dict[str, Any],
) -> dict[str, Any]:
    average = _metric(report, "average_rating")
    positive = _metric(report, "positive_rating_rate")
    negative = _metric(report, "negative_rating_rate")
    written = _metric(report, "written_coverage")
    channels = scene["comparison_lenses"]["channels"]
    home_depot = _row(channels, "group", "homedepot")
    ryobi = _row(channels, "group", "ryobi")

    large_yard = _row(
        scene["profiles"], "cohort_id", "large_complex_yard"
    )
    switcher = _row(scene["profiles"], "cohort_id", "gas_switcher")
    accessibility = _row(
        scene["profiles"], "cohort_id", "accessibility"
    )
    mowing = _row(scene["usage_scenarios"], "scenario_id", "mowing")
    charging_scene = _row(
        scene["usage_scenarios"], "scenario_id", "charging"
    )
    large_scale = _row(
        scene["usage_scenarios"], "scenario_id", "large_scale"
    )
    slope = _row(
        scene["usage_scenarios"], "scenario_id", "slope_rough"
    )

    noise = _row(scene["attribute_experience"], "attribute_id", "noise")
    assembly = _row(
        scene["attribute_experience"],
        "attribute_id",
        "assembly_storage",
    )
    handling = _row(
        scene["attribute_experience"], "attribute_id", "handling"
    )
    value = _row(scene["attribute_experience"], "attribute_id", "value")
    charging = _row(
        scene["attribute_experience"], "attribute_id", "charging"
    )
    runtime = _row(
        scene["attribute_experience"], "attribute_id", "runtime"
    )

    stopped = _row(
        scene["failure_chains"], "failure_id", "stopped_working"
    )
    breakage = _row(
        scene["failure_chains"], "failure_id", "breakage"
    )
    stalling = _row(scene["failure_chains"], "failure_id", "stalling")

    charging_need = _row(
        scene["needs_by_attribute"], "attribute_id", "charging"
    )
    drive_need = _row(
        scene["needs_by_attribute"], "attribute_id", "self_propel"
    )
    runtime_need = _row(
        scene["needs_by_attribute"], "attribute_id", "runtime"
    )
    bag_need = _row(
        scene["needs_by_attribute"],
        "attribute_id",
        "bagging_mulching",
    )
    power_need = _row(
        scene["needs_by_attribute"], "attribute_id", "power"
    )
    impressed = next(
        row
        for row in scene["decision_signals"]
        if row["script_id"] == "E24" and row["signal_id"] == "impressed"
    )
    recommend = next(
        row
        for row in scene["decision_signals"]
        if row["script_id"] == "E28" and row["signal_id"] == "recommend"
    )
    returns = next(
        row
        for row in scene["decision_signals"]
        if row["script_id"] == "E28" and row["signal_id"] == "return"
    )

    automation = _row(
        scene["explicit_smart_signals"], "signal_id", "automation"
    )
    connectivity = _row(
        scene["explicit_smart_signals"], "signal_id", "connectivity"
    )
    diagnosis = _row(
        scene["smart_hypotheses"],
        "hypothesis_id",
        "diagnostics_maintenance",
    )
    runtime_prediction = _row(
        scene["smart_hypotheses"],
        "hypothesis_id",
        "runtime_prediction",
    )
    adaptive_drive = _row(
        scene["smart_hypotheses"],
        "hypothesis_id",
        "adaptive_drive",
    )

    scene_sections = [
        {
            "scene_id": "overview",
            "title": "满意度与比较总览",
            "insights": [
                _insight(
                    "overview_satisfaction",
                    "总体口碑强，但不能用均值掩盖具体任务压力",
                    (
                        f"全部 {average['sample_size']:,} 条有效评分实体的"
                        f"平均星级为 {average['value']:.2f}，"
                        f"好评率为 {positive['value']:.1%}，"
                        f"差评率为 {negative['value']:.1%}。"
                        "后续章节把属性、任务和故障分开阅读，避免把总体好评"
                        "直接等同于每个体验环节都没有问题。"
                    ),
                    claim_type="measured",
                    confidence="strong",
                    metric_refs=[
                        _report_ref(
                            "metrics",
                            "average_rating",
                            "value",
                            average["value"],
                        ),
                        _report_ref(
                            "metrics",
                            "positive_rating_rate",
                            "value",
                            positive["value"],
                        ),
                        _report_ref(
                            "metrics",
                            "negative_rating_rate",
                            "value",
                            negative["value"],
                        ),
                        _report_ref(
                            "metrics",
                            "written_coverage",
                            "value",
                            written["value"],
                        ),
                    ],
                    evidence_refs=[],
                    limitations=["总体评分不提供属性级因果解释。"],
                ),
                _insight(
                    "overview_channel",
                    "渠道差异存在，但更像需要控制构成后的研究线索",
                    (
                        f"RYOBI 渠道样本平均星级 {ryobi['average_rating']:.3f}，"
                        f"Home Depot 为 {home_depot['average_rating']:.3f}；"
                        f"两者样本量分别为 {ryobi['sample_size']:,} 与 "
                        f"{home_depot['sample_size']:,}。差异通过当前检验，但"
                        "型号、时间和评论人群构成并未被实验控制，不能写成"
                        "渠道导致评分变化。"
                    ),
                    claim_type="association",
                    confidence="moderate",
                    metric_refs=[
                        _scene_ref(
                            "channels",
                            "ryobi",
                            "average_rating",
                            ryobi["average_rating"],
                        ),
                        _scene_ref(
                            "channels",
                            "homedepot",
                            "average_rating",
                            home_depot["average_rating"],
                        ),
                        _scene_ref(
                            "channels",
                            "ryobi",
                            "sample_size",
                            ryobi["sample_size"],
                        ),
                        _scene_ref(
                            "channels",
                            "homedepot",
                            "sample_size",
                            home_depot["sample_size"],
                        ),
                    ],
                    evidence_refs=[],
                    limitations=[
                        "观察性比较；未控制型号、时间和来源人群构成。",
                    ],
                ),
            ],
        },
        {
            "scene_id": "profiles",
            "title": "用户画像",
            "insights": [
                _insight(
                    "profile_large_yard",
                    "大面积或复杂草坪是最清晰的显式用户群",
                    (
                        f"{large_yard['review_entities']:,} 条评论明确提到草坪"
                        f"规模或复杂工况，占正文样本 {large_yard['coverage']:.1%}。"
                        "该群体最常同时提及充电、动力与自走控制，说明研究"
                        "优先级应围绕完整作业闭环，而非单个参数。"
                    ),
                    claim_type="association",
                    confidence="strong",
                    metric_refs=[
                        _scene_ref(
                            "profiles",
                            "large_complex_yard",
                            "review_entities",
                            large_yard["review_entities"],
                        ),
                        _scene_ref(
                            "profiles",
                            "large_complex_yard",
                            "coverage",
                            large_yard["coverage"],
                        ),
                    ],
                    evidence_refs=large_yard["evidence_ids"],
                    limitations=[
                        "群组由明确文本规则构成，可与其他画像重叠。",
                    ],
                ),
                _insight(
                    "profile_switcher",
                    "燃油转电动或换机用户的显式反馈偏积极",
                    (
                        f"识别到 {switcher['review_entities']:,} 条燃油转电动、"
                        f"旧机替换或换机评论，平均星级 "
                        f"{switcher['average_rating']:.2f}，4–5 星占 "
                        f"{switcher['high_star_rate']:.1%}。这支持继续研究"
                        "从燃油体验迁移时，哪些优势最能驱动切换。"
                    ),
                    claim_type="association",
                    confidence="moderate",
                    metric_refs=[
                        _scene_ref(
                            "profiles",
                            "gas_switcher",
                            "review_entities",
                            switcher["review_entities"],
                        ),
                        _scene_ref(
                            "profiles",
                            "gas_switcher",
                            "average_rating",
                            switcher["average_rating"],
                        ),
                        _scene_ref(
                            "profiles",
                            "gas_switcher",
                            "high_star_rate",
                            switcher["high_star_rate"],
                        ),
                    ],
                    evidence_refs=switcher["evidence_ids"],
                    limitations=["没有燃油产品对照组，不能计算转换提升。"],
                ),
                _insight(
                    "profile_accessibility",
                    "年长或能力受限用户首先关注自走与操控负担",
                    (
                        f"{accessibility['review_entities']:,} 条评论明确表达年龄"
                        "或行动、力量、健康限制；其中自走与速度控制、重量与"
                        "操控是最常共现的产品属性。该信号适合用于无障碍访谈"
                        "招募和任务测试，而不是推断购买者年龄结构。"
                    ),
                    claim_type="association",
                    confidence="moderate",
                    metric_refs=[
                        _scene_ref(
                            "profiles",
                            "accessibility",
                            "review_entities",
                            accessibility["review_entities"],
                        ),
                        _scene_ref(
                            "profiles",
                            "accessibility",
                            "coverage",
                            accessibility["coverage"],
                        ),
                    ],
                    evidence_refs=accessibility["evidence_ids"],
                    limitations=[
                        "只统计明确表达，不推断未披露的人口属性。",
                    ],
                ),
            ],
        },
        {
            "scene_id": "scenarios",
            "title": "使用场景",
            "insights": [
                _insight(
                    "scenario_energy_workflow",
                    "充电管理与大面积作业构成同一条体验链",
                    (
                        f"日常割草、充电与换电、大面积或长时间作业分别命中 "
                        f"{mowing['review_entities']:,}、"
                        f"{charging_scene['review_entities']:,} 和 "
                        f"{large_scale['review_entities']:,} 条评论。后两类平均"
                        f"星级为 {charging_scene['average_rating']:.2f} 和 "
                        f"{large_scale['average_rating']:.2f}，低于日常割草的 "
                        f"{mowing['average_rating']:.2f}。这是一条应整体评估的"
                        "续航—换电—充电—完成作业链路。"
                    ),
                    claim_type="association",
                    confidence="strong",
                    metric_refs=[
                        _scene_ref(
                            "usage_scenarios",
                            "mowing",
                            "review_entities",
                            mowing["review_entities"],
                        ),
                        _scene_ref(
                            "usage_scenarios",
                            "charging",
                            "review_entities",
                            charging_scene["review_entities"],
                        ),
                        _scene_ref(
                            "usage_scenarios",
                            "large_scale",
                            "review_entities",
                            large_scale["review_entities"],
                        ),
                        _scene_ref(
                            "usage_scenarios",
                            "mowing",
                            "average_rating",
                            mowing["average_rating"],
                        ),
                        _scene_ref(
                            "usage_scenarios",
                            "charging",
                            "average_rating",
                            charging_scene["average_rating"],
                        ),
                        _scene_ref(
                            "usage_scenarios",
                            "large_scale",
                            "average_rating",
                            large_scale["average_rating"],
                        ),
                    ],
                    evidence_refs=(
                        charging_scene["evidence_ids"]
                        + large_scale["evidence_ids"]
                    ),
                    limitations=[
                        "场景规则可重叠，命中数不可直接相加。",
                        "评分是评论级关联，不是场景句级情感。",
                    ],
                ),
                _insight(
                    "scenario_slope",
                    "坡地场景的故障共现更集中，但总体评分仍高",
                    (
                        f"坡地或崎岖地形命中 {slope['review_entities']:,} 条，"
                        f"故障共现率为 {slope['failure_rate']:.1%}，高于"
                        f"日常割草的 {mowing['failure_rate']:.1%}；但其平均"
                        f"星级仍为 {slope['average_rating']:.2f}。这说明"
                        "复杂地形更适合拆分成功与失败任务，不应只看均值。"
                    ),
                    claim_type="association",
                    confidence="moderate",
                    metric_refs=[
                        _scene_ref(
                            "usage_scenarios",
                            "slope_rough",
                            "review_entities",
                            slope["review_entities"],
                        ),
                        _scene_ref(
                            "usage_scenarios",
                            "slope_rough",
                            "failure_rate",
                            slope["failure_rate"],
                        ),
                        _scene_ref(
                            "usage_scenarios",
                            "mowing",
                            "failure_rate",
                            mowing["failure_rate"],
                        ),
                        _scene_ref(
                            "usage_scenarios",
                            "slope_rough",
                            "average_rating",
                            slope["average_rating"],
                        ),
                    ],
                    evidence_refs=slope["evidence_ids"],
                    limitations=[
                        "故障为同一评论实体共现，不代表坡地造成故障。",
                    ],
                ),
            ],
        },
        {
            "scene_id": "experience",
            "title": "产品体验与故障",
            "insights": [
                _insight(
                    "experience_strengths",
                    "低噪、易装配和易操控是最稳定的体验资产",
                    (
                        "在属性提及评论中，噪音与振动、重量与操控、装配与"
                        f"收纳的平均星级分别为 {noise['average_rating']:.2f}、"
                        f"{handling['average_rating']:.2f} 和 "
                        f"{assembly['average_rating']:.2f}。这些信号适合作为"
                        "价值主张与概念保留项，但仍需句级人工抽检。"
                    ),
                    claim_type="association",
                    confidence="strong",
                    metric_refs=[
                        _scene_ref(
                            "attribute_experience",
                            "noise",
                            "average_rating",
                            noise["average_rating"],
                        ),
                        _scene_ref(
                            "attribute_experience",
                            "handling",
                            "average_rating",
                            handling["average_rating"],
                        ),
                        _scene_ref(
                            "attribute_experience",
                            "assembly_storage",
                            "average_rating",
                            assembly["average_rating"],
                        ),
                    ],
                    evidence_refs=(
                        noise["evidence_ids"]
                        + handling["evidence_ids"]
                        + assembly["evidence_ids"]
                    ),
                    limitations=[
                        "属性与星级为评论级关联，不是属性句级情感。",
                    ],
                ),
                _insight(
                    "experience_pressure",
                    "价值、充电和续航是需要优先解释的体验压力",
                    (
                        f"价格与价值属性的平均星级为 {value['average_rating']:.2f}，"
                        f"1–2 星占 {value['low_star_rate']:.1%}；充电和续航"
                        f"的平均星级分别为 {charging['average_rating']:.2f} "
                        f"和 {runtime['average_rating']:.2f}。这些不是故障率，"
                        "但足以支持围绕价格合理性和完整作业能力继续验证。"
                    ),
                    claim_type="association",
                    confidence="strong",
                    metric_refs=[
                        _scene_ref(
                            "attribute_experience",
                            "value",
                            "average_rating",
                            value["average_rating"],
                        ),
                        _scene_ref(
                            "attribute_experience",
                            "value",
                            "low_star_rate",
                            value["low_star_rate"],
                        ),
                        _scene_ref(
                            "attribute_experience",
                            "charging",
                            "average_rating",
                            charging["average_rating"],
                        ),
                        _scene_ref(
                            "attribute_experience",
                            "runtime",
                            "average_rating",
                            runtime["average_rating"],
                        ),
                    ],
                    evidence_refs=(
                        value["evidence_ids"]
                        + charging["evidence_ids"]
                        + runtime["evidence_ids"]
                    ),
                    limitations=[
                        "提及率不等于真实发生率或市场渗透率。",
                    ],
                ),
                _insight(
                    "experience_failures",
                    "停止工作与破损覆盖不高，但负向后果明显",
                    (
                        f"停止工作命中 {stopped['review_entities']:,} 条，平均"
                        f"星级 {stopped['average_rating']:.2f}，1–2 星占 "
                        f"{stopped['low_star_rate']:.1%}；破损命中 "
                        f"{breakage['review_entities']:,} 条，平均星级 "
                        f"{breakage['average_rating']:.2f}，1–2 星占 "
                        f"{breakage['low_star_rate']:.1%}。两类问题都频繁"
                        "共现补救与服务表达，适合优先做根因复核。"
                    ),
                    claim_type="association",
                    confidence="strong",
                    metric_refs=[
                        _scene_ref(
                            "failure_chains",
                            "stopped_working",
                            "review_entities",
                            stopped["review_entities"],
                        ),
                        _scene_ref(
                            "failure_chains",
                            "stopped_working",
                            "average_rating",
                            stopped["average_rating"],
                        ),
                        _scene_ref(
                            "failure_chains",
                            "stopped_working",
                            "low_star_rate",
                            stopped["low_star_rate"],
                        ),
                        _scene_ref(
                            "failure_chains",
                            "breakage",
                            "review_entities",
                            breakage["review_entities"],
                        ),
                        _scene_ref(
                            "failure_chains",
                            "breakage",
                            "average_rating",
                            breakage["average_rating"],
                        ),
                        _scene_ref(
                            "failure_chains",
                            "breakage",
                            "low_star_rate",
                            breakage["low_star_rate"],
                        ),
                        _scene_ref(
                            "failure_chains",
                            "stalling",
                            "review_entities",
                            stalling["review_entities"],
                        ),
                    ],
                    evidence_refs=(
                        stopped["evidence_ids"]
                        + breakage["evidence_ids"]
                        + stalling["evidence_ids"]
                    ),
                    limitations=[
                        "规则命中不是保修数据库中的真实故障率。",
                        "服务与补救是实体级共现，不等于固定处理路径。",
                    ],
                ),
            ],
        },
        {
            "scene_id": "needs",
            "title": "需求与决策",
            "insights": [
                _insight(
                    "needs_explicit",
                    "明确需求集中在充电、自走、续航与集草闭环",
                    (
                        "明确需求与属性共现中，充电、自走与速度控制、续航、"
                        f"集草与碎草分别命中 {charging_need['review_entities']:,}、"
                        f"{drive_need['review_entities']:,}、"
                        f"{runtime_need['review_entities']:,} 和 "
                        f"{bag_need['review_entities']:,} 条。动力需求仅 "
                        f"{power_need['review_entities']:,} 条，但对应平均星级"
                        f"为 {power_need['average_rating']:.2f}，适合回看具体"
                        "负载与型号条件。"
                    ),
                    claim_type="association",
                    confidence="strong",
                    metric_refs=[
                        _scene_ref(
                            "needs_by_attribute",
                            "charging",
                            "review_entities",
                            charging_need["review_entities"],
                        ),
                        _scene_ref(
                            "needs_by_attribute",
                            "self_propel",
                            "review_entities",
                            drive_need["review_entities"],
                        ),
                        _scene_ref(
                            "needs_by_attribute",
                            "runtime",
                            "review_entities",
                            runtime_need["review_entities"],
                        ),
                        _scene_ref(
                            "needs_by_attribute",
                            "bagging_mulching",
                            "review_entities",
                            bag_need["review_entities"],
                        ),
                        _scene_ref(
                            "needs_by_attribute",
                            "power",
                            "review_entities",
                            power_need["review_entities"],
                        ),
                        _scene_ref(
                            "needs_by_attribute",
                            "power",
                            "average_rating",
                            power_need["average_rating"],
                        ),
                    ],
                    evidence_refs=(
                        charging_need["evidence_ids"]
                        + drive_need["evidence_ids"]
                        + runtime_need["evidence_ids"]
                        + bag_need["evidence_ids"]
                        + power_need["evidence_ids"]
                    ),
                    limitations=[
                        "需求共现不等于需求优先级；未加入成本或可行性。",
                    ],
                ),
                _insight(
                    "needs_retention",
                    "惊喜表达和推荐很多，退货信号虽少但评价极低",
                    (
                        f"“印象超预期”命中 {impressed['review_entities']:,} 条，"
                        f"平均星级 {impressed['average_rating']:.2f}；明确推荐"
                        f"命中 {recommend['review_entities']:,} 条。退货信号"
                        f"命中 {returns['review_entities']:,} 条，平均星级仅 "
                        f"{returns['average_rating']:.2f}。保留优势与降低退货"
                        "应作为两条并行的产品验证路线。"
                    ),
                    claim_type="association",
                    confidence="strong",
                    metric_refs=[
                        _scene_ref(
                            "decision_signals",
                            "E24:impressed",
                            "review_entities",
                            impressed["review_entities"],
                        ),
                        _scene_ref(
                            "decision_signals",
                            "E24:impressed",
                            "average_rating",
                            impressed["average_rating"],
                        ),
                        _scene_ref(
                            "decision_signals",
                            "E28:recommend",
                            "review_entities",
                            recommend["review_entities"],
                        ),
                        _scene_ref(
                            "decision_signals",
                            "E28:return",
                            "review_entities",
                            returns["review_entities"],
                        ),
                        _scene_ref(
                            "decision_signals",
                            "E28:return",
                            "average_rating",
                            returns["average_rating"],
                        ),
                    ],
                    evidence_refs=(
                        impressed["evidence_ids"]
                        + recommend["evidence_ids"]
                        + returns["evidence_ids"]
                    ),
                    limitations=[
                        "文本意向不等于真实复购、退货或推荐行为。",
                    ],
                ),
            ],
        },
        {
            "scene_id": "product_opportunities",
            "title": "产品机会",
            "insights": [
                _insight(
                    "opportunity_reliability",
                    "产品机会首先应从可靠性问题清单进入验证",
                    (
                        "停止工作、破损和停转/中断分别命中 "
                        f"{stopped['review_entities']:,}、"
                        f"{breakage['review_entities']:,} 和 "
                        f"{stalling['review_entities']:,} 条；三类评论的平均"
                        f"星级均不高于 {max(stopped['average_rating'], breakage['average_rating'], stalling['average_rating']):.2f}。"
                        "建议先核验型号、部件、工况和时间链，再决定是否进入"
                        "工程路线图。"
                    ),
                    claim_type="derived_candidate",
                    confidence="moderate",
                    metric_refs=[
                        _scene_ref(
                            "product_opportunities",
                            "quality_stopped_working",
                            "review_entities",
                            stopped["review_entities"],
                        ),
                        _scene_ref(
                            "product_opportunities",
                            "quality_breakage",
                            "review_entities",
                            breakage["review_entities"],
                        ),
                        _scene_ref(
                            "product_opportunities",
                            "quality_stalling",
                            "review_entities",
                            stalling["review_entities"],
                        ),
                    ],
                    evidence_refs=(
                        stopped["evidence_ids"]
                        + breakage["evidence_ids"]
                        + stalling["evidence_ids"]
                    ),
                    limitations=[
                        "这是证据候选排序，不含成本、战略或工程可行性。",
                        "未计算市场发生率。",
                    ],
                ),
            ],
        },
        {
            "scene_id": "smart_opportunities",
            "title": "智能化机会",
            "insights": [
                _insight(
                    "smart_explicit",
                    "直接智能化需求证据不足，不能据此承诺功能",
                    (
                        f"自动化明确表达命中 {automation['review_entities']:,} 条，"
                        f"连接能力仅 {connectivity['review_entities']:,} 条且属于"
                        "低样本。现有数据更适合回答“哪些问题可能被智能能力"
                        "缓解”，不适合回答“用户已经普遍要求某个 App 功能”。"
                    ),
                    claim_type="measured",
                    confidence="moderate",
                    metric_refs=[
                        _scene_ref(
                            "explicit_smart_signals",
                            "automation",
                            "review_entities",
                            automation["review_entities"],
                        ),
                        _scene_ref(
                            "explicit_smart_signals",
                            "connectivity",
                            "review_entities",
                            connectivity["review_entities"],
                        ),
                    ],
                    evidence_refs=(
                        automation["evidence_ids"]
                        + connectivity["evidence_ids"]
                    ),
                    limitations=[
                        "规则词典可能漏掉未收录的智能化同义表达。",
                    ],
                ),
                _insight(
                    "smart_hypotheses",
                    "诊断、续航预测和自走个性化是待验证假设",
                    (
                        "把非智能痛点映射为智能化候选后，故障诊断与维护提醒、"
                        "剩余续航/可完成范围预测、自走速度与操控个性化分别"
                        f"关联 {diagnosis['review_entities']:,}、"
                        f"{runtime_prediction['review_entities']:,} 和 "
                        f"{adaptive_drive['review_entities']:,} 条评论。它们是"
                        "问题空间假设，下一步必须通过概念测试验证价值、信任"
                        "和使用意愿。"
                    ),
                    claim_type="derived_hypothesis",
                    confidence="limited",
                    metric_refs=[
                        _scene_ref(
                            "smart_hypotheses",
                            "diagnostics_maintenance",
                            "review_entities",
                            diagnosis["review_entities"],
                        ),
                        _scene_ref(
                            "smart_hypotheses",
                            "runtime_prediction",
                            "review_entities",
                            runtime_prediction["review_entities"],
                        ),
                        _scene_ref(
                            "smart_hypotheses",
                            "adaptive_drive",
                            "review_entities",
                            adaptive_drive["review_entities"],
                        ),
                    ],
                    evidence_refs=(
                        diagnosis["evidence_ids"]
                        + runtime_prediction["evidence_ids"]
                        + adaptive_drive["evidence_ids"]
                    ),
                    limitations=[
                        "候选由痛点映射生成，不代表用户直接要求该功能。",
                        "不能从评论共现推断功能会解决问题。",
                    ],
                ),
            ],
        },
    ]
    bundle_core = {
        "schema_version": "insight-bundle/v1-prototype",
        "core_report_id": report["report_id"],
        "core_report_hash": report["hashes"]["core_result"],
        "data_snapshot_id": report["data_snapshot_id"],
        "scene_analysis_version": scene["schema_version"],
        "generator": {
            "mode": "codex_session",
            "model": "OpenAI Codex current session",
            "prompt_version": "stakeholder-insights/1.0.0",
            "llm_used": True,
            "third_party_api_used": False,
            "core_metrics_modified": False,
        },
        "scenes": scene_sections,
        "boundaries": [
            "所有数字来自 core Report JSON 或 scene-analysis/v1。",
            "派生文字不能覆盖、补齐或重新计算核心指标。",
            "关系单位为评论实体共现，不自动解释为同句或因果。",
            "产品机会与智能化机会均为待验证候选。",
            "当前样本仅含 RYOBI，不能生成真实竞品结论。",
        ],
    }
    bundle_hash = hashlib.sha256(
        canonical_json(bundle_core).encode("utf-8")
    ).hexdigest()
    return {
        **bundle_core,
        "insight_bundle_id": f"ins_{bundle_hash[:24]}",
        "content_hash": f"sha256:{bundle_hash}",
    }


def validate_insight_bundle(
    bundle: dict[str, Any],
    report: dict[str, Any],
    scene: dict[str, Any],
) -> None:
    evidence_ids = {item["evidence_id"] for item in scene["evidence"]}
    report_metrics = {
        item["metric_id"]: item for item in report["metrics"]
    }
    scene_rows: dict[tuple[str, str], dict[str, Any]] = {}
    id_fields = {
        "profiles": "cohort_id",
        "usage_scenarios": "scenario_id",
        "attribute_experience": "attribute_id",
        "failure_chains": "failure_id",
        "needs_by_attribute": "attribute_id",
        "product_opportunities": "opportunity_id",
        "explicit_smart_signals": "signal_id",
        "smart_hypotheses": "hypothesis_id",
        "channels": "group",
    }
    for collection, id_field in id_fields.items():
        rows = (
            scene["comparison_lenses"][collection]
            if collection == "channels"
            else scene[collection]
        )
        for item in rows:
            scene_rows[(collection, str(item[id_field]))] = item
    for item in scene["decision_signals"]:
        scene_rows[
            (
                "decision_signals",
                f"{item['script_id']}:{item['signal_id']}",
            )
        ] = item

    insight_ids: set[str] = set()
    for section in bundle["scenes"]:
        for insight in section["insights"]:
            insight_id = insight["insight_id"]
            if insight_id in insight_ids:
                raise ValueError(f"duplicate insight_id: {insight_id}")
            insight_ids.add(insight_id)
            missing = set(insight["evidence_refs"]) - evidence_ids
            if missing:
                raise ValueError(
                    f"{insight_id} has missing evidence: {sorted(missing)}"
                )
            for ref in insight["metric_refs"]:
                if ref["source"] == "core_report":
                    if ref["collection"] != "metrics":
                        raise ValueError(
                            f"unsupported core collection: {ref}"
                        )
                    row = report_metrics[ref["row_id"]]
                elif ref["source"] == "scene_analysis":
                    row = scene_rows[(ref["collection"], ref["row_id"])]
                else:
                    raise ValueError(f"unsupported metric source: {ref}")
                if row[ref["field"]] != ref["value"]:
                    raise ValueError(f"metric reference mismatch: {ref}")
