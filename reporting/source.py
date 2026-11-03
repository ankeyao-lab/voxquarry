from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from reporting.category_vocabulary import (
    canonical_category_name,
    category_id_for,
    category_resolution,
)
from reporting.controlled_energy import (
    SCHEMA_VERSION as CONTROLLED_ENERGY_SCHEMA_VERSION,
    controlled_energy_fact,
    energy_catalog,
)
from reporting.config import (
    ELIGIBLE_VALIDATION_STATUSES,
    FACT_SCHEMA_VERSION,
    LEGACY_FACT_SCHEMA_VERSION,
    LEGACY_NORMALIZATION_VERSION,
    LEGACY_PARSER_VERSION,
    MARKET_CONTRACT_VERSION,
    MARKET_REGIONS,
    NORMALIZATION_VERSION,
    PARSER_VERSION,
    SELECTION_POLICY_VERSION,
)
from reporting.source_completeness import (
    SourceCompletenessError,
    source_completeness_audit,
)
from reporting.product_master import (
    ProductMasterError,
    load_product_master,
    validate_task_product_master_binding,
)
from reporting.voltage_platform import (
    SCHEMA_VERSION as VOLTAGE_PLATFORM_SCHEMA_VERSION,
    platform_catalog,
    voltage_platform_fact,
)
from reporting.product_form import FORM_SCHEMA_VERSION, product_form_fact, form_catalog


LEGACY_STANDARD_FIELDS = (
    "channel",
    "product_id",
    "product_title",
    "product_url",
    "review_id",
    "source_site",
    "source_product_id",
    "is_syndicated",
    "submission_time",
    "title",
    "is_recommended",
    "user_nickname",
    "review_text",
    "rating",
    "verified_purchaser",
    "where_purchase",
    "age",
)
MARKET_STANDARD_FIELDS = (
    "display_site",
    "origin_source_name",
    "origin_source_url",
    "origin_source_site",
    "review_locale",
    "market_region",
    "market_region_evidence",
    "review_word_count",
    "is_valid_text_review",
    "is_eligible",
    "exclusion_reason",
    "is_translated",
)
STANDARD_FIELDS = LEGACY_STANDARD_FIELDS + MARKET_STANDARD_FIELDS
REQUIRED_MARKET_CSV_FIELDS = {
    "display_site",
    "source_site",
    "market_region",
    "market_region_evidence",
    "is_valid_text_review",
    "is_eligible",
}
MARKET_CONTRACT_MARKER_FIELDS = {
    "market_region",
    "market_region_evidence",
    "is_valid_text_review",
    "is_eligible",
}
TRUE_VALUES = {"1", "true", "t", "yes", "y"}
FALSE_VALUES = {"0", "false", "f", "no", "n"}
# This is a scope/catalog value, not a mutation of the Review Fact model field.
# Facts retain an empty model when the collection task did not identify one.
UNSPECIFIED_MODEL = "未明确型号"
SNAPSHOT_PACKAGE_SCHEMA = "voxquarry-reporting-snapshot/v2"


class MarketContractError(ValueError):
    """Raised when market/eligibility evidence cannot be trusted."""


def _enforce_snapshot_package_contract(
    snapshot_root: Path,
    runs: list[dict[str, Any]],
) -> None:
    """Reject a v2 package whose physical runs are not uniformly v2.

    Historical source roots and v1 snapshots remain readable.  Once a root
    declares itself as the immutable v2 reporting package, however, exposing
    even one legacy run would make Catalog selection misleading and would
    reintroduce mixed-contract report failures.
    """

    manifest_path = snapshot_root / "snapshot-manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketContractError(
            "报告快照清单不可读取，已拒绝使用该数据源"
        ) from exc
    if clean(manifest.get("schema_version")) != SNAPSHOT_PACKAGE_SCHEMA:
        return
    declared_fact = clean((manifest.get("contracts") or {}).get("review_fact"))
    if declared_fact != FACT_SCHEMA_VERSION:
        raise MarketContractError(
            "v2 报告快照声明了不兼容的 Review Fact 契约"
        )
    legacy_job_ids = sorted(
        clean(run.get("job", {}).get("id"))
        for run in runs
        if run.get("market_contract_mode") != "current"
    )
    if legacy_job_ids:
        preview = "、".join(legacy_job_ids[:5])
        suffix = "…" if len(legacy_job_ids) > 5 else ""
        raise MarketContractError(
            "v2 报告快照包含未升级任务：" + preview + suffix
        )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def model_scope_value(value: object) -> str:
    """Return the stable UI/scope value for a task's model dimension.

    An empty task model is meaningful source metadata: it identifies an
    otherwise reportable task whose product model was not established at
    collection time.  It must therefore remain selectable rather than being
    silently dropped by catalog construction or scope filtering.
    """

    return clean(value) or UNSPECIFIED_MODEL


def market_region_value(value: object, *, allow_missing: bool = False) -> str:
    normalized = clean(value)
    if not normalized and allow_missing:
        return ""
    if normalized not in MARKET_REGIONS:
        raise MarketContractError(
            "market_region 必须是 North America、Europe、Australia 或 Other"
        )
    return normalized


def _job_market_region(job: dict[str, Any]) -> str:
    return market_region_value(job.get("market_region"), allow_missing=True)


def _report_market_region(report: dict[str, Any]) -> str:
    product = report.get("product") or {}
    return market_region_value(
        report.get("market_region") or product.get("market_region"),
        allow_missing=True,
    )


def _run_contract_mode(run: dict[str, Any]) -> str:
    with run["csv_path"].open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        fields = set(csv.DictReader(handle).fieldnames or [])
    return _contract_mode_for_fields(run, fields)


def _contract_mode_for_fields(run: dict[str, Any], fields: set[str]) -> str:
    """The same contract check for a bounded, safely opened catalog header."""
    present = fields & MARKET_CONTRACT_MARKER_FIELDS
    if not present:
        return "legacy"
    missing = sorted(REQUIRED_MARKET_CSV_FIELDS - fields)
    if missing:
        raise MarketContractError(
            "新市场契约 CSV 缺少字段：" + ", ".join(missing)
        )
    job_region = _job_market_region(run["job"])
    report_region = _report_market_region(run["report"])
    if not job_region or not report_region:
        raise MarketContractError("新市场契约任务或 validation report 缺少地区")
    if job_region != report_region:
        raise MarketContractError(
            f"任务与 validation report 地区不一致：{job_region} != {report_region}"
        )
    if run["channel"] == "amazon_reviews_2023":
        archive_through = clean(
            run["report"].get("archive_through")
            or (run["report"].get("product") or {}).get("archive_through")
        )
        if job_region != "North America":
            raise MarketContractError(
                "Amazon Reviews 2023 必须标记为 North America 静态历史来源"
            )
        if archive_through != "2023-09-30":
            raise MarketContractError(
                "Amazon Reviews 2023 缺少 archive_through=2023-09-30"
            )
    return "current"


