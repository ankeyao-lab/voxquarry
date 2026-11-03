#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reporting.category_vocabulary import (  # noqa: E402
    CATEGORY_RESOLUTION_SCHEMA_VERSION,
    category_id_for,
    category_resolution,
)
from reporting.controlled_energy import (  # noqa: E402
    SCHEMA_VERSION as CONTROLLED_ENERGY_SCHEMA_VERSION,
    controlled_energy_fact,
)
from reporting.config import (  # noqa: E402
    FACT_SCHEMA_VERSION,
    MARKET_CONTRACT_VERSION,
    MARKET_REGIONS,
    NORMALIZATION_VERSION,
    PARSER_VERSION,
)
from reporting.source import (  # noqa: E402
    MARKET_CONTRACT_MARKER_FIELDS,
    REQUIRED_MARKET_CSV_FIELDS,
    build_review_fact,
    discover_selected_runs,
    load_jobs,
    parse_boolean,
)
from reporting.source_completeness import (  # noqa: E402
    SourceCompletenessError,
    source_completeness_audit,
)
from reporting.product_master import (  # noqa: E402
    HASH_ALGORITHM as PRODUCT_MASTER_HASH_ALGORITHM,
    PROJECTION_FILENAME as PRODUCT_MASTER_FILENAME,
    SCHEMA_VERSION as PRODUCT_MASTER_SCHEMA_VERSION,
    load_product_master_file,
)


SNAPSHOT_PACKAGE_VERSION = "voxquarry-reporting-snapshot/v2"
SNAPSHOT_TRANSFORM_VERSION = "legacy-to-review-fact-v2/1.2.0"
TEXT_POPULATION_POLICY = "all-eligible-records; any-nonempty-text-for-text-analysis"
TRUE_EMPTY_SOURCE_POLICY_VERSION = "verified-true-empty-source/1.1.0"
TRUE_EMPTY_SOURCE_REASON = "verified_true_empty_source"
UNSUPPORTED_PARTIAL_POLICY_VERSION = (
    "unsupported-legacy-partial-contract/1.1.0"
)
UNSUPPORTED_PARTIAL_REASON = "unsupported_legacy_partial_contract"
HISTORICAL_TEXT_VALIDITY_DETECTION_VERSION = (
    "historical-text-validity-contract-detection/1.1.0"
)
TEXT_VALIDITY_ANY_NONEMPTY = "any-nonempty-text"
TEXT_VALIDITY_MINIMUM_FIVE_WORDS = "minimum-five-words"
LEGACY_ZERO_REQUIRED_CSV_FIELDS = {
    "channel",
    "product_id",
    "review_id",
    "review_text",
    "rating",
}
LEGACY_ZERO_MISSING_V2_FIELDS = {
    "source_reconciled",
    "source_unique_reviews",
    "eligible_reviews",
    "excluded_reviews",
    "valid_text_reviews",
}
REVIEW_WORD_RE = re.compile(
    r"[^\W_]+(?:['’.-][^\W_]+)*",
    re.UNICODE,
)
REQUIRED_ORIGIN_FIELDS = {
    "display_site",
    "source_site",
    "origin_source_name",
    "origin_source_url",
    "origin_source_site",
    "market_region",
    "market_region_evidence",
    "review_word_count",
    "is_valid_text_review",
    "is_eligible",
}


def clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def host(value: object) -> str:
    text = str(value or "")
    parsed = urlsplit(text)
    if parsed.netloc:
        return parsed.netloc.casefold().removeprefix("www.")
    if "/" not in text and "." in text:
        return text.casefold().removeprefix("www.")
    return ""


def count_review_words(value: object) -> int:
    text = " ".join(str(value or "").split())
    return len(REVIEW_WORD_RE.findall(text))


def raw_review_ids(raw: dict[str, Any]) -> tuple[str, ...]:
    """Return every governed identifier carried by one raw review record.

    RYOBI public API records contain both ``id`` and ``guid`` while the
    normalized CSV intentionally uses ``guid`` as ``review_id``.  Indexing all
    explicit identifiers preserves exact row-to-raw reconciliation without
    fuzzy matching or weakening the origin-evidence gate.
    """

    values = (
        raw.get("id"),
        raw.get("Id"),
        raw.get("guid"),
        raw.get("Guid"),
        raw.get("review_id"),
        raw.get("reviewId"),
    )
    return tuple(dict.fromkeys(value for item in values if (value := clean(item))))


def raw_review_id(raw: dict[str, Any]) -> str:
    """Compatibility helper returning the first governed identifier."""

    return next(iter(raw_review_ids(raw)), "")


def raw_review_origin(raw: dict[str, Any]) -> dict[str, Any]:
    source = (
        raw.get("SyndicationSource")
        or raw.get("syndicationSource")
        or {}
    )
    if not isinstance(source, dict):
        source = {"Name": source}
    name = clean(
        source.get("Name")
        or source.get("name")
        or raw.get("SourceClient")
        or raw.get("sourceClient")
        or raw.get("clientName")
        or raw.get("ClientName")
        or raw.get("client")
        or raw.get("Client")
    )
    url = clean(
        source.get("ContentLink")
        or source.get("contentLink")
        or source.get("Url")
        or source.get("url")
    )
    site = host(url) or host(name)
    normalized_name = name.casefold()
    is_ego = bool(
        site in {"egopowerplus.com", "egopowerplus.co.uk"}
        or normalized_name in {
            "ego",
            "egopowerplus",
            "egopowerplus.com",
            "egopowerplus.co.uk",
        }
    )
    if is_ego and not site:
        site = "egopowerplus.com"
    return {
        "origin_source_name": name,
        "origin_source_url": url,
        "origin_source_site": site,
        "is_ego_official_origin": is_ego,
    }


def raw_origin_index(csv_path: Path) -> dict[str, dict[str, Any]]:
    raw_path = csv_path.with_name("reviews.raw.jsonl")
    if not raw_path.is_file():
        return {}
    origins: dict[str, dict[str, Any]] = {}
    with raw_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            origin = raw_review_origin(raw)
            for review_id in raw_review_ids(raw):
                origins[review_id] = origin
    return origins


def transformed_report(
    run: dict[str, Any],
    *,
    source_completeness: dict[str, Any],
    source_rows: int,
    eligible_rows: int,
    written_rows: int,
    ratings_only: int,
    short_text_rows: int,
    excluded_ego_rows: int,
    text_validity_mismatch_rows: int,
) -> dict[str, Any]:
    report = json.loads(json.dumps(run["report"]))
    channel = clean(run["job"].get("channel"))
    region = clean(run["job"].get("market_region"))
    if region not in MARKET_REGIONS:
        raise RuntimeError(
            f"{run['job']['id']} has invalid market_region={region!r}"
        )
    if run["market_contract_mode"] == "current":
        reported_source = int(
            report.get("source_unique_reviews")
            or source_rows + int(report.get("excluded_reviews") or 0)
        )
        existing_excluded = int(report.get("excluded_reviews") or 0)
        if reported_source != source_rows + existing_excluded:
            raise RuntimeError(
                f"{run['job']['id']} current source reconciliation failed"
            )
        excluded_rows = existing_excluded
    else:
        reported_unique = int(report.get("unique_reviews") or source_rows)
        if reported_unique != source_rows:
            raise RuntimeError(
                f"{run['job']['id']} legacy CSV/validation mismatch: "
                f"{source_rows} != {reported_unique}"
            )
        reported_source = source_rows
        excluded_rows = excluded_ego_rows
    if reported_source != eligible_rows + excluded_rows:
        raise RuntimeError(
            f"{run['job']['id']} transformed source reconciliation failed: "
            f"{reported_source} != {eligible_rows} + {excluded_rows}"
        )
    product = dict(report.get("product") or {})
    product["market_region"] = region
    report.update(
        {
            "product": product,
            "source_unique_reviews": reported_source,
            "source_reconciled": source_completeness["source_reconciled"],
            "declared_source_total": source_completeness[
                "declared_source_total"
            ],
            "collected_source_unique_reviews": source_completeness[
                "collected_source_unique_reviews"
            ],
            "missing_review_count": source_completeness[
                "missing_review_count"
            ],
            "source_completeness": source_completeness,
            "unique_reviews": eligible_rows,
            "eligible_reviews": eligible_rows,
            "excluded_reviews": excluded_rows,
            "excluded_ego_origin_reviews": int(
                report.get("excluded_ego_origin_reviews")
                or excluded_ego_rows
            ),
            "exclusion_reason_histogram": (
                {"syndicated_from_ego_official": excluded_rows}
                if excluded_rows
                else {}
            ),
            "valid_text_reviews": written_rows,
            "short_text_reviews": short_text_rows,
            "ratings_only": ratings_only,
            "market_region": region,
            "text_population_policy": TEXT_POPULATION_POLICY,
            "snapshot_transform_version": SNAPSHOT_TRANSFORM_VERSION,
        }
    )
    notes = dict(report.get("notes") or {})
    notes["reporting_snapshot_transform"] = {
        "version": SNAPSHOT_TRANSFORM_VERSION,
        "source_contract_mode": run["market_contract_mode"],
        "all_eligible_records_included": True,
        "text_analysis_requires": "nonempty review_text",
        "minimum_word_count": 0,
        "source_text_validity_mismatch_rows": text_validity_mismatch_rows,
        "source_text_validity_reconciliation": (
            "is_valid_text_review := bool(trim(review_text))"
        ),
        "legacy_origin_evidence": (
            "raw reviews.raw.jsonl"
            if run["market_contract_mode"] == "legacy"
            else "existing review-fact/v2 fields"
        ),
    }
    report["notes"] = notes
    return report


