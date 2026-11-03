from __future__ import annotations

from typing import Any

from reporting.scene_analysis import _display


SCRIPT_READING = {
    "E01": "属性提及用于定位体验讨论重心；需与评价、需求和场景联合阅读。",
    "E02": "部件提及用于把问题落到系统层；不能单独解释为部件故障。",
    "E03": "局部评价词提供文本倾向证据；当前未绑定到句内唯一属性对象。",
    "E04": "否定关系用于避免把“没有问题”误计为问题，也是后续语义解析门禁。",
    "E05": "强度、转折和不确定表达用于判断语气与证据力度，不直接计为满意度。",
    "E06": "故障与症状形成可靠性候选清单；命中率不是售后意义上的故障率。",
    "E07": "原因与触发条件用于拼接故障时间链；共现不代表已证明因果。",
    "E08": "后果与影响用于区分烦恼、任务中断和安全后果；低样本需逐条复核。",
    "E09": "补救动作揭示用户为恢复作业付出的额外步骤，可用于服务旅程分析。",
    "E10": "服务、保修和退货把产品问题连接到商业后果，但文本意向不等于真实行为。",
    "E11": "数值与单位可支持草坪规模、续航和电池规格的标准化，当前仍为原子值。",
    "E12": "耐久与复发用于识别早期失效和反复问题，需结合购买与使用时间。",
    "E13": "JTBD 任务把属性放回实际工作流，是场景报告的主索引。",
    "E14": "环境与工况用于拆分高草、湿草、坡地等条件，适合关联成功和失败任务。",
    "E15": "频率、规模与时长用于衡量作业负荷；缺失值不能被推断或补齐。",
    "E16": "身份与角色仅保留用户明确自述，不能外推总体用户人口构成。",
    "E17": "年龄只采纳结构化字段或明确文本，不使用语言模型猜测。",
    "E18": "能力限制用于无障碍与易用性研究，不把年龄自动等同于身体限制。",
    "E19": "经验、持有和换机用于分析迁移路径，当前没有燃油或竞品对照组。",
    "E20": "明确需求、请求和愿望是产品机会的直接证据入口，但不等同于优先级。",
    "E21": "预期差距定位宣称与实际体验之间的断点，需回到具体型号和工况核验。",
    "E22": "比较表达可识别竞品或旧机参照，当前 RYOBI 单品牌样本不足以做竞品结论。",
    "E23": "购买驱动用于解释选择原因；价格、品牌和生态信号需要独立统计。",
    "E24": "惊喜与愉悦帮助识别应保留的价值资产，不应只研究负向问题。",
    "E25": "生态与兼容性揭示电池或配件平台价值，需区分兼容与不兼容表达。",
    "E26": "安全、信任与隐私需要高召回和人工复核，低频不等于低风险。",
    "E27": "直接智能化表达用于确认显式需求；当前证据稀疏，不能替代概念测试。",
    "E28": "推荐、复购、退货和转离把体验连接到留存结果，但仍是文本信号。",
}


def _metric(report: dict[str, Any], metric_id: str) -> dict[str, Any]:
    return next(
        item for item in report["metrics"] if item["metric_id"] == metric_id
    )


def _table(report: dict[str, Any], table_id: str) -> list[dict[str, Any]]:
    return next(
        item["rows"]
        for item in report["tables"]
        if item["table_id"] == table_id
    )


def _labels(items: list[dict[str, Any]], key: str) -> str:
    return "、".join(str(item[key]) for item in items)


def _quality(value: str | None) -> str:
    return "低样本" if value == "low_sample" else "可描述"


def _metric_basis(refs: list[dict[str, Any]]) -> str:
    parts = []
    for ref in refs:
        value = ref["value"]
        if isinstance(value, float):
            value_text = f"{value:.4f}"
        else:
            value_text = str(value)
        parts.append(
            f"{ref['collection']}.{ref['row_id']}.{ref['field']}={value_text}"
        )
    return "；".join(parts)


def _scene_markdown(
    title: str,
    insights: list[dict[str, Any]],
    *,
    note: str,
) -> str:
    parts = [
        f"## {title}",
        "",
        f"> 派生洞察：由当前 Codex 会话根据确定性统计与证据生成。{note}",
    ]
    for insight in insights:
        parts.extend(
            [
                "",
                f"### {insight['headline']}",
                "",
                insight["body"],
                "",
                (
                    f"置信度：{insight['confidence']}｜"
                    f"证据引用：{len(insight['evidence_refs'])} 条"
                ),
            ]
        )
    return "\n".join(parts)


def _table_def(
    table_id: str,
    title: str,
    dataset: str,
    columns: list[dict[str, Any]],
    *,
    source_id: str,
    sort_field: str,
    sort_direction: str = "desc",
    subtitle: str | None = None,
) -> dict[str, Any]:
    return {
        "id": table_id,
        "title": title,
        "subtitle": subtitle,
        "dataset": dataset,
        "sourceId": source_id,
        "defaultSort": {
            "field": sort_field,
            "direction": sort_direction,
        },
        "columns": columns,
        "density": "spacious",
        "layout": "full",
    }


