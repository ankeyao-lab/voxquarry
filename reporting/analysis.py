from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from reporting.atomic_v2 import run_atomic_scripts
from reporting.config import (
    ANALYSIS_VERSION,
    ATOMIC_SCRIPT_REGISTRY_VERSION,
    DICTIONARY_VERSION,
    LOW_SAMPLE_N,
    PACKAGE_ROOT,
    RESEARCH_PLAN_VERSION,
    SCHEMA_VERSION,
)
from reporting.factors import (
    explicit_age_group,
    extract_atoms,
    factor_cooccurrence,
    keyword_document_frequency,
    load_dictionary,
)
from reporting.source import canonical_json, sha256_text
from reporting.source_completeness import limited_source_message


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
        / denominator
    )
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def _mean_ci(values: list[float]) -> list[float] | None:
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    margin = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    return [round(mean - margin, 4), round(mean + margin, 4)]


def _comparison_stats(
    group_values: list[float],
    reference_values: list[float],
    *,
    minimum_n: int,
) -> dict[str, Any]:
    if len(group_values) < minimum_n or len(reference_values) < minimum_n:
        return {
            "difference_vs_rest": None,
            "difference_interval_95": None,
            "effect_size": None,
            "p_value": None,
            "adjusted_p_value": None,
            "significant": False,
            "comparison_flag": "low_sample",
        }
    group_mean = statistics.fmean(group_values)
    reference_mean = statistics.fmean(reference_values)
    group_variance = statistics.variance(group_values)
    reference_variance = statistics.variance(reference_values)
    standard_error = math.sqrt(
        group_variance / len(group_values)
        + reference_variance / len(reference_values)
    )
    difference = group_mean - reference_mean
    if standard_error:
        z_score = difference / standard_error
        p_value = math.erfc(abs(z_score) / math.sqrt(2))
        interval = [
            round(difference - 1.96 * standard_error, 4),
            round(difference + 1.96 * standard_error, 4),
        ]
    else:
        p_value = 1.0 if difference == 0 else 0.0
        interval = [round(difference, 4), round(difference, 4)]
    pooled_denominator = len(group_values) + len(reference_values) - 2
    pooled_variance = (
        (
            (len(group_values) - 1) * group_variance
            + (len(reference_values) - 1) * reference_variance
        )
        / pooled_denominator
        if pooled_denominator > 0
        else 0
    )
    effect_size = (
        difference / math.sqrt(pooled_variance) if pooled_variance > 0 else 0.0
    )
    return {
        "difference_vs_rest": _round(difference, 3),
        "difference_interval_95": interval,
        "effect_size": _round(effect_size, 3),
        "p_value": _round(p_value, 6),
        "adjusted_p_value": None,
        "significant": False,
        "comparison_flag": None,
    }


def _apply_holm(rows: list[dict[str, Any]], alpha: float) -> None:
    tested = [
        (index, float(row["p_value"]))
        for index, row in enumerate(rows)
        if row.get("p_value") is not None
    ]
    tested.sort(key=lambda item: (item[1], item[0]))
    running_max = 0.0
    total = len(tested)
    for rank, (index, p_value) in enumerate(tested):
        adjusted = min(1.0, p_value * (total - rank))
        running_max = max(running_max, adjusted)
        rows[index]["adjusted_p_value"] = _round(running_max, 6)
        rows[index]["significant"] = running_max < alpha


def _metric_definition(
    metric_id: str,
    name: str,
    definition: str,
    unit: str,
    population_unit: str,
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "name": name,
        "definition": definition,
        "unit": unit,
        "population_unit": population_unit,
        "version": ANALYSIS_VERSION,
    }


def _metric(
    metric_id: str,
    value: float | int | None,
    *,
    sample_size: int,
    numerator: int | None = None,
    denominator: int | None = None,
    interval: list[float] | None = None,
    population_unit: str = "review_entity",
    filters: dict[str, Any] | None = None,
    evidence_ids: list[str] | None = None,
    quality_flags: list[str] | None = None,
) -> dict[str, Any]:
    flags = list(quality_flags or [])
    if sample_size < LOW_SAMPLE_N and "low_sample" not in flags:
        flags.append("low_sample")
    return {
        "metric_id": metric_id,
        "value": _round(value) if isinstance(value, float) else value,
        "sample_size": sample_size,
        "numerator": numerator,
        "denominator": denominator,
        "interval_95": interval,
        "population_unit": population_unit,
        "filters": filters or {},
        "evidence_ids": evidence_ids or [],
        "quality_flags": flags,
    }


def _entity_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        fact
        for fact in facts
        if fact["business_key_status"] == "incomplete"
        or fact["canonical_entity_rank"] == 1
    ]