def _job_category(job: dict[str, Any]) -> str:
    return canonical_category_name(job.get("category"))


def _job_category_id(job: dict[str, Any]) -> str:
    return category_id_for(job.get("category")) or ""


def parse_boolean(value: object) -> bool | None:
    normalized = clean(value).casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def parse_rating(value: object) -> int | None:
    normalized = clean(value)
    if not normalized:
        return None
    try:
        number = float(normalized)
    except ValueError:
        return None
    if number.is_integer() and 1 <= number <= 5:
        return int(number)
    return None


def parse_submission_time(value: object, source_site: object) -> tuple[str | None, str]:
    normalized = clean(value)
    if not normalized:
        return None, "blank"
    source = clean(source_site).casefold()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.date().isoformat(), "iso"
    except ValueError:
        pass
    formats = []
    if "ryobi" in source and ("eu" in source or "europe" in source):
        formats.append(("%d/%m/%Y", "source-rule-dmy"))
    formats.extend(
        (
            ("%Y-%m-%d", "date-iso"),
            ("%Y/%m/%d", "date-slash-ymd"),
            ("%m/%d/%Y", "date-slash-mdy"),
            ("%m/%d/%y", "date-slash-mdy-short"),
            ("%B %d, %Y", "month-name-mdy"),
            ("%b %d, %Y", "month-abbrev-mdy"),
            ("%d %B %Y", "month-name-dmy"),
            ("%d %b %Y", "month-abbrev-dmy"),
        )
    )
    for date_format, rule in formats:
        try:
            return datetime.strptime(normalized, date_format).date().isoformat(), rule
        except ValueError:
            continue
    return None, "unparsed"


def sqlite_uri(path: Path) -> str:
    encoded = quote(str(path.resolve()), safe="/:")
    return f"file:{encoded}?mode=ro&immutable=1"


def locate_database(snapshot_root: Path) -> Path:
    candidates = sorted(
        (snapshot_root / "data" / "backups").glob(
            "jobs-consistent-snapshot-*.sqlite3"
        )
    )
    if len(candidates) != 1:
        raise RuntimeError(
            "expected one consistent SQLite snapshot, "
            f"found {len(candidates)} under {snapshot_root}"
        )
    return candidates[0]


def load_jobs(snapshot_root: Path) -> list[dict[str, Any]]:
    database = locate_database(snapshot_root)
    connection = sqlite3.connect(sqlite_uri(database), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM jobs")]
    finally:
        connection.close()


def _package_non_reportable_exclusions(
    snapshot_root: Path,
) -> list[dict[str, Any]]:
    path = snapshot_root / "snapshot-manifest.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = (payload.get("selection") or {}).get(
        "non_reportable_exclusions"
    ) or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _public_non_reportable_exclusion(row: dict[str, Any]) -> dict[str, Any]:
    counts = row.get("counts") or {}
    return {
        "job_id": clean(row.get("job_id")),
        "reason_code": clean(row.get("reason_code")),
        "policy_version": clean(row.get("policy_version")),
        "category_id": clean(row.get("category_id")),
        "category": clean(row.get("category")),
        "brand": clean(row.get("brand")),
        "model": clean(row.get("model")),
        "channel": clean(row.get("channel")),
        "market_region": clean(row.get("market_region")),
        "excluded_records": int(row.get("excluded_records") or 0),
        "source_total": int(counts.get("source_total") or 0),
        "collected_reviews": int(counts.get("collected_reviews") or 0),
        "missing_reviews": int(counts.get("missing_reviews") or 0),
        "old_stop_reason": clean(row.get("old_stop_reason")) or None,
        "missing_contract_fields": list(
            row.get("missing_contract_fields") or []
        ),
        "message": (
            "旧部分采集契约不受支持，"
            f"{int(row.get('excluded_records') or 0):,}条未进入当前快照/报告"
        ),
    }