def _count_raw_source_rows(path: Path, *, job_id: str) -> int:
    if not path.is_file():
        raise SourceCompletenessError(
            f"{job_id} partial source lacks raw origin artifact"
        )
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise SourceCompletenessError(
                    f"{job_id} raw source row {line_number} is invalid JSON"
                ) from error
            if not isinstance(value, dict):
                raise SourceCompletenessError(
                    f"{job_id} raw source row {line_number} is not an object"
                )
            count += 1
    return count


def transform_run_artifacts(
    source_root: Path,
    staging_root: Path,
    run: dict[str, Any],
) -> dict[str, Any]:
    relative_csv = run["csv_path"].relative_to(staging_root)
    source_csv = source_root / relative_csv
    relative_report = run["report_path"].relative_to(staging_root)
    target_csv = staging_root / relative_csv
    target_report = staging_root / relative_report
    target_csv.parent.mkdir(parents=True, exist_ok=True)
    target_report.parent.mkdir(parents=True, exist_ok=True)

    source_report = source_root / relative_report
    source_artifacts = [
        {
            "role": "standard_reviews",
            "path": str(relative_csv),
            "bytes": source_csv.stat().st_size,
            "sha256": f"sha256:{sha256_file(source_csv)}",
        },
        {
            "role": "validation_report",
            "path": str(relative_report),
            "bytes": source_report.stat().st_size,
            "sha256": f"sha256:{sha256_file(source_report)}",
        },
    ]
    raw_path = source_csv.with_name("reviews.raw.jsonl")
    if (
        run["market_contract_mode"] == "legacy"
        and run["channel"] != "ego"
        and not raw_path.is_file()
    ):
        raise RuntimeError(
            f"{run['job']['id']} legacy non-EGO run lacks raw origin evidence"
        )
    if raw_path.is_file():
        source_artifacts.append(
            {
                "role": "raw_origin_evidence",
                "path": str(raw_path.relative_to(source_root)),
                "bytes": raw_path.stat().st_size,
                "sha256": f"sha256:{sha256_file(raw_path)}",
                "copied": False,
            }
        )

    origins = (
        raw_origin_index(source_csv)
        if run["market_contract_mode"] == "legacy"
        else {}
    )
    output_rows: list[dict[str, Any]] = []
    source_rows = 0
    excluded_ego_rows = 0
    written_rows = 0
    ratings_only = 0
    short_text_rows = 0
    text_validity_mismatch_rows = 0
    with source_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        source_fields = list(reader.fieldnames or [])
        for source_row in reader:
            source_rows += 1
            row = dict(source_row)
            review_id = clean(row.get("review_id"))
            if (
                run["market_contract_mode"] == "legacy"
                and run["channel"] != "ego"
                and review_id not in origins
            ):
                raise RuntimeError(
                    f"{run['job']['id']} raw origin evidence is missing "
                    f"review_id={review_id!r}"
                )
            origin = origins.get(review_id, {})
            if (
                run["market_contract_mode"] == "legacy"
                and run["channel"] != "ego"
                and origin.get("is_ego_official_origin") is True
            ):
                excluded_ego_rows += 1
                continue
            text = clean(row.get("review_text"))
            words = count_review_words(text)
            if run["market_contract_mode"] == "current":
                source_eligible = parse_boolean(row.get("is_eligible"))
                source_valid_text = parse_boolean(
                    row.get("is_valid_text_review")
                )
                row_region = clean(row.get("market_region"))
                task_region = clean(run["job"].get("market_region"))
                if source_eligible is not True:
                    raise RuntimeError(
                        f"{run['job']['id']} current source row is not "
                        "explicitly eligible"
                    )
                if source_valid_text is None:
                    raise RuntimeError(
                        f"{run['job']['id']} current source row has no "
                        "boolean is_valid_text_review"
                    )
                if row_region != task_region:
                    raise RuntimeError(
                        f"{run['job']['id']} current row market mismatch: "
                        f"{row_region!r} != {task_region!r}"
                    )
                if not clean(row.get("market_region_evidence")):
                    raise RuntimeError(
                        f"{run['job']['id']} current source row lacks "
                        "market_region_evidence"
                    )
                text_validity_mismatch_rows += (
                    source_valid_text is not bool(text)
                )
            if text:
                written_rows += 1
                if words < 5:
                    short_text_rows += 1
            else:
                ratings_only += 1
            display_site = clean(
                row.get("display_site")
                or (run["report"].get("product") or {}).get("source_site")
                or host(run["job"].get("target_url"))
                or run["channel"]
            )
            source_site = clean(
                origin.get("origin_source_site")
                or row.get("source_site")
                or display_site
            )
            row.update(
                {
                    "display_site": display_site,
                    "source_site": source_site,
                    "origin_source_name": clean(
                        row.get("origin_source_name")
                        or origin.get("origin_source_name")
                    ),
                    "origin_source_url": clean(
                        row.get("origin_source_url")
                        or origin.get("origin_source_url")
                    ),
                    "origin_source_site": clean(
                        row.get("origin_source_site")
                        or origin.get("origin_source_site")
                    ),
                    "is_ego_official_origin": str(
                        bool(
                            origin.get("is_ego_official_origin")
                            or clean(row.get("is_ego_official_origin"))
                            .casefold()
                            in {"1", "true", "yes"}
                        )
                    ),
                    "review_locale": clean(row.get("review_locale")),
                    "market_region": clean(
                        run["job"].get("market_region")
                    ),
                    "market_region_evidence": clean(
                        row.get("market_region_evidence")
                        or "task_market_region_backfill"
                    ),
                    "review_word_count": str(words),
                    "is_valid_text_review": str(bool(text)),
                    "is_eligible": "True",
                    "exclusion_reason": "",
                }
            )
            output_rows.append(row)

    output_fields = list(source_fields)
    for field in sorted(REQUIRED_ORIGIN_FIELDS | REQUIRED_MARKET_CSV_FIELDS):
        if field not in output_fields:
            output_fields.append(field)
    if "is_ego_official_origin" not in output_fields:
        output_fields.append("is_ego_official_origin")
    with target_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=output_fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(output_rows)

    partial_claim = (
        clean(run["job"].get("status")) == "partial_success"
        or clean(run["report"].get("status")) == "PARTIAL_SUCCESS"
        or run["report"].get("source_reconciled") is False
    )
    if partial_claim and run["market_contract_mode"] != "current":
        raise SourceCompletenessError(
            f"{run['job']['id']} partial source must use the current contract"
        )
    raw_source_rows = (
        _count_raw_source_rows(raw_path, job_id=str(run["job"]["id"]))
        if partial_claim
        else None
    )
    completeness_report = run["report"]
    completeness_job = run["job"]
    if run["market_contract_mode"] == "legacy" and not partial_claim:
        completeness_job = {**run["job"], "status": "completed"}
        completeness_report = {
            **run["report"],
            "status": clean(run["report"].get("status")) or "COMPLETE",
            "source_total": source_rows,
            "source_unique_reviews": source_rows,
            "source_reconciled": True,
            "unique_reviews": len(output_rows),
            "eligible_reviews": len(output_rows),
            "excluded_reviews": excluded_ego_rows,
        }
    elif (
        run["market_contract_mode"] == "current"
        and not partial_claim
        and "source_reconciled" not in run["report"]
    ):
        completeness_report = {
            **run["report"],
            "source_reconciled": True,
        }
    completeness = source_completeness_audit(
        completeness_job,
        completeness_report,
        csv_eligible_rows=len(output_rows),
        raw_source_rows=raw_source_rows,
    )

    report = transformed_report(
        run,
        source_completeness=completeness,
        source_rows=source_rows,
        eligible_rows=len(output_rows),
        written_rows=written_rows,
        ratings_only=ratings_only,
        short_text_rows=short_text_rows,
        excluded_ego_rows=excluded_ego_rows,
        text_validity_mismatch_rows=text_validity_mismatch_rows,
    )
    target_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "job_id": str(run["job"]["id"]),
        "category_id": str(
            run["job"].get("category_id")
            or category_id_for(run["job"].get("category"))
            or ""
        ),
        "category": str(run["job"].get("category") or ""),
        "category_resolution": category_resolution(
            run["job"].get("category")
        ),
        "channel": str(run["channel"]),
        "market_region": str(run["job"].get("market_region") or ""),
        "source_contract_mode": run["market_contract_mode"],
        "source_artifacts": source_artifacts,
        "source_unique_reviews": source_rows,
        "eligible_reviews": len(output_rows),
        "written_reviews": written_rows,
        "ratings_only": ratings_only,
        "short_text_reviews": short_text_rows,
        "excluded_ego_origin_reviews": excluded_ego_rows,
        "source_text_validity_mismatch_rows": (
            text_validity_mismatch_rows
        ),
        "source_completeness": completeness,
        "artifacts": [str(relative_csv), str(relative_report)],
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _backup_source_uri(source: Path) -> str:
    """Choose a read-only SQLite URI without hiding committed WAL records.

    Immutable handoffs have no writable bits on either the database or its
    directory and no WAL state.  Live collector databases remain ordinary
    read-only connections so SQLite can reconcile the main file with an
    existing WAL.  A WAL or SHM sidecar always takes precedence over the
    permission heuristic.
    """

    resolved = source.resolve()
    wal = Path(f"{resolved}-wal")
    shm = Path(f"{resolved}-shm")
    has_wal_state = wal.is_file() or shm.is_file()
    database_is_read_only = resolved.stat().st_mode & 0o222 == 0
    directory_is_read_only = resolved.parent.stat().st_mode & 0o222 == 0
    immutable = (
        not has_wal_state
        and database_is_read_only
        and directory_is_read_only
    )
    parameters = "mode=ro&immutable=1" if immutable else "mode=ro"
    return f"{resolved.as_uri()}?{parameters}"