def _chart_def(
    chart_id: str,
    title: str,
    dataset: str,
    x_field: str,
    x_label: str,
    y_field: str,
    y_label: str,
    *,
    source_id: str,
    value_format: str | None = None,
    subtitle: str | None = None,
    horizontal: bool = False,
) -> dict[str, Any]:
    chart = {
        "id": chart_id,
        "title": title,
        "subtitle": subtitle,
        "type": "bar",
        "dataset": dataset,
        "sourceId": source_id,
        "encodings": {
            "x": {
                "field": x_field,
                "type": "nominal",
                "label": x_label,
            },
            "y": {
                "field": y_field,
                "type": "quantitative",
                "label": y_label,
            },
        },
    }
    if value_format:
        chart["encodings"]["y"]["format"] = value_format
        chart["valueFormat"] = value_format
    if horizontal:
        chart["options"] = {
            "orientation": "horizontal",
            "grouping": "single",
        }
    return chart


def build_stakeholder_artifact(
    report: dict[str, Any],
    scene: dict[str, Any],
    bundle: dict[str, Any],
) -> dict[str, Any]:
    average = _metric(report, "average_rating")["value"]
    positive = _metric(report, "positive_rating_rate")["value"]
    negative = _metric(report, "negative_rating_rate")["value"]
    written_coverage = _metric(report, "written_coverage")["value"]
    atomic_coverage = _metric(report, "atomic_v2_review_coverage")["value"]
    ledger = report["method"]["atomic_execution_ledger"]
    source_core = {
        "id": "core_report_json",
        "label": "VoxQuarry deterministic Report JSON",
        "path": "source-report.json",
        "query": {
            "description": "读取已验证的 RYOBI mower Report JSON。",
            "engine": "DuckDB",
            "language": "sql",
            "sql": "SELECT * FROM read_json_auto('source-report.json')",
            "tables_used": ["source-report.json"],
            "filters": [
                "brand = RYOBI",
                "category = 割草机",
                "category != 骑乘割草机",
                "syndicated = include after entity-key deduplication",
            ],
            "metric_definitions": [
                "好评率 = 4–5 星唯一评论实体 / 有效评分唯一评论实体",
                "差评率 = 1–2 星唯一评论实体 / 有效评分唯一评论实体",
                "正文覆盖率 = 有正文唯一评论实体 / 全部唯一评论实体",
                "脚本覆盖率 = 某脚本命中实体 / 全部有正文实体",
            ],
        },
    }
    source_scene = {
        "id": "scene_analysis_json",
        "label": "VoxQuarry deterministic scene analysis",
        "path": "scene-analysis.json",
        "query": {
            "description": "读取由 28 个脚本原子记录聚合的场景分析。",
            "engine": "DuckDB",
            "language": "sql",
            "sql": "SELECT * FROM read_json_auto('scene-analysis.json')",
            "tables_used": ["scene-analysis.json"],
            "filters": ["has_text = true"],
            "metric_definitions": [
                "场景覆盖率 = 场景命中实体 / 全部有正文实体",
                "低星率 = 场景内 1–2 星实体 / 场景内有效评分实体",
                "故障共现率 = 同时命中故障脚本的场景实体 / 场景实体",
                "关系单位为唯一评论实体共现，不代表句内关系或因果",
            ],
        },
    }
    source_insight = {
        "id": "insight_bundle_json",
        "label": "Codex-derived Insight Bundle",
        "path": "insight-bundle.json",
        "query": {
            "description": (
                "读取由当前 Codex 会话生成、经指标与证据引用校验的派生洞察。"
            ),
            "engine": "DuckDB",
            "language": "sql",
            "sql": "SELECT * FROM read_json_auto('insight-bundle.json')",
            "tables_used": ["insight-bundle.json"],
            "filters": ["third_party_api_used = false"],
            "metric_definitions": [
                "派生洞察不修改或重新计算核心指标",
                "每条洞察保留 metric_refs、evidence_refs 与 limitations",
            ],
        },
    }
    sources = [source_core, source_scene, source_insight]

    profiles = [
        {
            "profile_id": row["cohort_id"],
            "profile": row["label"],
            "definition": row["definition"],
            "review_entities": row["review_entities"],
            "coverage": row["coverage"],
            "average_rating": row["average_rating"],
            "high_star_rate": row["high_star_rate"],
            "low_star_rate": row["low_star_rate"],
            "top_tasks": _labels(row["top_tasks"], "label_display"),
            "top_attributes": _labels(
                row["top_attributes"], "label_display"
            ),
            "top_failures": _labels(row["top_failures"], "label_display"),
            "quality": _quality(row["quality_flag"]),
        }
        for row in scene["profiles"]
    ]
    scenarios = [
        {
            "scenario_id": row["scenario_id"],
            "scenario": row["label"],
            "review_entities": row["review_entities"],
            "coverage": row["coverage"],
            "average_rating": row["average_rating"],
            "low_star_rate": row["low_star_rate"],
            "failure_rate": row["failure_rate"],
            "explicit_need_rate": row["explicit_need_rate"],
            "top_attributes": _labels(
                row["top_attributes"], "label_display"
            ),
            "top_failures": _labels(row["top_failures"], "label_display"),
            "quality": _quality(row["quality_flag"]),
        }
        for row in scene["usage_scenarios"]
    ]
    attributes = [
        {
            "attribute_id": row["attribute_id"],
            "attribute": row["label"],
            "review_entities": row["review_entities"],
            "coverage": row["coverage"],
            "average_rating": row["average_rating"],
            "positive_phrase_rate": row["positive_phrase_rate"],
            "negative_phrase_rate": row["negative_phrase_rate"],
            "low_star_rate": row["low_star_rate"],
            "failure_rate": row["failure_rate"],
            "explicit_need_rate": row["explicit_need_rate"],
            "top_components": _labels(
                row["top_components"], "label_display"
            ),
            "quality": _quality(row["quality_flag"]),
        }
        for row in scene["attribute_experience"]
    ]
    failures = [
        {
            "failure_id": row["failure_id"],
            "failure": row["label"],
            "review_entities": row["review_entities"],
            "coverage": row["coverage"],
            "average_rating": row["average_rating"],
            "low_star_rate": row["low_star_rate"],
            "recurrence_rate": row["recurrence_rate"],
            "impact_rate": row["impact_rate"],
            "workaround_rate": row["workaround_rate"],
            "service_rate": row["service_rate"],
            "top_components": _labels(
                row["top_components"], "label_display"
            ),
            "top_conditions": _labels(
                row["top_conditions"], "label_display"
            ),
            "quality": _quality(row["quality_flag"]),
        }
        for row in scene["failure_chains"]
    ]
    needs = [
        {
            "attribute_id": row["attribute_id"],
            "attribute": row["label"],
            "review_entities": row["review_entities"],
            "share_of_attribute_mentions": row[
                "share_of_attribute_mentions"
            ],
            "average_rating": row["average_rating"],
            "low_star_rate": row["low_star_rate"],
            "need_types": _labels(
                row["need_expression_types"], "label_display"
            ),
            "quality": _quality(row["quality_flag"]),
        }
        for row in scene["needs_by_attribute"]
    ]
    decisions = [
        {
            "script_id": row["script_id"],
            "script_name": row["script_name"],
            "signal_id": row["signal_id"],
            "signal": row["label"],
            "review_entities": row["review_entities"],
            "coverage": row["coverage"],
            "average_rating": row["average_rating"],
            "low_star_rate": row["low_star_rate"],
            "quality": _quality(row["quality_flag"]),
        }
        for row in scene["decision_signals"]
    ]
    opportunities = [
        {
            "opportunity_id": row["opportunity_id"],
            "problem": row["problem"],
            "review_entities": row["review_entities"],
            "coverage": row["coverage"],
            "low_star_rate": row["low_star_rate"],
            "impact_rate": row["impact_rate"],
            "explicit_need_entities": row["explicit_need_entities"],
            "recurrence_entities": row["recurrence_entities"],
            "service_entities": row["service_entities"],
            "top_components": _labels(
                row["top_components"], "label_display"
            ),
            "top_conditions": _labels(
                row["top_conditions"], "label_display"
            ),
            "confidence": row["confidence"],
        }
        for row in scene["product_opportunities"]
    ]
    smart_explicit = [
        {
            "signal_id": row["signal_id"],
            "signal": row["label"],
            "review_entities": row["review_entities"],
            "coverage": row["coverage"],
            "average_rating": row["average_rating"],
            "low_star_rate": row["low_star_rate"],
            "quality": _quality(row["quality_flag"]),
        }
        for row in scene["explicit_smart_signals"]
    ]
    smart_hypotheses = [
        {
            "hypothesis_id": row["hypothesis_id"],
            "hypothesis": row["label"],
            "review_entities": row["review_entities"],
            "coverage": row["coverage"],
            "average_rating": row["average_rating"],
            "low_star_rate": row["low_star_rate"],
            "explicit_smart_entities": row["explicit_smart_entities"],
            "top_attributes": _labels(
                row["top_attributes"], "label_display"
            ),
            "top_failures": _labels(row["top_failures"], "label_display"),
            "top_conditions": _labels(
                row["top_conditions"], "label_display"
            ),
            "quality": _quality(row["quality_flag"]),
            "claim_type": row["claim_type"],
        }
        for row in scene["smart_hypotheses"]
    ]
    channels = [
        {
            "channel": row["group"],
            "sample_size": row["sample_size"],
            "average_rating": row["average_rating"],
            "positive_rate": row["positive_rate"],
            "negative_rate": row["negative_rate"],
            "difference_vs_rest": row["difference_vs_rest"],
            "effect_size": row["effect_size"],
            "adjusted_p_value": row["adjusted_p_value"],
            "significant": row["significant"],
            "quality": _quality(row["quality_flag"]),
        }
        for row in scene["comparison_lenses"]["channels"]
    ]
    models = [
        {
            "model": row["group"],
            "sample_size": row["sample_size"],
            "average_rating": row["average_rating"],
            "positive_rate": row["positive_rate"],
            "negative_rate": row["negative_rate"],
            "difference_vs_rest": row["difference_vs_rest"],
            "effect_size": row["effect_size"],
            "adjusted_p_value": row["adjusted_p_value"],
            "significant": row["significant"],
            "quality": _quality(row["quality_flag"]),
        }
        for row in scene["comparison_lenses"]["models"]
    ]

    all_insights = [
        {
            "scene_id": section["scene_id"],
            "scene": section["title"],
            "insight_id": insight["insight_id"],
            "headline": insight["headline"],
            "conclusion": insight["body"],
            "claim_type": insight["claim_type"],
            "confidence": insight["confidence"],
            "metric_basis": _metric_basis(insight["metric_refs"]),
            "evidence_count": len(insight["evidence_refs"]),
            "limitations": "；".join(insight["limitations"]),
        }
        for section in bundle["scenes"]
        for insight in section["insights"]
    ]
    evidence_by_id = {
        item["evidence_id"]: item for item in scene["evidence"]
    }
    insight_evidence = []
    for section in bundle["scenes"]:
        for insight in section["insights"]:
            for evidence_id in insight["evidence_refs"]:
                item = evidence_by_id[evidence_id]
                labels = []
                for script_id, values in item["matched_labels"].items():
                    labels.append(f"{script_id}:{'、'.join(values)}")
                insight_evidence.append(
                    {
                        "scene": section["title"],
                        "insight_id": insight["insight_id"],
                        "headline": insight["headline"],
                        "evidence_id": evidence_id,
                        "snippet": item["snippet"],
                        "rating": item["rating"],
                        "model": item["model"],
                        "channel": item["channel"],
                        "submission_date": item["submission_date"],
                        "matched_labels": "；".join(labels),
                    }
                )

    execution = [
        {key: value for key, value in row.items() if key != "evidence_ids"}
        for row in _table(report, "atomic_script_execution")
    ]
    labels = [
        {key: value for key, value in row.items() if key != "evidence_ids"}
        for row in _table(report, "atomic_label_summary")
    ]
    top_by_script = {}
    for row in labels:
        script_id = row["script_id"]
        if script_id not in top_by_script:
            top_by_script[script_id] = row
    script_insights = []
    for row in execution:
        top = top_by_script.get(row["script_id"])
        script_insights.append(
            {
                "script_id": row["script_id"],
                "script": row["name"],
                "matched": row["matched"],
                "coverage": row["coverage"],
                "top_label": _display(top["label"]) if top else "未命中",
                "top_label_entities": top["review_entities"] if top else 0,
                "business_reading": SCRIPT_READING[row["script_id"]],
                "failed": row["failed"],
            }
        )
    ratings = [
        {
            "rating_label": f"{row['rating']} 星",
            "rating": row["rating"],
            "count": row["count"],
            "share": row["share"],
        }
        for row in _table(report, "rating_distribution")
    ]

    summary = [
        {
            "sample_size": report["samples"]["review_entities"],
            "written_entities": report["samples"][
                "written_review_entities"
            ],
            "written_coverage": written_coverage,
            "average_rating": average,
            "positive_rate": positive,
            "negative_rate": negative,
            "atomic_coverage": atomic_coverage,
            "ledger_records": ledger["record_count"],
            "scene_evidence": len(scene["evidence"]),
            "insight_count": len(all_insights),
        }
    ]
    datasets = {
        "summary": summary,
        "rating_distribution": ratings,
        "profiles": profiles,
        "usage_scenarios": scenarios,
        "attributes": attributes,
        "failure_chains": failures,
        "needs": needs,
        "decision_signals": decisions,
        "product_opportunities": opportunities,
        "smart_explicit": smart_explicit,
        "smart_hypotheses": smart_hypotheses,
        "channels": channels,
        "models": models,
        "insights": all_insights,
        "insight_evidence": insight_evidence,
        "script_execution": execution,
        "script_insights": script_insights,
        "atomic_labels": labels,
    }

    title = "RYOBI Mower｜用户、场景与产品机会洞察"
    scene_map = {
        section["scene_id"]: section for section in bundle["scenes"]
    }
    executive = (
        "## Executive Summary｜执行摘要\n\n"
        f"- **总体口碑强**：{summary[0]['sample_size']:,} 条唯一评论，"
        f"平均星级 {average:.2f}，好评率 {positive:.1%}。\n"
        "- **完整作业链是主要研究主线**：充电、续航、大面积作业与自走"
        "控制需要在同一工作流中验证。\n"
        "- **可靠性问题覆盖不高但后果明显**：停止工作、破损、停转/中断"
        "的低星关联与服务/补救共现值得优先复核。\n"
        "- **智能化需要分层**：直接智能表达稀疏；诊断、续航预测和自走"
        "个性化仅作为从痛点推导的概念假设。\n"
        "- **竞品结论暂不可生成**：本次范围只有 RYOBI；型号和渠道差异"
        "均按观察性线索阅读。"
    )
    blocks: list[dict[str, Any]] = [
        {"id": "title", "type": "markdown", "body": f"# {title}"},
        {
            "id": "executive_summary",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": executive,
        },
        {
            "id": "headline_metrics",
            "type": "metric-strip",
            "cardIds": [
                "sample_card",
                "rating_card",
                "positive_card",
                "coverage_card",
                "evidence_card",
            ],
        },
        {
            "id": "overview",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": _scene_markdown(
                scene_map["overview"]["title"],
                scene_map["overview"]["insights"],
                note="渠道和型号差异为观察性比较。",
            ),
        },
        {"id": "rating_chart_block", "type": "chart", "chartId": "rating_chart"},
        {
            "id": "channel_table_block",
            "type": "table",
            "tableId": "channel_table",
        },
        {
            "id": "model_table_block",
            "type": "table",
            "tableId": "model_table",
        },
        {
            "id": "profiles",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": _scene_markdown(
                scene_map["profiles"]["title"],
                scene_map["profiles"]["insights"],
                note="画像可重叠，只统计明确表达。",
            ),
        },
        {
            "id": "profile_chart_block",
            "type": "chart",
            "chartId": "profile_chart",
        },
        {
            "id": "profile_table_block",
            "type": "table",
            "tableId": "profile_table",
        },
        {
            "id": "scenarios",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": _scene_markdown(
                scene_map["scenarios"]["title"],
                scene_map["scenarios"]["insights"],
                note="场景可重叠，故障为评论实体级共现。",
            ),
        },
        {
            "id": "scenario_chart_block",
            "type": "chart",
            "chartId": "scenario_chart",
        },
        {
            "id": "scenario_table_block",
            "type": "table",
            "tableId": "scenario_table",
        },
        {
            "id": "experience",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": _scene_markdown(
                scene_map["experience"]["title"],
                scene_map["experience"]["insights"],
                note="属性与星级为评论级关联，不是句级因果。",
            ),
        },
        {
            "id": "attribute_chart_block",
            "type": "chart",
            "chartId": "attribute_chart",
        },
        {
            "id": "attribute_table_block",
            "type": "table",
            "tableId": "attribute_table",
        },
        {
            "id": "failure_chart_block",
            "type": "chart",
            "chartId": "failure_chart",
        },
        {
            "id": "failure_table_block",
            "type": "table",
            "tableId": "failure_table",
        },
        {
            "id": "needs",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": _scene_markdown(
                scene_map["needs"]["title"],
                scene_map["needs"]["insights"],
                note="明确需求和行为意向均需后续验证。",
            ),
        },
        {
            "id": "needs_chart_block",
            "type": "chart",
            "chartId": "needs_chart",
        },
        {
            "id": "needs_table_block",
            "type": "table",
            "tableId": "needs_table",
        },
        {
            "id": "decision_table_block",
            "type": "table",
            "tableId": "decision_table",
        },
        {
            "id": "product_opportunities",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": _scene_markdown(
                scene_map["product_opportunities"]["title"],
                scene_map["product_opportunities"]["insights"],
                note="候选未加入成本、战略与工程可行性。",
            ),
        },
        {
            "id": "opportunity_table_block",
            "type": "table",
            "tableId": "opportunity_table",
        },
        {
            "id": "smart_opportunities",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": _scene_markdown(
                scene_map["smart_opportunities"]["title"],
                scene_map["smart_opportunities"]["insights"],
                note="显式信号与痛点推导假设严格分开。",
            ),
        },
        {
            "id": "smart_explicit_table_block",
            "type": "table",
            "tableId": "smart_explicit_table",
        },
        {
            "id": "smart_hypothesis_chart_block",
            "type": "chart",
            "chartId": "smart_hypothesis_chart",
        },
        {
            "id": "smart_hypothesis_table_block",
            "type": "table",
            "tableId": "smart_hypothesis_table",
        },
        {
            "id": "recommendations",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": (
                "## Recommended Next Steps｜建议的下一步\n\n"
                "1. 对停止工作、破损、停转/中断做型号 × 部件 × 工况的原文复核，"
                "把证据候选转成工程可验证问题。\n"
                "2. 设计“大面积草坪完整作业”任务测试，覆盖续航、换电、充电、"
                "自走和集草闭环。\n"
                "3. 招募年长或能力受限用户，验证自走速度、启动步骤、转向与收纳"
                "负担。\n"
                "4. 对诊断、续航预测、自走个性化做低成本概念测试；在价值被验证"
                "之前不进入功能承诺。\n"
                "5. 补充竞品品牌的同口径数据后，再运行品牌/产品比较脚本。"
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": (
                "## Further Questions｜后续研究问题\n\n"
                "- 停止工作和破损是否集中在特定型号、部件或购买批次？\n"
                "- 大面积用户完成一次作业需要多少次换电，哪些充电组合最顺畅？\n"
                "- 坡地场景为什么同时出现较高评分和更集中的故障共现？\n"
                "- 退货表达背后的首要触发因素是可靠性、价值、续航还是服务？\n"
                "- 智能化概念能否降低任务不确定性，而不是增加连接与设置负担？"
            ),
        },
        {
            "id": "insight_evidence_method",
            "type": "markdown",
            "sourceId": source_insight["id"],
            "body": (
                "## Insight 与证据索引\n\n"
                "每条派生洞察都保留确定性指标引用、原文证据引用、置信度和限制。"
                "原文仅用于核验规则与上下文，不用个别引文替代全量统计。"
            ),
        },
        {
            "id": "insight_table_block",
            "type": "table",
            "tableId": "insight_table",
        },
        {
            "id": "evidence_table_block",
            "type": "table",
            "tableId": "evidence_table",
        },
        {
            "id": "script_appendix",
            "type": "markdown",
            "sourceId": source_core["id"],
            "body": (
                "## 28 个脚本的业务解读\n\n"
                f"E01–E28 均对 {scene['written_review_entities']:,} 条正文实体"
                f"执行，共留下 {ledger['record_count']:,} 条执行状态，失败为 0。"
                "下表逐一给出脚本覆盖、首要标签和业务用途；覆盖率只表示规则"
                "找到证据，不代表准确率、发生率或业务优先级。"
            ),
        },
        {
            "id": "script_chart_block",
            "type": "chart",
            "chartId": "script_chart",
        },
        {
            "id": "script_insight_table_block",
            "type": "table",
            "tableId": "script_insight_table",
        },
        {
            "id": "atomic_label_table_block",
            "type": "table",
            "tableId": "atomic_label_table",
        },
        {
            "id": "caveats",
            "type": "markdown",
            "sourceId": source_scene["id"],
            "body": (
                "## Caveats｜结论边界\n\n"
                "- 本次只有 RYOBI mower 数据，不能生成真实竞品结论。\n"
                "- 关系单位为同一唯一评论实体共现，不自动解释为同句或因果。\n"
                "- 画像、场景和需求群组可重叠，比例不可直接相加。\n"
                "- 属性正负以评论级星级和明确评价短语关联，不是完整句级 ABSA。\n"
                "- 故障脚本命中率不是售后或保修意义上的真实故障率。\n"
                "- verified purchaser 的 false 与 missing 当前无法区分。\n"
                "- syndicated 标志不完整；总体指标按实体业务键去重。\n"
                "- 规则优先精度与可解释性，未收录同义表达仍可能漏检。\n"
                "- 低于 20 条的行标记为低样本，不用于稳定结论。\n"
                "- LLM 只生成派生文字，不修改、补齐或重算核心数字。"
            ),
        },
        {
            "id": "reproducibility",
            "type": "markdown",
            "sourceId": source_core["id"],
            "body": (
                "## 可复现标识\n\n"
                f"- report_id: `{report['report_id']}`\n"
                f"- report_schema: `{report['schema_version']}`\n"
                f"- analysis_version: `{report['analysis_version']}`\n"
                f"- data_snapshot_id: `{report['data_snapshot_id']}`\n"
                f"- core_result: `{report['hashes']['core_result']}`\n"
                f"- atomic_ledger: `{ledger['logical_content_sha256']}`\n"
                f"- scene_analysis: `{scene['schema_version']}`\n"
                f"- insight_bundle_id: `{bundle['insight_bundle_id']}`\n"
                f"- insight_bundle_hash: `{bundle['content_hash']}`\n"
                "- third_party_api_used: `false`"
            ),
        },
    ]

    cards = [
        {
            "id": "sample_card",
            "dataset": "summary",
            "sourceId": source_core["id"],
            "description": "按来源业务键去重后的唯一评论实体。",
            "metrics": [
                {
                    "label": "唯一评论",
                    "field": "sample_size",
                    "format": "number",
                }
            ],
        },
        {
            "id": "rating_card",
            "dataset": "summary",
            "sourceId": source_core["id"],
            "description": "全部有效评分唯一评论实体的算术平均。",
            "metrics": [
                {
                    "label": "平均星级",
                    "field": "average_rating",
                    "format": "number",
                }
            ],
        },
        {
            "id": "positive_card",
            "dataset": "summary",
            "sourceId": source_core["id"],
            "description": "4–5 星实体占全部有效评分实体。",
            "metrics": [
                {
                    "label": "好评率",
                    "field": "positive_rate",
                    "format": "percent",
                },
                {
                    "label": "差评率",
                    "field": "negative_rate",
                    "format": "percent",
                },
            ],
        },
        {
            "id": "coverage_card",
            "dataset": "summary",
            "sourceId": source_core["id"],
            "description": "至少命中 E01–E28 任一脚本的正文实体。",
            "metrics": [
                {
                    "label": "原子覆盖率",
                    "field": "atomic_coverage",
                    "format": "percent",
                },
                {
                    "label": "正文覆盖率",
                    "field": "written_coverage",
                    "format": "percent",
                },
            ],
        },
        {
            "id": "evidence_card",
            "dataset": "summary",
            "sourceId": source_scene["id"],
            "description": "场景聚合保留的代表性原文证据。",
            "metrics": [
                {
                    "label": "场景证据",
                    "field": "scene_evidence",
                    "format": "number",
                },
                {
                    "label": "派生洞察",
                    "field": "insight_count",
                    "format": "number",
                },
            ],
        },
    ]
    charts = [
        _chart_def(
            "rating_chart",
            "星级分布",
            "rating_distribution",
            "rating_label",
            "星级",
            "share",
            "评论占比",
            source_id=source_core["id"],
            value_format="percent",
            subtitle="总体口碑强，但具体场景与问题仍需分开阅读。",
        ),
        _chart_def(
            "profile_chart",
            "显式用户画像覆盖率",
            "profiles",
            "profile",
            "画像",
            "coverage",
            "正文样本占比",
            source_id=source_scene["id"],
            value_format="percent",
            subtitle="大面积或复杂草坪用户是最清晰的显式群组。",
            horizontal=True,
        ),
        _chart_def(
            "scenario_chart",
            "使用场景覆盖率",
            "usage_scenarios",
            "scenario",
            "场景",
            "coverage",
            "正文样本占比",
            source_id=source_scene["id"],
            value_format="percent",
            subtitle="日常割草、充电管理和大面积作业构成主要工作流。",
            horizontal=True,
        ),
        _chart_def(
            "attribute_chart",
            "属性提及评论的低星率",
            "attributes",
            "attribute",
            "属性",
            "low_star_rate",
            "1–2 星占比",
            source_id=source_scene["id"],
            value_format="percent",
            subtitle="价格与价值、充电、续航的低星关联相对更高。",
            horizontal=True,
        ),
        _chart_def(
            "failure_chart",
            "故障类型的低星率",
            "failure_chains",
            "failure",
            "故障类型",
            "low_star_rate",
            "1–2 星占比",
            source_id=source_scene["id"],
            value_format="percent",
            subtitle="破损、停止工作和停转/中断的负向后果更明显。",
            horizontal=True,
        ),
        _chart_def(
            "needs_chart",
            "属性相关明确需求数量",
            "needs",
            "attribute",
            "属性",
            "review_entities",
            "评论实体",
            source_id=source_scene["id"],
            subtitle="充电、自走、续航与集草闭环的需求证据最多。",
            horizontal=True,
        ),
        _chart_def(
            "smart_hypothesis_chart",
            "智能化问题空间候选",
            "smart_hypotheses",
            "hypothesis",
            "候选",
            "review_entities",
            "关联评论实体",
            source_id=source_scene["id"],
            subtitle="这是痛点映射，不是用户已验证的功能需求。",
            horizontal=True,
        ),
        _chart_def(
            "script_chart",
            "E01–E28 脚本覆盖率",
            "script_execution",
            "script_id",
            "脚本",
            "coverage",
            "正文样本覆盖率",
            source_id=source_core["id"],
            value_format="percent",
            subtitle="所有脚本均对同一 4,307 条正文实体执行。",
        ),
    ]

    text_col = lambda field, label: {
        "field": field,
        "label": label,
        "type": "text",
    }
    num_col = lambda field, label: {
        "field": field,
        "label": label,
        "format": "number",
    }
    pct_col = lambda field, label: {
        "field": field,
        "label": label,
        "format": "percent",
    }
    tables = [
        _table_def(
            "channel_table",
            "渠道评分比较",
            "channels",
            [
                text_col("channel", "渠道"),
                num_col("sample_size", "样本"),
                num_col("average_rating", "平均星级"),
                pct_col("positive_rate", "好评率"),
                pct_col("negative_rate", "差评率"),
                num_col("effect_size", "效应量"),
                text_col("significant", "校正后显著"),
            ],
            source_id=source_core["id"],
            sort_field="sample_size",
            subtitle="观察性比较；未控制型号、时间和用户构成。",
        ),
        _table_def(
            "model_table",
            "型号评分比较",
            "models",
            [
                text_col("model", "型号"),
                num_col("sample_size", "样本"),
                num_col("average_rating", "平均星级"),
                pct_col("positive_rate", "好评率"),
                pct_col("negative_rate", "差评率"),
                num_col("effect_size", "效应量"),
                text_col("significant", "校正后显著"),
            ],
            source_id=source_core["id"],
            sort_field="sample_size",
            subtitle="低样本和观察性构成差异不用于因果解释。",
        ),
        _table_def(
            "profile_table",
            "显式用户画像",
            "profiles",
            [
                text_col("profile", "画像"),
                num_col("review_entities", "评论实体"),
                pct_col("coverage", "覆盖率"),
                num_col("average_rating", "平均星级"),
                pct_col("low_star_rate", "低星率"),
                text_col("top_tasks", "主要任务"),
                text_col("top_attributes", "主要属性"),
                text_col("quality", "质量"),
            ],
            source_id=source_scene["id"],
            sort_field="review_entities",
            subtitle="群组可重叠，只描述评论中明确表达的特征。",
        ),
        _table_def(
            "scenario_table",
            "使用场景表现",
            "usage_scenarios",
            [
                text_col("scenario", "场景"),
                num_col("review_entities", "评论实体"),
                pct_col("coverage", "覆盖率"),
                num_col("average_rating", "平均星级"),
                pct_col("low_star_rate", "低星率"),
                pct_col("failure_rate", "故障共现率"),
                pct_col("explicit_need_rate", "明确需求共现率"),
                text_col("top_attributes", "主要属性"),
            ],
            source_id=source_scene["id"],
            sort_field="review_entities",
            subtitle="场景可重叠；共现不是因果。",
        ),
        _table_def(
            "attribute_table",
            "产品属性体验",
            "attributes",
            [
                text_col("attribute", "属性"),
                num_col("review_entities", "评论实体"),
                pct_col("coverage", "覆盖率"),
                num_col("average_rating", "平均星级"),
                pct_col("positive_phrase_rate", "正向短语率"),
                pct_col("negative_phrase_rate", "负向短语率"),
                pct_col("low_star_rate", "低星率"),
                pct_col("failure_rate", "故障共现率"),
                pct_col("explicit_need_rate", "需求共现率"),
            ],
            source_id=source_scene["id"],
            sort_field="review_entities",
            subtitle="评分与短语均为评论级关联，不是完整句级 ABSA。",
        ),
        _table_def(
            "failure_table",
            "故障与后果链",
            "failure_chains",
            [
                text_col("failure", "故障"),
                num_col("review_entities", "评论实体"),
                pct_col("coverage", "覆盖率"),
                num_col("average_rating", "平均星级"),
                pct_col("low_star_rate", "低星率"),
                pct_col("recurrence_rate", "复发共现率"),
                pct_col("workaround_rate", "补救共现率"),
                pct_col("service_rate", "服务共现率"),
                text_col("top_components", "主要部件"),
                text_col("quality", "质量"),
            ],
            source_id=source_scene["id"],
            sort_field="review_entities",
            subtitle="命中率不是售后故障率；链路为实体级共现。",
        ),
        _table_def(
            "needs_table",
            "属性相关明确需求",
            "needs",
            [
                text_col("attribute", "属性"),
                num_col("review_entities", "需求实体"),
                pct_col(
                    "share_of_attribute_mentions",
                    "占属性提及",
                ),
                num_col("average_rating", "平均星级"),
                pct_col("low_star_rate", "低星率"),
                text_col("need_types", "需求表达"),
                text_col("quality", "质量"),
            ],
            source_id=source_scene["id"],
            sort_field="review_entities",
            subtitle="需求共现不等于路线图优先级。",
        ),
        _table_def(
            "decision_table",
            "需求、比较、购买与留存信号",
            "decision_signals",
            [
                text_col("script_id", "脚本"),
                text_col("script_name", "分析"),
                text_col("signal", "信号"),
                num_col("review_entities", "评论实体"),
                pct_col("coverage", "覆盖率"),
                num_col("average_rating", "平均星级"),
                pct_col("low_star_rate", "低星率"),
                text_col("quality", "质量"),
            ],
            source_id=source_scene["id"],
            sort_field="review_entities",
            subtitle="覆盖 E20–E28 的决策和结果信号。",
        ),
        _table_def(
            "opportunity_table",
            "产品机会证据候选",
            "product_opportunities",
            [
                text_col("problem", "问题"),
                num_col("review_entities", "评论实体"),
                pct_col("coverage", "覆盖率"),
                pct_col("low_star_rate", "低星率"),
                num_col("explicit_need_entities", "明确需求"),
                num_col("recurrence_entities", "复发"),
                num_col("service_entities", "服务"),
                text_col("top_components", "主要部件"),
                text_col("top_conditions", "主要条件"),
                text_col("confidence", "证据置信度"),
            ],
            source_id=source_scene["id"],
            sort_field="review_entities",
            subtitle="证据候选不包含工程成本、战略或市场优先级。",
        ),
        _table_def(
            "smart_explicit_table",
            "直接智能化表达",
            "smart_explicit",
            [
                text_col("signal", "信号"),
                num_col("review_entities", "评论实体"),
                pct_col("coverage", "覆盖率"),
                num_col("average_rating", "平均星级"),
                pct_col("low_star_rate", "低星率"),
                text_col("quality", "质量"),
            ],
            source_id=source_scene["id"],
            sort_field="review_entities",
            subtitle="仅统计评论中明确出现的智能与数字能力表达。",
        ),
        _table_def(
            "smart_hypothesis_table",
            "从痛点推导的智能化假设",
            "smart_hypotheses",
            [
                text_col("hypothesis", "假设"),
                num_col("review_entities", "关联实体"),
                pct_col("coverage", "覆盖率"),
                pct_col("low_star_rate", "低星率"),
                num_col("explicit_smart_entities", "显式智能共现"),
                text_col("top_attributes", "相关属性"),
                text_col("top_failures", "相关故障"),
                text_col("quality", "质量"),
            ],
            source_id=source_scene["id"],
            sort_field="review_entities",
            subtitle="候选不是用户直接要求，也不证明功能会解决问题。",
        ),
        _table_def(
            "insight_table",
            "派生 Insight 索引",
            "insights",
            [
                text_col("scene", "研究章节"),
                text_col("headline", "洞察"),
                text_col("claim_type", "结论类型"),
                text_col("confidence", "置信度"),
                num_col("evidence_count", "证据数"),
                text_col("metric_basis", "指标引用"),
                text_col("limitations", "限制"),
            ],
            source_id=source_insight["id"],
            sort_field="scene",
            sort_direction="asc",
            subtitle="所有数字引用均在生成时与确定性结果逐项校验。",
        ),
        _table_def(
            "evidence_table",
            "Insight 原文证据",
            "insight_evidence",
            [
                text_col("scene", "研究章节"),
                text_col("headline", "关联洞察"),
                text_col("evidence_id", "证据 ID"),
                text_col("snippet", "原文"),
                num_col("rating", "星级"),
                text_col("model", "型号"),
                text_col("channel", "渠道"),
                text_col("submission_date", "日期"),
                text_col("matched_labels", "规则标签"),
            ],
            source_id=source_scene["id"],
            sort_field="headline",
            sort_direction="asc",
            subtitle="代表性原文用于核验，不代替全量统计。",
        ),
        _table_def(
            "script_insight_table",
            "28 个原子脚本：结果与业务用途",
            "script_insights",
            [
                text_col("script_id", "脚本"),
                text_col("script", "原子信息"),
                num_col("matched", "命中实体"),
                pct_col("coverage", "覆盖率"),
                text_col("top_label", "首要标签"),
                num_col("top_label_entities", "首要标签实体"),
                text_col("business_reading", "业务解读"),
                num_col("failed", "失败"),
            ],
            source_id=source_core["id"],
            sort_field="script_id",
            sort_direction="asc",
            subtitle="脚本逐一全量执行；未命中和失败均保留。",
        ),
        _table_def(
            "atomic_label_table",
            "28 个脚本的全部原子标签",
            "atomic_labels",
            [
                text_col("script_id", "脚本"),
                text_col("script_name", "原子信息"),
                text_col("label", "原始标签"),
                num_col("review_entities", "命中实体"),
                pct_col("coverage", "覆盖率"),
            ],
            source_id=source_core["id"],
            sort_field="review_entities",
            subtitle="同一实体可命中多个标签，标签数量不可直接相加。",
        ),
    ]

    manifest = {
        "title": title,
        "version": 1,
        "surface": "report",
        "description": (
            "RYOBI mower 全量网评的场景化量化报告；"
            "确定性核心与 Codex 派生洞察分层保存。"
        ),
        "generatedAt": report["generated_at"],
        "sources": sources,
        "blocks": blocks,
        "cards": cards,
        "charts": charts,
        "tables": tables,
    }
    snapshot = {
        "version": 1,
        "status": "ready",
        "generatedAt": report["generated_at"],
        "datasets": datasets,
    }
    return {
        "manifest": manifest,
        "snapshot": snapshot,
        "sources": sources,
        "surface": "report",
        "package_info": {
            "report_id": report["report_id"],
            "schema_version": report["schema_version"],
            "analysis_version": report["analysis_version"],
            "data_snapshot_id": report["data_snapshot_id"],
            "core_result_hash": report["hashes"]["core_result"],
            "ledger_hash": ledger["logical_content_sha256"],
            "insight_bundle_id": bundle["insight_bundle_id"],
            "insight_bundle_hash": bundle["content_hash"],
            "third_party_api_used": False,
        },
    }
