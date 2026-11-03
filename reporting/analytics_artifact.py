from __future__ import annotations

from typing import Any


def _metric(report: dict[str, Any], metric_id: str) -> dict[str, Any]:
    return next(
        metric
        for metric in report["metrics"]
        if metric["metric_id"] == metric_id
    )


def _table(report: dict[str, Any], table_id: str) -> dict[str, Any]:
    return next(
        table
        for table in report["tables"]
        if table["table_id"] == table_id
    )


def build_analytics_artifact(report: dict[str, Any]) -> dict[str, Any]:
    samples = report["samples"]
    sample_size = _metric(report, "sample_size")["value"]
    average_rating = _metric(report, "average_rating")["value"]
    positive_rate = _metric(report, "positive_rating_rate")["value"]
    negative_rate = _metric(report, "negative_rating_rate")["value"]
    atomic_coverage = _metric(report, "atomic_v2_review_coverage")["value"]
    execution = [
        {key: value for key, value in row.items() if key != "evidence_ids"}
        for row in _table(report, "atomic_script_execution")["rows"]
    ]
    labels = [
        {key: value for key, value in row.items() if key != "evidence_ids"}
        for row in _table(report, "atomic_label_summary")["rows"]
    ]
    ratings = [
        {
            "rating": row["rating"],
            "rating_label": f"{row['rating']} 星",
            "count": row["count"],
            "share": row["share"],
        }
        for row in _table(report, "rating_distribution")["rows"]
    ]
    atomic_evidence = [
        {
            "script_id": item["factor_id"],
            "script_name": item["factor_label"],
            "snippet": item["snippet"],
            "matched_text": item.get("matched_text"),
            "rating": item.get("rating"),
            "model": item.get("model"),
            "channel": item.get("channel"),
            "submission_date": item.get("submission_date"),
            "extractor_version": item.get("extractor_version"),
        }
        for item in report["evidence"]
        if item.get("factor_namespace") == "atomic_v2"
    ]
    ledger = report["method"]["atomic_execution_ledger"]
    registry = report["method"]["atomic_script_registry"]
    top_script = max(execution, key=lambda row: (row["matched"], row["script_id"]))
    failure_total = sum(row["failed"] for row in execution)

    source = {
        "id": "ryobi_report_json",
        "label": "VoxQuarry RYOBI mower deterministic Report JSON",
        "path": "source-report.json",
        "query": {
            "description": (
                "读取已验证的 VoxQuarry Report JSON；可视化数据由 metrics、"
                "tables 和 method.atomic_execution_ledger 做确定性投影。"
            ),
            "engine": "DuckDB",
            "language": "sql",
            "sql": "SELECT * FROM read_json_auto('source-report.json')",
            "tables_used": ["source-report.json"],
            "filters": [
                "category = 割草机",
                "category != 骑乘割草机",
                "brand = RYOBI",
                "syndicated = include with entity-key deduplication",
            ],
            "metric_definitions": [
                "好评率 = 4–5 星唯一评论实体 / 有效评分唯一评论实体",
                "差评率 = 1–2 星唯一评论实体 / 有效评分唯一评论实体",
                "新版联合覆盖率 = 至少命中 E01–E28 任一脚本的有正文实体 / 有正文实体",
                "脚本覆盖率 = 某脚本命中实体 / 全部有正文实体",
                "每个脚本：命中 + 未命中 + 失败 = 应执行样本",
                "脚本命中只代表规则发现文本证据，不自动构成业务结论",
            ],
        },
    }
    title = "RYOBI Mower｜新版 28 脚本量化报告"
    manifest = {
        "title": title,
        "version": 1,
        "surface": "report",
        "description": (
            "基于 4,327 条唯一评论实体的确定性量化报告；"
            "28 个原子脚本对全部有正文样本执行，LLM 关闭。"
        ),
        "generatedAt": report["generated_at"],
        "sources": [source],
        "blocks": [
            {"id": "title", "type": "markdown", "body": f"# {title}"},
            {
                "id": "executive_summary",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 执行摘要\n\n"
                    f"- E01–E28 均对 {samples['written_review_entities']:,} 条有正文实体执行，"
                    f"形成 {ledger['record_count']:,} 条执行状态，失败 {failure_total:,} 条。\n"
                    f"- {samples['atomic_v2_covered_entities']:,} 条至少命中一个新版脚本，"
                    f"联合覆盖率 {atomic_coverage:.1%}；"
                    f"{samples['atomic_v2_unmatched_entities']:,} 条全脚本未命中并已留痕。\n"
                    f"- 单脚本覆盖最高的是 {top_script['script_id']}「{top_script['name']}」"
                    f"（{top_script['matched']:,} 条，{top_script['coverage']:.1%}）。"
                    "覆盖率只说明规则找到文本，不代表准确率或业务重要度。\n"
                    f"- 总体平均星级 {average_rating:.2f}，好评率 {positive_rate:.1%}，"
                    f"差评率 {negative_rate:.1%}；这些数字与脚本命中分开计算。"
                ),
            },
            {
                "id": "headline_metrics",
                "type": "metric-strip",
                "cardIds": [
                    "sample_card",
                    "written_card",
                    "ledger_card",
                    "coverage_card",
                    "unmatched_card",
                    "rating_card",
                ],
            },
            {
                "id": "atomic_method",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 28 个脚本如何阅读\n\n"
                    "每个脚本对应一种最小原子信息：属性、部件、局部评价、否定、"
                    "故障、原因、后果、补救、任务、工况、用户明确特征、需求、比较、"
                    "购买驱动、智能能力或留存意向。所有脚本使用版本化正则规则，"
                    "对同一完整样本集执行；未命中和失败不会被丢弃。"
                ),
            },
            {
                "id": "script_chart_block",
                "type": "chart",
                "chartId": "script_coverage_chart",
            },
            {
                "id": "script_table_block",
                "type": "table",
                "tableId": "script_execution_table",
            },
            {
                "id": "label_method",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 原子标签分布\n\n"
                    "标签表展示每个脚本内部的可解释分类，例如故障与症状脚本中的"
                    "停止工作、破损、停转、切割不均等。单条评论可命中多个标签，"
                    "所以标签数量不能直接相加为唯一评论数。"
                ),
            },
            {
                "id": "label_table_block",
                "type": "table",
                "tableId": "atomic_label_table",
            },
            {
                "id": "satisfaction_method",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 评分基线\n\n"
                    "星级统计使用全部有效评分实体。局部评价脚本 E03 只统计文本中的"
                    "明确评价词；两者用途不同，不用星级替代句级语义。"
                ),
            },
            {
                "id": "rating_chart_block",
                "type": "chart",
                "chartId": "rating_chart",
            },
            {
                "id": "evidence_method",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 代表性原文证据\n\n"
                    "下表只用于人工核对规则、字符位置和标签。完整执行账本保存每条"
                    "评论对每个脚本的 matched / no_match / failed 状态及全部命中位置。"
                ),
            },
            {
                "id": "evidence_table_block",
                "type": "table",
                "tableId": "atomic_evidence_table",
            },
            {
                "id": "boundaries",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 结论边界\n\n"
                    "- 脚本命中是可追溯文本证据，不等于故障率、因果关系或路线图优先级。\n"
                    "- 年龄、角色和身体限制仅统计明确表达，不推断缺失人口属性。\n"
                    "- 渠道、型号和时间差异属于观察性描述，不解释为因果。\n"
                    "- verified purchaser 当前无法区分 false 与 missing。\n"
                    "- 当前版本不使用 LLM；规则外的同义表达仍可能漏检。"
                ),
            },
            {
                "id": "agent_bridge",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 与产品智能体的衔接\n\n"
                    "智能体先通过 `list_reports → get_report` 复用已完成报告，再用 "
                    "`query_report_metric` 和 `get_report_evidence` 查询数字与原文。"
                    "若范围或版本不匹配，才调用 `create_report`。智能体可以解释、组合"
                    "和提出后续验证问题，但不得在提示词里重新计算核心数字。"
                ),
            },
            {
                "id": "reproducibility",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 可复现标识\n\n"
                    f"- report_id: `{report['report_id']}`\n"
                    f"- analysis_version: `{report['analysis_version']}`\n"
                    f"- script_registry: `{registry['version']}`\n"
                    f"- data_snapshot_id: `{report['data_snapshot_id']}`\n"
                    f"- core_result: `{report['hashes']['core_result']}`\n"
                    f"- ledger: `{ledger['logical_content_sha256']}`"
                ),
            },
        ],
        "cards": [
            {
                "id": "sample_card",
                "dataset": "summary",
                "sourceId": source["id"],
                "description": "按来源业务键去重后的唯一评论实体。",
                "metrics": [
                    {
                        "label": "唯一评论实体",
                        "field": "sample_size",
                        "format": "number",
                    }
                ],
            },
            {
                "id": "written_card",
                "dataset": "summary",
                "sourceId": source["id"],
                "description": "标题或正文非空的唯一评论实体。",
                "metrics": [
                    {
                        "label": "有正文样本",
                        "field": "written_entities",
                        "format": "number",
                    }
                ],
            },
            {
                "id": "ledger_card",
                "dataset": "summary",
                "sourceId": source["id"],
                "description": "有正文样本 × 28 个脚本。",
                "metrics": [
                    {
                        "label": "执行状态",
                        "field": "ledger_records",
                        "format": "number",
                    }
                ],
            },
            {
                "id": "coverage_card",
                "dataset": "summary",
                "sourceId": source["id"],
                "description": "至少命中 E01–E28 任一脚本。",
                "metrics": [
                    {
                        "label": "联合覆盖率",
                        "field": "atomic_coverage",
                        "format": "percent",
                    }
                ],
            },
            {
                "id": "unmatched_card",
                "dataset": "summary",
                "sourceId": source["id"],
                "description": "全部 28 个脚本均未命中，但执行状态已保留。",
                "metrics": [
                    {
                        "label": "全脚本未命中",
                        "field": "unmatched_entities",
                        "format": "number",
                    }
                ],
            },
            {
                "id": "rating_card",
                "dataset": "summary",
                "sourceId": source["id"],
                "description": "全部有效评分实体的算术平均。",
                "metrics": [
                    {
                        "label": "平均星级",
                        "field": "average_rating",
                        "format": "number",
                    },
                    {
                        "label": "好评率",
                        "field": "positive_rate",
                        "format": "percent",
                    },
                ],
            },
        ],
        "charts": [
            {
                "id": "script_coverage_chart",
                "title": "28 个原子脚本样本覆盖率",
                "subtitle": (
                    "所有脚本分母均为 4,307 条有正文实体；"
                    "覆盖率不代表规则准确率或业务重要度。"
                ),
                "type": "bar",
                "dataset": "script_execution",
                "sourceId": source["id"],
                "encodings": {
                    "x": {
                        "field": "script_id",
                        "type": "nominal",
                        "label": "脚本",
                    },
                    "y": {
                        "field": "coverage",
                        "type": "quantitative",
                        "label": "样本覆盖率",
                        "format": "percent",
                    },
                },
                "valueFormat": "percent",
            },
            {
                "id": "rating_chart",
                "title": "星级分布",
                "subtitle": "分母为全部 4,327 条有效评分唯一评论实体。",
                "type": "bar",
                "dataset": "rating_distribution",
                "sourceId": source["id"],
                "encodings": {
                    "x": {
                        "field": "rating_label",
                        "type": "nominal",
                        "label": "星级",
                    },
                    "y": {
                        "field": "share",
                        "type": "quantitative",
                        "label": "评论占比",
                        "format": "percent",
                    },
                },
                "valueFormat": "percent",
            },
        ],
        "tables": [
            {
                "id": "script_execution_table",
                "title": "E01–E28 全样本执行明细",
                "subtitle": "应执行样本 = 命中 + 未命中 + 失败。",
                "dataset": "script_execution",
                "sourceId": source["id"],
                "defaultSort": {"field": "script_id", "direction": "asc"},
                "columns": [
                    {"field": "script_id", "label": "脚本", "type": "text"},
                    {"field": "name", "label": "原子信息", "type": "text"},
                    {"field": "eligible", "label": "应执行", "format": "number"},
                    {"field": "matched", "label": "命中", "format": "number"},
                    {"field": "no_match", "label": "未命中", "format": "number"},
                    {"field": "failed", "label": "失败", "format": "number"},
                    {"field": "atom_count", "label": "原子记录", "format": "number"},
                    {"field": "coverage", "label": "覆盖率", "format": "percent"},
                ],
                "density": "spacious",
                "layout": "full",
            },
            {
                "id": "atomic_label_table",
                "title": "脚本内部原子标签",
                "subtitle": "同一实体可命中多个标签；按命中实体数排序。",
                "dataset": "atomic_labels",
                "sourceId": source["id"],
                "defaultSort": {
                    "field": "review_entities",
                    "direction": "desc",
                },
                "columns": [
                    {"field": "script_id", "label": "脚本", "type": "text"},
                    {"field": "script_name", "label": "原子信息", "type": "text"},
                    {"field": "label", "label": "标签", "type": "text"},
                    {
                        "field": "review_entities",
                        "label": "命中实体",
                        "format": "number",
                    },
                    {"field": "coverage", "label": "覆盖率", "format": "percent"},
                ],
                "density": "spacious",
                "layout": "full",
            },
            {
                "id": "atomic_evidence_table",
                "title": "新版脚本代表性原文",
                "subtitle": "每个脚本最多展示 3 条；完整命中位置保存在执行账本。",
                "dataset": "atomic_evidence",
                "sourceId": source["id"],
                "defaultSort": {"field": "script_id", "direction": "asc"},
                "columns": [
                    {"field": "script_id", "label": "脚本", "type": "text"},
                    {"field": "script_name", "label": "原子信息", "type": "text"},
                    {"field": "matched_text", "label": "命中文本", "type": "text"},
                    {"field": "snippet", "label": "原句", "type": "text"},
                    {"field": "rating", "label": "星级", "format": "number"},
                    {"field": "model", "label": "型号", "type": "text"},
                    {"field": "channel", "label": "渠道", "type": "text"},
                ],
                "density": "dense",
                "layout": "full",
            },
        ],
    }
    snapshot = {
        "version": 1,
        "status": "ready",
        "generatedAt": report["generated_at"],
        "datasets": {
            "summary": [
                {
                    "sample_size": sample_size,
                    "written_entities": samples["written_review_entities"],
                    "ledger_records": ledger["record_count"],
                    "atomic_coverage": atomic_coverage,
                    "unmatched_entities": samples["atomic_v2_unmatched_entities"],
                    "average_rating": average_rating,
                    "positive_rate": positive_rate,
                    "negative_rate": negative_rate,
                }
            ],
            "script_execution": execution,
            "atomic_labels": labels,
            "rating_distribution": ratings,
            "atomic_evidence": atomic_evidence,
        },
    }
    return {
        "manifest": manifest,
        "snapshot": snapshot,
        "sources": [source],
        "surface": "report",
        "package_info": {
            "report_id": report["report_id"],
            "schema_version": report["schema_version"],
            "analysis_version": report["analysis_version"],
            "data_snapshot_id": report["data_snapshot_id"],
            "core_result_hash": report["hashes"]["core_result"],
            "ledger_hash": ledger["logical_content_sha256"],
        },
    }