def _scope_non_reportable_exclusions(
    snapshot_root: Path,
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    category_id = clean(scope.get("category_id"))
    categories = {clean(value) for value in scope.get("categories") or []}
    dimensions = {
        "brand": {clean(value) for value in scope.get("brands") or []},
        "channel": {clean(value) for value in scope.get("channels") or []},
        "model": {clean(value) for value in scope.get("models") or []},
        "market_region": {
            clean(value) for value in scope.get("market_regions") or []
        },
    }
    matched: list[dict[str, Any]] = []
    for row in _package_non_reportable_exclusions(snapshot_root):
        if clean(row.get("reason_code")) != (
            "unsupported_legacy_partial_contract"
        ):
            continue
        if category_id and clean(row.get("category_id")) != category_id:
            continue
        if categories and clean(row.get("category")) not in categories:
            continue
        if any(
            allowed and clean(row.get(field)) not in allowed
            for field, allowed in dimensions.items()
        ):
            continue
        matched.append(_public_non_reportable_exclusion(row))
    return sorted(matched, key=lambda item: item["job_id"])


def validate_scope_job_ids(
    snapshot_root: Path, scope: dict[str, Any], *,
    source_jobs: list[dict[str, Any]] | None = None,
    catalog_payload: dict[str, Any] | None = None,
) -> None:
    """Reject task selections that do not exactly belong to the chosen category."""
    requested = {clean(value) for value in scope.get("job_ids") or [] if clean(value)}
    if not requested:
        raise ValueError("at least one reportable job_id is required")
    category_id = clean(scope.get("category_id"))
    if not category_id:
        raise ValueError("one stable category_id is required")
    known_job_ids = {
        clean(job.get("id")) for job in (load_jobs(snapshot_root) if source_jobs is None else source_jobs)
        if clean(job.get("id"))
    }
    missing = sorted(requested - known_job_ids)
    if missing:
        raise ValueError(f"unknown job_ids: {', '.join(missing)}")
    reportable_tasks = {
        clean(task.get("id")): task for task in (catalog(snapshot_root) if catalog_payload is None else catalog_payload)["tasks"]
    }
    invalid = sorted(requested - set(reportable_tasks))
    if invalid:
        raise ValueError(
            "job_ids are not reportable: " + ", ".join(invalid)
        )
    wrong_category = sorted(
        job_id
        for job_id in requested
        if clean(reportable_tasks[job_id].get("category_id")) != category_id
    )
    if wrong_category:
        raise ValueError(
            "job_ids do not belong to category_id "
            f"{category_id}: {', '.join(wrong_category)}"
        )
    requested_forms = set(scope.get("product_form_factors") or [])
    wrong_form = sorted(job_id for job_id in requested if requested_forms and
                        (reportable_tasks[job_id].get("product_form_factor") or "unknown") not in requested_forms)
    if wrong_form:
        raise ValueError("job_ids do not belong to product_form_factors: " + ", ".join(wrong_form))
    requested_energy = clean(scope.get("energy_type"))
    if category_id == "pressure_washer" and not requested_energy:
        raise ValueError("Pressure Washer 报告必须且只能选择一个能源类型")
    if requested_energy and requested_energy != "all":
        wrong_energy = sorted(
            job_id
            for job_id in requested
            if clean(reportable_tasks[job_id].get("energy_type"))
            != requested_energy
        )
        if wrong_energy:
            raise ValueError(
                "job_ids do not belong to energy_type "
                f"{requested_energy}: {', '.join(wrong_energy)}"
            )
    requested_regions = set(scope.get("market_regions") or [])
    if requested_regions:
        legacy = sorted(
            job_id
            for job_id in requested
            if reportable_tasks[job_id].get("market_contract_mode")
            != "current"
        )
        if legacy:
            raise MarketContractError(
                "旧契约任务不能按市场地区筛选：" + ", ".join(legacy)
            )
        wrong_region = sorted(
            job_id
            for job_id in requested
            if reportable_tasks[job_id].get("market_region")
            not in requested_regions
        )
        if wrong_region:
            raise MarketContractError(
                "所选 job_ids 不属于 scope.market_regions："
                + ", ".join(wrong_region)
            )


def _read_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _product_identity(report: dict[str, Any], job: dict[str, Any]) -> str:
    product = report.get("product") or {}
    return clean(product.get("id")) or clean(job.get("target_url")) or clean(job["id"])


def discover_selected_runs(
    snapshot_root: Path, scope: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    jobs = load_jobs(snapshot_root)
    job_ids = set(scope.get("job_ids") or [])
    categories = set(scope.get("categories") or [])
    excluded_categories = set(scope.get("exclude_categories") or [])
    channels = set(scope.get("channels") or [])
    brands = set(scope.get("brands") or [])
    models = set(scope.get("models") or [])
    market_regions = set(scope.get("market_regions") or [])
    energy_type = clean(scope.get("energy_type"))
    requested_forms = set(scope.get("product_form_factors") or [])

    candidates: list[dict[str, Any]] = []
    for job in jobs:
        if not clean(job.get("id")):
            continue
        if job_ids and clean(job.get("id")) not in job_ids:
            continue
        if clean(job.get("status")) not in {"completed", "partial_success"}:
            continue
        if clean(job.get("superseded_by")):
            continue
        job_category = _job_category(job)
        if categories and job_category not in categories:
            continue
        if job_category in excluded_categories:
            continue
        if requested_forms and product_form_fact(job)["value"] not in requested_forms:
            continue
        if channels and clean(job.get("channel")) not in channels:
            continue
        if brands and clean(job.get("brand")) not in brands:
            continue
        if models and model_scope_value(job.get("model")) not in models:
            continue
        if (
            energy_type
            and energy_type != "all"
            and controlled_energy_fact(job)["energy_type"] != energy_type
        ):
            continue
        channel = clean(job.get("channel"))
        active_run_path = clean(job.get("active_run_path"))
        if active_run_path:
            run_dir = snapshot_root / "data" / "runs" / active_run_path
        else:
            run_dir = snapshot_root / "data" / "runs" / clean(job["id"]) / channel
        report_path = run_dir / "validation_report.json"
        csv_path = run_dir / "reviews.csv"
        if not report_path.is_file() or not csv_path.is_file():
            continue
        report = _read_report(report_path)
        run = {
            "job": job,
            "channel": channel,
            "run_dir": run_dir,
            "report_path": report_path,
            "csv_path": csv_path,
            "report": report,
            "product_identity": _product_identity(report, job),
        }
        run["market_contract_mode"] = _run_contract_mode(run)
        if market_regions:
            if run["market_contract_mode"] != "current":
                raise MarketContractError(
                    "旧契约工件不能按市场地区筛选；请使用旧快照原口径展示"
                )
            if _job_market_region(job) not in market_regions:
                continue
        candidates.append(run)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in candidates:
        grouped[(run["channel"], run["product_identity"])].append(run)

    latest: list[dict[str, Any]] = []
    for values in grouped.values():
        values.sort(
            key=lambda run: (
                1 if clean(run["job"].get("status")) == "completed" else 0,
                clean(run["job"].get("finished_at")),
                clean(run["job"].get("created_at")),
                clean(run["job"]["id"]),
            )
        )
        latest.append(values[-1])
    latest.sort(key=lambda run: (run["channel"], run["product_identity"]))
    eligible = [
        run
        for run in latest
        if clean(run["report"].get("status")) in ELIGIBLE_VALIDATION_STATUSES
    ]
    return eligible, {
        "jobs_total": len(jobs),
        "candidate_completed_runs": len(candidates),
        "latest_product_runs": len(latest),
        "eligible_runs": len(eligible),
        "selected_job_ids": sorted(
            clean(run["job"].get("id")) for run in eligible
        ),
        "excluded_latest_runs": len(latest) - len(eligible),
        "latest_validation_statuses": dict(
            sorted(Counter(clean(run["report"].get("status")) for run in latest).items())
        ),
    }


def _business_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    review_id = clean(row.get("review_id"))
    source_site = clean(row.get("source_site")).casefold()
    source_product_id = clean(row.get("source_product_id")).casefold()
    if not review_id or not source_site or not source_product_id:
        return None
    return source_site, source_product_id, review_id


def _syndication_allowed(value: bool | None, policy: str) -> bool:
    if policy == "exclude_explicit":
        return value is not True
    if policy == "only_explicit":
        return value is True
    if policy == "unknown_only":
        return value is None
    return True


def _row_matches_scope(fact: dict[str, Any], scope: dict[str, Any]) -> bool:
    if scope.get("product_form_factors") and fact.get("product_form_factor", "unknown") not in scope["product_form_factors"]:
        return False
    if scope.get("source_product_ids") and fact.get("source_product_id") not in scope["source_product_ids"]:
        return False
    ratings = set(scope.get("ratings") or [])
    if ratings and fact["rating"] not in ratings:
        return False
    has_text = scope.get("has_text", "any")
    if has_text is True or has_text == "true":
        if not fact["has_text"]:
            return False
    elif has_text is False or has_text == "false":
        if fact["has_text"]:
            return False
    if not _syndication_allowed(
        fact["is_syndicated"], scope.get("syndicated", "include")
    ):
        return False
    if (
        scope.get("fallback_source") == "exclude"
        and fact["collection_method"] == "fallback"
    ):
        return False
    start_date = clean((scope.get("date_range") or {}).get("start"))
    end_date = clean((scope.get("date_range") or {}).get("end"))
    submission_date = fact["submission_date"]
    if start_date and (not submission_date or submission_date < start_date):
        return False
    if end_date and (not submission_date or submission_date > end_date):
        return False
    keywords = [
        clean(item).casefold()
        for item in scope.get("keywords") or []
        if clean(item)
    ]
    if keywords:
        keyword_field = clean(
            scope.get("keyword_field") or "title_and_body"
        ).casefold()
        if keyword_field == "title":
            target = fact["review_title"].casefold()
        elif keyword_field == "body":
            target = fact["review_text"].casefold()
        else:
            target = f"{fact['review_title']} {fact['review_text']}".casefold()
        mode = clean(scope.get("keyword_mode") or "or").casefold()
        if mode == "and" and not all(term in target for term in keywords):
            return False
        if mode == "exact" and not any(
            _term_phrase_match(target, term) for term in keywords
        ):
            return False
        if mode not in {"and", "exact"} and not any(
            term in target for term in keywords
        ):
            return False
    return True


def _term_phrase_match(target: str, term: str) -> bool:
    start = 0
    while True:
        index = target.find(term, start)
        if index < 0:
            return False
        before_ok = index == 0 or not target[index - 1].isalnum()
        end = index + len(term)
        after_ok = end == len(target) or not target[end].isalnum()
        if before_ok and after_ok:
            return True
        start = index + 1


def _normalized_fact(
    row: dict[str, Any],
    run: dict[str, Any],
    row_number: int,
    artifact_sha256: str,
    snapshot_root: Path,
    market_contract_mode: str,
) -> dict[str, Any]:
    job = run["job"]
    source_site_raw = clean(row.get("source_site"))
    submission_date, time_rule = parse_submission_time(
        row.get("submission_time"), source_site_raw
    )
    rating = parse_rating(row.get("rating"))
    syndicated = parse_boolean(row.get("is_syndicated"))
    recommended = parse_boolean(row.get("is_recommended"))
    verified = parse_boolean(row.get("verified_purchaser"))
    review_text = clean(row.get("review_text"))
    review_title = clean(row.get("title"))
    relative_path = str(run["csv_path"].relative_to(snapshot_root))
    occurrence_material = {
        "artifact": relative_path,
        "row_number": row_number,
        "job_id": clean(job["id"]),
        "review_id": clean(row.get("review_id")),
    }
    occurrence_id = f"occ_{sha256_text(canonical_json(occurrence_material))[:24]}"
    key = _business_key(row)
    entity_id = (
        f"rev_{sha256_text(canonical_json(key))[:24]}" if key is not None else None
    )
    normalized_fields = (
        STANDARD_FIELDS
        if market_contract_mode == "current"
        else LEGACY_STANDARD_FIELDS
    )
    normalized_content = {
        field: clean(row.get(field)) for field in normalized_fields
    }
    row_content_sha256 = f"sha256:{sha256_text(canonical_json(normalized_content))}"
    validation_status = clean(run["report"].get("status"))
    is_eligible = (
        parse_boolean(row.get("is_eligible"))
        if market_contract_mode == "current"
        else None
    )
    is_valid_text_review = (
        parse_boolean(row.get("is_valid_text_review"))
        if market_contract_mode == "current"
        else None
    )
    market_region = (
        market_region_value(row.get("market_region"))
        if market_contract_mode == "current"
        else ""
    )
    return {
        "review_occurrence_id": occurrence_id,
        "review_entity_id": entity_id,
        "review_id": clean(row.get("review_id")),
        "canonical_entity_rank": 1,
        "is_duplicate_occurrence": False,
        "job_id": clean(job["id"]),
        "job_finished_at": clean(job.get("finished_at")),
        "channel": clean(row.get("channel")) or run["channel"],
        "product_id": clean(row.get("product_id")) or run["product_identity"],
        "product_title": clean(row.get("product_title")),
        "product_url": clean(row.get("product_url")),
        "brand": clean(job.get("brand")),
        "category_id": _job_category_id(job),
        "category": _job_category(job),
        "product_form_factor": product_form_fact(job)["value"],
        "product_form": product_form_fact(job),
        "model": clean(job.get("model")),
        "market_region": market_region,
        "market_region_evidence": clean(
            row.get("market_region_evidence")
        ),
        "display_site": clean(row.get("display_site")),
        "source_site_raw": source_site_raw,
        "source_site_normalized": source_site_raw.casefold(),
        "origin_source_name": clean(row.get("origin_source_name")),
        "origin_source_url": clean(row.get("origin_source_url")),
        "origin_source_site": clean(row.get("origin_source_site")),
        "review_locale": clean(row.get("review_locale")),
        "source_product_id": clean(row.get("source_product_id")),
        "collection_method": (
            "fallback" if validation_status == "COMPLETE_FALLBACK_SOURCE" else "primary"
        ),
        "validation_status": validation_status,
        "is_syndicated_raw": clean(row.get("is_syndicated")),
        "is_syndicated": syndicated,
        "syndication_parse_status": "parsed" if syndicated is not None else "unknown",
        "submission_time_raw": clean(row.get("submission_time")),
        "submission_date": submission_date,
        "time_parse_status": "parsed" if submission_date else time_rule,
        "time_parse_rule": time_rule,
        "review_title": review_title,
        "review_text": review_text,
        "has_text": bool(review_text),
        "review_word_count": (
            int(clean(row.get("review_word_count")))
            if clean(row.get("review_word_count")).isdigit()
            else None
        ),
        "is_eligible": is_eligible,
        "is_valid_text_review": is_valid_text_review,
        "exclusion_reason": clean(row.get("exclusion_reason")),
        "is_translated": parse_boolean(row.get("is_translated")),
        "rating_raw": clean(row.get("rating")),
        "rating": rating,
        "rating_valid": rating is not None,
        "is_recommended_raw": clean(row.get("is_recommended")),
        "is_recommended": recommended,
        "recommended_parse_status": "parsed" if recommended is not None else "unknown",
        "verified_purchaser_raw": clean(row.get("verified_purchaser")),
        "verified_purchaser": verified,
        "verified_parse_status": "parsed" if verified is not None else "unknown",
        "where_purchase": clean(row.get("where_purchase")),
        "age": clean(row.get("age")),
        "business_key_status": "complete" if key is not None else "incomplete",
        "eligibility_status": (
            "eligible_text"
            if is_eligible is True and is_valid_text_review is True
            else "eligible_non_text"
            if is_eligible is True and is_valid_text_review is False
            else "legacy"
            if market_contract_mode == "legacy"
            else "ineligible"
            if is_eligible is False
            else "invalid_text"
        ),
        "exclusion_reasons": [
            value
            for value in (clean(row.get("exclusion_reason")),)
            if value
        ],
        "source_artifact_path": relative_path,
        "source_row_number": row_number,
        "source_artifact_sha256": f"sha256:{artifact_sha256}",
        "market_contract_mode": market_contract_mode,
        "fact_schema_version": (
            FACT_SCHEMA_VERSION
            if market_contract_mode == "current"
            else LEGACY_FACT_SCHEMA_VERSION
        ),
        "parser_version": (
            PARSER_VERSION
            if market_contract_mode == "current"
            else LEGACY_PARSER_VERSION
        ),
        "normalization_version": (
            NORMALIZATION_VERSION
            if market_contract_mode == "current"
            else LEGACY_NORMALIZATION_VERSION
        ),
        "row_content_sha256": row_content_sha256,
    }


def build_review_fact(
    snapshot_root: Path, scope: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    selected_runs, selection = discover_selected_runs(snapshot_root, scope)
    _enforce_snapshot_package_contract(snapshot_root, selected_runs)
    historical_sources = []
    for run in selected_runs:
        if run["channel"] != "amazon_reviews_2023":
            continue
        product = run["report"].get("product") or {}
        historical_sources.append(
            {
                "channel": run["channel"],
                "job_id": clean(run["job"].get("id")),
                "source_dataset": clean(
                    run["report"].get("source_dataset")
                    or product.get("source_dataset")
                ),
                "source_subset": clean(
                    run["report"].get("source_subset")
                    or product.get("source_subset")
                ),
                "archive_through": clean(
                    run["report"].get("archive_through")
                    or product.get("archive_through")
                ),
                "market_region": _job_market_region(run["job"]),
            }
        )
    historical_sources.sort(
        key=lambda item: (
            item["channel"],
            item["source_dataset"],
            item["source_subset"],
            item["job_id"],
        )
    )
    modes = {run["market_contract_mode"] for run in selected_runs}
    if len(modes) > 1:
        raise MarketContractError(
            "旧契约与新市场契约工件不得在同一报告中混合"
        )
    market_contract_mode = next(iter(modes), "legacy")
    if market_contract_mode == "legacy" and scope.get("market_regions"):
        raise MarketContractError(
            "旧契约快照缺少地区字段，不能按市场地区筛选"
        )
    facts: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    region_summary: dict[str, Counter[str]] = {
        region: Counter(
            {
                "record_count": 0,
                "written_review_count": 0,
                "eligible_valid_text_count": 0,
                "missing_contract_record_count": 0,
            }
        )
        for region in MARKET_REGIONS
    }
    raw_record_count = 0
    excluded_ineligible = 0
    excluded_invalid_text = 0
    non_text_records = 0
    legacy_contract_records = 0
    source_completeness: list[dict[str, Any]] = []
    for run in selected_runs:
        if market_contract_mode == "current":
            if run["report"].get("excluded_reviews") is None:
                raise MarketContractError(
                    "新市场契约 validation report 缺少 excluded_reviews"
                )
        csv_hash = sha256_file(run["csv_path"])
        report_hash = sha256_file(run["report_path"])
        inputs.extend(
            (
                {
                    "path": str(run["csv_path"].relative_to(snapshot_root)),
                    "sha256": f"sha256:{csv_hash}",
                    "kind": "normalized-reviews-csv",
                },
                {
                    "path": str(run["report_path"].relative_to(snapshot_root)),
                    "sha256": f"sha256:{report_hash}",
                    "kind": "validation-report",
                },
            )
        )
        with run["csv_path"].open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            run_eligible_records = 0
            run_valid_text_records = 0
            for row_number, row in enumerate(reader, start=2):
                if market_contract_mode == "current":
                    row_region = market_region_value(row.get("market_region"))
                    job_region = _job_market_region(run["job"])
                    if row_region != job_region:
                        raise MarketContractError(
                            "任务与评论地区不一致："
                            f"job={clean(run['job'].get('id'))} "
                            f"task={job_region} review={row_region}"
                        )
                    if not clean(row.get("market_region_evidence")):
                        raise MarketContractError(
                            "新市场契约评论缺少 market_region_evidence"
                        )
                    eligible = parse_boolean(row.get("is_eligible"))
                    valid_text = parse_boolean(
                        row.get("is_valid_text_review")
                    )
                    if eligible is None or valid_text is None:
                        raise MarketContractError(
                            "is_eligible 和 is_valid_text_review 必须是明确布尔值"
                        )
                    if valid_text is not bool(clean(row.get("review_text"))):
                        raise MarketContractError(
                            "is_valid_text_review 必须表示非空正文，"
                            "不能再使用五词门槛"
                        )
                    if eligible is not True:
                        raise MarketContractError(
                            "新市场契约标准 CSV 只能包含 is_eligible=true 的记录"
                        )
                    summary = region_summary[row_region]
                    summary["record_count"] += 1
                    summary["written_review_count"] += bool(
                        clean(row.get("review_text"))
                    )
                    if eligible and valid_text:
                        run_valid_text_records += 1
                    if eligible:
                        run_eligible_records += 1
                    if not valid_text:
                        non_text_records += 1
                else:
                    raw_record_count += 1
                    legacy_contract_records += 1
                fact = _normalized_fact(
                    row,
                    run,
                    row_number,
                    csv_hash,
                    snapshot_root,
                    market_contract_mode,
                )
                if _row_matches_scope(fact, scope):
                    facts.append(fact)
                    if market_contract_mode == "current":
                        if fact["has_text"]:
                            region_summary[fact["market_region"]][
                                "eligible_valid_text_count"
                            ] += 1
            if market_contract_mode == "current":
                csv_record_count = run_eligible_records
                reported_eligible = run["report"].get("eligible_reviews")
                reported_unique = run["report"].get("unique_reviews")
                reported_valid = run["report"].get("valid_text_reviews")
                if (
                    reported_eligible is None
                    or int(reported_eligible) != csv_record_count
                ):
                    raise MarketContractError(
                        "validation report eligible_reviews 与标准 CSV 不一致"
                    )
                if (
                    reported_unique is None
                    or int(reported_unique) != csv_record_count
                ):
                    raise MarketContractError(
                        "validation report unique_reviews 与标准 CSV 不一致"
                    )
                if (
                    reported_valid is None
                    or int(reported_valid) != run_valid_text_records
                ):
                    raise MarketContractError(
                        "validation report valid_text_reviews 与标准 CSV 不一致"
                    )
                reported_excluded = int(
                    run["report"].get("excluded_reviews") or 0
                )
                source_unique = run["report"].get(
                    "source_unique_reviews"
                )
                if (
                    source_unique is None
                    or int(source_unique)
                    != csv_record_count + reported_excluded
                ):
                    raise MarketContractError(
                        "validation report source_unique_reviews 必须等于 "
                        "eligible_reviews + excluded_reviews"
                    )
                try:
                    source_completeness.append(
                        source_completeness_audit(
                            run["job"],
                            run["report"],
                            csv_eligible_rows=csv_record_count,
                        )
                    )
                except SourceCompletenessError as error:
                    raise MarketContractError(
                        f"{clean(run['job'].get('id'))} 来源完整性契约无效：{error}"
                    ) from error
                raw_record_count += int(source_unique)
                excluded_ineligible += reported_excluded
                region_summary[_job_market_region(run["job"])][
                    "record_count"
                ] += reported_excluded

    facts.sort(
        key=lambda item: (
            item["review_entity_id"] or item["review_occurrence_id"],
            item["channel"],
            item["product_id"],
            item["job_id"],
            item["source_row_number"],
        )
    )
    entity_ranks: Counter[str] = Counter()
    for fact in facts:
        if fact["review_entity_id"]:
            entity_ranks[fact["review_entity_id"]] += 1
            rank = entity_ranks[fact["review_entity_id"]]
            fact["canonical_entity_rank"] = rank
            fact["is_duplicate_occurrence"] = rank > 1

    non_reportable_exclusions = _scope_non_reportable_exclusions(
        snapshot_root, scope
    )
    quality = {
        **selection,
        "market_contract_version": (
            MARKET_CONTRACT_VERSION
            if market_contract_mode == "current"
            else None
        ),
        "market_contract_mode": market_contract_mode,
        "raw_record_count": raw_record_count,
        "legacy_contract_record_count": legacy_contract_records,
        "missing_contract_record_count": 0,
        "excluded_ineligible_records": excluded_ineligible,
        "excluded_invalid_text_records": excluded_invalid_text,
        "non_text_records": non_text_records,
        "market_region_summary": {
            region: dict(region_summary[region])
            for region in MARKET_REGIONS
            if region_summary[region]["record_count"]
        },
        "historical_sources": historical_sources,
        "source_completeness": sorted(
            source_completeness,
            key=lambda item: item["job_id"],
        ),
        "non_reportable_exclusions": non_reportable_exclusions,
        "non_reportable_excluded_records": sum(
            int(row.get("excluded_records") or 0)
            for row in non_reportable_exclusions
        ),
        "occurrences": len(facts),
        "complete_entity_key_occurrences": sum(
            fact["business_key_status"] == "complete" for fact in facts
        ),
        "unique_complete_entities": len(
            {fact["review_entity_id"] for fact in facts if fact["review_entity_id"]}
        ),
        "unknown_identity_occurrences": sum(
            fact["business_key_status"] == "incomplete" for fact in facts
        ),
        "duplicate_occurrences": sum(fact["is_duplicate_occurrence"] for fact in facts),
        "written_occurrences": sum(fact["has_text"] for fact in facts),
        "valid_rating_occurrences": sum(fact["rating_valid"] for fact in facts),
        "fallback_occurrences": sum(
            fact["collection_method"] == "fallback" for fact in facts
        ),
        "syndication_unknown_occurrences": sum(
            fact["is_syndicated"] is None for fact in facts
        ),
    }
    return facts, quality, sorted(inputs, key=lambda item: (item["path"], item["kind"]))


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def _write_preview_fact(path: Path, facts: Iterable[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as raw_handle:
        temp_name = raw_handle.name
        with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as zipped:
            for fact in facts:
                zipped.write((canonical_json(fact) + "\n").encode("utf-8"))
    os.replace(temp_name, path)
    return sha256_file(path)


def materialize_snapshot(
    snapshot_root: Path,
    scope: dict[str, Any],
    output_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    facts, quality, inputs = build_review_fact(snapshot_root, scope)
    logical_hash = sha256_text(
        "\n".join(fact["row_content_sha256"] for fact in facts)
    )
    database = locate_database(snapshot_root)
    current_contract = quality["market_contract_mode"] == "current"
    fact_schema_version = (
        FACT_SCHEMA_VERSION
        if current_contract
        else LEGACY_FACT_SCHEMA_VERSION
    )
    parser_version = (
        PARSER_VERSION if current_contract else LEGACY_PARSER_VERSION
    )
    normalization_version = (
        NORMALIZATION_VERSION
        if current_contract
        else LEGACY_NORMALIZATION_VERSION
    )
    selected_job_ids = list(quality.get("selected_job_ids") or [])
    identity_scope = dict(scope)
    if not current_contract and not identity_scope.get("market_regions"):
        identity_scope.pop("market_regions", None)
    identity = {
        "fact_schema_version": fact_schema_version,
        "parser_version": parser_version,
        "normalization_version": normalization_version,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "database_sha256": f"sha256:{sha256_file(database)}",
        "inputs": inputs,
        "scope": identity_scope,
        "logical_content_sha256": f"sha256:{logical_hash}",
        "source_completeness": quality.get("source_completeness") or [],
        "non_reportable_exclusions": (
            quality.get("non_reportable_exclusions") or []
        ),
    }
    if quality.get("historical_sources"):
        identity["historical_sources"] = quality["historical_sources"]
    identity_hash = sha256_text(canonical_json(identity))
    snapshot_id = f"snap_{identity_hash[:24]}"
    snapshot_dir = output_root / "snapshots" / snapshot_id
    fact_path = snapshot_dir / "review_fact.preview.jsonl.gz"
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.exists():
        fact_hash = _write_preview_fact(fact_path, facts)
        manifest = {
            "schema_version": (
                "snapshot-manifest/v2"
                if current_contract
                else "snapshot-manifest/v1"
            ),
            **(
                {"content_hash": f"sha256:{identity_hash}"}
                if current_contract
                else {}
            ),
            "snapshot_id": snapshot_id,
            "created_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "analysis_input_version": fact_schema_version,
            "parser_version": parser_version,
            "normalization_version": normalization_version,
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "job_ids": (
                selected_job_ids
                if current_contract
                else sorted({fact["job_id"] for fact in facts})
            ),
            "scope": {
                **identity_scope,
                "source_completeness": quality.get("source_completeness") or [],
                "non_reportable_exclusions": (
                    quality.get("non_reportable_exclusions") or []
                ),
            },
            **(
                {
                    "controlled_energy": {
                        "schema_version": CONTROLLED_ENERGY_SCHEMA_VERSION,
                        "energy_type": scope.get("energy_type"),
                        "selection": scope.get("energy_selection"),
                    }
                }
                if scope.get("energy_type")
                else {}
            ),
            **(
                {
                    "filters": {
                        "market_regions": list(
                            scope.get("market_regions") or []
                        ),
                        "job_ids": selected_job_ids,
                        "energy_type": scope.get("energy_type") or None,
                    }
                }
                if current_contract
                else {}
            ),
            "syndication_policy": scope.get("syndicated", "include"),
            "record_counts": {
                "occurrences": quality["occurrences"],
                "complete_entity_keys": quality["complete_entity_key_occurrences"],
                "unique_entities": quality["unique_complete_entities"],
                "written_reviews": quality["written_occurrences"],
                "eligible_valid_text": (
                    quality["written_occurrences"]
                    if current_contract
                    else None
                ),
                "raw_records": quality["raw_record_count"],
                "legacy_contract_records": quality[
                    "legacy_contract_record_count"
                ],
                "missing_contract_records": quality[
                    "missing_contract_record_count"
                ],
                "excluded_occurrences": (
                    quality["excluded_ineligible_records"]
                ),
            },
            "quality_summary": quality,
            "inputs": inputs,
            "artifacts": [
                {
                    "path": fact_path.name,
                    "format": "jsonl-gzip-preview",
                    "target_contract": "parquet",
                }
            ],
            "hashes": {
                "input_manifest": f"sha256:{identity_hash}",
                "logical_content": f"sha256:{logical_hash}",
                "preview_fact": f"sha256:{fact_hash}",
            },
        }
        _atomic_write_json(manifest_path, manifest)
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return facts, manifest


def catalog(snapshot_root: Path) -> dict[str, Any]:
    snapshot_metadata: dict[str, Any] = {}
    package_manifest_path = snapshot_root / "snapshot-manifest.json"
    if package_manifest_path.is_file():
        try:
            package_manifest = json.loads(
                package_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            package_manifest = {}
        snapshot_metadata = {
            "snapshot_id": clean(package_manifest.get("snapshot_id")),
            "schema_version": clean(package_manifest.get("schema_version")),
            "created_at": clean(package_manifest.get("created_at")),
            "content_sha256": clean(
                package_manifest.get("content_sha256")
                or (package_manifest.get("hashes") or {}).get("logical_content")
            ),
        }
    jobs = load_jobs(snapshot_root)
    try:
        product_master = load_product_master(snapshot_root)
        product_master_error = ""
    except ProductMasterError as exc:
        product_master = None
        product_master_error = str(exc)
    package_exclusions = _package_non_reportable_exclusions(snapshot_root)
    excluded_job_ids = {
        clean(row.get("job_id")) for row in package_exclusions
    }
    usable = [
        job
        for job in jobs
        if clean(job.get("status")) in {"completed", "partial_success"}
        and not clean(job.get("superseded_by"))
        and clean(job.get("id")) not in excluded_job_ids
    ]
    categories = sorted(
        {_job_category(job) for job in usable if _job_category(job)}
    )
    brands = sorted({clean(job.get("brand")) for job in usable if clean(job.get("brand"))})
    channels = sorted({clean(job.get("channel")) for job in usable if clean(job.get("channel"))})
    models = sorted({model_scope_value(job.get("model")) for job in usable})
    eligible_runs, _ = discover_selected_runs(snapshot_root, {})
    _enforce_snapshot_package_contract(snapshot_root, eligible_runs)
    current_runs = [
        run
        for run in eligible_runs
        if run["market_contract_mode"] == "current"
    ]
    market_regions = [
        region
        for region in MARKET_REGIONS
        if any(
            _job_market_region(run["job"]) == region
            for run in current_runs
        )
    ]
    models_by_category: dict[str, list[str]] = {}
    brands_by_category: dict[str, list[str]] = {}
    channels_by_category: dict[str, list[str]] = {}
    market_regions_by_category: dict[str, list[str]] = {}
    for category in categories:
        category_jobs = [
            job for job in usable if _job_category(job) == category
        ]
        models_by_category[category] = sorted(
            {
                model_scope_value(job.get("model"))
                for job in category_jobs
            }
        )
        brands_by_category[category] = sorted(
            {
                clean(job.get("brand"))
                for job in category_jobs
                if clean(job.get("brand"))
            }
        )
        channels_by_category[category] = sorted(
            {
                clean(job.get("channel"))
                for job in category_jobs
                if clean(job.get("channel"))
            }
        )
        market_regions_by_category[category] = [
            region
            for region in MARKET_REGIONS
            if any(
                _job_category(run["job"]) == category
                and _job_market_region(run["job"]) == region
                for run in current_runs
            )
        ]
    tasks = []
    product_master_rejections = []
    bound_records: dict[str, dict[str, Any]] = {}
    for run in eligible_runs:
        job = run["job"]
        model_id = ""
        master_record = None
        binding_audit: dict[str, Any] | None = None
        binding_status = "legacy_unbound"
        if product_master is not None:
            try:
                binding = validate_task_product_master_binding(
                    product_master, job
                )
                if binding is not None:
                    master_record, binding_audit = binding
                    model_id = clean(master_record.get("model_id"))
                    binding_status = "verified"
            except ProductMasterError as exc:
                binding_status = "invalid"
                product_master_rejections.append({
                    "job_id": clean(job.get("id")),
                    "model_id": clean(job.get("product_master_model_id")),
                    "reason": exc.code,
                    "message": str(exc),
                })
        if master_record is not None:
            eligibility = master_record["analysis_eligibility"]
            if not eligibility["eligible"]:
                product_master_rejections.append({
                    "job_id": clean(job.get("id")),
                    "model_id": model_id,
                    "reason": clean(eligibility.get("reason")),
                    "policy_version": clean(eligibility.get("policy_version")),
                })
            master_category_id = clean(
                (master_record.get("category") or {}).get("code")
            )
            job_category_id = _job_category_id(job)
            if master_category_id != job_category_id:
                product_master_rejections.append({
                    "job_id": clean(job.get("id")),
                    "model_id": model_id,
                    "reason": "master_category_mismatch",
                    "job_category_id": job_category_id,
                    "master_category_id": master_category_id,
                    "policy_version": clean(eligibility.get("policy_version")),
                })
            if eligibility["eligible"] and master_category_id == job_category_id:
                bound_records[model_id] = master_record
        platform_fact = voltage_platform_fact(job)
        form_fact = product_form_fact(job)
        energy_fact = controlled_energy_fact(job)
        tasks.append({
            "id": clean(job.get("id")),
            "channel": clean(job.get("channel")),
            "brand": clean(job.get("brand")),
            "model": clean(job.get("model")),
            "model_id": model_id,
            "product_master_binding_status": binding_status,
            "product_master_schema_version": (
                binding_audit["product_master_schema_version"]
                if binding_audit is not None else ""
            ),
            "product_master_snapshot_id": (
                binding_audit["product_master_snapshot_id"]
                if binding_audit is not None else ""
            ),
            "product_master_model_id": (
                binding_audit["product_master_model_id"]
                if binding_audit is not None else ""
            ),
            "product_master_entity_version": (
                binding_audit["product_master_entity_version"]
                if binding_audit is not None else None
            ),
            "product_master_record_snapshot_hash": (
                binding_audit["product_master_record_snapshot_hash"]
                if binding_audit is not None else ""
            ),
            "model_entity_version": (
                int(master_record["entity_version"])
                if master_record is not None else None
            ),
            "model_record_snapshot_hash": (
                clean(master_record.get("record_snapshot_hash"))
                if master_record is not None else ""
            ),
            "voltage_platform_schema_version": platform_fact["schema_version"],
            "voltage_platform_status": platform_fact["status"],
            "voltage_platform_key": platform_fact["platform_key"],
            "voltage_platform_brand": platform_fact["brand"],
            "voltage_platform_voltage": platform_fact["canonical_voltage"],
            "voltage_platform_label": platform_fact["display_name"],
            "voltage_platform_evidence_source": platform_fact["evidence_source"],
            "voltage_platform_null_reason": platform_fact["null_reason"],
            "product_observation_hash": platform_fact["product_observation_hash"],
            "energy_schema_version": energy_fact["schema_version"],
            "energy_type": energy_fact["energy_type"],
            "energy_label": energy_fact["label"],
            "energy_readiness_status": energy_fact["readiness_status"],
            "energy_evidence_source": energy_fact["evidence_source"],
            "energy_canonical_voltage": energy_fact["canonical_voltage"],
            "energy_unknown_reason": energy_fact["unknown_reason"],
            "energy_contract_status": energy_fact["contract_status"],
            "energy_product_observation_hash": energy_fact[
                "product_observation_hash"
            ],
            "market_region": (
                _job_market_region(job)
                if run["market_contract_mode"] == "current"
                else ""
            ),
            "market_contract_mode": run["market_contract_mode"],
            "source_category": str(job.get("category") or ""),
            "product_form_factor": form_fact["value"],
            "product_form": form_fact,
            "category_resolution": category_resolution(job.get("category")),
            "category_id": _job_category_id(job),
            "category": _job_category(job),
            "status": clean(job.get("status")),
            "finished_at": clean(job.get("finished_at")),
            "source_total": int(run["report"].get("source_total") or 0),
            "source_unique_reviews": int(
                run["report"].get("source_unique_reviews")
                or run["report"].get("unique_reviews")
                or 0
            ),
            "unique_reviews": int(run["report"].get("unique_reviews") or 0),
            "eligible_reviews": int(
                run["report"].get("eligible_reviews")
                or run["report"].get("unique_reviews")
                or 0
            ),
            "excluded_reviews": int(
                run["report"].get("excluded_reviews") or 0
            ),
            "written_reviews": int(
                run["report"].get("valid_text_reviews") or 0
            ),
            "eligible_valid_text_reviews": int(
                run["report"].get("valid_text_reviews")
                or (
                    run["report"].get("unique_reviews")
                    if run["market_contract_mode"] == "legacy"
                    else 0
                )
                or 0
            ),
            "validation_status": clean(run["report"].get("status")),
            "source_completeness": (
                source_completeness_audit(run["job"], run["report"])
                if run["market_contract_mode"] == "current"
                else None
            ),
        })
    product_master_contract = {
        "status": "unavailable",
        "schema_version": "retail-product-master/v2",
        "reason": (
            "authoritative_product_master_projection_invalid"
            if product_master_error
            else "authoritative_product_master_projection_missing"
        ),
    }
    voltage_platforms = platform_catalog([run["job"] for run in eligible_runs])
    energy_types = energy_catalog([run["job"] for run in eligible_runs])
    if product_master is not None:
        product_master_contract = {
            "status": "verified",
            "schema_version": product_master.payload["schema_version"],
            "snapshot_id": product_master.snapshot_id,
            "hash_algorithm": product_master.payload["hash_algorithm"],
            "authority": product_master.payload["authority"],
            "bound_models": len(bound_records),
            "unbound_tasks": sum(not task["model_id"] for task in tasks),
            "rejected_tasks": len(product_master_rejections),
        }
    platform_status_counts = Counter(
        str(task["voltage_platform_status"]) for task in tasks
    )
    energy_status_counts = Counter(
        str(task["energy_contract_status"]) for task in tasks
    )
    return {
        "channels": channels,
        "brands": brands,
        "categories": categories,
        "models": models,
        "market_regions": market_regions,
        "models_by_category": models_by_category,
        "brands_by_category": brands_by_category,
        "channels_by_category": channels_by_category,
        "market_regions_by_category": market_regions_by_category,
        "source_snapshot": snapshot_metadata,
        "product_master_contract": product_master_contract,
        "product_master_rejections": product_master_rejections,
        "product_form_factors": form_catalog(tasks),
        "product_form_contract": {"schema_version": FORM_SCHEMA_VERSION, "inference_policy": "explicit_product_metadata_only"},
        "voltage_platforms": voltage_platforms,
        "voltage_platform_contract": {
            "status": "available",
            "schema_version": VOLTAGE_PLATFORM_SCHEMA_VERSION,
            "authority": "wotu_task_fact",
            "known_tasks": int(platform_status_counts.get("known", 0)),
            "unknown_tasks": int(platform_status_counts.get("unknown", 0)),
            "non_dc_tasks": int(platform_status_counts.get("non_dc", 0)),
            "unknown_option_available": bool(platform_status_counts.get("unknown", 0)),
        },
        "energy_types": energy_types,
        "energy_contract": {
            "status": "available",
            "schema_version": CONTROLLED_ENERGY_SCHEMA_VERSION,
            "authority": "wotu_task_fact",
            "governed_tasks": int(energy_status_counts.get("verified", 0)),
            "legacy_null_tasks": int(energy_status_counts.get("legacy_null", 0)),
            "legacy_null_display": "unknown",
            "inference_policy": "never_infer",
        },
        "tasks": tasks,
        "data_quality": {
            "schema_version": "snapshot-data-quality/v1",
            "non_reportable_exclusions": [
                _public_non_reportable_exclusion(row)
                for row in package_exclusions
                if clean(row.get("reason_code"))
                == "unsupported_legacy_partial_contract"
            ],
            "non_reportable_excluded_records": sum(
                int(row.get("excluded_records") or 0)
                for row in package_exclusions
                if clean(row.get("reason_code"))
                == "unsupported_legacy_partial_contract"
            ),
        },
        "capabilities": {
            "verified_purchaser": {
                "status": "unavailable",
                "reason": "当前采集契约无法区分 false 与 missing",
            },
            "explicit_age": {
                "status": "conditional",
                "reason": "只统计明确年龄；不推断缺失年龄",
            },
            "syndication": {
                "status": "conditional",
                "reason": "来源标志不完整；实体业务键始终参与去重",
            },
        },
    }