def backup_database(source: Path, target: Path) -> int:
    source_connection = sqlite3.connect(_backup_source_uri(source), uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.execute("PRAGMA query_only = ON")
        with target_connection:
            source_connection.backup(target_connection)
        result = target_connection.execute("PRAGMA integrity_check").fetchone()[0]
        job_count = target_connection.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]
    finally:
        target_connection.close()
        source_connection.close()
    if result != "ok":
        raise RuntimeError(f"jobs SQLite integrity_check failed: {result}")
    return int(job_count)


def header_fields(path: Path) -> set[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return set(csv.DictReader(handle).fieldnames or [])


def _strict_zero(report: dict[str, Any], field: str, *, job_id: str) -> None:
    value = report.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value != 0:
        raise RuntimeError(
            f"{job_id} true-empty-source validation requires {field}=0"
        )


def _source_artifact(
    source_root: Path,
    path: Path,
    role: str,
) -> dict[str, Any]:
    return {
        "role": role,
        "path": str(path.relative_to(source_root)),
        "bytes": path.stat().st_size,
        "sha256": f"sha256:{sha256_file(path)}",
    }


def _explicit_nonnegative_int(
    report: dict[str, Any],
    field: str,
    *,
    job_id: str,
) -> int:
    value = report.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceCompletenessError(
            f"{job_id} unsupported partial requires explicit {field}"
        )
    return value


def _cached_validation(
    job: dict[str, Any],
    *,
    job_id: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    cached = job.get("result_json")
    if isinstance(cached, str):
        try:
            cached = json.loads(cached)
        except json.JSONDecodeError as error:
            raise SourceCompletenessError(
                f"{job_id} cached validation is not valid JSON"
            ) from error
    if not isinstance(cached, dict):
        raise SourceCompletenessError(
            f"{job_id} unsupported partial lacks cached validation"
        )
    channel = clean(job.get("channel"))
    if not channel:
        raise SourceCompletenessError(
            f"{job_id} unsupported partial lacks task channel"
        )
    if set(cached) != {channel}:
        raise SourceCompletenessError(
            f"{job_id} cached result_json must contain exactly the current "
            f"channel envelope {channel!r}"
        )
    payload = cached.get(channel)
    if not isinstance(payload, dict):
        raise SourceCompletenessError(
            f"{job_id} cached current-channel payload is not an object"
        )
    return payload, {
        "shape": "channel_envelope",
        "channel": channel,
    }


def _reconcile_historical_partial_cache(
    cached: dict[str, Any],
    report: dict[str, Any],
    *,
    job_id: str,
) -> list[dict[str, str]]:
    """Verify cache equality while auditing one non-authoritative sidecar key.

    Historical task cache rows may add ``artifacts.report`` after publishing
    the physical validation artifact.  That pointer is derived storage
    metadata, not a validation fact.  No other cache-only, physical-only, or
    value difference is accepted by the unsupported-partial downgrade.
    """

    cached_core = dict(cached)
    report_core = dict(report)
    cache_delta: list[dict[str, str]] = []
    cached_artifacts = cached_core.get("artifacts")
    report_artifacts = report_core.get("artifacts")
    if isinstance(cached_artifacts, dict):
        cached_artifacts = dict(cached_artifacts)
        if "report" in cached_artifacts and not (
            isinstance(report_artifacts, dict) and "report" in report_artifacts
        ):
            cached_artifacts.pop("report")
            cache_delta.append(
                {
                    "path": "artifacts.report",
                    "kind": "cache_only_non_authoritative_derived_key",
                }
            )
        if cached_artifacts:
            cached_core["artifacts"] = cached_artifacts
        else:
            cached_core.pop("artifacts", None)
    if isinstance(report_artifacts, dict) and not report_artifacts:
        report_core.pop("artifacts", None)
    if cached_core != report_core:
        raise SourceCompletenessError(
            f"{job_id} cached result_json core fields do not match physical "
            "validation"
        )
    return cache_delta


def _strict_raw_review_identity_mapping(
    raw_path: Path,
    csv_review_ids: set[str],
    *,
    job_id: str,
) -> int:
    record_count = 0
    matched_csv_ids: set[str] = set()
    with raw_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise SourceCompletenessError(
                    f"{job_id} raw origin line {line_number} is invalid"
                ) from error
            if not isinstance(raw, dict):
                raise SourceCompletenessError(
                    f"{job_id} raw origin line {line_number} is not an object"
                )
            governed_ids = set(raw_review_ids(raw))
            if not governed_ids:
                raise SourceCompletenessError(
                    f"{job_id} raw origin line {line_number} lacks review identity"
                )
            matches = governed_ids & csv_review_ids
            if len(matches) != 1:
                raise SourceCompletenessError(
                    f"{job_id} raw origin line {line_number} must map to "
                    "exactly one CSV review identity"
                )
            matched_id = next(iter(matches))
            if matched_id in matched_csv_ids:
                raise SourceCompletenessError(
                    f"{job_id} raw origin review identity is duplicated: "
                    f"{matched_id!r}"
                )
            matched_csv_ids.add(matched_id)
            record_count += 1
    if matched_csv_ids != csv_review_ids:
        missing = sorted(csv_review_ids - matched_csv_ids)
        raise SourceCompletenessError(
            f"{job_id} raw origin evidence is missing review_id="
            f"{(missing[0] if missing else '')!r}"
        )
    return record_count


def verified_unsupported_partial_exclusion(
    source_root: Path,
    staging_root: Path,
    run: dict[str, Any],
) -> dict[str, Any] | None:
    """Return an auditable tombstone for one unsupported historical partial.

    A current governed partial is deliberately left to the ordinary transform
    and source-completeness validator.  Only a physically self-consistent
    historical partial with no validation schema may become non-reportable;
    malformed artifacts or contradictory cache/validation claims still abort
    the complete snapshot build.
    """

    job = run["job"]
    report = run["report"]
    job_id = clean(job.get("id"))
    partial_claim = (
        clean(job.get("status")) == "partial_success"
        or clean(report.get("status")) == "PARTIAL_SUCCESS"
        or report.get("source_reconciled") is False
    )
    if not partial_claim:
        return None
    validation_schema = clean(report.get("validation_schema_version"))
    if validation_schema:
        # Supported and unsupported versioned contracts are not silently
        # downgraded to historical exclusions.  The current one proceeds to
        # the authoritative validator; every other explicit version fails.
        if validation_schema == (
            "voxquarry.external-review-import-validation/v1"
        ):
            return None
        raise SourceCompletenessError(
            f"{job_id} has unsupported explicit partial validation schema "
            f"{validation_schema}"
        )
    if clean(job.get("status")) != "partial_success":
        raise SourceCompletenessError(
            f"{job_id} historical partial requires task status partial_success"
        )
    if clean(report.get("status")) != "PARTIAL_SUCCESS":
        raise SourceCompletenessError(
            f"{job_id} historical partial requires validation PARTIAL_SUCCESS"
        )
    source_reconciled = report.get("source_reconciled")
    if source_reconciled is not None and source_reconciled is not False:
        raise SourceCompletenessError(
            f"{job_id} historical partial cannot claim source_reconciled=true"
        )
    cached, cache_payload = _cached_validation(job, job_id=job_id)
    cache_delta = _reconcile_historical_partial_cache(
        cached, report, job_id=job_id
    )

    report_channel = clean(report.get("channel"))
    channel = clean(job.get("channel"))
    if report_channel and report_channel != channel:
        raise SourceCompletenessError(
            f"{job_id} validation channel does not match task channel"
        )
    product = report.get("product") or {}
    if not isinstance(product, dict):
        raise SourceCompletenessError(
            f"{job_id} validation product must be an object"
        )
    product_identity = clean(product.get("id"))
    if not product_identity:
        raise SourceCompletenessError(
            f"{job_id} historical partial lacks product identity"
        )

    csv_path = source_root / run["csv_path"].relative_to(staging_root)
    report_path = source_root / run["report_path"].relative_to(staging_root)
    raw_path = csv_path.with_name("reviews.raw.jsonl")
    if not raw_path.is_file():
        raise SourceCompletenessError(
            f"{job_id} historical partial lacks raw origin evidence"
        )
    notes = report.get("notes") or {}
    if not isinstance(notes, dict):
        raise SourceCompletenessError(
            f"{job_id} validation notes must be an object"
        )
    nonempty_policy_present = (
        "nonempty_text_is_valid_text_review" in notes
    )
    nonempty_policy_value = notes.get(
        "nonempty_text_is_valid_text_review"
    )
    if nonempty_policy_present and not isinstance(
        nonempty_policy_value, bool
    ):
        raise SourceCompletenessError(
            f"{job_id} historical partial has invalid explicit text-validity "
            "policy"
        )
    declared_text_policy = (
        TEXT_VALIDITY_ANY_NONEMPTY
        if nonempty_policy_value is True
        else None
    )
    csv_review_ids: set[str] = set()
    csv_rows = 0
    current_text_semantics_match = True
    legacy_five_word_semantics_match = True
    modern_mismatch_rows = 0
    legacy_five_word_mismatch_rows = 0
    flagged_valid_text_rows = 0
    nonempty_text_rows = 0
    empty_text_rows = 0
    short_nonempty_text_rows = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SourceCompletenessError(
                f"{job_id} historical partial CSV lacks a header"
            )
        for row_number, row in enumerate(reader, start=2):
            review_id = clean(row.get("review_id"))
            if not review_id or review_id in csv_review_ids:
                raise SourceCompletenessError(
                    f"{job_id} CSV review identity is missing or duplicated "
                    f"at row {row_number}"
                )
            if clean(row.get("channel")) not in {"", channel}:
                raise SourceCompletenessError(
                    f"{job_id} CSV channel contradicts task at row {row_number}"
                )
            if clean(row.get("product_id")) not in {"", product_identity}:
                raise SourceCompletenessError(
                    f"{job_id} CSV product identity contradicts validation"
                )
            if run["market_contract_mode"] == "current":
                eligible = parse_boolean(row.get("is_eligible"))
                valid_text = parse_boolean(row.get("is_valid_text_review"))
                if eligible is not True or valid_text is None:
                    raise SourceCompletenessError(
                        f"{job_id} current historical partial has invalid "
                        "eligibility fields"
                    )
                text = clean(row.get("review_text"))
                word_count = count_review_words(text)
                current_row_matches = valid_text is bool(text)
                legacy_five_word_row_matches = valid_text is (
                    word_count >= 5
                )
                current_text_semantics_match = (
                    current_text_semantics_match and current_row_matches
                )
                legacy_five_word_semantics_match = (
                    legacy_five_word_semantics_match
                    and legacy_five_word_row_matches
                )
                modern_mismatch_rows += int(not current_row_matches)
                legacy_five_word_mismatch_rows += int(
                    not legacy_five_word_row_matches
                )
                flagged_valid_text_rows += int(valid_text)
                nonempty_text_rows += int(bool(text))
                empty_text_rows += int(not text)
                short_nonempty_text_rows += int(bool(text) and word_count < 5)
                if clean(row.get("market_region")) != clean(
                    job.get("market_region")
                ) or not clean(row.get("market_region_evidence")):
                    raise SourceCompletenessError(
                        f"{job_id} current historical partial has invalid "
                        "market evidence"
                    )
            csv_review_ids.add(review_id)
            csv_rows += 1
    if csv_rows <= 0:
        raise SourceCompletenessError(
            f"{job_id} historical partial must contain review rows"
        )
    text_validity_contract_detection: dict[str, Any] | None = None
    if run["market_contract_mode"] == "current":
        if not (
            current_text_semantics_match
            or legacy_five_word_semantics_match
        ):
            raise SourceCompletenessError(
                f"{job_id} current historical partial has invalid "
                "text-validity semantics"
            )
        matching_policies = [
            policy
            for policy, matches in (
                (TEXT_VALIDITY_ANY_NONEMPTY, current_text_semantics_match),
                (
                    TEXT_VALIDITY_MINIMUM_FIVE_WORDS,
                    legacy_five_word_semantics_match,
                ),
            )
            if matches
        ]
        if (
            nonempty_policy_value is True
            and TEXT_VALIDITY_ANY_NONEMPTY not in matching_policies
        ):
            raise SourceCompletenessError(
                f"{job_id} explicit text-validity policy contradicts CSV"
            )
        if (
            nonempty_policy_value is False
            and TEXT_VALIDITY_ANY_NONEMPTY in matching_policies
        ):
            raise SourceCompletenessError(
                f"{job_id} explicit text-validity policy contradicts CSV"
            )
        if "valid_text_reviews" in report and _explicit_nonnegative_int(
            report, "valid_text_reviews", job_id=job_id
        ) != flagged_valid_text_rows:
            raise SourceCompletenessError(
                f"{job_id} validation valid_text_reviews contradicts CSV flags"
            )
        if "ratings_only" in report and _explicit_nonnegative_int(
            report, "ratings_only", job_id=job_id
        ) != empty_text_rows:
            raise SourceCompletenessError(
                f"{job_id} validation ratings_only contradicts CSV text"
            )
        observed_short_text_reviews = None
        short_text_definitions = {
            TEXT_VALIDITY_ANY_NONEMPTY: {
                "definition": "external-import-short-nonempty/v1",
                "expected": short_nonempty_text_rows,
                "basis": "0 < count_review_words(review_text) < 5",
            },
            TEXT_VALIDITY_MINIMUM_FIVE_WORDS: {
                "definition": "historical-false-valid-flag/v1",
                "expected": csv_rows - flagged_valid_text_rows,
                "basis": "csv_rows - flagged_valid_text_rows",
            },
        }
        matching_short_text_definitions: list[str] = []
        if "short_text_reviews" in report:
            observed_short_text_reviews = _explicit_nonnegative_int(
                report, "short_text_reviews", job_id=job_id
            )
            matching_short_text_definitions = [
                short_text_definitions[policy]["definition"]
                for policy in matching_policies
                if observed_short_text_reviews
                == short_text_definitions[policy]["expected"]
            ]
            if not matching_short_text_definitions:
                raise SourceCompletenessError(
                    f"{job_id} validation short_text_reviews contradicts "
                    "detected text-validity policy"
                )
        text_validity_contract_detection = {
            "version": HISTORICAL_TEXT_VALIDITY_DETECTION_VERSION,
            "matching_policies": matching_policies,
            "declared_policy": declared_text_policy,
            "observed_contract_hint": (
                {
                    "field": "notes.nonempty_text_is_valid_text_review",
                    "value": nonempty_policy_value,
                }
                if nonempty_policy_present
                else None
            ),
            "detected_policy": (
                matching_policies[0] if len(matching_policies) == 1 else None
            ),
            "ambiguous_compatible_policies": len(matching_policies) > 1,
            "modern_mismatch_rows": modern_mismatch_rows,
            "legacy_five_word_mismatch_rows": (
                legacy_five_word_mismatch_rows
            ),
            "observed_counts": {
                "csv_rows": csv_rows,
                "flagged_valid_text_rows": flagged_valid_text_rows,
                "nonempty_text_rows": nonempty_text_rows,
                "empty_text_rows": empty_text_rows,
                "short_nonempty_text_rows": short_nonempty_text_rows,
                "validation_valid_text_reviews": report.get(
                    "valid_text_reviews"
                ),
                "validation_short_text_reviews": (
                    observed_short_text_reviews
                ),
                "validation_ratings_only": report.get("ratings_only"),
            },
            "short_text_definition": {
                "version": "policy-specific-short-text/v1",
                "expected_by_policy": {
                    policy: short_text_definitions[policy]
                    for policy in matching_policies
                },
                "matching_short_definitions": (
                    matching_short_text_definitions
                ),
                "ambiguous": len(matching_short_text_definitions) > 1,
                "observed": observed_short_text_reviews,
            },
            "source_flags_preserved": True,
            "reporting_population_changed": False,
        }
    raw_rows = _strict_raw_review_identity_mapping(
        raw_path, csv_review_ids, job_id=job_id
    )

    declared = _explicit_nonnegative_int(
        report, "source_total", job_id=job_id
    )
    collected_field = next(
        (
            field
            for field in (
                "source_records_collected",
                "source_unique_reviews",
                "unique_reviews",
            )
            if field in report
        ),
        "",
    )
    if not collected_field:
        raise SourceCompletenessError(
            f"{job_id} historical partial lacks collected review count"
        )
    collected = _explicit_nonnegative_int(
        report, collected_field, job_id=job_id
    )
    unique = _explicit_nonnegative_int(
        report, "unique_reviews", job_id=job_id
    )
    derived_from_physical_artifacts: dict[str, dict[str, Any]] = {}
    if run["market_contract_mode"] == "legacy":
        eligible = csv_rows
        excluded = 0
        derived_from_physical_artifacts = {
            "eligible_reviews": {
                "value": eligible,
                "basis": "one_to_one_csv_raw_review_identity",
            },
            "excluded_reviews": {
                "value": excluded,
                "basis": "no_unmapped_raw_records",
            },
        }
        for field, derived_value in (
            ("eligible_reviews", eligible),
            ("excluded_reviews", excluded),
        ):
            if field in report and _explicit_nonnegative_int(
                report, field, job_id=job_id
            ) != derived_value:
                raise SourceCompletenessError(
                    f"{job_id} historical partial {field} contradicts "
                    "physical identity reconciliation"
                )
    else:
        eligible = _explicit_nonnegative_int(
            report, "eligible_reviews", job_id=job_id
        )
        excluded = _explicit_nonnegative_int(
            report, "excluded_reviews", job_id=job_id
        )
    if not (
        declared > 0 and collected > 0 and declared != collected
        and collected == eligible + excluded
        and unique == eligible
        and csv_rows == eligible
        and raw_rows == collected
    ):
        raise SourceCompletenessError(
            f"{job_id} historical partial physical counts do not reconcile"
        )

    import_schema_present = "import_schema_version" in notes
    raw_import_schema = notes.get("import_schema_version")
    if import_schema_present and not isinstance(raw_import_schema, str):
        raise SourceCompletenessError(
            f"{job_id} schema-less historical partial import_schema_version "
            "must be a string"
        )
    import_schema = clean(raw_import_schema) if import_schema_present else ""
    if import_schema_present and import_schema != "voxquarry.review-import/v1":
        raise SourceCompletenessError(
            f"{job_id} schema-less historical partial carries unexpected "
            "import_schema_version"
        )
    old_stop_reason = clean(report.get("stop_reason"))
    missing_contract_fields = [
        "validation_schema_version",
        *(
            ["notes.import_schema_version"]
            if not import_schema_present
            else []
        ),
        *(
            ["source_records_collected"]
            if "source_records_collected" not in report
            else []
        ),
        *(
            ["source_missing_records"]
            if "source_missing_records" not in report
            else []
        ),
        *(
            ["source_reconciled"]
            if "source_reconciled" not in report
            else []
        ),
        *(["stop_reason"] if "stop_reason" not in report else []),
        *(
            ["stop_reason=external_import_declared_incomplete"]
            if old_stop_reason != "external_import_declared_incomplete"
            else []
        ),
    ]
    for field in (
        "source_unique_reviews",
        "eligible_reviews",
        "excluded_reviews",
        "valid_text_reviews",
        "market_region",
    ):
        if field not in report:
            missing_contract_fields.append(field)
    observed_fields = {
        field: report[field]
        for field in (
            "validation_schema_version",
            "status",
            "source_total",
            "source_records_collected",
            "source_unique_reviews",
            "unique_reviews",
            "eligible_reviews",
            "excluded_reviews",
            "valid_text_reviews",
            "short_text_reviews",
            "ratings_only",
            "market_region",
            "source_reconciled",
            "stop_reason",
        )
        if field in report
    }
    if import_schema:
        observed_fields["notes.import_schema_version"] = import_schema
    if nonempty_policy_present:
        observed_fields["notes.nonempty_text_is_valid_text_review"] = (
            nonempty_policy_value
        )
    return {
        "job_id": job_id,
        "channel": channel,
        "category_id": clean(job.get("category_id"))
        or clean(category_id_for(job.get("category"))),
        "category": clean(job.get("category")),
        "brand": clean(job.get("brand")),
        "model": clean(job.get("model")),
        "market_region": clean(job.get("market_region")),
        "product_identity": product_identity,
        "finished_at": clean(job.get("finished_at")),
        "created_at": clean(job.get("created_at")),
        "reason_code": UNSUPPORTED_PARTIAL_REASON,
        "policy_version": UNSUPPORTED_PARTIAL_POLICY_VERSION,
        "reason": (
            "The physically reconciled partial source predates the only "
            "supported governed partial contract; its review rows are "
            "excluded from Review Fact and every reporting denominator."
        ),
        "validation_status": "PARTIAL_SUCCESS",
        "source_contract_mode": run["market_contract_mode"],
        "source_reconciled": report.get("source_reconciled"),
        "old_stop_reason": old_stop_reason or None,
        "missing_contract_fields": sorted(missing_contract_fields),
        "observed_fields": observed_fields,
        "derived_from_physical_artifacts": derived_from_physical_artifacts,
        "text_validity_contract_detection": (
            text_validity_contract_detection
        ),
        "cache_delta": cache_delta,
        "cache_payload": cache_payload,
        "contract_detection": {
            "validation_schema_version": None,
            "import_schema_version": import_schema or None,
            "declared_text_validity_policy": declared_text_policy,
            "supported_current_contract": False,
            "detection": "schema_less_historical_partial",
        },
        "excluded_records": eligible,
        "declared_total_conflict": collected > declared,
        "counts": {
            "source_total": declared,
            "collected_reviews": collected,
            "eligible_reviews": eligible,
            "excluded_reviews": excluded,
            "missing_reviews": max(0, declared - collected),
            "excess_reviews": max(0, collected - declared),
            "csv_rows": csv_rows,
            "raw_records": raw_rows,
        },
        "source_artifacts": [
            _source_artifact(source_root, csv_path, "standard_reviews"),
            _source_artifact(source_root, report_path, "validation_report"),
            _source_artifact(source_root, raw_path, "raw_origin_evidence"),
        ],
    }


def _completed_job_recency(job: dict[str, Any]) -> tuple[str, str, str]:
    return (
        clean(job.get("finished_at")),
        clean(job.get("created_at")),
        clean(job.get("id")),
    )


def verified_true_empty_source_exclusions(
    source_root: Path,
    staging_root: Path,
) -> list[dict[str, Any]]:
    """Prove and describe completed zero-source jobs before contract discovery.

    A legacy or partial CSV header cannot establish a Review Fact contract.  A
    task with no review rows has no facts to migrate, though, so the snapshot
    builder may classify it as non-reportable only when the collector's own
    validation and every physical artifact independently prove a true empty
    source.  Any claimed empty task that fails one of these checks aborts the
    build instead of falling through to a permissive conversion.
    """

    exclusions: list[dict[str, Any]] = []
    for job in load_jobs(staging_root):
        job_id = clean(job.get("id"))
        if not job_id or clean(job.get("status")) != "completed":
            continue
        if clean(job.get("superseded_by")):
            continue
        channel = clean(job.get("channel"))
        active_run_path = clean(job.get("active_run_path"))
        relative_run = (
            Path(active_run_path)
            if active_run_path
            else Path(job_id) / channel
        )
        run_dir = source_root / "data" / "runs" / relative_run
        report_path = run_dir / "validation_report.json"
        csv_path = run_dir / "reviews.csv"
        raw_path = run_dir / "reviews.raw.jsonl"
        if not report_path.is_file() or not csv_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError(
                f"{job_id} validation report cannot prove true empty source"
            ) from error
        notes = report.get("notes")
        empty_confirmed = (
            isinstance(notes, dict)
            and notes.get("empty_source_confirmed") is True
        )
        if not empty_confirmed:
            continue

        if clean(report.get("status")) != "COMPLETE":
            raise RuntimeError(
                f"{job_id} true-empty-source validation status is not COMPLETE"
            )
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            has_data_row = next(reader, None) is not None
        if not fields or has_data_row:
            raise RuntimeError(
                f"{job_id} true-empty-source CSV must contain one header and no rows"
            )
        csv_fields = set(fields)
        legacy_zero_schema = not bool(
            csv_fields & MARKET_CONTRACT_MARKER_FIELDS
        )
        if legacy_zero_schema:
            missing_legacy_fields = sorted(
                LEGACY_ZERO_REQUIRED_CSV_FIELDS - csv_fields
            )
            if missing_legacy_fields:
                raise RuntimeError(
                    f"{job_id} legacy true-empty-source CSV lacks fields: "
                    + ", ".join(missing_legacy_fields)
                )
            unexpected_v2_fields = sorted(
                field
                for field in LEGACY_ZERO_MISSING_V2_FIELDS
                if field in report
            )
            if unexpected_v2_fields:
                raise RuntimeError(
                    f"{job_id} legacy true-empty-source validation contains "
                    "partial v2 fields: " + ", ".join(unexpected_v2_fields)
                )
            for field in (
                "source_total",
                "unique_reviews",
                "ratings_only",
                "duplicates_removed",
            ):
                _strict_zero(report, field, job_id=job_id)
            source_contract_mode = "legacy"
            reconciliation_mode = "legacy_zero_reconciled"
            missing_validation_fields = sorted(
                LEGACY_ZERO_MISSING_V2_FIELDS
            )
            derived_reconciliation = {
                "source_reconciled": True,
                "source_unique_reviews": 0,
                "eligible_reviews": 0,
                "excluded_reviews": 0,
                "valid_text_reviews": 0,
            }
        else:
            if report.get("source_reconciled") is not True:
                raise RuntimeError(
                    f"{job_id} true-empty-source validation is not source_reconciled"
                )
            for field in (
                "source_total",
                "source_unique_reviews",
                "unique_reviews",
                "eligible_reviews",
                "excluded_reviews",
                "valid_text_reviews",
                "ratings_only",
            ):
                _strict_zero(report, field, job_id=job_id)
            for optional_field in ("duplicates_removed", "short_text_reviews"):
                if optional_field in report:
                    _strict_zero(report, optional_field, job_id=job_id)
            source_contract_mode = "current"
            reconciliation_mode = "validation_source_reconciled"
            missing_validation_fields = []
            derived_reconciliation = {}
        if not raw_path.is_file():
            raise RuntimeError(
                f"{job_id} true-empty-source raw origin artifact is missing"
            )
        with raw_path.open("rb") as handle:
            if any(line.strip() for line in handle):
                raise RuntimeError(
                    f"{job_id} true-empty-source raw origin artifact is not empty"
                )

        exclusions.append(
            {
                "job_id": job_id,
                "channel": channel,
                "product_identity": clean(
                    (report.get("product") or {}).get("id")
                )
                or clean(job.get("target_url"))
                or job_id,
                "finished_at": clean(job.get("finished_at")),
                "created_at": clean(job.get("created_at")),
                "reason_code": TRUE_EMPTY_SOURCE_REASON,
                "reason": (
                    "Collector validation and source artifacts prove a "
                    "complete source with zero review records; no Review Fact "
                    "exists to include in reporting."
                ),
                "validation_status": "COMPLETE",
                "empty_source_confirmed": True,
                "source_reconciled": True,
                "source_contract_mode": source_contract_mode,
                "reconciliation_mode": reconciliation_mode,
                "missing_validation_fields": missing_validation_fields,
                "derived_reconciliation": derived_reconciliation,
                "reconciliation_basis": {
                    "validation_status": "COMPLETE",
                    "empty_source_confirmed": True,
                    "source_total": report["source_total"],
                    "unique_reviews": report["unique_reviews"],
                    "csv_data_rows": 0,
                    "raw_records": 0,
                },
                "counts": {
                    field: report[field]
                    for field in (
                        "source_total",
                        "source_unique_reviews",
                        "unique_reviews",
                        "eligible_reviews",
                        "excluded_reviews",
                        "valid_text_reviews",
                        "ratings_only",
                        "duplicates_removed",
                    )
                    if field in report
                },
                "source_artifacts": [
                    _source_artifact(
                        source_root,
                        csv_path,
                        "standard_reviews",
                    ),
                    _source_artifact(
                        source_root,
                        report_path,
                        "validation_report",
                    ),
                    _source_artifact(
                        source_root,
                        raw_path,
                        "raw_origin_evidence",
                    ),
                ],
            }
        )
    return sorted(exclusions, key=lambda item: item["job_id"])


def _create_filtered_runs_view(
    source_root: Path,
    staging_root: Path,
    excluded_job_ids: set[str],
) -> Path:
    source_runs = source_root / "data" / "runs"
    if not source_runs.is_dir():
        raise RuntimeError("source data/runs directory is missing")
    runs_view = staging_root / "data" / "runs"
    runs_view.mkdir(parents=True)
    for source_entry in sorted(source_runs.iterdir(), key=lambda path: path.name):
        if source_entry.name in excluded_job_ids:
            continue
        target = runs_view / source_entry.name
        target.symlink_to(
            source_entry.resolve(),
            target_is_directory=source_entry.is_dir(),
        )
    return runs_view


def _remove_filtered_runs_view(runs_view: Path) -> None:
    for child in runs_view.iterdir():
        if not child.is_symlink():
            raise RuntimeError(f"unexpected non-symlink in discovery view: {child}")
        child.unlink()
    runs_view.rmdir()


def copy_reportable_artifacts(
    source_root: Path,
    staging_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    exclusions = verified_true_empty_source_exclusions(
        source_root,
        staging_root,
    )
    excluded_job_ids = {row["job_id"] for row in exclusions}
    excluded_product_recency: dict[tuple[str, str], tuple[str, str, str]] = {}
    for row in exclusions:
        key = (row["channel"], row["product_identity"])
        rank = (row["finished_at"], row["created_at"], row["job_id"])
        excluded_product_recency[key] = max(
            rank,
            excluded_product_recency.get(key, rank),
        )
    runs_link = _create_filtered_runs_view(
        source_root,
        staging_root,
        excluded_job_ids,
    )
    try:
        discovered, _ = discover_selected_runs(staging_root, {})
    finally:
        _remove_filtered_runs_view(runs_link)
    # The newest excluded true-empty run is the current product-level tombstone.
    # Do not let removal from the temporary discovery view resurrect an older
    # non-empty run; a genuinely newer non-empty run still supersedes it.
    discovered = [
        run
        for run in discovered
        if (
            (run["channel"], run["product_identity"])
            not in excluded_product_recency
            or _completed_job_recency(run["job"])
            > excluded_product_recency[
                (run["channel"], run["product_identity"])
            ]
        )
    ]
    unsupported_partial_exclusions: list[dict[str, Any]] = []
    retained_discovered: list[dict[str, Any]] = []
    for run in discovered:
        exclusion = verified_unsupported_partial_exclusion(
            source_root, staging_root, run
        )
        if exclusion is None:
            retained_discovered.append(run)
        else:
            unsupported_partial_exclusions.append(exclusion)
    discovered = retained_discovered
    exclusions = sorted(
        [*exclusions, *unsupported_partial_exclusions],
        key=lambda item: item["job_id"],
    )
    reportable = [
        run
        for run in discovered
        if category_id_for(run["job"].get("category"))
    ]
    if not reportable:
        raise RuntimeError("snapshot has no reportable review runs")

    # Validate the governed task-energy and voltage-platform pair before any
    # report artifact is transformed or copied. A drifted task fact must never
    # leave partial snapshot output, even in the temporary staging directory.
    for run in reportable:
        controlled_energy_fact(run["job"])
    runs_link.mkdir(parents=True)

    copied = []
    for run in reportable:
        category_id = str(
            run["job"].get("category_id")
            or category_id_for(run["job"].get("category"))
            or ""
        )
        if not category_id:
            raise RuntimeError(
                f"{run['job']['id']} has no stable category_id"
            )
        transformed = transform_run_artifacts(
            source_root,
            staging_root,
            run,
        )
        if transformed["category_id"] != category_id:
            raise RuntimeError(
                f"{run['job']['id']} category identity changed"
            )
        missing = sorted(
            (REQUIRED_MARKET_CSV_FIELDS | REQUIRED_ORIGIN_FIELDS)
            - header_fields(
                staging_root / Path(transformed["artifacts"][0])
            )
        )
        if missing:
            raise RuntimeError(
                f"{run['job']['id']} transformed CSV missing fields: "
                + ", ".join(missing)
            )
        copied.append(transformed)
    return copied, exclusions


def validate_snapshot(
    snapshot_root: Path,
    expected_job_ids: set[str],
) -> dict[str, Any]:
    runs, _ = discover_selected_runs(snapshot_root, {})
    if any(run["market_contract_mode"] != "current" for run in runs):
        raise RuntimeError("snapshot contains legacy review-fact/v1 runs")
    actual_job_ids = {str(run["job"]["id"]) for run in runs}
    if actual_job_ids != expected_job_ids:
        raise RuntimeError(
            "snapshot reportable job set changed: "
            f"expected={len(expected_job_ids)} actual={len(actual_job_ids)}"
        )

    categories: dict[str, Counter[str]] = defaultdict(Counter)
    markets: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    output_contracts: Counter[str] = Counter()
    totals = Counter()
    amazon_jobs = 0
    for run in runs:
        job_id = str(run["job"]["id"])
        facts, quality, _ = build_review_fact(
            snapshot_root,
            {"job_ids": [job_id]},
        )
        report = run["report"]
        eligible = len(facts)
        written = sum(bool(fact.get("has_text")) for fact in facts)
        reported_valid = int(report.get("valid_text_reviews") or 0)
        reported_eligible = int(report.get("eligible_reviews") or 0)
        if eligible != reported_eligible:
            raise RuntimeError(
                f"{job_id} eligible review mismatch: "
                f"{eligible} != {reported_eligible}"
            )
        if written != reported_valid:
            raise RuntimeError(
                f"{job_id} written review mismatch: "
                f"{written} != {reported_valid}"
            )
        category_id = str(
            run["job"].get("category_id")
            or category_id_for(run["job"].get("category"))
            or ""
        )
        region = str(run["job"].get("market_region") or "")
        channel = str(run["channel"])
        output_contracts[str(run["market_contract_mode"])] += 1
        categories[category_id]["jobs"] += 1
        categories[category_id]["source_unique_reviews"] += int(
            report.get("source_unique_reviews") or 0
        )
        categories[category_id]["eligible_reviews"] += int(
            report.get("eligible_reviews") or 0
        )
        categories[category_id]["written_reviews"] += written
        markets[region] += eligible
        channels[channel] += eligible
        totals["jobs"] += 1
        totals["source_unique_reviews"] += int(
            report.get("source_unique_reviews") or 0
        )
        totals["eligible_reviews"] += int(report.get("eligible_reviews") or 0)
        totals["written_reviews"] += written
        totals["excluded_reviews"] += int(report.get("excluded_reviews") or 0)
        totals["raw_records"] += int(quality.get("raw_record_count") or 0)
        if channel == "amazon_reviews_2023":
            amazon_jobs += 1
            if region != "North America":
                raise RuntimeError(f"{job_id} Amazon market is not North America")
            archive_through = str(
                report.get("archive_through")
                or (report.get("product") or {}).get("archive_through")
                or ""
            )
            if archive_through != "2023-09-30":
                raise RuntimeError(
                    f"{job_id} Amazon archive_through={archive_through!r}"
                )

    return {
        "reportable_jobs": totals["jobs"],
        "source_unique_reviews": totals["source_unique_reviews"],
        "eligible_reviews": totals["eligible_reviews"],
        "written_reviews": totals["written_reviews"],
        "excluded_reviews": totals["excluded_reviews"],
        "raw_records_reconciled": totals["raw_records"],
        "amazon_jobs": amazon_jobs,
        "output_contract_counts": dict(sorted(output_contracts.items())),
        "by_market_eligible": dict(sorted(markets.items())),
        "by_channel_eligible": dict(sorted(channels.items())),
        "by_category": {
            category_id: dict(counts)
            for category_id, counts in sorted(categories.items())
        },
    }


def file_manifest(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file()
        and candidate.name not in {"snapshot-manifest.json", "SHA256SUMS"}
    ):
        rows.append(
            {
                "path": str(path.relative_to(root)),
                "bytes": path.stat().st_size,
                "sha256": f"sha256:{sha256_file(path)}",
            }
        )
    return rows


def make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def build_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise RuntimeError(f"output already exists: {output_root}")
    if not (source_root / "data" / "jobs.sqlite3").is_file():
        raise RuntimeError("source jobs.sqlite3 is missing")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_root.name}-",
            dir=output_root.parent,
        )
    )
    try:
        backups_dir = staging_root / "data" / "backups"
        backups_dir.mkdir(parents=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        database = backups_dir / f"jobs-consistent-snapshot-{timestamp}.sqlite3"
        database_jobs = backup_database(
            source_root / "data" / "jobs.sqlite3",
            database,
        )
        copied, non_reportable_exclusions = copy_reportable_artifacts(
            source_root,
            staging_root,
        )
        product_master_manifest = None
        projection_source = getattr(args, "product_master_projection", None)
        if projection_source is not None:
            projection_source = Path(projection_source).resolve()
            projection = load_product_master_file(projection_source)
            projection_target = staging_root / PRODUCT_MASTER_FILENAME
            shutil.copy2(projection_source, projection_target)
            product_master_manifest = {
                "schema_version": PRODUCT_MASTER_SCHEMA_VERSION,
                "snapshot_id": projection.snapshot_id,
                "hash_algorithm": PRODUCT_MASTER_HASH_ALGORITHM,
                "authority": "chengmu",
                "path": PRODUCT_MASTER_FILENAME,
                "sha256": f"sha256:{sha256_file(projection_target)}",
                "records": len(projection.records),
            }
        expected_job_ids = {row["job_id"] for row in copied}
        reportable_jobs = [
            job
            for job in load_jobs(staging_root)
            if str(job.get("id") or "") in expected_job_ids
        ]
        energy_facts = [controlled_energy_fact(job) for job in reportable_jobs]
        energy_type_counts = dict(sorted(Counter(
            str(fact["energy_type"]) for fact in energy_facts
        ).items()))
        energy_contract_status_counts = dict(sorted(Counter(
            str(fact["contract_status"]) for fact in energy_facts
        ).items()))
        controlled_energy = {
            "schema_version": CONTROLLED_ENERGY_SCHEMA_VERSION,
            "authority": "wotu_task_fact",
            "reportable_job_count": len(reportable_jobs),
            "energy_type_counts": energy_type_counts,
            "contract_status_counts": energy_contract_status_counts,
            "legacy_null_display": "unknown",
            "inference_policy": "never_infer",
        }
        source_contract_counts = dict(
            sorted(Counter(row["source_contract_mode"] for row in copied).items())
        )
        text_validity_reconciliation = {
            "policy": "is_valid_text_review := bool(trim(review_text))",
            "jobs_with_mismatch": sum(
                int(row["source_text_validity_mismatch_rows"] > 0)
                for row in copied
            ),
            "mismatch_rows": sum(
                int(row["source_text_validity_mismatch_rows"])
                for row in copied
            ),
        }
        source_completeness = sorted(
            (row["source_completeness"] for row in copied),
            key=lambda item: item["job_id"],
        )
        category_resolutions = {
            "schema_version": CATEGORY_RESOLUTION_SCHEMA_VERSION,
            "jobs": sorted(
                (
                    {
                        "job_id": str(row["job_id"]),
                        **dict(row["category_resolution"]),
                    }
                    for row in copied
                ),
                key=lambda item: item["job_id"],
            ),
        }
        validation = validate_snapshot(staging_root, expected_job_ids)
        files = file_manifest(staging_root)
        identity = {
            "package_version": SNAPSHOT_PACKAGE_VERSION,
            "connector_commit": args.connector_commit,
            "reporting_commit": args.reporting_commit,
            "files": files,
            "reportable_job_ids": sorted(expected_job_ids),
            "non_reportable_exclusions": non_reportable_exclusions,
            "source_completeness": source_completeness,
            "controlled_energy": controlled_energy,
            "category_resolution": category_resolutions,
        }
        identity_hash = hashlib.sha256(
            canonical_json(identity).encode("utf-8")
        ).hexdigest()
        manifest = {
            "schema_version": SNAPSHOT_PACKAGE_VERSION,
            "snapshot_id": f"prod_v2_{identity_hash[:24]}",
            "created_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "production_commits": {
                "connector": args.connector_commit,
                "reporting": args.reporting_commit,
            },
            "contracts": {
                "review_fact": FACT_SCHEMA_VERSION,
                "snapshot_manifest": "snapshot-manifest/v2",
                "market_eligibility": MARKET_CONTRACT_VERSION,
                "parser": PARSER_VERSION,
                "normalization": NORMALIZATION_VERSION,
                "snapshot_transform": SNAPSHOT_TRANSFORM_VERSION,
                "true_empty_source_policy": TRUE_EMPTY_SOURCE_POLICY_VERSION,
                "unsupported_partial_policy": (
                    UNSUPPORTED_PARTIAL_POLICY_VERSION
                ),
                "text_population_policy": TEXT_POPULATION_POLICY,
                "source_completeness": "source-completeness-audit/v1",
                "controlled_energy": CONTROLLED_ENERGY_SCHEMA_VERSION,
                "category_resolution": (
                    CATEGORY_RESOLUTION_SCHEMA_VERSION
                ),
                **(
                    {"product_master": PRODUCT_MASTER_SCHEMA_VERSION}
                    if product_master_manifest is not None
                    else {}
                ),
            },
            **(
                {"product_master": product_master_manifest}
                if product_master_manifest is not None
                else {}
            ),
            "database": {
                "path": str(database.relative_to(staging_root)),
                "integrity_check": "ok",
                "jobs": database_jobs,
                "sha256": f"sha256:{sha256_file(database)}",
            },
            "selection": {
                "reportable_contract": "normalized-v2-all-eligible",
                "output_contract": FACT_SCHEMA_VERSION,
                "source_contract_counts": source_contract_counts,
                "transformed_legacy_jobs": source_contract_counts.get("legacy", 0),
                "current_source_jobs": source_contract_counts.get("current", 0),
                "text_validity_reconciliation": (
                    text_validity_reconciliation
                ),
                "reportable_jobs": len(expected_job_ids),
                "job_ids": sorted(expected_job_ids),
                "copied_artifacts": copied,
                "source_completeness": source_completeness,
                "controlled_energy": controlled_energy,
                "category_resolution": category_resolutions,
                "non_reportable_jobs": len(non_reportable_exclusions),
                "non_reportable_exclusions": non_reportable_exclusions,
            },
            "reconciliation": validation,
            "files": files,
            "content_sha256": f"sha256:{identity_hash}",
        }
        manifest_path = staging_root / "snapshot-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        checksum_rows = [
            f"{sha256_file(path)}  {path.relative_to(staging_root)}"
            for path in sorted(
                candidate
                for candidate in staging_root.rglob("*")
                if candidate.is_file()
                and candidate.name != "SHA256SUMS"
            )
        ]
        (staging_root / "SHA256SUMS").write_text(
            "\n".join(checksum_rows) + "\n",
            encoding="utf-8",
        )
        os.replace(staging_root, output_root)
        make_read_only(output_root)
        return manifest
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build an immutable current-contract reporting snapshot package"
        )
    )
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--connector-commit", required=True)
    parser.add_argument("--reporting-commit", required=True)
    parser.add_argument(
        "--product-master-projection",
        type=Path,
        help=(
            "Optional approved/published retail-product-master/v2 complete "
            "projection to validate and freeze into the snapshot"
        ),
    )
    return parser.parse_args()


def main() -> int:
    manifest = build_snapshot(parse_args())
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
