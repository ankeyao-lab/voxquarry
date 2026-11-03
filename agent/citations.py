from __future__ import annotations

from typing import Any, Mapping

from .contracts import AgentError, new_id, normalize_text


def validate_claims(
    claims: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    validation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not validation.get("valid"):
        raise AgentError("citation_invalid", "引用未通过服务端重读校验", http_status=409)
    validation_items = validation.get("items")
    if not isinstance(validation_items, list):
        raise AgentError("citation_invalid", "引用校验结果不完整", http_status=409)
    valid_ids = {
        item.get("citation_id")
        for item in validation_items
        if isinstance(item, dict) and item.get("valid") is True
    }
    citation_by_id = {
        item.get("citation_id"): item
        for item in citations
        if isinstance(item, dict) and item.get("citation_id")
    }
    normalized: list[dict[str, Any]] = []
    for raw in claims:
        if not isinstance(raw, dict):
            raise AgentError("citation_invalid", "关键结论形状无效", http_status=409)
        required = {
            "text",
            "report_id",
            "metric_id",
            "evidence_group_id",
            "citation_ids",
        }
        if set(raw) != required:
            raise AgentError("citation_invalid", "关键结论字段不完整", http_status=409)
        text = normalize_text(raw["text"], field="claim.text", maximum=2000)
        report_id = normalize_text(raw["report_id"], field="claim.report_id", maximum=100)
        metric_id = normalize_text(raw["metric_id"], field="claim.metric_id", maximum=160)
        group_id = normalize_text(
            raw["evidence_group_id"], field="claim.evidence_group_id", maximum=180
        )
        ids = raw["citation_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(value, str) for value in ids):
            raise AgentError("citation_invalid", "关键结论 citation_ids 无效", http_status=409)
        if not set(ids).issubset(valid_ids) or not set(ids).issubset(citation_by_id):
            raise AgentError("citation_invalid", "关键结论包含未验证引用", http_status=409)
        selected = [citation_by_id[value] for value in ids]
        if any(item.get("report_id") != report_id for item in selected):
            raise AgentError("citation_invalid", "关键结论与引用报告范围不一致", http_status=409)
        metric_refs = [
            item
            for item in selected
            if item.get("citation_type") == "metric" and item.get("metric_id") == metric_id
        ]
        evidence_refs = [
            item
            for item in selected
            if item.get("citation_type") in {"evidence_group", "evidence"}
            and item.get("evidence_group_id") == group_id
        ]
        if not metric_refs or not evidence_refs:
            raise AgentError(
                "insufficient_evidence",
                "关键结论必须同时引用指标和 Evidence group",
                http_status=409,
            )
        normalized.append(
            {
                "claim_id": new_id("clm"),
                "text": text,
                "report_id": report_id,
                "metric_id": metric_id,
                "evidence_group_id": group_id,
                "citation_ids": ids,
            }
        )
    if not normalized:
        raise AgentError("insufficient_evidence", "没有可验证关键结论", http_status=409)
    return normalized
