from __future__ import annotations

from typing import Any, Iterable, Iterator


SOURCE_COMPLETENESS_CONTRACT_VERSION = "source-completeness-audit/v1"
DECLARED_INCOMPLETE_STOP_REASON = "external_import_declared_incomplete"
EXTERNAL_IMPORT_VALIDATION_SCHEMA_VERSION = (
    "voxquarry.external-review-import-validation/v1"
)
EXTERNAL_IMPORT_SCHEMA_VERSION = "voxquarry.review-import/v2"
SUPPORTED_EXTERNAL_IMPORT_SCHEMA_VERSIONS = {
    "voxquarry.review-import/v1",
    EXTERNAL_IMPORT_SCHEMA_VERSION,
}
EXTERNAL_IMPORT_CHANNELS = {
    "homedepot", "amazon_live", "walmart", "ace", "harborfreight",
    "husqvarna", "stihl", "tractorsupply",
}


class SourceCompletenessError(ValueError):
    """Raised when a source-completeness claim cannot be proven."""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _integer(
    report: dict[str, Any],
    field: str,
    *,
    fallback: str | None = None,
    strict: bool = False,
) -> int:
    value = report.get(field)
    if value is None and fallback:
        value = report.get(fallback)
    if isinstance(value, bool):
        raise SourceCompletenessError(f"{field} must be a non-negative integer")
    if strict and not isinstance(value, int):
        raise SourceCompletenessError(f"{field} must be an explicit integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise SourceCompletenessError(
            f"{field} must be a non-negative integer"
        ) from error
    if number < 0:
        raise SourceCompletenessError(f"{field} must be a non-negative integer")
    return number


def source_completeness_audit(
    job: dict[str, Any],
    report: dict[str, Any],
    *,
    csv_eligible_rows: int | None = None,
    raw_source_rows: int | None = None,
) -> dict[str, Any]:
    """Validate and normalize one job's declared source completeness.

    ``partial_success`` is intentionally accepted through one narrow governed
    path only.  Every other incomplete or contradictory claim fails closed.
    """

    job_id = _clean(job.get("id"))
    collection_status = _clean(job.get("status"))
    validation_status = _clean(report.get("status"))
    partial_status_claim = (
        collection_status == "partial_success"
        or validation_status == "PARTIAL_SUCCESS"
    )
    validation_schema_version = _clean(
        report.get("validation_schema_version")
    )
    notes = report.get("notes") or {}
    if not isinstance(notes, dict):
        raise SourceCompletenessError("validation notes must be an object")
    import_schema_version = _clean(notes.get("import_schema_version"))
    bounded = validation_schema_version == "voxquarry.bounded-collection/v1"
    if partial_status_claim:
        if bounded:
            from collector.bounded import STOP_REASONS
            if report.get("channel") not in {"homedepot", "walmart"} or notes.get("collection_schema_version") != validation_schema_version or report.get("stop_reason") not in STOP_REASONS:
                raise SourceCompletenessError("invalid bounded collection contract")
        if not bounded and (
            validation_schema_version
            != EXTERNAL_IMPORT_VALIDATION_SCHEMA_VERSION
        ):
            raise SourceCompletenessError(
                "partial validation_schema_version must be "
                f"{EXTERNAL_IMPORT_VALIDATION_SCHEMA_VERSION}"
            )
        if not bounded and import_schema_version not in SUPPORTED_EXTERNAL_IMPORT_SCHEMA_VERSIONS:
            raise SourceCompletenessError(
                "partial import_schema_version must be a supported governed import schema"
            )
        report_channel = _clean(report.get("channel"))
        job_channel = _clean(job.get("channel"))
        if report_channel not in EXTERNAL_IMPORT_CHANNELS:
            raise SourceCompletenessError(
                "partial validation channel is not a governed external import"
            )
        if job_channel and report_channel != job_channel:
            raise SourceCompletenessError(
                "partial validation channel does not match the task"
            )
        declared = _integer(report, "source_total", strict=True)
        collected = _integer(
            report, "source_records_collected", strict=True
        )
        source_unique = _integer(
            report, "source_unique_reviews", strict=True
        )
        if collected != source_unique:
            raise SourceCompletenessError(
                "source_records_collected must equal source_unique_reviews"
            )
    else:
        declared = _integer(
            report, "declared_source_total", fallback="source_total"
        )
        collected = _integer(
            report,
            "collected_source_unique_reviews",
            fallback="source_unique_reviews",
        )
    eligible = _integer(report, "eligible_reviews", fallback="unique_reviews")
    excluded = _integer(report, "excluded_reviews")
    if collected != eligible + excluded:
        raise SourceCompletenessError(
            "collected_source_unique_reviews must equal eligible_reviews + "
            "excluded_reviews"
        )
    if csv_eligible_rows is not None and int(csv_eligible_rows) != eligible:
        raise SourceCompletenessError(
            "CSV eligible rows do not reconcile with eligible_reviews"
        )
    if raw_source_rows is not None and int(raw_source_rows) != collected:
        raise SourceCompletenessError(
            "raw source rows do not reconcile with "
            "collected_source_unique_reviews"
        )
    if declared < collected:
        raise SourceCompletenessError(
            "declared_source_total must be greater than or equal to collected"
        )

    missing = declared - collected
    if partial_status_claim:
        normalized_missing = _integer(
            report, "source_missing_records", strict=True
        )
        if normalized_missing != missing:
            raise SourceCompletenessError(
                "source_missing_records must equal source_total - "
                "source_records_collected"
            )
        unique_reviews = _integer(report, "unique_reviews", strict=True)
        if unique_reviews != eligible:
            raise SourceCompletenessError(
                "unique_reviews must equal eligible_reviews"
            )
    elif report.get("missing_review_count") is not None:
        normalized_missing = _integer(report, "missing_review_count")
        if normalized_missing != missing:
            raise SourceCompletenessError(
                "missing_review_count must equal declared_source_total - "
                "collected_source_unique_reviews"
            )

    source_reconciled = report.get("source_reconciled")
    if not isinstance(source_reconciled, bool):
        # Older complete review-fact/v2 fixtures and immutable snapshots did
        # not persist this redundant flag.  A complete claim can be derived
        # only when both terminal statuses agree and the declared and
        # collected populations reconcile exactly.  The governed partial
        # path always requires an explicit false value.
        if (
            not partial_status_claim
            and collection_status == "completed"
            and validation_status in {"COMPLETE", "COMPLETE_FALLBACK_SOURCE"}
            and missing == 0
        ):
            source_reconciled = True
        else:
            raise SourceCompletenessError(
                "source_reconciled must be an explicit boolean"
            )
    stop_reason = _clean(report.get("stop_reason"))
    partial_claim = partial_status_claim or source_reconciled is False
    if partial_claim:
        if collection_status != "partial_success":
            raise SourceCompletenessError(
                "partial validation requires 任务状态 partial_success"
            )
        if validation_status != "PARTIAL_SUCCESS":
            raise SourceCompletenessError(
                "partial job requires validation status PARTIAL_SUCCESS"
            )
        if collected <= 0:
            raise SourceCompletenessError(
                "partial source requires collected_source_unique_reviews > 0"
            )
        if source_reconciled is not False:
            raise SourceCompletenessError(
                "partial source_reconciled must remain false"
            )
        if not bounded and stop_reason != DECLARED_INCOMPLETE_STOP_REASON:
            raise SourceCompletenessError(
                "partial stop_reason must be "
                f"{DECLARED_INCOMPLETE_STOP_REASON}"
            )
        if missing <= 0:
            raise SourceCompletenessError(
                "partial source must retain a positive missing_review_count"
            )
    else:
        if collection_status != "completed":
            raise SourceCompletenessError(
                "complete validation requires collection status completed"
            )
        if validation_status not in {"COMPLETE", "COMPLETE_FALLBACK_SOURCE"}:
            raise SourceCompletenessError(
                "completed job has unsupported validation status"
            )
        if source_reconciled is not True or missing != 0:
            raise SourceCompletenessError(
                "completed source must be fully reconciled"
            )

    return {
        "schema_version": SOURCE_COMPLETENESS_CONTRACT_VERSION,
        "job_id": job_id,
        "collection_status": collection_status,
        "validation_status": validation_status,
        "validation_schema_version": (
            validation_schema_version or None
        ),
        "import_schema_version": import_schema_version or None,
        "declared_source_total": declared,
        "collected_source_unique_reviews": collected,
        "missing_review_count": missing,
        "source_reconciled": source_reconciled,
        "stop_reason": (stop_reason or None) if partial_claim else None,
        "is_limited_source": partial_claim,
    }