def _group_summary(
    facts: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
    *,
    minimum_n: int = LOW_SAMPLE_N,
    alpha: float = 0.05,
    compare_to_rest: bool = True,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        key = key_fn(fact)
        if key:
            grouped[key].append(fact)
    rows = []
    for key in sorted(grouped):
        values = grouped[key]
        ratings = [fact["rating"] for fact in values if fact["rating"] is not None]
        reference_ratings = [
            fact["rating"]
            for other_key, other_values in grouped.items()
            if other_key != key
            for fact in other_values
            if fact["rating"] is not None
        ]
        written = sum(fact["has_text"] for fact in values)
        positive = sum(rating >= 4 for rating in ratings)
        negative = sum(rating <= 2 for rating in ratings)
        comparison = (
            _comparison_stats(
                [float(value) for value in ratings],
                [float(value) for value in reference_ratings],
                minimum_n=minimum_n,
            )
            if compare_to_rest
            else {}
        )
        rows.append(
            {
                "group": key,
                "sample_size": len(values),
                "rating_n": len(ratings),
                "average_rating": _round(
                    statistics.fmean(ratings) if ratings else None, 3
                ),
                "positive_rate": _round(_ratio(positive, len(ratings))),
                "negative_rate": _round(_ratio(negative, len(ratings))),
                "written_reviews": written,
                "written_coverage": _round(_ratio(written, len(values))),
                "quality_flag": (
                    "low_sample" if len(values) < minimum_n else None
                ),
                **comparison,
            }
        )
    if compare_to_rest:
        _apply_holm(rows, alpha)
    rows.sort(key=lambda item: (-item["sample_size"], item["group"]))
    return rows


def _atom_rows(
    atoms: list[dict[str, Any]],
    namespace: str,
    written_n: int,
    evidence_by_factor: dict[tuple[str, str], list[str]],
) -> list[dict[str, Any]]:
    by_factor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labels: dict[str, str] = {}
    frameworks: dict[str, str | None] = {}
    for atom in atoms:
        if atom["factor_namespace"] != namespace:
            continue
        by_factor[atom["factor_id"]].append(atom)
        labels[atom["factor_id"]] = atom["factor_label"]
        frameworks[atom["factor_id"]] = atom.get("framework")
    rows = []
    for factor_id in sorted(by_factor):
        values = by_factor[factor_id]
        entity_count = len({item["entity_ref"] for item in values})
        rated = [item for item in values if item["polarity"] != "unknown"]
        positive = sum(item["polarity"] == "positive" for item in rated)
        negative = sum(item["polarity"] == "negative" for item in rated)
        rows.append(
            {
                "factor_id": factor_id,
                "label": labels[factor_id],
                "framework": frameworks[factor_id],
                "mention_count": entity_count,
                "mention_rate": _round(_ratio(entity_count, written_n)),
                "rated_mentions": len(rated),
                "positive_count": positive,
                "positive_share": _round(_ratio(positive, len(rated))),
                "negative_count": negative,
                "negative_share": _round(_ratio(negative, len(rated))),
                "net_rating_polarity": _round(
                    _ratio(positive - negative, len(rated))
                ),
                "evidence_ids": evidence_by_factor.get((namespace, factor_id), []),
                "quality_flag": (
                    "low_sample" if entity_count < LOW_SAMPLE_N else None
                ),
            }
        )
    rows.sort(
        key=lambda item: (
            -item["mention_count"],
            -item["negative_count"],
            item["factor_id"],
        )
    )
    return rows


def _kano_candidates(attribute_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for row in attribute_rows:
        positive = row["positive_share"] or 0
        negative = row["negative_share"] or 0
        mention = row["mention_rate"] or 0
        if row["rated_mentions"] < LOW_SAMPLE_N:
            classification = "证据不足"
        elif negative >= 0.25 and positive < 0.65:
            classification = "基本型候选"
        elif positive >= 0.65 and negative >= 0.12:
            classification = "表现型候选"
        elif positive >= 0.78 and negative < 0.12:
            classification = "正向属性（Kano 类型待验证）"
        elif mention < 0.01:
            classification = "低关注候选"
        else:
            classification = "Kano 类型待验证"
        candidates.append(
            {
                "factor_id": row["factor_id"],
                "label": row["label"],
                "classification": classification,
                "mention_count": row["mention_count"],
                "mention_rate": row["mention_rate"],
                "positive_share": row["positive_share"],
                "negative_share": row["negative_share"],
                "evidence_ids": row["evidence_ids"],
            }
        )
    return candidates


def _opportunity_rows(
    pain_rows: list[dict[str, Any]],
    attribute_rows: list[dict[str, Any]],
    atoms: list[dict[str, Any]],
    entity_lookup: dict[str, dict[str, Any]],
    written_entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    attribute_by_id = {row["factor_id"]: row for row in attribute_rows}
    opportunities = []
    for pain in pain_rows:
        factor_atoms = [
            atom
            for atom in atoms
            if atom["factor_namespace"] == "pain"
            and atom["factor_id"] == pain["factor_id"]
        ]
        brands = {
            entity_lookup[item["entity_ref"]]["brand"]
            for item in factor_atoms
            if item["entity_ref"] in entity_lookup
            and entity_lookup[item["entity_ref"]]["brand"]
        }
        models = {
            entity_lookup[item["entity_ref"]]["model"]
            for item in factor_atoms
            if item["entity_ref"] in entity_lookup
            and entity_lookup[item["entity_ref"]]["model"]
        }
        channels = {
            entity_lookup[item["entity_ref"]]["channel"]
            for item in factor_atoms
            if item["entity_ref"] in entity_lookup
            and entity_lookup[item["entity_ref"]]["channel"]
        }
        negative_entities = {
            item["entity_ref"]
            for item in factor_atoms
            if item["polarity"] == "negative"
        }
        ego_written = [
            fact for fact in written_entities if fact["brand"].casefold() == "ego"
        ]
        competitor_written = [
            fact for fact in written_entities if fact["brand"].casefold() != "ego"
        ]
        ego_negative = sum(
            (fact["review_entity_id"] or fact["review_occurrence_id"])
            in negative_entities
            for fact in ego_written
        )
        competitor_negative = sum(
            (fact["review_entity_id"] or fact["review_occurrence_id"])
            in negative_entities
            for fact in competitor_written
        )
        ego_negative_rate = _ratio(ego_negative, len(ego_written))
        competitor_negative_rate = _ratio(
            competitor_negative, len(competitor_written)
        )
        recent_rate, prior_rate = _factor_period_rates(
            factor_atoms, written_entities
        )
        related = attribute_by_id.get(pain["factor_id"])
        opportunities.append(
            {
                "opportunity_id": f"opp_{pain['factor_id']}",
                "title": f"降低“{pain['label']}”的发生与后果",
                "problem": pain["label"],
                "mention_count": pain["mention_count"],
                "mention_rate": pain["mention_rate"],
                "negative_count": pain["negative_count"],
                "negative_share": pain["negative_share"],
                "cross_brand_count": len(brands),
                "cross_model_count": len(models),
                "cross_channel_count": len(channels),
                "related_attribute": related["label"] if related else None,
                "recent_12m_mention_rate": _round(recent_rate),
                "prior_12m_mention_rate": _round(prior_rate),
                "trend_delta": (
                    _round(recent_rate - prior_rate)
                    if recent_rate is not None and prior_rate is not None
                    else None
                ),
                "ego_negative_rate": _round(ego_negative_rate),
                "competitor_negative_rate": _round(competitor_negative_rate),
                "competitor_gap": (
                    _round(ego_negative_rate - competitor_negative_rate)
                    if ego_negative_rate is not None
                    and competitor_negative_rate is not None
                    else None
                ),
                "evidence_confidence": (
                    "strong"
                    if pain["negative_count"] >= 100 and len(brands) >= 2
                    else "moderate"
                    if pain["negative_count"] >= LOW_SAMPLE_N
                    else "limited"
                ),
                "evidence_ids": pain["evidence_ids"],
                "boundary": "网评证据机会，不代表工程、成本或路线图优先级",
            }
        )
    opportunities.sort(
        key=lambda item: (
            -item["negative_count"],
            -item["mention_count"],
            item["opportunity_id"],
        )
    )
    return opportunities


def _month_index(date_value: str | None) -> int | None:
    if not date_value or len(date_value) < 7:
        return None
    try:
        year, month = date_value[:7].split("-")
        return int(year) * 12 + int(month) - 1
    except (TypeError, ValueError):
        return None


def _factor_period_rates(
    factor_atoms: list[dict[str, Any]],
    written_entities: list[dict[str, Any]],
) -> tuple[float | None, float | None]:
    months = [
        _month_index(fact.get("submission_date")) for fact in written_entities
    ]
    available = [value for value in months if value is not None]
    if not available:
        return None, None
    latest = max(available)
    recent_start = latest - 11
    prior_start = latest - 23
    entity_month = {
        fact["review_entity_id"] or fact["review_occurrence_id"]: _month_index(
            fact.get("submission_date")
        )
        for fact in written_entities
    }
    factor_entities = {atom["entity_ref"] for atom in factor_atoms}
    recent_entities = {
        entity
        for entity, month in entity_month.items()
        if month is not None and recent_start <= month <= latest
    }
    prior_entities = {
        entity
        for entity, month in entity_month.items()
        if month is not None and prior_start <= month < recent_start
    }
    return (
        _ratio(len(recent_entities & factor_entities), len(recent_entities)),
        _ratio(len(prior_entities & factor_entities), len(prior_entities)),
    )


def _time_rows(entity_facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = [
        fact for fact in entity_facts if fact["submission_date"] and fact["rating"]
    ]
    rows = _group_summary(parsed, lambda fact: fact["submission_date"][:7])
    rows.sort(key=lambda item: item["group"])
    return rows


def _channel_overlap(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    channels_by_entity: dict[str, set[str]] = defaultdict(set)
    for fact in facts:
        if fact["review_entity_id"]:
            channels_by_entity[fact["review_entity_id"]].add(fact["channel"])
    patterns = Counter(
        " + ".join(sorted(channels))
        for channels in channels_by_entity.values()
        if len(channels) > 1
    )
    return [
        {"channel_pattern": pattern, "shared_entities": count}
        for pattern, count in sorted(
            patterns.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def _rule_segment_rows(
    atoms: list[dict[str, Any]],
    entity_lookup: dict[str, dict[str, Any]],
    written_n: int,
) -> list[dict[str, Any]]:
    labels_by_entity: dict[str, set[str]] = defaultdict(set)
    for atom in atoms:
        if atom["factor_namespace"] == "user_trait":
            labels_by_entity[atom["entity_ref"]].add(atom["factor_label"])
    grouped: dict[str, set[str]] = defaultdict(set)
    for entity_ref, labels in labels_by_entity.items():
        if labels:
            grouped[" × ".join(sorted(labels))].add(entity_ref)
    rows = []
    for label, entity_refs in grouped.items():
        ratings = [
            entity_lookup[entity_ref]["rating"]
            for entity_ref in entity_refs
            if entity_ref in entity_lookup
            and entity_lookup[entity_ref]["rating"] is not None
        ]
        rows.append(
            {
                "segment": label,
                "review_entities": len(entity_refs),
                "coverage_of_written": _round(
                    _ratio(len(entity_refs), written_n)
                ),
                "average_rating": _round(
                    statistics.fmean(ratings) if ratings else None, 3
                ),
                "quality_flag": (
                    "low_sample" if len(entity_refs) < LOW_SAMPLE_N else None
                ),
                "boundary": "基于明确规则标签，群组可能重叠且不代表总体人口分群",
            }
        )
    rows.sort(
        key=lambda item: (-item["review_entities"], item["segment"])
    )
    return rows[:20]


def _smart_rows_with_framework(
    rows: list[dict[str, Any]], dictionary: dict[str, Any]
) -> list[dict[str, Any]]:
    by_id = {row["factor_id"]: row for row in rows}
    resolved = []
    for spec in dictionary.get("smart_signals", []):
        row = by_id.get(
            spec["id"],
            {
                "factor_id": spec["id"],
                "label": spec["label"],
                "framework": spec.get("framework"),
                "mention_count": 0,
                "mention_rate": 0.0,
                "rated_mentions": 0,
                "positive_count": 0,
                "positive_share": None,
                "negative_count": 0,
                "negative_share": None,
                "net_rating_polarity": None,
                "evidence_ids": [],
                "quality_flag": "low_sample",
            },
        )
        resolved.append(
            {
                **row,
                "trigger": spec.get("trigger"),
                "candidate_action": spec.get("candidate_action"),
                "feedback": spec.get("feedback"),
                "user_value": spec.get("user_value"),
                "risk": spec.get("risk"),
                "boundary": "明确语句信号；候选动作是方法论映射，不是用户原话",
            }
        )
    resolved.sort(
        key=lambda item: (
            -item["mention_count"],
            item.get("framework") or "",
            item["factor_id"],
        )
    )
    return resolved


def _table(
    table_id: str,
    title: str,
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    *,
    denominator_note: str,
    default_sort: dict[str, str],
) -> dict[str, Any]:
    return {
        "table_id": table_id,
        "title": title,
        "columns": columns,
        "rows": rows,
        "denominator_note": denominator_note,
        "default_sort": default_sort,
    }


def build_report(
    facts: list[dict[str, Any]],
    snapshot_manifest: dict[str, Any],
    research_plan: dict[str, Any] | None = None,
    report_name: str | None = None,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    plan = research_plan or json.loads(
        (PACKAGE_ROOT / "research_plans" / "full-category-v1.json").read_text(
            encoding="utf-8"
        )
    )
    dictionary = load_dictionary()
    entities = _entity_facts(facts)
    market_contract_mode = snapshot_manifest["quality_summary"].get(
        "market_contract_mode", "legacy"
    )
    entity_lookup = {
        fact["review_entity_id"] or fact["review_occurrence_id"]: fact
        for fact in entities
    }
    written = [fact for fact in entities if fact["has_text"]]
    market_region_counts = Counter(
        fact.get("market_region")
        for fact in entities
        if fact.get("market_region")
    )
    market_region_rows = [
        {
            "market_region": region,
            "eligible_valid_text_entities": count,
            "share": _round(_ratio(count, len(entities))),
        }
        for region, count in sorted(
            market_region_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    market_time_ranges: dict[str, tuple[str | None, str | None]] = {}
    for region in market_region_counts:
        dates = sorted(
            fact["submission_date"]
            for fact in entities
            if fact.get("market_region") == region
            and fact.get("submission_date")
        )
        market_time_ranges[region] = (
            dates[0] if dates else None,
            dates[-1] if dates else None,
        )
    ratings = [fact["rating"] for fact in entities if fact["rating"] is not None]
    atoms, evidence, extraction = extract_atoms(written, dictionary)
    atomic_v2 = run_atomic_scripts(
        written,
        snapshot_id=snapshot_manifest["snapshot_id"],
        output_root=artifact_root,
    )
    evidence.extend(atomic_v2["evidence"])
    evidence_by_factor: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in evidence:
        evidence_by_factor[
            (item["factor_namespace"], item["factor_id"])
        ].append(item["evidence_id"])

    rating_counts = Counter(ratings)
    positive = sum(value >= 4 for value in ratings)
    negative = sum(value <= 2 for value in ratings)
    recommended_values = [
        fact["is_recommended"]
        for fact in entities
        if fact["is_recommended"] is not None
    ]
    recommended_true = sum(value is True for value in recommended_values)

    metric_definitions = [
        _metric_definition(
            "sample_size",
            "唯一评论实体样本量",
            "完整业务键按来源实体去重；业务键不完整记录按出现记录保留并单列限制",
            "count",
            "review_entity",
        ),
        _metric_definition(
            "written_review_count",
            "有正文样本量",
            "review_text 去空白后非空的唯一评论实体",
            "count",
            "review_entity",
        ),
        _metric_definition(
            "written_coverage",
            "正文覆盖率",
            "有正文唯一评论实体数 / 当前范围唯一评论实体数",
            "ratio",
            "review_entity",
        ),
        _metric_definition(
            "average_rating",
            "平均星级",
            "有效 1–5 星评分之和 / 有效评分唯一评论实体数",
            "stars",
            "review_entity",
        ),
        _metric_definition(
            "positive_rating_rate",
            "好评率",
            "4–5 星唯一评论实体数 / 有效评分唯一评论实体数",
            "ratio",
            "review_entity",
        ),
        _metric_definition(
            "negative_rating_rate",
            "差评率",
            "1–2 星唯一评论实体数 / 有效评分唯一评论实体数",
            "ratio",
            "review_entity",
        ),
        _metric_definition(
            "recommendation_rate",
            "推荐率",
            "推荐字段可解析为 true 的实体数 / 推荐字段可解析实体数",
            "ratio",
            "review_entity",
        ),
        _metric_definition(
            "factor_extraction_coverage",
            "原子因子覆盖率",
            "至少命中一个版本化因子的有正文实体数 / 有正文实体数",
            "ratio",
            "written_review_entity",
        ),
        _metric_definition(
            "atomic_v2_review_coverage",
            "新版 28 脚本联合覆盖率",
            "28 个版本化确定性脚本中至少一个命中的有正文实体数 / 有正文实体数",
            "ratio",
            "written_review_entity",
        ),
    ]

    metrics = [
        _metric("sample_size", len(entities), sample_size=len(entities)),
        _metric(
            "written_review_count",
            len(written),
            sample_size=len(entities),
            numerator=len(written),
            denominator=len(entities),
        ),
        _metric(
            "written_coverage",
            _ratio(len(written), len(entities)),
            sample_size=len(entities),
            numerator=len(written),
            denominator=len(entities),
            interval=_wilson(len(written), len(entities)),
        ),
        _metric(
            "average_rating",
            statistics.fmean(ratings) if ratings else None,
            sample_size=len(ratings),
            denominator=len(ratings),
            interval=_mean_ci([float(value) for value in ratings]),
        ),
        _metric(
            "positive_rating_rate",
            _ratio(positive, len(ratings)),
            sample_size=len(ratings),
            numerator=positive,
            denominator=len(ratings),
            interval=_wilson(positive, len(ratings)),
        ),
        _metric(
            "negative_rating_rate",
            _ratio(negative, len(ratings)),
            sample_size=len(ratings),
            numerator=negative,
            denominator=len(ratings),
            interval=_wilson(negative, len(ratings)),
        ),
        _metric(
            "recommendation_rate",
            _ratio(recommended_true, len(recommended_values)),
            sample_size=len(recommended_values),
            numerator=recommended_true,
            denominator=len(recommended_values),
            interval=_wilson(recommended_true, len(recommended_values)),
            quality_flags=(
                ["field_coverage_limited"]
                if len(recommended_values) < len(entities)
                else []
            ),
        ),
        _metric(
            "factor_extraction_coverage",
            extraction["coverage_rate"],
            sample_size=extraction["written_entities"],
            numerator=extraction["covered_entities"],
            denominator=extraction["written_entities"],
            interval=_wilson(
                extraction["covered_entities"], extraction["written_entities"]
            ),
        ),
        _metric(
            "atomic_v2_review_coverage",
            atomic_v2["coverage_rate"],
            sample_size=atomic_v2["written_entities"],
            numerator=atomic_v2["covered_entities"],
            denominator=atomic_v2["written_entities"],
            interval=_wilson(
                atomic_v2["covered_entities"], atomic_v2["written_entities"]
            ),
        ),
    ]

    comparison_config = plan.get("comparison") or {}
    comparison_minimum_n = int(
        comparison_config.get("minimum_n") or LOW_SAMPLE_N
    )
    comparison_alpha = float(comparison_config.get("alpha") or 0.05)
    star_rows = [
        {
            "rating": star,
            "count": rating_counts[star],
            "share": _round(_ratio(rating_counts[star], len(ratings))),
            "interval_95": _wilson(rating_counts[star], len(ratings)),
        }
        for star in (5, 4, 3, 2, 1)
    ]
    brand_rows = _group_summary(
        entities,
        lambda fact: fact["brand"],
        minimum_n=comparison_minimum_n,
        alpha=comparison_alpha,
    )
    channel_comparison_facts = [
        fact for fact in facts if fact["collection_method"] != "fallback"
    ]
    channel_rows = _group_summary(
        channel_comparison_facts,
        lambda fact: fact["channel"],
        minimum_n=comparison_minimum_n,
        alpha=comparison_alpha,
    )
    model_rows = _group_summary(
        entities,
        lambda fact: fact["model"],
        minimum_n=comparison_minimum_n,
        alpha=comparison_alpha,
    )
    time_rows = _time_rows(entities)
    overlap_rows = _channel_overlap(facts)
    attribute_rows = _atom_rows(
        atoms, "attribute", len(written), evidence_by_factor
    )
    pain_rows = _atom_rows(atoms, "pain", len(written), evidence_by_factor)
    use_case_rows = _atom_rows(
        atoms, "use_case", len(written), evidence_by_factor
    )
    user_trait_rows = _atom_rows(
        atoms, "user_trait", len(written), evidence_by_factor
    )
    need_rows = _atom_rows(atoms, "need", len(written), evidence_by_factor)
    smart_rows = _smart_rows_with_framework(
        _atom_rows(
            atoms, "smart_signal", len(written), evidence_by_factor
        ),
        dictionary,
    )

    age_counts: Counter[str] = Counter()
    age_source_counts: Counter[str] = Counter()
    for fact in entities:
        bucket, source = explicit_age_group(fact)
        if bucket:
            age_counts[bucket] += 1
            age_source_counts[source or "unknown"] += 1
    age_known = sum(age_counts.values())
    age_rows = [
        {
            "age_group": group,
            "count": count,
            "share_of_known": _round(_ratio(count, age_known)),
            "coverage_of_all": _round(_ratio(count, len(entities))),
        }
        for group, count in sorted(
            age_counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    keyword_rows = keyword_document_frequency(written)
    cooccurrence_rows = factor_cooccurrence(atoms)
    segment_rows = _rule_segment_rows(atoms, entity_lookup, len(written))
    kano_rows = _kano_candidates(attribute_rows)
    opportunity_rows = _opportunity_rows(
        pain_rows, attribute_rows, atoms, entity_lookup, written
    )

    base_columns = [
        {"field": "group", "label": "分组"},
        {"field": "sample_size", "label": "样本量"},
        {"field": "average_rating", "label": "平均星级"},
        {"field": "positive_rate", "label": "好评率"},
        {"field": "negative_rate", "label": "差评率"},
        {"field": "written_coverage", "label": "正文覆盖率"},
    ]
    comparison_columns = [
        *base_columns,
        {"field": "difference_vs_rest", "label": "与其余组星级差"},
        {"field": "effect_size", "label": "效应量 d"},
        {"field": "adjusted_p_value", "label": "Holm 校正 p"},
        {"field": "significant", "label": "显著"},
    ]
    factor_columns = [
        {"field": "label", "label": "因子"},
        {"field": "mention_count", "label": "提及实体"},
        {"field": "mention_rate", "label": "提及率"},
        {"field": "positive_share", "label": "好评占比"},
        {"field": "negative_share", "label": "差评占比"},
    ]
    market_region_tables = (
        [
            _table(
                "market_region_composition",
                "来源市场样本构成",
                [
                    {"field": "market_region", "label": "来源市场"},
                    {
                        "field": "eligible_valid_text_entities",
                        "label": "有效文字评论",
                    },
                    {"field": "share", "label": "占比"},
                ],
                market_region_rows,
                denominator_note=(
                    f"分母：{len(entities):,} 条最终快照中的 eligible && "
                    "valid_text 唯一评论实体；来源市场不等于评论者国籍。"
                    if market_region_rows
                    else "旧契约快照不提供来源市场构成，不推断地区。"
                ),
                default_sort={
                    "field": "eligible_valid_text_entities",
                    "direction": "desc",
                },
            )
        ]
        if market_contract_mode == "current"
        else []
    )
    tables = [
        *market_region_tables,
        _table(
            "rating_distribution",
            "星级分布",
            [
                {"field": "rating", "label": "星级"},
                {"field": "count", "label": "实体数"},
                {"field": "share", "label": "占比"},
            ],
            star_rows,
            denominator_note=f"分母：{len(ratings):,} 条有效评分唯一评论实体",
            default_sort={"field": "rating", "direction": "desc"},
        ),
        _table(
            "brand_comparison",
            "品牌对比",
            comparison_columns,
            brand_rows,
            denominator_note="分母：各品牌当前范围内唯一评论实体；低于 20 条仅展示不解释",
            default_sort={"field": "sample_size", "direction": "desc"},
        ),
        _table(
            "channel_comparison",
            "渠道页面出现记录对比",
            comparison_columns,
            channel_rows,
            denominator_note="分母：各渠道出现记录；排除 fallback source，保留跨页面重叠",
            default_sort={"field": "sample_size", "direction": "desc"},
        ),
        _table(
            "model_comparison",
            "型号对比",
            comparison_columns,
            model_rows,
            denominator_note="分母：各型号唯一评论实体；低样本型号不生成方向性结论",
            default_sort={"field": "sample_size", "direction": "desc"},
        ),
        _table(
            "time_trend",
            "月度评分趋势",
            base_columns,
            time_rows,
            denominator_note="分母：各月时间可解析且评分有效的唯一评论实体",
            default_sort={"field": "group", "direction": "asc"},
        ),
        _table(
            "atomic_script_execution",
            "新版 28 个原子脚本全样本执行",
            [
                {"field": "script_id", "label": "脚本"},
                {"field": "name", "label": "原子信息"},
                {"field": "eligible", "label": "应执行样本"},
                {"field": "matched", "label": "命中样本"},
                {"field": "no_match", "label": "未命中样本"},
                {"field": "failed", "label": "失败样本"},
                {"field": "atom_count", "label": "原子记录"},
                {"field": "coverage", "label": "样本覆盖率"},
            ],
            atomic_v2["execution"],
            denominator_note=(
                f"每个脚本均对全部 {len(written):,} 条有正文唯一评论实体执行；"
                "命中 + 未命中 + 失败 = 应执行样本。"
            ),
            default_sort={"field": "script_id", "direction": "asc"},
        ),
        _table(
            "atomic_label_summary",
            "新版脚本原子标签分布",
            [
                {"field": "script_id", "label": "脚本"},
                {"field": "script_name", "label": "原子信息"},
                {"field": "label", "label": "原子标签"},
                {"field": "review_entities", "label": "命中实体"},
                {"field": "coverage", "label": "实体覆盖率"},
            ],
            atomic_v2["labels"],
            denominator_note=(
                f"覆盖率分母：{len(written):,} 条有正文唯一评论实体；"
                "单条评论可产生多个标签和多个原子记录。"
            ),
            default_sort={"field": "review_entities", "direction": "desc"},
        ),
        _table(
            "attribute_performance",
            "产品属性表现",
            factor_columns,
            attribute_rows,
            denominator_note=f"提及率分母：{len(written):,} 条有正文唯一评论实体；正负占比使用属性提及且评分有效实体",
            default_sort={"field": "mention_count", "direction": "desc"},
        ),
        _table(
            "pain_points",
            "痛点与故障",
            factor_columns,
            pain_rows,
            denominator_note=f"提及率分母：{len(written):,} 条有正文唯一评论实体",
            default_sort={"field": "negative_count", "direction": "desc"},
        ),
        _table(
            "use_cases",
            "需求与使用场景",
            factor_columns,
            use_case_rows,
            denominator_note=f"分母：{len(written):,} 条有正文唯一评论实体；单条可命中多个场景",
            default_sort={"field": "mention_count", "direction": "desc"},
        ),
        _table(
            "explicit_needs",
            "明确需求表达",
            factor_columns,
            need_rows,
            denominator_note="仅统计需求提示词与属性词典在同一评论内的共现",
            default_sort={"field": "mention_count", "direction": "desc"},
        ),
        _table(
            "user_traits",
            "规则用户群组",
            factor_columns,
            user_trait_rows,
            denominator_note="群组非互斥，只描述评论中明确表达的特征，不推断总体人口画像",
            default_sort={"field": "mention_count", "direction": "desc"},
        ),
        _table(
            "rule_segments",
            "明确特征规则分群",
            [
                {"field": "segment", "label": "群组"},
                {"field": "review_entities", "label": "实体数"},
                {"field": "coverage_of_written", "label": "正文覆盖率"},
                {"field": "average_rating", "label": "平均星级"},
            ],
            segment_rows,
            denominator_note=(
                "按评论中明确用户特征标签的稳定组合分群；群组可重叠，"
                "不推断未表达的人口属性"
            ),
            default_sort={"field": "review_entities", "direction": "desc"},
        ),
        _table(
            "explicit_age",
            "明确年龄分布",
            [
                {"field": "age_group", "label": "年龄段"},
                {"field": "count", "label": "明确样本"},
                {"field": "share_of_known", "label": "已知年龄内占比"},
                {"field": "coverage_of_all", "label": "全部样本覆盖率"},
            ],
            age_rows,
            denominator_note=f"已知年龄 {age_known:,} / 全部实体 {len(entities):,}；缺失年龄不推断",
            default_sort={"field": "count", "direction": "desc"},
        ),
        _table(
            "kano_candidates",
            "网评 Kano 候选",
            [
                {"field": "label", "label": "属性"},
                {"field": "classification", "label": "候选类型"},
                {"field": "mention_count", "label": "提及实体"},
                {"field": "positive_share", "label": "好评占比"},
                {"field": "negative_share", "label": "差评占比"},
            ],
            kano_rows,
            denominator_note="规则候选，不等同于双向问卷 Kano 调研结论",
            default_sort={"field": "mention_count", "direction": "desc"},
        ),
        _table(
            "product_opportunities",
            "产品机会（网评证据）",
            [
                {"field": "title", "label": "机会"},
                {"field": "mention_count", "label": "提及实体"},
                {"field": "negative_count", "label": "差评实体"},
                {"field": "negative_share", "label": "差评占比"},
                {"field": "trend_delta", "label": "近 12 月趋势差"},
                {"field": "competitor_gap", "label": "EGO-竞品差评率差"},
                {"field": "cross_brand_count", "label": "覆盖品牌"},
                {"field": "cross_model_count", "label": "覆盖型号"},
                {"field": "evidence_confidence", "label": "证据强度"},
            ],
            opportunity_rows,
            denominator_note="按差评证据量、覆盖和跨产品广度分别展示；不合成为业务优先级",
            default_sort={"field": "negative_count", "direction": "desc"},
        ),
        _table(
            "smart_opportunities",
            "智能化机会信号",
            [
                {"field": "framework", "label": "A1–A8"},
                {"field": "label", "label": "信号"},
                {"field": "mention_count", "label": "提及实体"},
                {"field": "mention_rate", "label": "提及率"},
                {"field": "negative_share", "label": "差评占比"},
                {"field": "trigger", "label": "触发"},
                {"field": "candidate_action", "label": "候选动作"},
                {"field": "user_value", "label": "用户价值"},
                {"field": "risk", "label": "风险"},
            ],
            smart_rows,
            denominator_note="仅统计明确智能表达；与一般产品机会分开，不用 LLM 扩写",
            default_sort={"field": "mention_count", "direction": "desc"},
        ),
        _table(
            "keyword_document_frequency",
            "高频 n-gram 文档频次",
            [
                {"field": "term", "label": "词项"},
                {"field": "document_count", "label": "文档数"},
                {"field": "document_rate", "label": "文档覆盖率"},
                {"field": "ngram", "label": "类型"},
            ],
            keyword_rows,
            denominator_note=f"分母：{len(written):,} 条有正文唯一评论实体；统计文档频次而非词频总和",
            default_sort={"field": "document_count", "direction": "desc"},
        ),
        _table(
            "factor_cooccurrence",
            "原子因子文档共现",
            [
                {"field": "left_label", "label": "因子 A"},
                {"field": "right_label", "label": "因子 B"},
                {"field": "document_count", "label": "共现文档数"},
            ],
            cooccurrence_rows,
            denominator_note=(
                "同一唯一评论实体内至少各命中一次；不表示同句、因果或方向"
            ),
            default_sort={"field": "document_count", "direction": "desc"},
        ),
        _table(
            "channel_overlap",
            "跨渠道实体重叠",
            [
                {"field": "channel_pattern", "label": "渠道组合"},
                {"field": "shared_entities", "label": "共享实体"},
            ],
            overlap_rows,
            denominator_note="仅使用完整来源业务键识别共享实体",
            default_sort={"field": "shared_entities", "direction": "desc"},
        ),
    ]

    charts = [
        {
            "chart_id": "chart_rating_distribution",
            "title": "星级分布",
            "type": "bar",
            "table_id": "rating_distribution",
            "encodings": {
                "x": {"field": "rating", "type": "ordinal"},
                "y": {"field": "count", "type": "quantitative"},
            },
        },
        {
            "chart_id": "chart_brand_rating",
            "title": "品牌平均星级与样本量",
            "type": "bar",
            "table_id": "brand_comparison",
            "encodings": {
                "x": {"field": "group", "type": "nominal"},
                "y": {"field": "average_rating", "type": "quantitative"},
            },
        },
        {
            "chart_id": "chart_attribute_mentions",
            "title": "属性提及率与差评占比",
            "type": "grouped-bar",
            "table_id": "attribute_performance",
            "encodings": {
                "x": {"field": "label", "type": "nominal"},
                "y": {
                    "fields": ["mention_rate", "negative_share"],
                    "type": "quantitative",
                },
            },
        },
        {
            "chart_id": "chart_pain_mentions",
            "title": "痛点提及与差评实体",
            "type": "bar",
            "table_id": "pain_points",
            "encodings": {
                "x": {"field": "label", "type": "nominal"},
                "y": {"field": "negative_count", "type": "quantitative"},
            },
        },
        {
            "chart_id": "chart_time_trend",
            "title": "月度平均星级趋势",
            "type": "line",
            "table_id": "time_trend",
            "encodings": {
                "x": {"field": "group", "type": "temporal"},
                "y": {"field": "average_rating", "type": "quantitative"},
            },
        },
        {
            "chart_id": "chart_atomic_script_coverage",
            "title": "28 个原子脚本样本覆盖率",
            "type": "bar",
            "table_id": "atomic_script_execution",
            "encodings": {
                "x": {"field": "script_id", "type": "nominal"},
                "y": {"field": "coverage", "type": "quantitative"},
            },
        },
    ]

    findings: list[dict[str, Any]] = []
    findings.append(
        {
            "finding_id": "finding_scope",
            "title": "样本基线",
            "body": (
                f"本报告纳入 {len(entities):,} 条唯一评论实体，其中 "
                f"{len(written):,} 条有正文（{_ratio(len(written), len(entities)):.1%}）。"
            ),
            "metric_refs": ["sample_size", "written_coverage"],
            "evidence_refs": [],
            "confidence": "measured",
        }
    )
    findings.append(
        {
            "finding_id": "finding_atomic_v2_execution",
            "title": "28 脚本全样本执行完成",
            "body": (
                f"28 个确定性脚本均执行 {atomic_v2['written_entities']:,} 条有正文实体，"
                f"共留下 {atomic_v2['ledger']['record_count']:,} 条执行状态；"
                f"{atomic_v2['covered_entities']:,} 条至少命中一个脚本，"
                f"{atomic_v2['unmatched_entities']:,} 条未命中。"
            ),
            "metric_refs": ["atomic_v2_review_coverage"],
            "table_refs": ["atomic_script_execution"],
            "evidence_refs": [],
            "confidence": "measured",
        }
    )
    if pain_rows:
        top_pain = max(
            pain_rows,
            key=lambda item: (
                item["negative_count"],
                item["mention_count"],
                item["factor_id"],
            ),
        )
        findings.append(
            {
                "finding_id": "finding_top_pain",
                "title": "主要痛点证据",
                "body": (
                    f"“{top_pain['label']}”命中 {top_pain['mention_count']:,} 条有正文实体，"
                    f"其中 {top_pain['negative_count']:,} 条为 1–2 星。"
                ),
                "metric_refs": [],
                "table_refs": ["pain_points"],
                "evidence_refs": top_pain["evidence_ids"],
                "confidence": (
                    "measured"
                    if top_pain["mention_count"] >= LOW_SAMPLE_N
                    else "low-sample"
                ),
            }
        )
    if attribute_rows:
        strongest = max(
            attribute_rows,
            key=lambda item: (
                item["positive_count"],
                item["mention_count"],
                item["factor_id"],
            ),
        )
        findings.append(
            {
                "finding_id": "finding_attribute_strength",
                "title": "属性正向表现",
                "body": (
                    f"“{strongest['label']}”在命中且评分有效的实体中有 "
                    f"{strongest['positive_count']:,} 条 4–5 星；"
                    "这是评分关联，不等同于句级因果情感。"
                ),
                "metric_refs": [],
                "table_refs": ["attribute_performance"],
                "evidence_refs": strongest["evidence_ids"],
                "confidence": (
                    "measured"
                    if strongest["mention_count"] >= LOW_SAMPLE_N
                    else "low-sample"
                ),
            }
        )
    if opportunity_rows:
        top_opportunity = opportunity_rows[0]
        findings.append(
            {
                "finding_id": "finding_product_opportunity",
                "title": "首要网评证据机会",
                "body": (
                    f"{top_opportunity['title']}：覆盖 {top_opportunity['mention_count']:,} 条实体、"
                    f"{top_opportunity['cross_brand_count']} 个品牌和 "
                    f"{top_opportunity['cross_model_count']} 个型号。"
                    "该排序只反映网评负向证据量，不代表路线图优先级。"
                ),
                "metric_refs": [],
                "table_refs": ["product_opportunities"],
                "evidence_refs": top_opportunity["evidence_ids"],
                "confidence": top_opportunity["evidence_confidence"],
            }
        )

    average_rating = statistics.fmean(ratings) if ratings else None
    summary_bullets = [
        f"唯一评论实体 {len(entities):,} 条；正文覆盖 {_ratio(len(written), len(entities)):.1%}。",
        (
            f"平均星级 {average_rating:.2f}；好评率 {_ratio(positive, len(ratings)):.1%}，"
            f"差评率 {_ratio(negative, len(ratings)):.1%}。"
            if average_rating is not None
            else "当前范围没有有效评分。"
        ),
    ]
    if pain_rows:
        summary_bullets.append(
            f"负向证据量最高的规则痛点是“{pain_rows[0]['label']}”；可下钻到原文和口径。"
        )
    if opportunity_rows:
        summary_bullets.append(
            "产品机会按覆盖、负向程度和跨产品广度分别展示，不混入成本、工程或战略判断。"
        )
    summary_bullets.append(
        f"新版 {atomic_v2['script_count']} 个脚本已全量执行，"
        f"执行账本 {atomic_v2['ledger']['record_count']:,} 条；"
        "未命中样本同样留痕。"
    )
    historical_sources = list(
        snapshot_manifest["quality_summary"].get("historical_sources") or []
    )
    source_completeness = list(
        snapshot_manifest["quality_summary"].get("source_completeness") or []
    )
    non_reportable_exclusions = list(
        snapshot_manifest["quality_summary"].get(
            "non_reportable_exclusions"
        ) or []
    )
    non_reportable_excluded_records = sum(
        int(item.get("excluded_records") or 0)
        for item in non_reportable_exclusions
    )
    source_limit_message = limited_source_message(source_completeness)
    if source_limit_message:
        summary_bullets.append(source_limit_message)
    if non_reportable_excluded_records:
        summary_bullets.append(
            "旧部分采集契约不受支持，"
            f"{non_reportable_excluded_records:,}条未进入当前快照/报告；"
            "这些记录不计入任何指标分母。"
        )
    if historical_sources:
        archive_dates = sorted(
            {
                str(item.get("archive_through") or "")
                for item in historical_sources
                if item.get("archive_through")
            }
        )
        summary_bullets.append(
            "Amazon Reviews 2023 为 North America 静态历史来源，"
            f"归档截至 {'、'.join(archive_dates) or '未知日期'}；"
            "不代表当前市场表现。"
        )

    limitations = [
        {
            "code": "text_coverage",
            "severity": "material",
            "message": (
                f"语义分析只覆盖 {len(written):,} 条有正文实体，"
                f"占全部实体 {_ratio(len(written), len(entities)):.1%}。"
            ),
        },
        {
            "code": "unknown_business_keys",
            "severity": "material",
            "message": (
                f"{snapshot_manifest['quality_summary']['unknown_identity_occurrences']:,} 条出现记录"
                "缺少完整来源业务键，无法跨页面确认唯一实体。"
            ),
        },
        {
            "code": "syndication_signal",
            "severity": "material",
            "message": "syndicated 来源标志不完整；总体指标始终使用实体业务键去重。",
        },
        {
            "code": "verified_purchaser_disabled",
            "severity": "blocking-for-metric",
            "message": "verified purchaser 的 false 与 missing 无法区分，未生成筛选或指标。",
        },
        {
            "code": "rating_linked_sentiment",
            "severity": "method",
            "message": "属性和痛点正负占比使用评论级星级关联，不声称为句级语义情感。",
        },
        {
            "code": "rule_recall",
            "severity": "method",
            "message": (
                f"原子提取使用版本化精确短语词典 {DICTIONARY_VERSION}；"
                "优先可解释精度，但会漏掉未收录同义表达。"
            ),
        },
        {
            "code": "atomic_v2_rule_boundary",
            "severity": "method",
            "message": (
                f"新版原子层使用 {ATOMIC_SCRIPT_REGISTRY_VERSION} 的确定性正则规则，"
                "全部样本均执行且不使用 LLM；规则命中不自动构成因果、用户群或产品结论，"
                "未收录的同义表达仍可能漏检。"
            ),
        },
        {
            "code": "kano_proxy",
            "severity": "method",
            "message": "Kano 为网评规则候选，不等同于正式双向问卷 Kano 调研。",
        },
        {
            "code": "comparison_inference",
            "severity": "method",
            "message": (
                "品牌、渠道和型号比较使用组内与其余组的均值差、正态近似检验、"
                "Cohen's d 与 Holm 多重比较校正；显著不等同于业务重要。"
            ),
        },
        {
            "code": "comparison_observational",
            "severity": "material",
            "message": (
                "品牌和渠道差异同时受型号、时间、正文覆盖与来源人群构成影响；"
                "当前比较是观察性描述，不解释为品牌或渠道造成的因果效果。"
            ),
        },
        {
            "code": "preview_fact_storage",
            "severity": "implementation",
            "message": "本地纵向样例使用内容寻址 JSONL-gzip Fact；正式 V1 仍按 ADR-0003 落 Parquet。",
        },
    ]
    if source_limit_message:
        limitations.insert(
            0,
            {
                "code": "limited_source_sample",
                "severity": "material",
                "message": source_limit_message,
            },
        )
    if non_reportable_excluded_records:
        limitations.insert(
            0,
            {
                "code": "unsupported_legacy_partial_excluded",
                "severity": "material",
                "message": (
                    "旧部分采集契约不受支持，"
                    f"{non_reportable_excluded_records:,}条未进入当前快照/报告；"
                    "这些记录未生成 Review Fact，也不计入任何指标分母。"
                ),
            },
        )
    if historical_sources:
        archive_dates = sorted(
            {
                str(item.get("archive_through") or "")
                for item in historical_sources
                if item.get("archive_through")
            }
        )
        limitations.append(
            {
                "code": "amazon_static_history",
                "severity": "material",
                "message": (
                    "Amazon Reviews 2023 仅作为 North America 静态历史来源；"
                    f"归档截至 {'、'.join(archive_dates) or '未知日期'}，"
                    "不代表当前市场表现。"
                ),
            }
        )
    if market_contract_mode == "current" and len(market_region_counts) == 1:
        limitations.append(
            {
                "code": "single_market_region",
                "severity": "method",
                "message": (
                    "本报告只覆盖一个来源市场，不具备跨地区比较条件。"
                ),
            }
        )
    elif market_contract_mode == "current" and len(market_region_counts) > 1:
        low_regions = sorted(
            region
            for region, count in market_region_counts.items()
            if count < LOW_SAMPLE_N
        )
        if low_regions:
            limitations.append(
                {
                    "code": "market_region_low_sample",
                    "severity": "material",
                    "message": (
                        "以下来源市场有效文字评论少于 "
                        f"{LOW_SAMPLE_N} 条，仅展示构成、不解释差异："
                        + "、".join(low_regions)
                    ),
                }
            )
        if len(set(market_time_ranges.values())) > 1:
            limitations.append(
                {
                    "code": "market_region_time_coverage",
                    "severity": "material",
                    "message": (
                        "各来源市场的评论时间覆盖不一致；当前只展示样本构成，"
                        "不把观察差异写成欧美或区域表现结论。"
                    ),
                }
            )

    all_sections = [
        {
            "section_id": "overview",
            "scene_id": "satisfaction-overview",
            "title": "满意度总览",
            "metric_refs": [
                "sample_size",
                "written_coverage",
                "average_rating",
                "positive_rating_rate",
                "negative_rating_rate",
            ],
            "chart_refs": ["chart_rating_distribution", "chart_time_trend"],
            "table_refs": [
                *(
                    ["market_region_composition"]
                    if market_contract_mode == "current"
                    else []
                ),
                "rating_distribution",
                "time_trend",
            ],
        },
        {
            "section_id": "pain",
            "scene_id": "pain-and-failure",
            "title": "痛点与故障",
            "chart_refs": ["chart_pain_mentions"],
            "table_refs": ["pain_points"],
        },
        {
            "section_id": "atomic_v2",
            "scene_id": None,
            "title": "28 个原子脚本执行与结果",
            "metric_refs": ["atomic_v2_review_coverage"],
            "chart_refs": ["chart_atomic_script_coverage"],
            "table_refs": [
                "atomic_script_execution",
                "atomic_label_summary",
            ],
        },
        {
            "section_id": "attributes",
            "scene_id": "product-attribute-performance",
            "title": "属性表现与 Pros / Cons",
            "chart_refs": ["chart_attribute_mentions"],
            "table_refs": ["attribute_performance", "kano_candidates"],
        },
        {
            "section_id": "needs",
            "scene_id": "needs-and-use-cases",
            "title": "需求与使用场景",
            "table_refs": [
                "use_cases",
                "explicit_needs",
            ],
        },
        {
            "section_id": "product_opportunity",
            "scene_id": "product-opportunity",
            "title": "产品机会",
            "table_refs": ["product_opportunities"],
        },
        {
            "section_id": "brand_comparison",
            "scene_id": "brand-product-comparison",
            "title": "品牌 / 产品竞品对比",
            "chart_refs": ["chart_brand_rating"],
            "table_refs": ["brand_comparison"],
        },
        {
            "section_id": "channel_comparison",
            "scene_id": "channel-comparison",
            "title": "渠道差异",
            "table_refs": ["channel_comparison", "channel_overlap"],
        },
        {
            "section_id": "model_time_comparison",
            "scene_id": "model-time-comparison",
            "title": "型号与时间段对比",
            "chart_refs": ["chart_time_trend"],
            "table_refs": ["model_comparison", "time_trend"],
        },
        {
            "section_id": "user_profile",
            "scene_id": "user-profile",
            "title": "用户特征与规则分群",
            "table_refs": ["user_traits", "rule_segments", "explicit_age"],
        },
        {
            "section_id": "smart_opportunity",
            "scene_id": "smart-opportunity",
            "title": "智能化机会",
            "table_refs": ["smart_opportunities"],
        },
        {
            "section_id": "language",
            "scene_id": None,
            "title": "高频表达",
            "table_refs": [
                "keyword_document_frequency",
                "factor_cooccurrence",
            ],
        },
    ]

    active_scenes = [
        plan.get("primary_direction") or "product-opportunity",
        *(plan.get("analysis_modules") or []),
    ]
    active_scenes = list(dict.fromkeys(active_scenes))
    selected_factors = set(plan.get("factors") or [])
    table_factor = {
        "rating_distribution": "ratings",
        "time_trend": "ratings",
        "brand_comparison": "ratings",
        "channel_comparison": "ratings",
        "model_comparison": "ratings",
        "attribute_performance": "attributes",
        "pain_points": "pain-points",
        "use_cases": "use-cases",
        "explicit_needs": "use-cases",
        "user_traits": "user-traits",
        "rule_segments": "user-traits",
        "explicit_age": "explicit-age",
        "kano_candidates": "kano-candidates",
        "product_opportunities": "product-opportunities",
        "smart_opportunities": "smart-signals",
        "keyword_document_frequency": "keyword-document-frequency",
        "factor_cooccurrence": "factor-cooccurrence",
    }
    sections = [
        section
        for scene_id in active_scenes
        for section in all_sections
        if section.get("scene_id") == scene_id
    ]
    if selected_factors & {
        "keyword-document-frequency",
        "factor-cooccurrence",
    }:
        sections.append(
            next(
                section
                for section in all_sections
                if section["section_id"] == "language"
            )
        )
    sections.insert(
        0,
        next(
            section
            for section in all_sections
            if section["section_id"] == "atomic_v2"
        ),
    )
    for section in sections:
        section["table_refs"] = [
            table_id
            for table_id in section.get("table_refs", [])
            if table_factor.get(table_id) is None
            or table_factor[table_id] in selected_factors
        ]
        section["chart_refs"] = [
            chart_id
            for chart_id in section.get("chart_refs", [])
            if any(
                chart["chart_id"] == chart_id
                and (
                    table_factor.get(chart["table_id"]) is None
                    or table_factor[chart["table_id"]] in selected_factors
                )
                for chart in charts
            )
        ]
    section_table_ids = {
        table_id
        for section in sections
        for table_id in section.get("table_refs", [])
    }
    section_chart_ids = {
        chart_id
        for section in sections
        for chart_id in section.get("chart_refs", [])
    }
    tables = [
        table for table in tables if table["table_id"] in section_table_ids
    ]
    charts = [
        chart for chart in charts if chart["chart_id"] in section_chart_ids
    ]
    allowed_metric_ids = {
        "sample_size",
        "written_review_count",
        "atomic_v2_review_coverage",
    }
    if "written-coverage" in selected_factors:
        allowed_metric_ids.add("written_coverage")
    if "ratings" in selected_factors:
        allowed_metric_ids.update(
            {"average_rating", "positive_rating_rate", "negative_rating_rate"}
        )
    if "recommendation" in selected_factors:
        allowed_metric_ids.add("recommendation_rate")
    if selected_factors & {
        "attributes",
        "pain-points",
        "use-cases",
        "user-traits",
        "smart-signals",
        "product-opportunities",
        "factor-cooccurrence",
    }:
        allowed_metric_ids.add("factor_extraction_coverage")
    metrics = [
        metric
        for metric in metrics
        if metric["metric_id"] in allowed_metric_ids
    ]
    metric_definitions = [
        definition
        for definition in metric_definitions
        if definition["metric_id"] in allowed_metric_ids
    ]
    available_table_ids = {table["table_id"] for table in tables}
    findings = [
        finding
        for finding in findings
        if not finding.get("table_refs")
        or any(
            table_id in available_table_ids
            for table_id in finding.get("table_refs", [])
        )
    ]
    for finding in findings:
        finding["metric_refs"] = [
            metric_id
            for metric_id in finding.get("metric_refs", [])
            if metric_id in allowed_metric_ids
        ]
        finding["table_refs"] = [
            table_id
            for table_id in finding.get("table_refs", [])
            if table_id in available_table_ids
        ]
    used_evidence_ids = {
        evidence_id
        for table in tables
        for row in table.get("rows", [])
        for evidence_id in row.get("evidence_ids", [])
    }
    used_evidence_ids.update(
        evidence_id
        for finding in findings
        for evidence_id in finding.get("evidence_refs", [])
    )
    evidence = [
        item for item in evidence if item["evidence_id"] in used_evidence_ids
    ]

    config_hash = sha256_text(canonical_json(plan))
    data_hash = snapshot_manifest["hashes"]["logical_content"].split(":", 1)[-1]
    core = {
        "schema_version": SCHEMA_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "research_plan": {
            **plan,
            "resolved_version": RESEARCH_PLAN_VERSION,
            "dictionary_version": DICTIONARY_VERSION,
            "config_sha256": f"sha256:{config_hash}",
        },
        "data_snapshot_id": snapshot_manifest["snapshot_id"],
        "scope": snapshot_manifest["scope"],
        "quality": {
            "schema_version": "report-data-quality/v1",
            "source_completeness": source_completeness,
            "limited_source_jobs": sum(
                bool(item.get("is_limited_source"))
                for item in source_completeness
            ),
            "non_reportable_exclusions": non_reportable_exclusions,
            "non_reportable_excluded_records": (
                non_reportable_excluded_records
            ),
        },
        "samples": {
            "occurrences": len(facts),
            "review_entities": len(entities),
            "written_review_entities": len(written),
            "valid_rating_entities": len(ratings),
            "unknown_identity_occurrences": snapshot_manifest[
                "quality_summary"
            ]["unknown_identity_occurrences"],
            "duplicate_occurrences": snapshot_manifest["quality_summary"][
                "duplicate_occurrences"
            ],
            "fallback_occurrences": snapshot_manifest["quality_summary"][
                "fallback_occurrences"
            ],
            **(
                {
                    "market_contract_mode": market_contract_mode,
                    "market_region_summary": market_region_rows,
                    "raw_source_records": snapshot_manifest[
                        "quality_summary"
                    ].get("raw_record_count", len(facts)),
                    "excluded_ineligible_records": snapshot_manifest[
                        "quality_summary"
                    ].get("excluded_ineligible_records", 0),
                    "excluded_invalid_text_records": snapshot_manifest[
                        "quality_summary"
                    ].get("excluded_invalid_text_records", 0),
                    "non_text_records": snapshot_manifest[
                        "quality_summary"
                    ].get("non_text_records", 0),
                    "historical_sources": historical_sources,
                }
                if market_contract_mode == "current"
                else {}
            ),
            "age_known_entities": age_known,
            "age_source_counts": dict(sorted(age_source_counts.items())),
            "atomic_v2_covered_entities": atomic_v2["covered_entities"],
            "atomic_v2_unmatched_entities": atomic_v2["unmatched_entities"],
        },
        "metric_definitions": metric_definitions,
        "metrics": metrics,
        "tables": tables,
        "charts": charts,
        "findings": findings,
        "evidence": evidence,
        "limitations": limitations,
        "sections": sections,
        "method": {
            "llm_used": False,
            "core_numbers_source": "deterministic Python aggregation and versioned rules",
            "factor_extraction": extraction,
            "atomic_script_registry": {
                "version": atomic_v2["registry_version"],
                "script_count": atomic_v2["script_count"],
                "scripts": atomic_v2["scripts"],
            },
            "atomic_execution_ledger": atomic_v2["ledger"],
            "low_sample_threshold": LOW_SAMPLE_N,
            **(
                {
                    "market_contract": {
                        "mode": market_contract_mode,
                        "version": snapshot_manifest[
                            "quality_summary"
                        ].get("market_contract_version"),
                        "analysis_population": (
                            "all is_eligible=true records for ratings and "
                            "all nonempty review_text records for text analysis"
                        ),
                        "market_region_semantics": (
                            "source market; not reviewer nationality"
                        ),
                    }
                }
                if market_contract_mode == "current"
                else {}
            ),
        },
    }
    core_result_hash = sha256_text(canonical_json(core))
    report_id = f"rpt_{core_result_hash[:24]}"
    headline_by_scene = {
        "satisfaction-overview": "满意度、评分与覆盖基线",
        "pain-and-failure": "痛点与故障量化分析",
        "product-attribute-performance": "产品属性 Pros / Cons 量化分析",
        "needs-and-use-cases": "用户需求与使用场景量化分析",
        "product-opportunity": "网评证据产品机会分析",
        "brand-product-comparison": "品牌与产品竞品量化对比",
        "channel-comparison": "渠道样本与表现差异分析",
        "model-time-comparison": "型号与时间段表现对比",
        "user-profile": "明确用户特征与规则分群",
        "smart-opportunity": "A1–A8 智能化机会信号",
    }
    return {
        "report_id": report_id,
        "title": report_name or "Mower 全场景量化洞察",
        "schema_version": SCHEMA_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "status": "completed",
        "summary_text": summary_bullets[0],
        "executive_summary": {
            "headline": headline_by_scene.get(
                plan.get("primary_direction"), "网评量化洞察"
            ),
            "bullets": summary_bullets,
        },
        **{key: value for key, value in core.items() if key not in {"schema_version", "analysis_version"}},
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "hashes": {
            "data": f"sha256:{data_hash}",
            "config": f"sha256:{config_hash}",
            "core_result": f"sha256:{core_result_hash}",
        },
    }
