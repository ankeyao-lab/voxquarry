from __future__ import annotations

from reporting.product_form import FORM_LABELS

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote

from reporting.controlled_energy import ENERGY_LABELS, ENERGY_TYPES


def _text(value: Any, fallback: str = "—") -> str:
    text = str(value or "").strip()
    return text or fallback


def _number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _items(values: Iterable[Any] | None) -> str:
    rows = [_text(value, "") for value in (values or [])]
    rows = [value for value in rows if value]
    return "、".join(rows) if rows else "全部"


def _voltage_platform_scope(scope: dict[str, Any]) -> str:
    platforms = [
        _text(row.get("display_name"), "")
        for row in scope.get("voltage_platforms") or []
        if isinstance(row, dict)
    ]
    platforms = [value for value in platforms if value]
    if platforms:
        text = "、".join(platforms)
        return (
            f"{text} + 未知 / 未绑定"
            if scope.get("include_unknown_voltage_platform") else text
        )
    if scope.get("include_unknown_voltage_platform"):
        return "未知 / 未绑定"
    if "voltage_platform_keys" in scope:
        return "全部"
    return "未明确（历史报告）"


def _energy_scope(scope: dict[str, Any]) -> str:
    energy = scope.get("energy")
    if isinstance(energy, dict) and _text(energy.get("label"), ""):
        return (
            f"{_text(energy.get('label'))}"
            f"（{_text(energy.get('energy_type'))}）"
        )
    energy_type = _text(scope.get("energy_type"), "")
    if energy_type:
        return energy_type
    return "未知（历史报告未保存）"


def report_category(report: dict[str, Any]) -> str:
    scope = report.get("scope") or {}
    insight = report.get("product_insight") or {}
    categories = scope.get("categories") or []
    return _text(
        categories[0] if categories else insight.get("category_label"),
        "Unknown Category",
    )


def _generated_stamp(report: dict[str, Any], format_string: str) -> str:
    generated = _text(report.get("generated_at"), "")
    try:
        return datetime.fromisoformat(
            generated.replace("Z", "+00:00")
        ).astimezone(timezone.utc).strftime(format_string)
    except ValueError:
        return datetime.now(timezone.utc).strftime(format_string)


def report_export_title(report: dict[str, Any]) -> str:
    return "｜".join(
        (
            _text(report.get("title"), "VoxQuarry 量化报告"),
            report_category(report),
            _generated_stamp(report, "%Y-%m-%d"),
            _text(report.get("analysis_version"), "legacy"),
        )
    )


def report_export_basename(report: dict[str, Any]) -> str:
    stamp = _generated_stamp(report, "%Y%m%d-%H%M")
    raw = "-".join(
        (
            _text(report.get("title"), "VoxQuarry Report"),
            report_category(report),
            stamp,
            _text(report.get("analysis_version"), "legacy"),
        )
    )
    normalized = unicodedata.normalize("NFKC", raw)
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", normalized)
    safe = re.sub(r"\s+", " ", safe).strip(" .-_")
    return safe[:160] or f"VoxQuarry-Report-{stamp}"


def content_disposition(report: dict[str, Any], extension: str) -> str:
    basename = report_export_basename(report)
    ascii_name = unicodedata.normalize("NFKD", basename).encode(
        "ascii", "ignore"
    ).decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "-", ascii_name).strip(" .-")
    if not ascii_name:
        ascii_name = "VoxQuarry-Report"
    extension = extension.lstrip(".")
    return (
        f'attachment; filename="{ascii_name}.{extension}"; '
        f"filename*=UTF-8''{quote(f'{basename}.{extension}')}"
    )


