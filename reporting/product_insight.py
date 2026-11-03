from __future__ import annotations

import gzip
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sized

from reporting.evidence_store import evidence_manifest
from reporting.category_vocabulary import (
    canonical_category_name,
    category_id_for,
)
from reporting.ope_product_report import (
    build_ope_product_report,
    category_profile,
)
from reporting.source_completeness import limited_source_message
from reporting.validation import recompute_report_identity


PRODUCT_INSIGHT_VERSION = "product-insight-bundle/2.6.0"
REPORT_READINESS_THRESHOLDS = {
    "full": 3000,
    "limited": 1000,
    "pilot": 500,
    "exploratory": 1,
}
PUBLISHABLE_READINESS_STATUSES = frozenset({"full", "limited", "pilot", "quantitative", "exploratory"})


def report_readiness_status(valid_text_reviews: int) -> str:
    """Classify a report scope by its deduplicated eligible text evidence."""

    count = max(0, int(valid_text_reviews))
    if count >= REPORT_READINESS_THRESHOLDS["full"]:
        return "full"
    if count >= REPORT_READINESS_THRESHOLDS["limited"]:
        return "limited"
    if count >= REPORT_READINESS_THRESHOLDS["pilot"]:
        return "pilot"
    if count:
        return "exploratory"
    return "no_data"


def scope_category(report: dict[str, Any]) -> tuple[str | None, str | None]:
    scope = report.get("scope") or {}
    explicit_category_id = str(scope.get("category_id") or "").strip()
    categories = [
        str(value).strip()
        for value in scope.get("categories") or []
        if str(value).strip()
    ]
    category = categories[0] if len(categories) == 1 else None
    category_id = explicit_category_id or category_id_for(category or "")
    if category_id and category:
        return category_id, canonical_category_name(category)
    if category_id:
        profile = category_profile(category_id)
        return category_id, profile.category_label if profile else None
    return None, category


def product_insight_support(
    report: dict[str, Any],
    *,
    valid_text_reviews: int | None = None,
    eligible_reviews: int | None = None,
) -> dict[str, Any]:
    category_id, category = scope_category(report)
    profile = category_profile(
        category_id or category or "",
        product_form_factors=(report.get("scope") or {}).get("product_form_factors"),
    )
    if profile:
        status = (
            "full"
            if valid_text_reviews is None
            else report_readiness_status(valid_text_reviews)
        )
        text_status = status
        if (
            eligible_reviews is not None
            and eligible_reviews >= REPORT_READINESS_THRESHOLDS["pilot"]
            and status in {"exploratory", "insufficient", "no_data"}
        ):
            status = "quantitative"
        can_generate = (
            valid_text_reviews is None
            or status in PUBLISHABLE_READINESS_STATUSES
        )
        reason = None
        if status == "exploratory":
            reason = (
                "当前范围只有 "
                f"{int(valid_text_reviews or 0):,} 条去重后合规有效正文，"
                "仅适用于探索性分析，不代表总体。"
            )
        elif status == "no_data":
            reason = "当前范围没有通过资格、正文和完整性门禁的评论。"
        return {
            "status": status,
            "category_id": profile.category_id,
            "category": profile.category_label,
            "category_profile_id": profile.profile_id,
            "valid_text_reviews": valid_text_reviews,
            "eligible_reviews": eligible_reviews,
            "text_readiness_status": text_status,
            "admission_basis": "deduplicated_eligible_reviews" if eligible_reviews is not None else "deduplicated_text_reviews",
            "thresholds": dict(REPORT_READINESS_THRESHOLDS),
            "can_generate": can_generate,
            "reason": reason,
        }
    if category:
        return {
            "status": "unsupported",
            "category_id": category_id,
            "category": category,
            "category_profile_id": None,
            "valid_text_reviews": valid_text_reviews,
            "thresholds": dict(REPORT_READINESS_THRESHOLDS),
            "can_generate": False,
            "reason": (
                "该品类尚未注册版本化提取 Profile；系统只生成完整报告，"
                "因此本次报告生成已阻止。"
            ),
        }
    return {
        "status": "unsupported",
        "category_id": None,
        "category": None,
        "category_profile_id": None,
        "valid_text_reviews": valid_text_reviews,
        "thresholds": dict(REPORT_READINESS_THRESHOLDS),
        "can_generate": False,
        "reason": (
            "完整产品洞察要求单一品类；系统不会为多品类范围生成降级报告。"
        ),
    }


