from __future__ import annotations

import hashlib
import statistics
from collections import Counter, defaultdict
from typing import Any, Iterable

from reporting.config import LOW_SAMPLE_N
from reporting.source import canonical_json


LABEL_ZH = {
    "assembly_storage": "装配与收纳",
    "accessory_compatibility": "配件兼容",
    "automation": "自动化",
    "bag": "集草袋",
    "bag_collect": "集草",
    "bagging_mulching": "集草与碎草",
    "battery": "电池",
    "battery_compatibility": "电池兼容",
    "battery_failure": "电池失效",
    "better": "优于比较对象",
    "brand_driver": "品牌驱动",
    "brand_switch": "品牌或产品切换",
    "breakage": "破损",
    "causal_marker": "明确原因表达",
    "charge_manage": "充电与换电",
    "charger": "充电器",
    "charging": "充电",
    "clean_clear": "清洁或疏通",
    "clogging": "堵塞",
    "comparison": "比较",
    "condition_trigger": "工况触发",
    "connectivity": "连接能力",
    "control": "控制件",
    "cost_impact": "额外成本",
    "customer_service": "客户服务",
    "cut_quality": "切割质量",
    "deck": "割草机底盘",
    "delight": "惊喜体验",
    "diagnostics": "诊断",
    "did_not_meet": "未达到预期",
    "drive": "驱动系统",
    "duration": "作业时长",
    "early_failure": "早期失效",
    "ecosystem_driver": "生态购买驱动",
    "electric": "电压或电量规格",
    "exceeded_expectations": "超出预期",
    "expected_but": "预期与实际转折",
    "explicit_age": "明确年龄",
    "extra_work": "额外作业",
    "family_role": "家庭成员使用",
    "favorite": "偏爱或最佳评价",
    "feature_driver": "功能购买驱动",
    "fire_electric": "起火、冒烟或过热",
    "first_time_user": "首次用户",
    "frequency": "使用频率",
    "gas_to_battery": "燃油转电动",
    "generic_comparison": "一般比较",
    "handle": "手柄",
    "handling": "重量与操控",
    "health_condition": "健康限制",
    "hedge": "不确定表达",
    "homeowner": "家庭草坪用户",
    "impressed": "印象超预期",
    "incompatibility": "不兼容",
    "intensifier": "强度表达",
    "length": "长度规格",
    "longevity_positive": "长期可用",
    "money": "价格金额",
    "motor": "电机",
    "mow_cut": "割草",
    "mobility": "行动限制",
    "mulch": "碎草覆盖",
    "multiple_properties": "多处作业",
    "need": "明确需要",
    "negative": "明确负向评价",
    "negation": "否定表达",
    "noise": "噪音与振动",
    "not_start": "无法启动",
    "notification": "通知与提醒",
    "ownership_duration": "持有时长",
    "platform": "工具平台",
    "positive": "明确正向评价",
    "power": "动力与负载能力",
    "previous_owner": "旧机持有经验",
    "price_driver": "价格购买驱动",
    "privacy": "隐私",
    "professional": "专业或商用用户",
    "purchase_driver": "购买驱动",
    "recommend": "明确推荐",
    "recurrence": "问题复发",
    "refund": "退款",
    "repair_fix": "维修或修复",
    "replace_exchange": "更换或换货",
    "replacement": "替换机",
    "repurchase": "复购意向",
    "request": "改进请求",
    "restart_reset": "重启或复位",
    "return": "退货",
    "return_refund": "退货或退款",
    "rough_terrain": "崎岖或不平地面",
    "runtime": "续航",
    "safety_impact": "安全后果",
    "self_propel": "自走与速度控制",
    "senior": "年长用户",
    "sensor": "传感能力",
    "should": "应该改进",
    "should_have": "本应具备",
    "similar": "相似比较",
    "slope_hill": "坡地",
    "smart_control": "智能控制",
    "stalling": "停转或中断",
    "stopped_working": "停止工作",
    "store_transport": "收纳与运输",
    "strength": "力量限制",
    "switch_away": "转离意向",
    "task_blocked": "任务无法完成",
    "temporal_trigger": "时间触发",
    "thick_tall_grass": "高草或厚草",
    "time": "时间",
    "trim_edge": "修边",
    "trust": "可靠与信任",
    "uneven_result": "切割不均",
    "unsafe": "不安全",
    "value": "价格与价值",
    "warranty": "保修",
    "weather": "天气与温度",
    "wet_condition": "湿草或雨后",
    "wheel": "轮子",
    "wish": "愿望表达",
    "worse": "劣于比较对象",
    "bought_because": "明确购买原因",
    "keep": "保留意向",
    "less_than_claimed": "低于宣称表现",
    "yard_size": "明确草坪规模",
    "years_experience": "长期使用经验",
}


