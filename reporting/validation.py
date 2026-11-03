from __future__ import annotations

from typing import Any

from reporting.config import MARKET_REGIONS
from reporting.source import canonical_json, sha256_text


class ReportValidationError(ValueError):
    """Raised when a generated Report JSON violates internal references."""


CORE_KEYS = (
    "schema_version",
    "analysis_version",
    "research_plan",
    "data_snapshot_id",
    "scope",
    "samples",
    "metric_definitions",
    "metrics",
    "tables",
    "charts",
    "findings",
    "evidence",
    "limitations",
    "sections",
    "method",
)
OPTIONAL_CORE_KEYS = (
    "quality",
    "product_insight",
    "product_insight_availability",
)


def report_core(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: report[key]
        for key in (*CORE_KEYS, *OPTIONAL_CORE_KEYS)
        if key in report
    }


def recompute_report_identity(report: dict[str, Any]) -> dict[str, Any]:
    core_hash = sha256_text(canonical_json(report_core(report)))
    report["report_id"] = f"rpt_{core_hash[:24]}"
    report.setdefault("hashes", {})["core_result"] = f"sha256:{core_hash}"
    return report


def _unique(items: list[dict[str, Any]], key: str) -> set[str]:
    values = [str(item.get(key) or "") for item in items]
    if any(not value for value in values):
        raise ReportValidationError(f"{key} 不能为空")
    if len(values) != len(set(values)):
        raise ReportValidationError(f"{key} 必须唯一")
    return set(values)


def validate_report(report: dict[str, Any]) -> None:
    required = {
        "report_id",
        "title",
        "schema_version",
        "analysis_version",
        "status",
        "generated_at",
        "hashes",
        *CORE_KEYS,
    }
    missing = sorted(required - set(report))
    if missing:
        raise ReportValidationError(f"Report JSON 缺少字段：{', '.join(missing)}")
    if report["schema_version"] != "report-json/v1":
        raise ReportValidationError("不支持的 Report JSON schema_version")
    if report["status"] != "completed":
        raise ReportValidationError("只有 completed 报告可发布")
    if report.get("method", {}).get("llm_used") is not False:
        raise ReportValidationError("当前分析版本要求 llm_used=false")
    market_contract = report.get("method", {}).get("market_contract")
    scope_regions = report.get("scope", {}).get("market_regions") or []
    unknown_regions = sorted(set(scope_regions) - set(MARKET_REGIONS))
    if unknown_regions:
        raise ReportValidationError("Report scope 包含未知市场地区")
    if market_contract:
        if market_contract.get("mode") != "current":
            raise ReportValidationError("新市场契约 mode 必须为 current")
        region_rows = report.get("samples", {}).get(
            "market_region_summary"
        ) or []
        region_total = sum(
            int(row.get("eligible_valid_text_entities") or 0)
            for row in region_rows
        )
        if region_total != int(
            report.get("samples", {}).get("review_entities") or 0
        ):
            raise ReportValidationError(
                "地区样本构成必须等于最终有效评论实体数"
            )
        if any(
            row.get("market_region") not in MARKET_REGIONS
            for row in region_rows
        ):
            raise ReportValidationError("地区样本构成包含未知市场地区")

    metric_ids = _unique(report["metrics"], "metric_id")
    definition_ids = _unique(report["metric_definitions"], "metric_id")
    if not metric_ids <= definition_ids:
        raise ReportValidationError("存在没有指标定义的 metric_id")
    table_ids = _unique(report["tables"], "table_id")
    chart_ids = _unique(report["charts"], "chart_id")
    evidence_ids = _unique(report["evidence"], "evidence_id")
    if market_contract:
        market_evidence = list(report["evidence"])
        market_evidence.extend(
            (report.get("product_insight") or {}).get("evidence") or []
        )
        for evidence in market_evidence:
            if evidence.get("market_region") not in MARKET_REGIONS:
                raise ReportValidationError(
                    "新契约报告证据缺少规范 market_region"
                )
            if not str(
                evidence.get("market_region_evidence") or ""
            ).strip():
                raise ReportValidationError(
                    "新契约报告证据缺少 market_region_evidence"
                )
    _unique(report["findings"], "finding_id")
    _unique(report["sections"], "section_id")

    for chart in report["charts"]:
        if chart["table_id"] not in table_ids:
            raise ReportValidationError(
                f"图表引用不存在的表：{chart['table_id']}"
            )
    for section in report["sections"]:
        if not set(section.get("table_refs") or []) <= table_ids:
            raise ReportValidationError(
                f"章节 {section['section_id']} 引用不存在的表"
            )
        if not set(section.get("chart_refs") or []) <= chart_ids:
            raise ReportValidationError(
                f"章节 {section['section_id']} 引用不存在的图表"
            )
        if not set(section.get("metric_refs") or []) <= metric_ids:
            raise ReportValidationError(
                f"章节 {section['section_id']} 引用不存在的指标"
            )
    for finding in report["findings"]:
        if not set(finding.get("table_refs") or []) <= table_ids:
            raise ReportValidationError(
                f"发现 {finding['finding_id']} 引用不存在的表"
            )
        if not set(finding.get("metric_refs") or []) <= metric_ids:
            raise ReportValidationError(
                f"发现 {finding['finding_id']} 引用不存在的指标"
            )
        if not set(finding.get("evidence_refs") or []) <= evidence_ids:
            raise ReportValidationError(
                f"发现 {finding['finding_id']} 引用不存在的证据"
            )

    core_hash = sha256_text(canonical_json(report_core(report)))
    expected_hash = f"sha256:{core_hash}"
    if report.get("hashes", {}).get("core_result") != expected_hash:
        raise ReportValidationError("core_result 哈希与报告内容不一致")
    if report["report_id"] != f"rpt_{core_hash[:24]}":
        raise ReportValidationError("report_id 与核心结果哈希不一致")