def report_to_markdown(report: dict[str, Any]) -> str:
    scope = report.get("scope") or {}
    samples = report.get("samples") or {}
    insight = report.get("product_insight") or {}
    insight_sample = insight.get("sample") or {}
    lines = [
        f"# {report_export_title(report)}",
        "",
        f"- 品类：{report_category(report)}",
        f"- 生成时间：{_text(report.get('generated_at'))}",
        f"- 报告 ID：{_text(report.get('report_id'))}",
        f"- 分析版本：{_text(report.get('analysis_version'))}",
        f"- 数据快照：{_text(report.get('data_snapshot_id'))}",
        f"- 品牌：{_items(scope.get('brands'))}",
        f"- 产品形态：{_items([FORM_LABELS.get(value, value) for value in scope.get('product_form_factors') or []])}",
        f"- 渠道：{_items(scope.get('channels'))}",
        f"- 型号：{_items(scope.get('models'))}",
        f"- 来源市场：{_items(scope.get('market_regions'))}",
        f"- 电压平台：{_voltage_platform_scope(scope)}",
        f"- 能源类型：{_energy_scope(scope)}",
        "",
        "## 数据概览",
        "",
        f"- 唯一评论实体：{_number(samples.get('review_entities') or insight_sample.get('review_entities'))}",
        f"- 正文实体：{_number(samples.get('written_review_entities') or insight_sample.get('written_review_entities'))}",
        f"- 平均评分：{_text(samples.get('average_rating') or insight_sample.get('average_rating'))}",
        f"- 好评率：{_percent(samples.get('positive_rate') or insight_sample.get('positive_rate'))}",
        f"- 差评率：{_percent(samples.get('negative_rate') or insight_sample.get('negative_rate'))}",
        "",
    ]
    summary = _text(report.get("summary_text"), "")
    if summary:
        lines.extend(["## 决策摘要", "", summary, ""])

    market_regions = insight_sample.get("market_regions") or []
    if market_regions:
        lines.extend(["## 来源市场样本构成", ""])
        for row in market_regions:
            lines.append(
                f"- {_text(row.get('market_region'))}："
                f"{_number(row.get('eligible_valid_text_entities'))} 条正文实体，"
                f"占比 {_percent(row.get('share'))}"
            )
        lines.append("")

    source_completeness = (
        (report.get("quality") or {}).get("source_completeness") or []
    )
    limited_sources = [
        row for row in source_completeness if row.get("is_limited_source")
    ]
    if limited_sources:
        lines.extend(["## 数据完整性", ""])
        for row in limited_sources:
            lines.append(
                "- 有限来源样本：已收集 "
                f"{_number(row.get('collected_source_unique_reviews'))}/"
                f"已声明 {_number(row.get('declared_source_total'))}/"
                f"缺 {_number(row.get('missing_review_count'))}；"
                f"任务 {_text(row.get('job_id'))}；"
                f"原因 {_text(row.get('stop_reason'))}"
            )
        lines.append("")

    non_reportable_exclusions = (
        (report.get("quality") or {}).get("non_reportable_exclusions") or []
    )
    if non_reportable_exclusions:
        lines.extend(["## 未进入报告的数据", ""])
        for row in non_reportable_exclusions:
            lines.append(
                "- 旧部分采集契约不受支持："
                f"任务 {_text(row.get('job_id'))}，"
                f"{_number(row.get('excluded_records'))}条未进入当前快照/报告；"
                "不计入任何指标分母。"
            )
        lines.append("")

    energy_selection = scope.get("energy_selection")
    if isinstance(energy_selection, dict):
        task_counts = energy_selection.get("category_energy_task_counts") or {}
        review_counts = energy_selection.get("category_energy_review_counts") or {}
        review_total = sum(int(review_counts.get(code) or 0) for code in ENERGY_TYPES)
        lines.extend([
            "## 能源筛选审计",
            "",
            f"- 当前能源可报告任务：{_number(energy_selection.get('category_energy_job_count'))}",
            f"- 本报告纳入任务：{_number(energy_selection.get('selected_job_count'))}",
            f"- 排除其他能源任务：{_number(energy_selection.get('excluded_other_energy_job_count'))}",
            f"- 其中旧任务空值映射为未知：{_number(energy_selection.get('selected_legacy_null_job_count'))}",
            "- 品类能源任务分布：" + "；".join(
                f"{ENERGY_LABELS[code]} {_number(task_counts.get(code))}"
                for code in ENERGY_TYPES
            ),
            "- 品类能源评论分布：" + "；".join(
                f"{ENERGY_LABELS[code]} {_number(review_counts.get(code))}"
                + (
                    f"（{int(review_counts.get(code) or 0) / review_total:.1%}）"
                    if review_total else "（0.0%）"
                )
                for code in ENERGY_TYPES
            ),
            "",
        ])

    personas = insight.get("personas") or []
    if personas:
        lines.extend(["## 有证据支持的用户群", ""])
        for row in personas:
            lines.extend(
                [
                    f"### {_text(row.get('title'))}",
                    "",
                    f"- 用户任务：{_text(row.get('jtbd'))}",
                    f"- 证据实体：{_number(row.get('review_entities'))}",
                    f"- 边界：{_text(row.get('boundary'))}",
                    "",
                ]
            )

    aspects = insight.get("aspect_experience") or []
    if aspects:
        lines.extend(["## Pros & Cons", ""])
        for row in aspects:
            lines.append(
                f"- **{_text(row.get('label'))}**：提及 "
                f"{_number(row.get('review_entities'))}；正向 "
                f"{_number(row.get('explicit_positive_entities'))}；负向 "
                f"{_number(row.get('explicit_negative_entities'))}"
            )
        lines.append("")

    kano = insight.get("kano") or []
    if kano:
        lines.extend(["## Kano 候选", ""])
        for row in kano:
            lines.append(
                f"- **{_text(row.get('label') or row.get('title'))}**："
                f"{_text(row.get('classification') or row.get('kano_type'))}；"
                f"证据实体 {_number(row.get('review_entities'))}"
            )
        lines.append("")

    opportunities = insight.get("product_opportunities") or []
    if opportunities:
        lines.extend(["## 产品机会", ""])
        for row in opportunities:
            lines.extend(
                [
                    f"### {_text(row.get('title'))}",
                    "",
                    f"- 用户问题：{_text(row.get('problem'))}",
                    f"- 目标结果：{_text(row.get('desired_outcome') or row.get('opportunity'))}",
                    f"- 证据实体：{_number(row.get('review_entities') or row.get('source_problem_entities'))}",
                    "",
                ]
            )

    smart = insight.get("smart_opportunities") or []
    if smart:
        lines.extend(["## 智能化机会", ""])
        for row in smart:
            lines.extend(
                [
                    f"### {_text(row.get('code'))} · {_text(row.get('title'))}",
                    "",
                    f"- 方法类型：{_text(row.get('method'))}",
                    f"- 用户需求：{_text(row.get('user_need') or row.get('value_hypothesis'))}",
                    f"- 智能功能：{_text(row.get('smart_function') or row.get('value_hypothesis'))}",
                    f"- 证据基础：{_text(row.get('claim_basis'))}；{_number(row.get('source_problem_entities'))} 条问题评论",
                    f"- 所需数据：{_text(row.get('required_data'))}",
                    f"- 产品护栏：{_text(row.get('guardrail'))}",
                    "",
                ]
            )

    limitations = insight.get("limitations") or report.get("limitations") or []
    if limitations:
        lines.extend(["## 边界与限制", ""])
        lines.extend(
            f"- {_text(row.get('message')) if isinstance(row, dict) else _text(row)}"
            for row in limitations
        )
        lines.append("")

    evidence = insight.get("evidence_access") or {}
    if evidence:
        lines.extend(
            [
                "## 证据资产",
                "",
                f"- 完整证据行：{_number(evidence.get('evidence_rows'))}",
                f"- 证据组：{_number(evidence.get('evidence_groups'))}",
                f"- 评论实体：{_number(evidence.get('unique_review_entities'))}",
                "",
                "> 完整原文证据请通过报告详情页的 Evidence CSV / NDJSON 导出。",
                "",
            ]
        )

    lines.extend(
        [
            "---",
            "",
            "本文件由 VoxQuarry 基于不可变报告 JSON 确定性导出；导出过程不重新计算核心指标。",
            "",
        ]
    )
    return "\n".join(lines)