def _display(label: str) -> str:
    return LABEL_ZH.get(label, label.replace("_", " "))


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _entity_ref(fact: dict[str, Any]) -> str:
    return str(fact.get("review_entity_id") or fact["review_occurrence_id"])


class SceneIndex:
    def __init__(
        self,
        facts: list[dict[str, Any]],
        ledger_rows: Iterable[dict[str, Any]],
    ) -> None:
        self.facts = {
            _entity_ref(fact): fact for fact in facts if fact.get("has_text")
        }
        self.labels: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.matches: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in ledger_rows:
            if row["status"] != "matched":
                continue
            entity_ref = row["review_entity_ref"]
            script_id = row["script_id"]
            for match in row["matches"]:
                self.labels[entity_ref][script_id].add(match["label"])
                self.matches[entity_ref][script_id].append(match)
        self.all_entities = set(self.facts)
        self.evidence: dict[str, dict[str, Any]] = {}

    def entities(
        self,
        script_id: str,
        labels: set[str] | None = None,
    ) -> set[str]:
        return {
            entity_ref
            for entity_ref in self.all_entities
            if script_id in self.labels[entity_ref]
            and (
                labels is None
                or bool(self.labels[entity_ref][script_id] & labels)
            )
        }

    def union(self, specs: list[tuple[str, set[str] | None]]) -> set[str]:
        result: set[str] = set()
        for script_id, labels in specs:
            result |= self.entities(script_id, labels)
        return result

    def intersection(
        self, specs: list[tuple[str, set[str] | None]]
    ) -> set[str]:
        if not specs:
            return set()
        result = self.entities(*specs[0])
        for spec in specs[1:]:
            result &= self.entities(*spec)
        return result

    def stats(self, entity_refs: set[str]) -> dict[str, Any]:
        ratings = [
            self.facts[entity]["rating"]
            for entity in entity_refs
            if self.facts[entity].get("rating") is not None
        ]
        low_star = sum(rating <= 2 for rating in ratings)
        high_star = sum(rating >= 4 for rating in ratings)
        negative_phrase = len(entity_refs & self.entities("E03", {"negative"}))
        positive_phrase = len(entity_refs & self.entities("E03", {"positive"}))
        return {
            "review_entities": len(entity_refs),
            "coverage": _ratio(len(entity_refs), len(self.all_entities)),
            "rating_n": len(ratings),
            "average_rating": (
                round(statistics.fmean(ratings), 4) if ratings else None
            ),
            "high_star_entities": high_star,
            "high_star_rate": _ratio(high_star, len(ratings)),
            "low_star_entities": low_star,
            "low_star_rate": _ratio(low_star, len(ratings)),
            "positive_phrase_entities": positive_phrase,
            "positive_phrase_rate": _ratio(
                positive_phrase, len(entity_refs)
            ),
            "negative_phrase_entities": negative_phrase,
            "negative_phrase_rate": _ratio(
                negative_phrase, len(entity_refs)
            ),
            "quality_flag": (
                "low_sample" if len(entity_refs) < LOW_SAMPLE_N else None
            ),
        }

    def top_labels(
        self,
        entity_refs: set[str],
        script_id: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        counts: Counter[str] = Counter()
        for entity_ref in entity_refs:
            counts.update(self.labels[entity_ref].get(script_id, set()))
        return [
            {
                "label": label,
                "label_display": _display(label),
                "review_entities": count,
                "coverage_of_group": _ratio(count, len(entity_refs)),
            }
            for label, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            )[:limit]
        ]

    def evidence_for(
        self,
        entity_refs: set[str],
        *,
        focus_scripts: tuple[str, ...],
        limit: int = 3,
        prefer_low_rating: bool = False,
    ) -> list[str]:
        values = sorted(
            entity_refs,
            key=lambda entity: (
                (
                    self.facts[entity].get("rating")
                    if self.facts[entity].get("rating") is not None
                    else 9
                )
                if prefer_low_rating
                else -(
                    self.facts[entity].get("rating")
                    if self.facts[entity].get("rating") is not None
                    else 0
                ),
                self.facts[entity].get("submission_date") or "",
                entity,
            ),
        )
        selected: list[str] = []
        seen_snippets: set[str] = set()
        for entity_ref in values:
            snippets = [
                match["sentence"]
                for script_id in focus_scripts
                for match in self.matches[entity_ref].get(script_id, [])
                if match.get("sentence")
            ]
            snippet = (
                snippets[0]
                if snippets
                else str(self.facts[entity_ref].get("review_text") or "")[:500]
            ).strip()
            normalized = " ".join(snippet.casefold().split())
            if not normalized or normalized in seen_snippets:
                continue
            seen_snippets.add(normalized)
            material = {
                "entity_ref": entity_ref,
                "focus_scripts": focus_scripts,
                "snippet": snippet,
            }
            evidence_id = (
                "ev_scene_"
                + hashlib.sha256(
                    canonical_json(material).encode("utf-8")
                ).hexdigest()[:24]
            )
            fact = self.facts[entity_ref]
            self.evidence[evidence_id] = {
                "evidence_id": evidence_id,
                "review_entity_id": fact.get("review_entity_id"),
                "channel": fact.get("channel"),
                "brand": fact.get("brand"),
                "model": fact.get("model"),
                "rating": fact.get("rating"),
                "submission_date": fact.get("submission_date"),
                "snippet": snippet,
                "focus_scripts": list(focus_scripts),
                "matched_labels": {
                    script_id: [
                        _display(label)
                        for label in sorted(
                            self.labels[entity_ref].get(script_id, set())
                        )
                    ]
                    for script_id in focus_scripts
                    if self.labels[entity_ref].get(script_id)
                },
                "method": "deterministic entity-level atomic evidence selection",
            }
            selected.append(evidence_id)
            if len(selected) >= limit:
                break
        return selected