def build_product_insight_bundle(
    facts: list[dict[str, Any]],
    source_report: dict[str, Any],
    *,
    enforce_readiness: bool = False,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    Iterable[dict[str, Any]],
]:
    text_entity_ids = {
        fact.get("review_entity_id") or fact.get("review_occurrence_id")
        for fact in facts
        if fact.get("has_text")
        and (
            fact.get("business_key_status") == "incomplete"
            or int(fact.get("canonical_entity_rank") or 1) == 1
        )
    }
    written_entity_count = len(text_entity_ids)
    support = product_insight_support(
        source_report,
        valid_text_reviews=(
            written_entity_count if enforce_readiness else None
        ),
        eligible_reviews=(len({fact.get("review_entity_id") or fact.get("review_occurrence_id") for fact in facts}) if enforce_readiness else None),
    )
    source_report["product_insight_availability"] = support
    source_report.setdefault("method", {}).update(
        {
            "runtime_llm_used": False,
            "narrative_origin": "codex_authored_template",
            "third_party_llm_api_used": False,
            "core_metrics_modified_by_llm": False,
        }
    )
    if not support["can_generate"]:
        raise ValueError(str(support["reason"]))
    if support["status"] != "full":
        source_report.setdefault("limitations", []).append(
            {
                "code": f"report_readiness_{support['status']}",
                "severity": "material",
                "message": (
                    (
                        f"本报告可用于基础量化分析：去重后有效评论 {support['eligible_reviews']:,} 条，"
                        f"其中有效正文 {written_entity_count:,} 条。正文少于 500 条，"
                        "细分场景和需求结论仅作为探索性线索；未提及不代表没有该需求。"
                    ) if support["status"] == "quantitative" else (
                        f"本报告为 {support['status']} 质量等级："
                        f"去重后有效正文 {written_entity_count:,} 条；"
                        f"充分正文样本门槛为 {REPORT_READINESS_THRESHOLDS['full']:,} 条。"
                    )
                ),
            }
        )

    insight, ledger, groups = build_ope_product_report(
        facts,
        source_report,
        include_evidence_groups=True,
    )
    source_limit_message = limited_source_message(
        (source_report.get("quality") or {}).get("source_completeness") or []
    )
    if source_limit_message:
        insight.setdefault("limitations", []).insert(0, source_limit_message)
    full_evidence = list(insight["evidence"])
    manifest = evidence_manifest(full_evidence, groups)
    representative_ids = _representative_evidence_ids(insight)
    insight["evidence"] = [
        row
        for row in full_evidence
        if row["evidence_id"] in representative_ids
    ]
    insight["evidence_access"] = {
        **manifest,
        "default_page_size": 50,
        "maximum_page_size": 100,
        "query_endpoint": (
            "/reporting/api/v1/reports/{report_id}/"
            "evidence-groups/{evidence_group_id}"
        ),
        "export_endpoint": (
            "/reporting/api/v1/reports/{report_id}/"
            "evidence-groups/{evidence_group_id}/export"
        ),
    }
    insight["bundle_version"] = PRODUCT_INSIGHT_VERSION
    source_report["product_insight"] = insight
    source_report["report_type"] = "product_insight"
    recompute_report_identity(source_report)
    return source_report, full_evidence, groups, ledger


def write_extraction_ledger(
    root: Path,
    report_id: str,
    rows: Iterable[dict[str, Any]] | Sized,
) -> dict[str, Any] | None:
    row_count = len(rows) if isinstance(rows, Sized) else None
    if row_count == 0:
        return None
    target_dir = root / "ope-ledgers"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{report_id}.jsonl.gz"
    if target.exists():
        return {
            "path": str(target),
            "rows": row_count,
            "bytes": target.stat().st_size,
        }
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{report_id}-",
        suffix=".jsonl.gz",
        dir=target_dir,
        delete=False,
    )
    temporary = Path(handle.name)
    handle.close()
    try:
        with temporary.open("wb") as raw_handle:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_handle,
                mtime=0,
            ) as gzip_handle:
                with io.TextIOWrapper(
                    gzip_handle, encoding="utf-8"
                ) as text_handle:
                    for row in rows:
                        text_handle.write(
                            json.dumps(
                                row,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            + "\n"
                        )
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": str(target),
        "rows": row_count,
        "bytes": target.stat().st_size,
    }


def _representative_evidence_ids(value: Any) -> set[str]:
    values: set[str] = set()

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.endswith("evidence_ids") and isinstance(child, list):
                    values.update(str(entry) for entry in child)
                elif key != "evidence":
                    collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    return values