def limited_source_message(audits: Iterable[dict[str, Any]]) -> str:
    limited = [row for row in audits if row.get("is_limited_source")]
    if not limited:
        return ""
    collected = sum(
        int(row.get("collected_source_unique_reviews") or 0) for row in limited
    )
    declared = sum(int(row.get("declared_source_total") or 0) for row in limited)
    missing = sum(int(row.get("missing_review_count") or 0) for row in limited)
    return (
        f"有限来源样本：已收集 {collected:,}/已声明 {declared:,}/缺 {missing:,}；"
        f"涉及 {len(limited):,} 个部分来源任务，结论仅代表已发布样本。"
    )


def enrich_evidence_rows(
    rows: Iterable[dict[str, Any]],
    audits: Iterable[dict[str, Any]],
    non_reportable_exclusions: Iterable[dict[str, Any]] = (),
) -> Iterator[dict[str, Any]]:
    by_job = {
        str(row.get("job_id") or ""): row
        for row in audits
        if row.get("job_id")
    }
    exclusions = list(non_reportable_exclusions)
    excluded_records = sum(
        int(item.get("excluded_records") or 0) for item in exclusions
    )
    exclusion_fields = (
        {
            "non_reportable_exclusion_policy": (
                ";".join(sorted({str(item.get("policy_version") or "unsupported-legacy-partial-contract/1.0.0") for item in exclusions}))
            ),
            "non_reportable_excluded_records": excluded_records,
            "non_reportable_exclusion_message": (
                "旧部分采集契约不受支持，"
                f"{excluded_records}条未进入当前快照/报告"
            ),
        }
        if excluded_records
        else {}
    )
    for row in rows:
        audit = by_job.get(str(row.get("job_id") or ""))
        if not audit:
            yield {**row, **exclusion_fields}
            continue
        yield {
            **row,
            **exclusion_fields,
            "source_completeness_status": (
                "limited" if audit.get("is_limited_source") else "complete"
            ),
            "source_completeness_collection_status": audit.get(
                "collection_status"
            ),
            "source_completeness_validation_status": audit.get(
                "validation_status"
            ),
            "source_completeness_validation_schema_version": audit.get(
                "validation_schema_version"
            ),
            "source_completeness_import_schema_version": audit.get(
                "import_schema_version"
            ),
            "declared_source_total": audit.get("declared_source_total"),
            "collected_source_unique_reviews": audit.get(
                "collected_source_unique_reviews"
            ),
            "missing_review_count": audit.get("missing_review_count"),
            "source_reconciled": audit.get("source_reconciled"),
            "source_completeness_stop_reason": audit.get("stop_reason"),
        }