def _cohort_row(
    index: SceneIndex,
    cohort_id: str,
    label: str,
    definition: str,
    entity_refs: set[str],
    *,
    focus_scripts: tuple[str, ...],
    prefer_low_rating: bool = False,
) -> dict[str, Any]:
    return {
        "cohort_id": cohort_id,
        "label": label,
        "definition": definition,
        **index.stats(entity_refs),
        "top_tasks": index.top_labels(entity_refs, "E13"),
        "top_attributes": index.top_labels(entity_refs, "E01"),
        "top_failures": index.top_labels(entity_refs, "E06"),
        "evidence_ids": index.evidence_for(
            entity_refs,
            focus_scripts=focus_scripts,
            prefer_low_rating=prefer_low_rating,
        ),
        "boundary": "规则群组可重叠，只描述评论中明确表达的特征",
    }


def build_scene_analysis(
    facts: list[dict[str, Any]],
    ledger_rows: Iterable[dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Any]:
    index = SceneIndex(facts, ledger_rows)
    written_n = len(index.all_entities)

    profiles = [
        _cohort_row(
            index,
            "homeowner",
            "家庭草坪用户",
            "E16 明确家庭草坪或 homeowner 表达",
            index.entities("E16", {"homeowner"}),
            focus_scripts=("E16", "E13", "E01"),
        ),
        _cohort_row(
            index,
            "large_complex_yard",
            "大面积或复杂草坪用户",
            "E14 明确草坪规模、坡地、高草、湿草或崎岖地面，或 E15 明确面积",
            index.union(
                [
                    (
                        "E14",
                        {
                            "yard_size",
                            "slope_hill",
                            "thick_tall_grass",
                            "wet_condition",
                            "rough_terrain",
                        },
                    ),
                    ("E15", {"area_scale"}),
                ]
            ),
            focus_scripts=("E14", "E15", "E13"),
        ),
        _cohort_row(
            index,
            "gas_switcher",
            "燃油转电动或换机用户",
            "E19 明确燃油转电动、旧机替换或品牌/产品切换",
            index.entities(
                "E19",
                {"gas_to_battery", "brand_switch", "previous_owner"},
            ),
            focus_scripts=("E19", "E23", "E01"),
        ),
        _cohort_row(
            index,
            "ecosystem_owner",
            "电池生态持有用户",
            "E23 生态购买驱动或 E25 电池/平台兼容表达",
            index.union(
                [
                    ("E23", {"ecosystem_driver"}),
                    (
                        "E25",
                        {
                            "battery_compatibility",
                            "platform",
                            "accessory_compatibility",
                        },
                    ),
                ]
            ),
            focus_scripts=("E23", "E25", "E02"),
        ),
        _cohort_row(
            index,
            "accessibility",
            "年长或能力受限用户",
            "E17 明确年龄或 E18 明确年长、行动、力量、健康限制",
            index.union([("E17", None), ("E18", None)]),
            focus_scripts=("E17", "E18", "E01"),
        ),
        _cohort_row(
            index,
            "professional",
            "专业或多场地用户",
            "E16 专业/商用表达或 E15 多处作业",
            index.union(
                [
                    ("E16", {"professional"}),
                    ("E15", {"multiple_properties"}),
                ]
            ),
            focus_scripts=("E16", "E15", "E13"),
        ),
    ]
    profiles.sort(
        key=lambda row: (-row["review_entities"], row["cohort_id"])
    )

    scenario_specs = [
        (
            "mowing",
            "日常割草",
            index.entities("E13", {"mow_cut"}),
            ("E13", "E14", "E01"),
        ),
        (
            "charging",
            "充电与换电管理",
            index.entities("E13", {"charge_manage"}),
            ("E13", "E02", "E11"),
        ),
        (
            "large_scale",
            "大面积或长时间作业",
            index.union(
                [
                    ("E15", {"area_scale", "duration"}),
                    ("E14", {"yard_size"}),
                ]
            ),
            ("E15", "E14", "E01"),
        ),
        (
            "bag_mulch",
            "集草与碎草覆盖",
            index.entities("E13", {"bag_collect", "mulch"}),
            ("E13", "E01", "E06"),
        ),
        (
            "demanding_grass",
            "高草、厚草或湿草",
            index.entities(
                "E14", {"thick_tall_grass", "wet_condition"}
            ),
            ("E14", "E06", "E07"),
        ),
        (
            "slope_rough",
            "坡地或崎岖地形",
            index.entities("E14", {"slope_hill", "rough_terrain"}),
            ("E14", "E01", "E06"),
        ),
        (
            "storage_transport",
            "收纳与运输",
            index.entities("E13", {"store_transport"}),
            ("E13", "E01", "E02"),
        ),
    ]
    scenarios = []
    failure_entities = index.entities("E06")
    need_entities = index.entities("E20")
    for scenario_id, label, entities, focus in scenario_specs:
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "label": label,
                **index.stats(entities),
                "failure_entities": len(entities & failure_entities),
                "failure_rate": _ratio(
                    len(entities & failure_entities), len(entities)
                ),
                "explicit_need_entities": len(entities & need_entities),
                "explicit_need_rate": _ratio(
                    len(entities & need_entities), len(entities)
                ),
                "top_attributes": index.top_labels(entities, "E01"),
                "top_failures": index.top_labels(entities, "E06"),
                "evidence_ids": index.evidence_for(
                    entities,
                    focus_scripts=focus,
                    prefer_low_rating=True,
                ),
            }
        )
    scenarios.sort(
        key=lambda row: (-row["review_entities"], row["scenario_id"])
    )

    attributes = []
    for attribute in sorted(
        {
            label
            for entity_ref in index.all_entities
            for label in index.labels[entity_ref].get("E01", set())
        }
    ):
        entities = index.entities("E01", {attribute})
        row = {
            "attribute_id": attribute,
            "label": _display(attribute),
            **index.stats(entities),
            "failure_entities": len(entities & failure_entities),
            "failure_rate": _ratio(
                len(entities & failure_entities), len(entities)
            ),
            "explicit_need_entities": len(entities & need_entities),
            "explicit_need_rate": _ratio(
                len(entities & need_entities), len(entities)
            ),
            "top_components": index.top_labels(entities, "E02"),
            "evidence_ids": index.evidence_for(
                entities,
                focus_scripts=("E01", "E03", "E06"),
                prefer_low_rating=True,
            ),
            "boundary": "评价、故障与需求为同一评论实体共现，不声称句级因果",
        }
        attributes.append(row)
    attributes.sort(
        key=lambda row: (-row["review_entities"], row["attribute_id"])
    )

    impact_entities = index.entities("E08")
    workaround_entities = index.entities("E09")
    service_entities = index.entities("E10")
    recurrence_entities = index.entities("E12")
    failure_chains = []
    for failure in sorted(
        {
            label
            for entity_ref in index.all_entities
            for label in index.labels[entity_ref].get("E06", set())
        }
    ):
        entities = index.entities("E06", {failure})
        failure_chains.append(
            {
                "failure_id": failure,
                "label": _display(failure),
                **index.stats(entities),
                "impact_entities": len(entities & impact_entities),
                "impact_rate": _ratio(
                    len(entities & impact_entities), len(entities)
                ),
                "workaround_entities": len(entities & workaround_entities),
                "workaround_rate": _ratio(
                    len(entities & workaround_entities), len(entities)
                ),
                "service_entities": len(entities & service_entities),
                "service_rate": _ratio(
                    len(entities & service_entities), len(entities)
                ),
                "recurrence_entities": len(entities & recurrence_entities),
                "recurrence_rate": _ratio(
                    len(entities & recurrence_entities), len(entities)
                ),
                "explicit_need_entities": len(entities & need_entities),
                "top_components": index.top_labels(entities, "E02"),
                "top_triggers": index.top_labels(entities, "E07"),
                "top_conditions": index.top_labels(entities, "E14"),
                "evidence_ids": index.evidence_for(
                    entities,
                    focus_scripts=(
                        "E06",
                        "E02",
                        "E08",
                        "E09",
                        "E10",
                        "E12",
                    ),
                    prefer_low_rating=True,
                ),
                "boundary": "链路字段为同一评论实体共现，未验证句级因果顺序",
            }
        )
    failure_chains.sort(
        key=lambda row: (
            -row["low_star_entities"],
            -row["review_entities"],
            row["failure_id"],
        )
    )

    needs_by_attribute = []
    for attribute_row in attributes:
        entities = (
            index.entities("E01", {attribute_row["attribute_id"]})
            & need_entities
        )
        if not entities:
            continue
        needs_by_attribute.append(
            {
                "attribute_id": attribute_row["attribute_id"],
                "label": attribute_row["label"],
                **index.stats(entities),
                "share_of_attribute_mentions": _ratio(
                    len(entities), attribute_row["review_entities"]
                ),
                "need_expression_types": index.top_labels(entities, "E20"),
                "evidence_ids": index.evidence_for(
                    entities,
                    focus_scripts=("E20", "E01", "E21"),
                    prefer_low_rating=True,
                ),
            }
        )
    needs_by_attribute.sort(
        key=lambda row: (-row["review_entities"], row["attribute_id"])
    )

    decision_signals = []
    for script_id, name in (
        ("E20", "明确需求、请求与愿望"),
        ("E21", "预期差距"),
        ("E22", "比较关系"),
        ("E23", "购买驱动"),
        ("E24", "惊喜与愉悦"),
        ("E25", "生态与兼容性"),
        ("E26", "安全、信任与隐私"),
        ("E27", "智能与数字能力"),
        ("E28", "推荐、复购与留存"),
    ):
        for label_row in index.top_labels(
            index.entities(script_id), script_id, limit=20
        ):
            entities = index.entities(script_id, {label_row["label"]})
            decision_signals.append(
                {
                    "script_id": script_id,
                    "script_name": name,
                    "signal_id": label_row["label"],
                    "label": label_row["label_display"],
                    **index.stats(entities),
                    "evidence_ids": index.evidence_for(
                        entities,
                        focus_scripts=(script_id, "E01", "E02"),
                        prefer_low_rating=script_id in {"E20", "E21", "E26"},
                    ),
                }
            )
    decision_signals.sort(
        key=lambda row: (
            row["script_id"],
            -row["review_entities"],
            row["signal_id"],
        )
    )

    opportunities = [
        {
            "opportunity_id": f"quality_{row['failure_id']}",
            "opportunity_type": "product_quality",
            "problem": row["label"],
            "review_entities": row["review_entities"],
            "coverage": row["coverage"],
            "low_star_entities": row["low_star_entities"],
            "low_star_rate": row["low_star_rate"],
            "negative_phrase_entities": row["negative_phrase_entities"],
            "impact_entities": row["impact_entities"],
            "impact_rate": row["impact_rate"],
            "recurrence_entities": row["recurrence_entities"],
            "service_entities": row["service_entities"],
            "explicit_need_entities": row["explicit_need_entities"],
            "top_components": row["top_components"],
            "top_conditions": row["top_conditions"],
            "evidence_ids": row["evidence_ids"],
            "confidence": (
                "moderate"
                if row["review_entities"] >= LOW_SAMPLE_N
                else "limited"
            ),
            "boundary": "网评问题证据候选，不包含成本、工程可行性或路线图优先级",
        }
        for row in failure_chains
    ]

    explicit_smart = [
        row for row in decision_signals if row["script_id"] == "E27"
    ]
    smart_specs = [
        (
            "runtime_prediction",
            "剩余续航与可完成作业范围预测",
            (
                index.union(
                    [
                        ("E01", {"runtime", "charging"}),
                        ("E02", {"battery", "charger"}),
                    ]
                )
                & index.union(
                    [
                        ("E11", {"time", "area", "electric"}),
                        ("E15", {"area_scale", "duration"}),
                    ]
                )
                & index.union(
                    [
                        ("E03", {"negative"}),
                        ("E06", {"battery_failure"}),
                        ("E20", None),
                        ("E21", None),
                    ]
                )
            ),
            ("E01", "E02", "E11", "E15", "E20"),
        ),
        (
            "load_adaptation",
            "高草、湿草与负载自适应提示",
            (
                index.entities(
                    "E14", {"thick_tall_grass", "wet_condition"}
                )
                & index.union(
                    [
                        ("E06", {"stalling", "clogging", "uneven_result"}),
                        ("E20", None),
                        ("E21", None),
                    ]
                )
            ),
            ("E14", "E06", "E20", "E21"),
        ),
        (
            "diagnostics_maintenance",
            "故障诊断与维护提醒",
            (
                failure_entities
                & index.union(
                    [
                        ("E09", None),
                        ("E10", None),
                        ("E12", None),
                    ]
                )
            ),
            ("E06", "E09", "E10", "E12"),
        ),
        (
            "adaptive_drive",
            "自走速度与操控个性化",
            (
                index.entities("E01", {"self_propel", "handling"})
                & index.union(
                    [
                        ("E18", None),
                        ("E20", None),
                        ("E21", None),
                        ("E03", {"negative"}),
                    ]
                )
            ),
            ("E01", "E18", "E20", "E21"),
        ),
        (
            "safety_state",
            "异常状态与安全风险提示",
            index.entities(
                "E26", {"unsafe", "fire_electric"}
            )
            & index.union([("E06", None), ("E08", None)]),
            ("E26", "E06", "E08"),
        ),
    ]
    smart_hypotheses = []
    for hypothesis_id, label, entities, focus_scripts in smart_specs:
        smart_hypotheses.append(
            {
                "hypothesis_id": hypothesis_id,
                "label": label,
                **index.stats(entities),
                "top_attributes": index.top_labels(entities, "E01"),
                "top_failures": index.top_labels(entities, "E06"),
                "top_conditions": index.top_labels(entities, "E14"),
                "explicit_smart_entities": len(
                    entities & index.entities("E27")
                ),
                "evidence_ids": index.evidence_for(
                    entities,
                    focus_scripts=focus_scripts,
                    prefer_low_rating=True,
                ),
                "claim_type": "derived_hypothesis",
                "boundary": "由非智能痛点映射的智能化机会假设，不代表用户直接要求该功能",
            }
        )
    smart_hypotheses.sort(
        key=lambda row: (-row["review_entities"], row["hypothesis_id"])
    )

    tables = {
        table["table_id"]: table["rows"] for table in report["tables"]
    }
    return {
        "schema_version": "scene-analysis/v1",
        "analysis_version": report["analysis_version"],
        "data_snapshot_id": report["data_snapshot_id"],
        "report_id": report["report_id"],
        "written_review_entities": written_n,
        "profiles": profiles,
        "usage_scenarios": scenarios,
        "attribute_experience": attributes,
        "failure_chains": failure_chains,
        "needs_by_attribute": needs_by_attribute,
        "decision_signals": decision_signals,
        "product_opportunities": opportunities,
        "explicit_smart_signals": explicit_smart,
        "smart_hypotheses": smart_hypotheses,
        "comparison_lenses": {
            "channels": tables.get("channel_comparison", []),
            "models": tables.get("model_comparison", []),
            "time": tables.get("time_trend", []),
        },
        "evidence": [
            index.evidence[evidence_id]
            for evidence_id in sorted(index.evidence)
        ],
        "method": {
            "llm_used": False,
            "relation_unit": "review_entity_cooccurrence",
            "full_sample": True,
            "low_sample_threshold": LOW_SAMPLE_N,
            "boundaries": [
                "关系为同一评论实体共现，不自动解释为同句或因果",
                "规则群组可重叠，不外推总体人口结构",
                "产品机会不包含人工业务优先级",
                "智能化推导项明确标记为 hypothesis",
            ],
        },
    }
