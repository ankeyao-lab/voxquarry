"""Report-independent, frozen review datasets for a trusted Wenquan broker.

This module is not an authentication boundary. Its caller authenticates users
and supplies a configured source root (never a model-provided filesystem path).
Only consistent snapshot databases are discovered. For live collection sources,
the broker supplies selected run metadata; this module never opens jobs.sqlite3.

Review normalization and entity identity are the reporting source implementation.
Rows are streamed into a disk SQLite spool, sorted/deduplicated there, then
published as immutable JSONL shards and an indexed database. Query result
membership is cached on disk, so later pages do not rescan source artifacts.
Raw channel JSONL is frozen separately when present. Its records may include
excluded reviews and are never mixed into normalized review counts.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
import weakref
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from reporting import source
from reporting.source_completeness import SourceCompletenessError, source_completeness_audit


CONTRACT = "voxquarry.wenquan-dataset/v1"
VERSION = "wenquan-dataset/1.2.0"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DATASET = re.compile(r"ds_[0-9a-f]{64}\Z")
_SCOPE_KEYS = {"voltage_platform_keys", "source_product_ids", "job_ids", "categories", "category_ids", "channels", "brands", "models", "market_regions", "energy_type", "ratings", "has_text", "date_range", "keywords", "keyword_field", "keyword_mode", "syndicated", "fallback_source"}
_GROUP_FIELDS = {"channel", "brand", "model", "market_region", "category_id", "rating", "has_text", "job_id", "submission_month"}
_CSV_LOCK = threading.RLock()
_ARTIFACT_NAMES = {"validation_report.json", "reviews.csv", "reviews.raw.jsonl"}
DIAGNOSTIC_FIELDS = frozenset({'eligible_reviews', 'unique_reviews', 'valid_text_reviews',
    'collected_source_unique_reviews', 'source_records_collected', 'source_unique_reviews', 'source_total',
    'declared_source_total', 'source_missing_records', 'missing_review_count', 'excluded_reviews',
    'csv_eligible_rows', 'raw_source_rows'})
DIAGNOSTIC_PHASES = frozenset({'catalog', 'snapshot_catalog', 'freeze', 'normalization', 'source_completeness',
    'query', 'aggregate', 'union', 'preparation', 'validation_manifest', 'artifact_access',
    'normalized_reviews', 'raw_reviews', 'artifact_hashing', 'attempt_identity'})
DIAGNOSTIC_REASONS = frozenset({'catalog_sqlite_error', 'dataset_sqlite_error', 'catalog_source_unavailable',
    'catalog_source_version_changed', 'catalog_total_deadline', 'no_available_sources', 'preparation_deadline',
    'preparation_receipt_bytes', 'preparation_receipt_invalid', 'requested_sources_unavailable', 'catalog_run_count',
    'mixed_review_contracts', 'source_file_bytes', 'source_metadata_bytes', 'source_not_published',
    'source_status_mismatch', 'validation_json_invalid', 'identity_manifest_mismatch', 'normalization_invalid',
    'product_form_invalid', 'review_channel_mismatch', 'review_contract_invalid', 'review_region_mismatch',
    'review_text_flag_mismatch', 'source_completeness_mismatch', 'validation_count_mismatch',
    'mixed_normalization_versions', 'overlapping_version_conflict'})
COMPLETENESS_REASONS = frozenset({'invalid_count', 'validation_notes_invalid', 'partial_validation_schema_invalid',
    'partial_import_schema_invalid', 'partial_channel_not_governed', 'partial_channel_mismatch',
    'collected_count_mismatch', 'eligible_excluded_mismatch', 'csv_count_mismatch', 'raw_count_mismatch',
    'declared_below_collected', 'missing_count_mismatch', 'unique_count_mismatch', 'source_reconciled_invalid',
    'partial_status_mismatch', 'partial_validation_status_mismatch', 'partial_empty_source',
    'partial_reconciled_true', 'partial_stop_reason_invalid', 'partial_missing_count_invalid',
    'complete_status_mismatch', 'complete_validation_status_invalid', 'complete_not_reconciled',
    'source_contract_invalid'})


def _expected_artifacts(value):
    if value is None:
        return {}
    if not isinstance(value, list) or len(value) != 3:
        _invalid("来源工件清单需要完整的三个固定文件")
    result = {}
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str) or item["name"] not in _ARTIFACT_NAMES or item["name"] in result or type(item.get("bytes")) is not int or item["bytes"] < 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("sha256", ""))):
            _invalid("来源工件清单无效")
        result[item["name"]] = {"bytes": item["bytes"], "sha256": item["sha256"]}
    return result


class DatasetError(ValueError):
    def __init__(self, code: str, message: str, http_status: int = 409, *, reason=None, observed=None, limit=None,
                 expected=None, phase=None, field=None, sqlite_code=None, source_reason=None, counts=None,
                 job_id=None, validation_sha256=None):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status
        self.diagnostic = {}
        if isinstance(reason, str) and reason in DIAGNOSTIC_REASONS:
            self.diagnostic['reason'] = reason
        for key, value in (('observed', observed), ('limit', limit), ('expected', expected)):
            if type(value) is int and 0 <= value <= 2**63 - 1:
                self.diagnostic[key] = value
        for key, value, choices in (('phase', phase, DIAGNOSTIC_PHASES), ('field', field, DIAGNOSTIC_FIELDS),
                                    ('source_reason', source_reason, COMPLETENESS_REASONS)):
            if isinstance(value, str) and value in choices:
                self.diagnostic[key] = value
        if type(sqlite_code) is int and 0 <= sqlite_code <= 28:
            self.diagnostic['sqlite_code'] = sqlite_code
        if isinstance(counts, dict):
            self.diagnostic['counts'] = {key: value for key, value in counts.items()
                if key in DIAGNOSTIC_FIELDS and type(value) is int and 0 <= value <= 2**63 - 1}
        if isinstance(job_id, str) and _ID.fullmatch(job_id):
            self.diagnostic['job_id'] = job_id
        if isinstance(validation_sha256, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', validation_sha256):
            self.diagnostic['validation_sha256'] = validation_sha256


@dataclass(frozen=True)
class DatasetLimits:
    max_source_bytes: int = 8 * 1024**3
    max_storage_bytes: int = 16 * 1024**3
    max_record_bytes: int = 4 * 1024**2
    max_metadata_bytes: int = 32 * 1024**2
    max_catalog_bytes: int = 256 * 1024**2
    max_records: int = 5_000_000
    max_runs: int = 10_000
    shard_bytes: int = 16 * 1024**2
    page_bytes: int = 8 * 1024**2
    page_size: int = 100
    max_groups: int = 1000
    max_cached_queries: int = 100
    max_cache_bytes: int = 512 * 1024**2
    max_shards: int = 10_000
    max_duration_seconds: int = 600
    max_query_seconds: int = 30

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in vars(self).values()):
            raise ValueError("dataset limits must be positive integers")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def compact_job(job):
    # The physical validation report is authoritative. Neither serialized
    # copy of its result is needed to identify or normalize a selected run.
    return {key: value for key, value in job.items() if key not in {'result', 'result_json'}}


def _normalization_contracts():
    return {key: getattr(source, key) for key in
        ('FACT_SCHEMA_VERSION', 'LEGACY_FACT_SCHEMA_VERSION', 'PARSER_VERSION', 'LEGACY_PARSER_VERSION',
         'NORMALIZATION_VERSION', 'LEGACY_NORMALIZATION_VERSION', 'FORM_SCHEMA_VERSION')}


def _identity(value: os.stat_result) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _sqlite_code(error):
    # sqlite_errorcode arrived in Python 3.11. Keep supported local 3.9 tests
    # narrow too: only exact SQLite messages map to these primary result codes.
    return getattr(error, "sqlite_errorcode", {
        "database or disk is full": 13, "database is locked": 5,
        "database table is locked": 6, "interrupted": 9,
    }.get(str(error), 0)) & 255


def _completeness_diagnostic(report, csv_count, error):
    """Map known contract failures, never echo an exception or source text."""
    counts = {key: value for key, value in report.items() if key in DIAGNOSTIC_FIELDS
              and type(value) is int and 0 <= value <= 2**63 - 1}
    counts['csv_eligible_rows'] = csv_count
    collected = report.get('collected_source_unique_reviews', report.get('source_unique_reviews'))
    declared = report.get('declared_source_total', report.get('source_total'))
    eligible, excluded = report.get('eligible_reviews', report.get('unique_reviews')), report.get('excluded_reviews')
    summed = eligible + excluded if type(eligible) is int and type(excluded) is int else None
    missing = declared - collected if type(declared) is int and type(collected) is int else None
    numeric = {
        'source_records_collected must equal source_unique_reviews': ('collected_count_mismatch', 'source_records_collected', report.get('source_unique_reviews'), report.get('source_records_collected')),
        'collected_source_unique_reviews must equal eligible_reviews + excluded_reviews': ('eligible_excluded_mismatch', 'collected_source_unique_reviews', summed, collected),
        'CSV eligible rows do not reconcile with eligible_reviews': ('csv_count_mismatch', 'csv_eligible_rows', eligible, csv_count),
        'raw source rows do not reconcile with collected_source_unique_reviews': ('raw_count_mismatch', 'raw_source_rows', collected, None),
        'declared_source_total must be greater than or equal to collected': ('declared_below_collected', 'declared_source_total', collected, declared),
        'source_missing_records must equal source_total - source_records_collected': ('missing_count_mismatch', 'source_missing_records', missing, report.get('source_missing_records')),
        'missing_review_count must equal declared_source_total - collected_source_unique_reviews': ('missing_count_mismatch', 'missing_review_count', missing, report.get('missing_review_count')),
        'unique_reviews must equal eligible_reviews': ('unique_count_mismatch', 'unique_reviews', eligible, report.get('unique_reviews')),
    }
    message = str(error)
    if message in numeric:
        reason, field, expected, observed = numeric[message]
        return {'source_reason': reason, 'field': field, 'expected': expected, 'observed': observed, 'counts': counts}
    for field in DIAGNOSTIC_FIELDS:
        if message in {field + ' must be a non-negative integer', field + ' must be an explicit integer'}:
            return {'source_reason': 'invalid_count', 'field': field, 'counts': counts}
    labels = {
        'validation notes must be an object': 'validation_notes_invalid',
        'partial validation_schema_version must be voxquarry.external-review-import-validation/v1': 'partial_validation_schema_invalid',
        'partial import_schema_version must be a supported governed import schema': 'partial_import_schema_invalid',
        'partial validation channel is not a governed external import': 'partial_channel_not_governed',
        'partial validation channel does not match the task': 'partial_channel_mismatch',
        'source_reconciled must be an explicit boolean': 'source_reconciled_invalid',
        'partial validation requires 任务状态 partial_success': 'partial_status_mismatch',
        'partial job requires validation status PARTIAL_SUCCESS': 'partial_validation_status_mismatch',
        'partial source requires collected_source_unique_reviews > 0': 'partial_empty_source',
        'partial source_reconciled must remain false': 'partial_reconciled_true',
        'partial stop_reason must be external_import_declared_incomplete': 'partial_stop_reason_invalid',
        'partial source must retain a positive missing_review_count': 'partial_missing_count_invalid',
        'complete validation requires collection status completed': 'complete_status_mismatch',
        'completed job has unsupported validation status': 'complete_validation_status_invalid',
        'completed source must be fully reconciled': 'complete_not_reconciled',
    }
    return {'source_reason': labels.get(message, 'source_contract_invalid'), 'counts': counts}


def _invalid(message: str) -> None:
    raise DatasetError("invalid_request", message, 422)


def _scope(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        _invalid("数据筛选必须是对象")
    result = dict(value or {})
    if set(result) - _SCOPE_KEYS:
        _invalid("数据筛选包含未知字段")
    for key in ("voltage_platform_keys", "job_ids", "source_product_ids", "categories", "category_ids", "channels", "brands", "models", "market_regions", "keywords"):
        if key in result:
            items = result[key]
            if not isinstance(items, list) or len(items) > 10000 or any(not isinstance(v, str) or not v or len(v) > 500 for v in items):
                _invalid("数据筛选列表无效")
            result[key] = sorted(set(items))
    if "ratings" in result and (not isinstance(result["ratings"], list) or any(type(v) is not int or v not in range(1, 6) for v in result["ratings"])):
        _invalid("评分必须是 1 至 5 的整数")
    if "energy_type" in result and (not isinstance(result["energy_type"], str) or len(result["energy_type"]) > 100):
        _invalid("能源筛选无效")
    for key, choices in {"has_text": (True, False, "true", "false", "any"), "keyword_field": ("title", "body", "title_and_body"), "keyword_mode": ("and", "or", "exact"), "syndicated": ("include", "exclude_explicit", "only_explicit", "unknown_only"), "fallback_source": ("include", "exclude")}.items():
        if key in result and result[key] not in choices:
            _invalid("数据筛选选项无效")
    dates = result.get("date_range", {})
    if not isinstance(dates, dict) or set(dates) - {"start", "end"}:
        _invalid("日期筛选无效")
    try:
        for value in dates.values():
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        if dates.get("start", "") > dates.get("end", "9999-12-31"):
            raise ValueError
    except ValueError:
        _invalid("日期筛选无效")
    return result


def _csv_rows(path: Path, maximum: int):
    """Limit a complete logical CSV record, including multiline quoted fields."""
    class Lines:
        consumed = 0

        def __iter__(self):
            return self

        def __next__(self):
            line = handle.readline(maximum + 1)
            if not line:
                raise StopIteration
            self.consumed += len(line.encode("utf-8"))
            if self.consumed > maximum:
                raise DatasetError("dataset_too_large", "CSV 记录超过完整原文容量")
            return line

    # csv's field-size setting is process-global. Serialize this module's
    # readers and restore it; the additional record bound remains authoritative.
    with _CSV_LOCK, path.open("r", encoding="utf-8-sig", newline="") as handle:
        previous = csv.field_size_limit()
        csv.field_size_limit(maximum)
        try:
            lines = Lines()
            reader = csv.DictReader(lines)
            fields = reader.fieldnames
            if not fields or len(fields) != len(set(fields)):
                raise DatasetError("invalid_source", "评论 CSV 字段重复或缺失")
            lines.consumed = 0
            for number, row in enumerate(reader, 2):
                yield number, row
                lines.consumed = 0
        finally:
            csv.field_size_limit(previous)


class WenquanDatasets:
    """Trusted service API: catalog, open, query, aggregate and export.

    ``selected_runs`` entries contain ``job`` metadata and optionally a trusted
    ``run_dir``/``csv_path`` inside source_root. Supplying this list selects exact
    runs; source discovery otherwise selects complete-first/latest per product.
    Public responses contain identifiers and relative shard names, never paths.
    ``export`` yields bytes; the HTTP broker owns authorization and streaming.
    """

    def __init__(self, source_root: Path | str, storage_root: Path | str, *, limits: DatasetLimits | None = None):
        self._source_alias = Path(source_root).absolute()
        self.source_root = Path(source_root).resolve(strict=True)
        output = Path(storage_root).absolute()
        if output.is_symlink():
            raise DatasetError("unsafe_path", "数据集目录不能是链接")
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.storage_root = output.resolve(strict=True)
        st = self.storage_root.stat()
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise DatasetError("unsafe_path", "数据集目录必须由服务用户独占")
        if self.source_root == self.storage_root or self.source_root.is_relative_to(self.storage_root):
            raise DatasetError("unsafe_path", "数据集输出不能包含来源目录")
        self.limits = limits or DatasetLimits()
        self._verified: dict[tuple, bool] = {}
        self._metrics_lock = threading.Lock()
        self._metrics = {"hits": 0, "misses": 0, "fallbacks": 0, "evictions": 0,
                         "queries": 0, "query_seconds": 0.0, "last_error": None}
        self._catalog_lock = threading.RLock()
        self._catalog_copy = None

    def _metric(self, **changes):
        with self._metrics_lock:
            for key, value in changes.items():
                if key == "last_error":
                    self._metrics[key] = value
                else:
                    self._metrics[key] += value

    def query_metrics(self):
        with self._metrics_lock:
            return {"scope": "process_since_start", **self._metrics}

    @staticmethod
    def cache_health(storage_root, limits=None):
        """Read-only probe: never creates a file or inserts a query key."""
        limits = limits or DatasetLimits()
        path = Path(storage_root) / "query-cache.sqlite3"
        result = {"max_keys": limits.max_cached_queries, "max_bytes": limits.max_cache_bytes}
        try:
            if path.is_symlink():
                return {**result, "status": "unsafe_path"}
            if not path.exists():
                return {**result, "status": "empty", "keys": 0, "bytes": 0}
            info = path.stat()
            if not path.is_file() or info.st_nlink != 1:
                return {**result, "status": "unsafe_path"}
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
                db.execute("PRAGMA query_only=ON")
                deadline = time.monotonic() + 0.1
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='queries' AND type='table'").fetchone()
                count = db.execute("SELECT COUNT(*) FROM queries").fetchone()[0] if exists else 0
            full = count >= limits.max_cached_queries or info.st_size >= limits.max_cache_bytes
            return {**result, "status": "capacity_reached_fallback_available" if full else "ok",
                    "keys": count, "bytes": info.st_size}
        except (OSError, sqlite3.Error):
            return {**result, "status": "unavailable"}

    @contextmanager
    def _read(self, root: Path, relative: str | Path, maximum: int):
        relative = Path(relative)
        if relative.is_absolute() or not relative.parts or any(p in {".", ".."} for p in relative.parts):
            raise DatasetError("unsafe_path", "资料路径无效")
        directories, fd = [], None
        try:
            directories.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for part in relative.parts[:-1]:
                directories.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directories[-1]))
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directories[-1])
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise DatasetError("unsafe_source", "资料文件类型不符合要求")
            if before.st_size > maximum:
                raise DatasetError("dataset_too_large", "资料文件超过本次读取容量", reason="source_file_bytes", observed=before.st_size, limit=maximum)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                yield handle, before
            after = os.fstat(fd)
            current = os.stat(relative.name, dir_fd=directories[-1], follow_symlinks=False)
            if _identity(before) != _identity(after) or _identity(after) != _identity(current):
                raise DatasetError("source_changed", "资料在读取期间发生变化，请重新打开数据集")
            # Detect a replaced parent too; held descriptors alone do not prove
            # that the current source catalog still points to those directories.
            for index, part in enumerate(relative.parts[:-1]):
                linked = os.stat(part, dir_fd=directories[index], follow_symlinks=False)
                held = os.fstat(directories[index + 1])
                if (linked.st_dev, linked.st_ino) != (held.st_dev, held.st_ino):
                    raise DatasetError("source_changed", "资料目录在读取期间发生变化")
        except OSError as exc:
            raise DatasetError("unsafe_source", "资料不存在或不能安全读取") from exc
        finally:
            if fd is not None:
                os.close(fd)
            for directory in reversed(directories):
                os.close(directory)

    def _copy(self, relative: Path, destination: Path, maximum: int, *, check_active=None) -> dict[str, Any]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, count = hashlib.sha256(), 0
        with self._read(self.source_root, relative, maximum) as (src, before), destination.open("xb") as out:
            while chunk := src.read(1024 * 1024):
                if check_active:
                    check_active()
                count += len(chunk)
                if count > maximum:
                    raise DatasetError("dataset_too_large", "资料超过本次导出容量")
                digest.update(chunk)
                out.write(chunk)
        return {"path": str(relative), "sha256": "sha256:" + digest.hexdigest(), "bytes": count, "identity": _identity(before)}

    def _jobs(self, selected_runs: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
        return list(self._iter_jobs(selected_runs))

    @contextmanager
    def _snapshot_database(self):
        """Reuse one private immutable copy while its source identity is unchanged.

        Both source and copy are held through the query. A replacement invalidates
        the copy; no live jobs database or unchecked path is opened by SQLite.
        """
        with self._catalog_lock:
            try:
                database = source.locate_database(self.source_root)
            except RuntimeError as exc:
                raise DatasetError("source_catalog_unavailable", "需要一致快照目录或由可信服务提供选中任务") from exc
            relative = database.relative_to(self.source_root)
            with self._read(self.source_root, relative, self.limits.max_catalog_bytes) as (_, original):
                identity = (str(relative), _identity(original))
                cached = self._catalog_copy
                if cached is None or cached[0] != identity:
                    temporary = tempfile.TemporaryDirectory(prefix=".catalog-", dir=self.storage_root)
                    copied = Path(temporary.name) / "jobs.sqlite3"
                    try:
                        self._copy(relative, copied, self.limits.max_catalog_bytes)
                        copied.chmod(0o400)
                        self._catalog_copy = (identity, temporary, _identity(copied.stat()),
                                              weakref.finalize(self, temporary.cleanup))
                    except BaseException:
                        temporary.cleanup()
                        raise
                    if cached is not None:
                        cached[3]()
                    cached = self._catalog_copy
                copied = Path(cached[1].name) / "jobs.sqlite3"
                with self._read(self.storage_root, copied.relative_to(self.storage_root), self.limits.max_catalog_bytes) as (_, current):
                    if _identity(current) != cached[2]:
                        raise DatasetError('source_changed', '目录副本版本已变化')
                    with closing(sqlite3.connect(source.sqlite_uri(copied), uri=True)) as db:
                        db.row_factory = sqlite3.Row
                        yield db

    def _iter_jobs(self, selected_runs=None, job_ids=None, scope=None):
        if selected_runs is not None:
            for number, item in enumerate(selected_runs):
                if number >= self.limits.max_runs:
                    raise DatasetError("dataset_too_large", "选中的采集任务过多")
                if not isinstance(item, Mapping) or not isinstance(item.get("job"), Mapping):
                    _invalid("选中的来源需要任务元数据")
                yield {"job": compact_job(item["job"]), **{k: item[k] for k in ("run_dir", "csv_path", "expected_artifacts") if k in item}}
            return
        # Filter before loading task JSON or physical validation metadata. The
        # category functions are the same canonical vocabulary used by facts.
        with self._snapshot_database() as db:
            scope = {**(scope or {}), **({'job_ids': job_ids} if job_ids else {})}
            db.create_function('gt_category_id', 1, source.category_id_for, deterministic=True)
            db.create_function('gt_category', 1, source.canonical_category_name, deterministic=True)
            db.create_function('gt_model', 1, source.model_scope_value, deterministic=True)
            fields = {'job_ids': 'id', 'channels': 'channel', 'brands': 'brand',
                      'models': 'gt_model(model)', 'categories': 'gt_category(category)',
                      'category_ids': 'gt_category_id(category)', 'market_regions': 'market_region'}
            clauses, parameters = [], []
            for key, expression in fields.items():
                if scope.get(key):
                    clauses.append(expression + ' IN (SELECT value FROM json_each(?))')
                    parameters.append(_json(scope[key]))
            columns = [row[1] for row in db.execute('PRAGMA table_info(jobs)') if row[1] != 'result_json']
            projection = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            rows = db.execute('SELECT ' + projection + ' FROM jobs' + where + ' ORDER BY id LIMIT ?',
                              (*parameters, self.limits.max_runs + 1))
            for number, row in enumerate(rows):
                if number >= self.limits.max_runs:
                    raise DatasetError("dataset_too_large", "来源目录过大，请由可信服务筛选任务", reason="catalog_run_count", observed=number + 1, limit=self.limits.max_runs)
                yield {"job": compact_job(dict(row))}

    @staticmethod
    def _matches_job(scope, job):
        matching = {"voltage_platform_keys": source.voltage_platform_fact(job)["platform_key"],
                    "job_ids": str(job.get('id', '')), "channels": job.get('channel'), "brands": job.get("brand"),
                    "models": source.model_scope_value(job.get("model")), "categories": source._job_category(job),
                    "category_ids": source._job_category_id(job), "market_regions": job.get("market_region")}
        if any(scope.get(key) and value not in scope[key] for key, value in matching.items()):
            return False
        return scope.get('energy_type') in {None, '', 'all'} or source.controlled_energy_fact(job)['energy_type'] == scope['energy_type']

    def _runs(self, scope: dict[str, Any], selected_runs=None, *, unavailable_sources=None) -> list[dict[str, Any]]:
        exact = selected_runs is not None or bool(scope.get("job_ids"))
        runs, metadata_bytes = [], 0
        for item in self._iter_jobs(selected_runs, job_ids=scope.get('job_ids'), scope=scope):
            job = item["job"]
            job_id, channel = str(job.get("id", "")), str(job.get("channel", ""))
            if not _ID.fullmatch(job_id) or not _ID.fullmatch(channel):
                raise DatasetError("unsafe_source", "来源任务标识无效")
            effective = job.get("active_result_status") or job.get("status")
            if effective not in {"completed", "partial_success"} or job.get("superseded_by"):
                continue
            if not self._matches_job(scope, job):
                continue
            path = Path(item.get("run_dir") or (Path(item["csv_path"]).parent if item.get("csv_path") else Path("data/runs") / (job.get("active_run_path") or f"{job_id}/{channel}")))
            if path.is_absolute():
                try:
                    path = path.relative_to(self._source_alias) if path.is_relative_to(self._source_alias) else path.relative_to(self.source_root)
                except ValueError as exc:
                    raise DatasetError("unsafe_path", "来源目录超出允许位置") from exc
            try:
                with self._read(self.source_root, path / "validation_report.json", self.limits.max_metadata_bytes) as (handle, before):
                    raw = handle.read(self.limits.max_metadata_bytes + 1)
                    try:
                        report = json.loads(raw)
                    except (ValueError, UnicodeError) as exc:
                        raise DatasetError("invalid_source", "来源验证文件不是有效 JSON", reason="validation_json_invalid") from exc
            except DatasetError as exc:
                # Historical catalog metadata can outlive its archived files.
                # Only unqualified discovery may enumerate these as missing;
                # explicit selections and all freezes retain their full scope.
                # Never turn link/permission/type/version errors into omissions.
                if not exact and unavailable_sources is not None and isinstance(exc.__cause__, FileNotFoundError):
                    unavailable_sources.append({"job_id": job_id, "channel": channel, "reason": "validation_manifest_missing"})
                    continue
                raise
            metadata_bytes += len(raw) + len(_json(job).encode())
            if metadata_bytes > self.limits.max_metadata_bytes:
                raise DatasetError("dataset_too_large", "来源元数据超过本次容量，请按原目标的品牌或渠道分组读取", reason="source_metadata_bytes", observed=metadata_bytes, limit=self.limits.max_metadata_bytes)
            if not isinstance(report, dict) or report.get("status") not in {"COMPLETE", "COMPLETE_FALLBACK_SOURCE", "PARTIAL_SUCCESS"}:
                raise DatasetError("invalid_source", "来源没有可分析的发布结果", reason="source_not_published")
            expected_status = "partial_success" if report["status"] == "PARTIAL_SUCCESS" else "completed"
            if effective != expected_status or report.get("channel", channel) != channel:
                raise DatasetError("invalid_source", "任务与物理结果不一致", reason="source_status_mismatch")
            expected = _expected_artifacts(item.get("expected_artifacts"))
            if expected and (type(job.get("active_attempt_no")) is not int or job["active_attempt_no"] < 1):
                raise DatasetError("invalid_source", "带工件哈希的来源必须指定准确采集次数")
            report_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
            if expected and expected["validation_report.json"] != {"sha256": report_hash, "bytes": len(raw)}:
                raise DatasetError("source_changed", "采集来源版本已经变化，请重新获取目录")
            normalized_job = {**job, "status": effective}
            product = source._product_identity(report, normalized_job)
            runs.append({"job": normalized_job, "channel": channel, "relative": path, "report": report, "product_identity": product, "report_stat": _identity(before), "report_hash": report_hash, "expected_artifacts": expected})
        # Unqualified discovery follows complete-first/latest. Exact selections
        # retain different versions, while repeated attachments of one run do
        # not multiply rows. Review entity deduplication occurs after freezing.
        chosen = {}
        for run in runs:
            job = run["job"]
            key = (job["id"], str(run["relative"]), str(job.get("active_attempt_no"))) if exact else (run["channel"], run["product_identity"])
            score = (job["status"] == "completed", str(job.get("finished_at") or ""), str(job.get("created_at") or ""), job["id"])
            if key not in chosen or score > chosen[key][0]:
                chosen[key] = score, run
        selected = [value[1] for _, value in sorted(chosen.items())]
        if scope.get("job_ids") and set(scope["job_ids"]) - {run["job"]["id"] for run in runs}:
            raise DatasetError("source_unavailable", "部分指定任务没有可用原文，未缩小资料后冒充完整结果")
        return selected

    def catalog(self, scope: Mapping[str, Any] | None = None, *, selected_runs=None) -> dict[str, Any]:
        """Read physical metadata; open() additionally reconciles every CSV row."""
        items, unavailable = [], []
        for run in self._runs(_scope(scope), selected_runs, unavailable_sources=unavailable):
            job, report = run["job"], run["report"]
            items.append({"job_id": job["id"], "channel": run["channel"], "brand": job.get("brand"), "model": job.get("model"), "category_id": source._job_category_id(job), "market_region": job.get("market_region"), "product_id": run["product_identity"], "product_url": job.get("target_url"), "attempt_no": job.get("active_attempt_no"), "collected_at": job.get("finished_at"), "status": job["status"], "eligible_reviews": report.get("eligible_reviews", report.get("unique_reviews")), "written_reviews": report.get("valid_text_reviews"), "counts_verified": False, "voltage_platform": source.voltage_platform_fact(job), "validation_sha256": run["report_hash"]})
        return {"contract": CONTRACT, "items": items, "total": len(items), "report_required": False,
                "catalog_complete": not unavailable, "unavailable_sources": unavailable}

    def open(self, scope: Mapping[str, Any] | None = None, *, selected_runs=None, check_active=None) -> dict[str, Any]:
        """Freeze selected standard CSVs, reconcile, then atomically publish."""
        started = time.monotonic()
        if check_active is not None and not callable(check_active):
            _invalid("任务状态检查必须由可信服务提供")
        def guard():
            if time.monotonic() - started > self.limits.max_duration_seconds:
                raise DatasetError("dataset_timeout", "数据物化超过执行时间，未发布部分结果")
            if check_active is not None:
                check_active()
        guard()
        scope = _scope(scope)
        try:
            runs = self._runs(scope, selected_runs)
            if not runs:
                raise DatasetError("source_unavailable", "没有可分析的已发布评论")
            return self._freeze(runs, scope, guard)
        except DatasetError:
            raise
        except OSError as exc:
            raise DatasetError("dataset_storage_unavailable", "数据集存储未完成，请检查可用空间") from exc
        except sqlite3.OperationalError as exc:
            raise DatasetError('dataset_storage_unavailable', '数据索引操作未完成，请检查可用空间或读取时限',
                               reason='dataset_sqlite_error', phase='freeze', sqlite_code=_sqlite_code(exc)) from exc
        except (source.MarketContractError, ValueError, csv.Error, UnicodeError, sqlite3.Error) as exc:
            raise DatasetError("invalid_source", f"来源校验失败：{type(exc).__name__}") from exc

    def _freeze(self, runs, scope, check_active):
        with tempfile.TemporaryDirectory(prefix=".freeze-", dir=self.storage_root) as temporary:
            stage = Path(temporary)
            copied_root, output = stage / "source", stage / "dataset"
            copied_root.mkdir()
            output.mkdir()
            inputs, total_bytes, identities, raw_artifacts = [], 0, [], []
            for run in runs:
                check_active()
                job = run["job"]
                evidence = []
                names = ["validation_report.json", "reviews.csv"]
                raw_relative = run["relative"] / "reviews.raw.jsonl"
                if run["expected_artifacts"] or (self.source_root / raw_relative).exists() or (self.source_root / raw_relative).is_symlink():
                    names.append("reviews.raw.jsonl")
                for name in names:
                    relative = run["relative"] / name
                    info = self._copy(relative, copied_root / relative, self.limits.max_source_bytes - total_bytes, check_active=check_active)
                    total_bytes += info["bytes"]
                    identities.append(info)
                    if name == "validation_report.json" and (info["sha256"] != run["report_hash"] or info["identity"] != run["report_stat"]):
                        raise DatasetError("source_changed", "来源目录与导出版本不一致")
                    expected = run["expected_artifacts"].get(name)
                    if expected and expected != {"sha256": info["sha256"], "bytes": info["bytes"]}:
                        raise DatasetError("source_changed", "采集工件不再属于已选版本，未混入新一次采集")
                    evidence.append({k: v for k, v in info.items() if k != "identity"})
                    if name == "reviews.raw.jsonl":
                        raw_artifacts.append(self._raw_shards(copied_root / relative, output, run, len(raw_artifacts), info, check_active,
                            sum(a["records"] for a in raw_artifacts), sum(len(a["shards"]) for a in raw_artifacts)))
                run["csv_path"] = copied_root / run["relative"] / "reviews.csv"
                run["report_path"] = copied_root / run["relative"] / "validation_report.json"
                # Bound the header before the legacy helper reopens it.
                with closing(_csv_rows(run["csv_path"], self.limits.max_record_bytes)) as rows:
                    next(rows, None)
                try:
                    run["market_contract_mode"] = source._run_contract_mode(run)
                except (source.MarketContractError, ValueError) as exc:
                    raise DatasetError('invalid_source', '来源评论契约不兼容', reason='review_contract_invalid',
                                       job_id=job['id'], validation_sha256=run['report_hash']) from exc
                # Metadata included in identity because model/category/attempt
                # corrections must create a new dataset despite identical CSV.
                public_job = {k: job.get(k) for k in ("id", "channel", "brand", "model", "category", "market_region", "target_url", "finished_at", "active_attempt_no", "source_product_id", "source_product_identity",
                    "product_form_factor", "product_form_evidence_source", "product_form_source_category", "product_form_schema_version")}
                try:
                    form = source.product_form_fact(job)
                except ValueError as exc:
                    raise DatasetError('invalid_source', '产品形态事实不符合契约', reason='product_form_invalid',
                                       job_id=job['id'], validation_sha256=run['report_hash']) from exc
                inputs.append({"job": public_job, "job_metadata_sha256": _hash(compact_job(job)),
                    "normalization_facts": {"category_id": source._job_category_id(job),
                        "category": source._job_category(job), "product_form": form},
                    "artifacts": evidence, "product_identity": run["product_identity"], "status": job["status"]})
            modes = {run["market_contract_mode"] for run in runs}
            if len(modes) != 1 or ("legacy" in modes and scope.get("market_regions")):
                raise DatasetError("invalid_source", "不同评论契约不能混为一个数据集，请分别打开各契约组并保留原目标范围", reason="mixed_review_contracts")
            if (self.source_root / "snapshot-manifest.json").exists():
                self._copy(Path("snapshot-manifest.json"), copied_root / "snapshot-manifest.json", self.limits.max_metadata_bytes)
                source._enforce_snapshot_package_contract(copied_root, runs)
            identity = {"contract": CONTRACT, "version": VERSION, "normalization_version": source.NORMALIZATION_VERSION,
                "normalization_contracts": _normalization_contracts(),
                "scope": scope, "inputs": inputs, "shard_bytes": self.limits.shard_bytes}
            dataset_id = "ds_" + _hash(identity).split(":")[1]
            database = output / "reviews.sqlite3"
            audits, records = [], 0
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("PRAGMA temp_store=FILE")
                db.execute("PRAGMA cache_size=-4096")
                db.execute(f"PRAGMA max_page_count={max(1, self.limits.max_storage_bytes // 4096)}")
                db.execute("CREATE TABLE facts(seq INTEGER PRIMARY KEY, occurrence TEXT UNIQUE, entity TEXT, review_id TEXT, channel TEXT, brand TEXT, model TEXT, market_region TEXT, category_id TEXT, rating INTEGER, has_text INTEGER, job_id TEXT, submission_date TEXT, submission_month TEXT, search_text TEXT, sort_key TEXT, canonical INTEGER, payload TEXT)")
                for run, item in zip(runs, inputs):
                    count = written = 0
                    csv_hash = item["artifacts"][1]["sha256"].split(":")[1]
                    for number, row in _csv_rows(run["csv_path"], self.limits.max_record_bytes):
                        if number % 1000 == 2:
                            check_active()
                        if None in row or None in row.values():
                            raise DatasetError("invalid_source", "评论 CSV 行字段不完整")
                        if len(_json(row).encode()) > self.limits.max_record_bytes:
                            raise DatasetError("dataset_too_large", "完整评论超过单条容量，未截断原文")
                        self._validate_row(row, run)
                        count += 1
                        written += bool(source.clean(row.get("review_text")))
                        try:
                            fact = source._normalized_fact(row, run, number, csv_hash, copied_root, run["market_contract_mode"])
                        except (source.MarketContractError, ValueError) as exc:
                            raise DatasetError('invalid_source', '来源记录或产品事实不能规范化', reason='normalization_invalid',
                                               job_id=run['job']['id'], validation_sha256=run['report_hash']) from exc
                        if scope.get("source_product_ids") and fact.get("source_product_id") not in scope["source_product_ids"]:
                            continue
                        if not source._row_matches_scope(fact, scope):
                            continue
                        records += 1
                        if records > self.limits.max_records:
                            raise DatasetError("dataset_too_large", "评论数量超过本次导出容量")
                        fact["attempt_no"] = run["job"].get("active_attempt_no")
                        fact["original_fields"] = {key: row[key] for key in source.STANDARD_FIELDS if key in row}
                        fact["source_completeness_status"] = run["job"]["status"]
                        fact["record_id"] = fact["review_occurrence_id"]
                        key = fact["review_entity_id"] or fact["review_occurrence_id"]
                        sort_key = _json([key, fact["channel"], fact["product_id"], fact["job_id"], f"{number:020d}"])
                        db.execute("INSERT INTO facts VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)", (fact["review_occurrence_id"], key, fact["review_id"], fact["channel"], fact["brand"], fact["model"], fact["market_region"], fact["category_id"], fact["rating"], int(fact["has_text"]), fact["job_id"], fact["submission_date"], (fact["submission_date"] or "")[:7], (fact["review_title"] + " " + fact["review_text"]).casefold(), sort_key, _json(fact)))
                    audits.append(self._audit(run, count, written))
                db.execute("CREATE INDEX facts_entity ON facts(entity, sort_key)")
                # The disk engine performs ordering; no Python list/set grows
                # with the population. Canonical ranking matches source.py.
                for seq, rank in self._ranks(db):
                    db.execute("UPDATE facts SET canonical=? WHERE seq=?", (int(rank == 1), seq))
                db.execute("CREATE INDEX facts_canonical ON facts(canonical,seq)")
                for field in ("channel", "brand", "model", "rating", "job_id", "submission_date", "occurrence", "review_id"):
                    db.execute(f"CREATE INDEX idx_{field} ON facts({field},seq)")
                counts = {"occurrences": records, "review_entities": db.execute("SELECT COUNT(*) FROM facts WHERE canonical=1").fetchone()[0], "written_occurrences": db.execute("SELECT COUNT(*) FROM facts WHERE has_text=1").fetchone()[0], "written_entities": db.execute("SELECT COUNT(DISTINCT entity) FROM facts WHERE has_text=1").fetchone()[0]}
                check_active()
                shards = self._write_shards(db, output, dataset_id, check_active)
                if len(shards) + sum(len(a["shards"]) for a in raw_artifacts) > self.limits.max_shards:
                    raise DatasetError("dataset_too_large", "全部导出分片数量超过本次容量")
                db.commit()
            # Recheck every original after all rows were processed, not merely
            # after each file copy. Published datasets never reread live paths.
            for info in identities:
                with self._read(self.source_root, info["path"], self.limits.max_source_bytes) as (_handle, current):
                    if _identity(current) != info["identity"]:
                        raise DatasetError("source_changed", "导出期间来源工件发生变化")
            index_hash = self._file_hash(database)
            manifest = {**identity, "dataset_id": dataset_id, "dataset_version": dataset_id, "manifest_version": 1, "record_counts": counts, "source_completeness": audits, "export_complete": True, "source_complete": all(a.get("source_reconciled") is True for a in audits), "raw_record_count": sum(a["collected_source_unique_reviews"] for a in audits), "eligible_source_records": sum(a["eligible_records"] for a in audits), "source_excluded_records": sum(a["excluded_records"] for a in audits), "shards": shards, "raw_artifacts": raw_artifacts, "raw_payload_records": sum(a["records"] for a in raw_artifacts), "raw_payload_complete": len(raw_artifacts) == len(runs), "index_sha256": index_hash, "limitations": ["渠道原始 JSON 包含所选采集的全部记录，可能含排除项；不等于通过筛选的有效评论，不混入统计。原始记录的逐条资格未从渠道字段猜测。", *(["部分历史来源未保存原始渠道 JSON"] if len(raw_artifacts) < len(runs) else []), *(["历史工件缺少明确 attempt，未猜测编号"] if any(run["job"].get("active_attempt_no") is None for run in runs) else [])]}
            manifest["manifest_sha256"] = _hash(manifest)
            encoded_manifest = _json(manifest)
            if len(encoded_manifest.encode()) > self.limits.max_metadata_bytes:
                raise DatasetError("dataset_too_large", "数据集清单超过容量")
            (output / "manifest.json").write_text(encoded_manifest, encoding="utf-8")
            if sum(p.stat().st_size for p in output.iterdir()) > self.limits.max_storage_bytes:
                raise DatasetError("dataset_too_large", "数据集输出超过磁盘容量")
            destination = self.storage_root / dataset_id
            if destination.exists():
                existing = self.manifest(dataset_id)
                if existing["manifest_sha256"] != manifest["manifest_sha256"]:
                    raise DatasetError("dataset_conflict", "已有数据集内容与固定身份不一致", reason='identity_manifest_mismatch')
                return existing
            for file in output.iterdir():
                file.chmod(0o400)
            output.chmod(0o700)
            check_active()
            os.rename(output, destination)
            return manifest

    def _raw_shards(self, path, output, run, source_index, info, check_active, prior_records, prior_shards):
        """Preserve channel payloads as separate JSONL; never normalize/count
        these as eligible reviews. The original source digest stays in manifest.
        """
        shards, handle, records, size, shard_records, digest = [], None, 0, 0, 0, None
        total_bytes = sum(p.stat().st_size for p in output.iterdir())
        def invalid_constant(_value):
            raise ValueError("non-JSON numeric constant")
        def close():
            if handle is not None:
                handle.close()
                shards.append({"name": name, "bytes": size, "records": shard_records,
                    "sha256": "sha256:" + digest.hexdigest(), "source_type": "channel_raw_payload"})
        try:
            with path.open("rb") as source_handle:
                while line := source_handle.readline(self.limits.max_record_bytes + 1):
                    if records % 1000 == 0:
                        check_active()
                    if len(line) > self.limits.max_record_bytes or not line.strip():
                        raise DatasetError("invalid_source", "渠道原始 JSON 行无效或超过单条容量")
                    try:
                        # Validate JSON without rewriting any field or text.
                        json.loads(line, parse_constant=invalid_constant)
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise DatasetError("invalid_source", "渠道原始记录不是有效 JSON") from exc
                    if not line.endswith(b"\n"):
                        line += b"\n"
                    if len(line) > self.limits.max_record_bytes:
                        raise DatasetError("dataset_too_large", "原始 JSON 记录超过导出单条容量")
                    if handle is None or (size and size + len(line) > self.limits.shard_bytes):
                        close()
                        if prior_shards + len(shards) >= self.limits.max_shards:
                            raise DatasetError("dataset_too_large", "渠道原始分片超过本次容量")
                        name = f"raw-{source_index:05d}-{len(shards):05d}.jsonl"
                        handle = (output / name).open("xb")
                        size, shard_records, digest = 0, 0, hashlib.sha256()
                    handle.write(line)
                    digest.update(line)
                    size += len(line)
                    total_bytes += len(line)
                    records += 1
                    shard_records += 1
                    if prior_records + records > self.limits.max_records or total_bytes > self.limits.max_storage_bytes:
                        raise DatasetError("dataset_too_large", "渠道原始记录超过本次容量")
            close()
        finally:
            if handle is not None and not handle.closed:
                handle.close()
        declared = run["report"].get("source_unique_reviews")
        if type(declared) is int and declared != records:
            raise DatasetError("invalid_source", "渠道原始 JSON 数量与验证报告不一致")
        return {"job_id": run["job"]["id"], "attempt_no": run["job"].get("active_attempt_no"),
            "channel": run["channel"], "source_type": "channel_raw_payload", "records": records,
            "source_sha256": info["sha256"], "source_bytes": info["bytes"], "shards": shards,
            "declared_excluded_records": run["report"].get("excluded_reviews"),
            "eligibility": "not_inferred_from_raw_payload", "scope": "selected_runs_before_review_filters",
            "included_in_normalized_counts": False}

    @staticmethod
    def _ranks(db):
        cursor = db.execute("SELECT seq, ROW_NUMBER() OVER(PARTITION BY entity ORDER BY sort_key) FROM facts")
        yield from cursor

    @staticmethod
    def _validate_row(row, run):
        identity = {'job_id': run['job']['id'], 'validation_sha256': run.get('report_hash')}
        if row.get("channel") not in {None, "", run["channel"]}:
            raise DatasetError("invalid_source", "评论渠道与任务不一致", reason='review_channel_mismatch', **identity)
        if run["market_contract_mode"] != "current":
            return
        if source.market_region_value(row.get("market_region")) != source._job_market_region(run["job"]) or not source.clean(row.get("market_region_evidence")):
            raise DatasetError("invalid_source", "任务与评论地区证据不一致", reason='review_region_mismatch', **identity)
        if source.parse_boolean(row.get("is_eligible")) is not True or source.parse_boolean(row.get("is_valid_text_review")) is not bool(source.clean(row.get("review_text"))):
            raise DatasetError("invalid_source", "评论有效性与完整正文不一致", reason='review_text_flag_mismatch', **identity)

    @staticmethod
    def _audit(run, count, written):
        report = run["report"]
        identity = {'job_id': run['job']['id'], 'validation_sha256': run.get('report_hash')}
        if run["market_contract_mode"] == "current":
            for field, expected in (("eligible_reviews", count), ("unique_reviews", count), ("valid_text_reviews", written)):
                if type(report.get(field)) is not int or report[field] != expected:
                    raise DatasetError("invalid_source", "验证报告数量与实际 CSV 不一致", reason='validation_count_mismatch',
                                       phase='validation_manifest', field=field, expected=expected,
                                       observed=report.get(field), **identity)
            try:
                audit = source_completeness_audit(run["job"], report, csv_eligible_rows=count)
            except SourceCompletenessError as exc:
                raise DatasetError('invalid_source', '来源完整度合同与采集结果不一致', reason='source_completeness_mismatch',
                                   phase='source_completeness', **_completeness_diagnostic(report, count, exc), **identity) from exc
            return {**audit, "eligible_records": count, "written_records": written, "excluded_records": report["excluded_reviews"]}
        if report.get("status") == "PARTIAL_SUCCESS":
            raise DatasetError("invalid_source", "部分结果缺少可核验的完整度合同")
        if type(report.get("unique_reviews")) is not int or report["unique_reviews"] != count:
            raise DatasetError("invalid_source", "历史验证报告与实际记录不一致")
        total = report.get("source_total")
        return {"job_id": run["job"]["id"], "collection_status": run["job"]["status"], "source_reconciled": total == count if type(total) is int else None, "declared_source_total": total if type(total) is int else None, "collected_source_unique_reviews": count, "eligible_records": count, "written_records": written, "excluded_records": 0, "completeness_known": type(total) is int and total == count}

    def _write_shards(self, db, output, dataset_id, check_active):
        shards, handle, size, count, digest = [], None, 0, 0, None
        ranks = db.execute("SELECT seq,payload,ROW_NUMBER() OVER(PARTITION BY entity ORDER BY sort_key) FROM facts ORDER BY sort_key")
        def close():
            if handle is not None:
                handle.close()
                shards.append({"name": name, "bytes": size, "records": count, "sha256": "sha256:" + digest.hexdigest()})
        total = sum(p.stat().st_size for p in output.iterdir() if p.name.startswith("raw-"))
        try:
            for seq, payload, rank in ranks:
                if seq % 1000 == 1:
                    check_active()
                fact = json.loads(payload)
                fact.update(canonical_entity_rank=rank, is_duplicate_occurrence=rank > 1)
                fact["source_id"] = "dsrc_" + _hash({"dataset_id": dataset_id, "record_id": fact["record_id"], "row_content_sha256": fact["row_content_sha256"]}).split(":")[1]
                line = (_json(fact) + "\n").encode()
                if len(line) > self.limits.max_record_bytes:
                    raise DatasetError("dataset_too_large", "完整评论超过导出单条容量")
                if handle is None or (size and size + len(line) > self.limits.shard_bytes):
                    close()
                    if len(shards) >= self.limits.max_shards:
                        raise DatasetError("dataset_too_large", "导出分片数量超过本次容量")
                    name = f"reviews-{len(shards):05d}.jsonl"
                    handle = (output / name).open("xb")
                    size, count, digest = 0, 0, hashlib.sha256()
                handle.write(line)
                digest.update(line)
                size += len(line)
                total += len(line)
                count += 1
                if total + (output / "reviews.sqlite3").stat().st_size > self.limits.max_storage_bytes:
                    raise DatasetError("dataset_too_large", "数据集输出超过磁盘容量")
                db.execute("UPDATE facts SET payload=? WHERE seq=?", (line.decode().rstrip("\n"), seq))
            close()
        finally:
            if handle is not None and not handle.closed:
                handle.close()
        return shards

    @staticmethod
    def _file_hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def manifest(self, dataset_id: str) -> dict[str, Any]:
        if not isinstance(dataset_id, str) or not _DATASET.fullmatch(dataset_id):
            _invalid("数据集标识无效")
        with self._read(self.storage_root, Path(dataset_id) / "manifest.json", self.limits.max_metadata_bytes) as (handle, _):
            manifest = json.load(handle)
        material = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
        if manifest.get("dataset_id") != dataset_id or manifest.get("contract") != CONTRACT or manifest.get("manifest_sha256") != _hash(material):
            raise DatasetError("dataset_changed", "数据集清单不一致")
        identity = {key: manifest.get(key) for key in ("contract", "version", "normalization_version", "scope", "inputs", "shard_bytes")}
        # Old frozen references retain their original identity recipe. New
        # datasets bind every emitted normalization version explicitly.
        if manifest.get('version') == 'wenquan-dataset/1.2.0':
            identity['normalization_contracts'] = manifest.get('normalization_contracts')
        if "ds_" + _hash(identity).split(":")[1] != dataset_id:
            raise DatasetError("dataset_changed", "数据集身份与清单内容不一致")
        return manifest

    @contextmanager
    def _index(self, dataset_id):
        manifest = self.manifest(dataset_id)
        with self._read(self.storage_root, Path(dataset_id) / "reviews.sqlite3", self.limits.max_storage_bytes) as (handle, before):
            key = dataset_id, _identity(before), manifest["index_sha256"]
            if key not in self._verified:
                digest = hashlib.sha256()
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                if "sha256:" + digest.hexdigest() != manifest["index_sha256"]:
                    raise DatasetError("dataset_changed", "数据集索引发生变化")
                self._verified[key] = True
            # Parent directories are service-private. FD remains held and is
            # checked against the path on exit; no untrusted SQL is accepted.
            with closing(sqlite3.connect(source.sqlite_uri(self.storage_root / dataset_id / "reviews.sqlite3"), uri=True)) as db:
                yield db, manifest

    def _where(self, filters=None, query=None, review_ids=None, population="entities"):
        if filters is not None and not isinstance(filters, Mapping):
            _invalid("评论筛选必须是对象")
        filters = dict(filters or {})
        allowed = {"source_product_ids", "ratings", "channels", "brands", "models", "market_regions", "category_ids", "job_ids", "has_text", "date_start", "date_end"}
        if set(filters) - allowed or population not in {"entities", "occurrences"}:
            _invalid("评论筛选无效")
        clauses, args = (["canonical=1"] if population == "entities" else []), []
        for key, field in {"source_product_ids": "json_extract(payload, '$.source_product_id')", "ratings": "rating", "channels": "channel", "brands": "brand", "models": "model", "market_regions": "market_region", "category_ids": "category_id", "job_ids": "job_id"}.items():
            if key not in filters:
                continue
            values = filters[key]
            if not isinstance(values, list) or len(values) > 200 or any((type(v) is not int or v not in range(1, 6)) if key == "ratings" else (not isinstance(v, str) or len(v) > 500) for v in values):
                _invalid("评论筛选列表无效")
            if values:
                clauses.append(f"{field} IN ({','.join('?' for _ in values)})")
                args.extend(values)
        if "has_text" in filters:
            if type(filters["has_text"]) is not bool:
                _invalid("正文筛选必须为布尔值")
            clauses.append("has_text=?")
            args.append(int(filters["has_text"]))
        for key, operator in (("date_start", ">="), ("date_end", "<=")):
            if key in filters:
                try:
                    if date.fromisoformat(filters[key]).isoformat() != filters[key]:
                        raise ValueError
                except (ValueError, TypeError):
                    _invalid("日期无效")
                clauses.append(f"submission_date {operator} ?")
                args.append(filters[key])
        if query is not None:
            if not isinstance(query, str) or len(query) > 1000:
                _invalid("搜索词无效")
            if query:
                clauses.append("instr(search_text,?)>0")
                args.append(query.casefold())
        if review_ids is not None:
            if not isinstance(review_ids, list) or not review_ids or len(review_ids) > 100 or any(not isinstance(v, str) or len(v) > 200 for v in review_ids):
                _invalid("原文标识无效")
            marks = ','.join('?' for _ in review_ids)
            clauses.append(f"(occurrence IN ({marks}) OR entity IN ({marks}) OR review_id IN ({marks}))")
            args.extend(review_ids * 3)
        return " AND ".join(clauses) or "1", args

    def _page_items(self, cursor):
        items, size = [], 0
        try:
            for (payload,) in cursor:
                length = len(payload.encode())
                if length > self.limits.page_bytes:
                    raise DatasetError("review_too_large", "完整原文超过页面容量，请使用分片导出")
                if items and size + length > self.limits.page_bytes:
                    break
                size += length
                items.append(json.loads(payload))
        finally:
            cursor.close()
        return items

    def _cached_page(self, db, key, where, args, offset, limit):
        """Cache and read one page in the same transaction; eviction is safe.

        Existing queries/members schemas remain compatible with rollback images.
        The cache is disposable acceleration, never authority for source facts.
        """
        cache_path = self.storage_root / "query-cache.sqlite3"
        if cache_path.is_symlink() or (cache_path.exists() and (not cache_path.is_file() or cache_path.stat().st_nlink != 1)):
            raise DatasetError("unsafe_path", "查询缓存文件不安全")
        try:
            db.execute("ATTACH DATABASE ? AS cache", (str(cache_path),))
            db.execute("PRAGMA busy_timeout=100")
            db.execute("PRAGMA cache.cache_size=-4096")
            page_size = db.execute("PRAGMA cache.page_size").fetchone()[0]
            page_count = db.execute("PRAGMA cache.page_count").fetchone()[0]
            if page_count * page_size > self.limits.max_cache_bytes:
                return None
            db.execute(f"PRAGMA cache.max_page_count={max(1, self.limits.max_cache_bytes // page_size)}")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS cache.queries(key TEXT PRIMARY KEY,total INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS cache.members(key TEXT,position INTEGER,seq INTEGER,PRIMARY KEY(key,position))")
            db.execute("CREATE TABLE IF NOT EXISTS cache.query_usage(key TEXT PRIMARY KEY,last_used REAL NOT NULL)")
            row = db.execute("SELECT total FROM cache.queries WHERE key=?", (key,)).fetchone()
            missed = row is None
            expired = []
            if row is None:
                count = db.execute("SELECT COUNT(*) FROM cache.queries").fetchone()[0]
                remove = max(0, count - self.limits.max_cached_queries + 1)
                expired = db.execute("SELECT q.key FROM cache.queries q LEFT JOIN cache.query_usage u ON u.key=q.key ORDER BY COALESCE(u.last_used,0),q.key LIMIT ?", (remove,)).fetchall()
                for (old_key,) in expired:
                    for table in ("members", "queries", "query_usage"):
                        db.execute(f"DELETE FROM cache.{table} WHERE key=?", (old_key,))
                db.execute(f"INSERT INTO cache.members SELECT ?,ROW_NUMBER() OVER(ORDER BY seq)-1,seq FROM facts WHERE {where}", [key, *args])
                total = db.execute("SELECT COUNT(*) FROM cache.members WHERE key=?", (key,)).fetchone()[0]
                db.execute("INSERT INTO cache.queries VALUES(?,?)", (key, total))
                row = (total,)
            db.execute("INSERT OR REPLACE INTO cache.query_usage VALUES(?,?)", (key, time.time()))
            items = self._page_items(db.execute("SELECT f.payload FROM cache.members m JOIN facts f ON f.seq=m.seq WHERE m.key=? AND m.position>=? ORDER BY m.position LIMIT ?", (key, offset, limit)))
            db.commit()
            self._metric(**{"misses" if missed else "hits": 1}, evictions=len(expired))
            return row[0], items
        except sqlite3.OperationalError as exc:
            db.rollback()
            # Full/contended cache: read the immutable facts with the same
            # predicate and ordering. Corruption, IO and unsafe paths fail closed.
            if _sqlite_code(exc) not in {13, 5, 6}:  # FULL, BUSY, LOCKED
                raise
            return None

    def query(self, dataset_id: str, *, filters=None, query=None, review_ids=None, population="entities", offset=0, limit=50, cursor=None) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= self.limits.page_size:
            _invalid("分页参数无效")
        where, args = self._where(filters, query, review_ids, population)
        key = _hash([dataset_id, where, args])
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 4096:
                    raise ValueError
                value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if set(value) != {"query", "offset"} or value["query"] != key or type(value["offset"]) is not int or value["offset"] < 0:
                    raise ValueError
                offset = value["offset"]
            except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
                raise DatasetError("invalid_request", "分页标识与数据查询不一致", 422) from exc
        with self._index(dataset_id) as (db, manifest):
            started = time.monotonic()
            deadline = started + self.limits.max_query_seconds
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                result = self._cached_page(db, key, where, args, offset, limit)
                if result is None:
                    self._metric(fallbacks=1)
                    total = db.execute(f"SELECT COUNT(*) FROM facts WHERE {where}", args).fetchone()[0]
                    items = self._page_items(db.execute(f"SELECT payload FROM facts WHERE {where} ORDER BY seq LIMIT ? OFFSET ?", [*args, limit, offset]))
                else:
                    total, items = result
            except sqlite3.OperationalError as exc:
                self._metric(last_error="query_timeout" if _sqlite_code(exc) == 9 else "sqlite_error")
                if _sqlite_code(exc) == 9:  # INTERRUPT
                    raise DatasetError("query_timeout", "查询超过本次时间，请缩小条件或使用分片导出", 503,
                                       reason='dataset_sqlite_error', phase='query', sqlite_code=_sqlite_code(exc)) from exc
                raise DatasetError('dataset_storage_unavailable', '数据查询未完成，未返回部分结果', 503,
                                   reason='dataset_sqlite_error', phase='query', sqlite_code=_sqlite_code(exc)) from exc
            except DatasetError as exc:
                self._metric(last_error=exc.code)
                raise
            finally:
                db.set_progress_handler(None, 0)
                self._metric(queries=1, query_seconds=max(0, time.monotonic() - started))
        following = offset + len(items)
        return {"contract": CONTRACT, "dataset_id": dataset_id, "manifest_sha256": manifest["manifest_sha256"], "items": items, "population": population, "total": total, "page": {"offset": offset, "returned": len(items), "has_more": following < total}, "next_cursor": base64.urlsafe_b64encode(_json({"query": key, "offset": following}).encode()).decode() if following < total else None}

    def aggregate(self, dataset_id: str, *, group_by=None, filters=None, query=None, population="entities") -> dict[str, Any]:
        with self._index(dataset_id) as (db, manifest):
            result = self._aggregate_index(db, group_by=group_by, filters=filters, query=query, population=population)
        return {"contract": CONTRACT, "dataset_id": dataset_id, "manifest_sha256": manifest["manifest_sha256"], **result}

    def _aggregate_index(self, db, *, group_by=None, filters=None, query=None, population='entities'):
        if group_by is not None and (not isinstance(group_by, list) or any(not isinstance(g, str) for g in group_by)):
            _invalid("分组字段必须是文本列表")
        groups = list(group_by or [])
        if len(groups) > 3 or len(set(groups)) != len(groups) or any(g not in _GROUP_FIELDS for g in groups):
            _invalid("分组字段无效")
        where, args = self._where(filters, query, None, population)
        columns = ','.join(groups)
        suffix = (f" GROUP BY {columns}" if groups else "")
        rows = db.execute(f"SELECT {columns + ',' if columns else ''}COUNT(*) FROM facts WHERE {where}{suffix} LIMIT ?", [*args, self.limits.max_groups + 1]).fetchall()
        if len(rows) > self.limits.max_groups:
            raise DatasetError("aggregate_too_large", "分组数量过多，请缩小条件")
        result = [{"group": dict(zip(groups, row[:-1])), "count": row[-1]} for row in rows]
        result.sort(key=lambda item: (-item["count"], _json(item["group"])))
        return {"population": population, "groups": result, "total": sum(item["count"] for item in result), "count_basis": "去重评论实体" if population == "entities" else "评论出现记录"}

    def export(self, dataset_id: str, *, shard: str | None = None, source_type: str = "normalized_review") -> Iterator[bytes]:
        """Yield verified complete JSONL shards; broker chooses HTTP framing.

        Hash verification happens before yielding each shard. A failed stream
        remains incomplete; consumers must validate the manifest and all shards.
        """
        manifest = self.manifest(dataset_id)
        if not isinstance(source_type, str) or source_type not in {"normalized_review", "channel_raw_payload"}:
            _invalid("原文导出类型无效")
        parts = manifest["shards"] if source_type == "normalized_review" else [part for artifact in manifest.get("raw_artifacts", []) for part in artifact["shards"]]
        if source_type == "channel_raw_payload" and not manifest.get("raw_payload_complete") and shard is None:
            raise DatasetError("raw_source_unavailable", "部分来源缺少渠道原始 JSON，请按已有分片读取")
        if shard is not None:
            parts = [part for part in parts if part["name"] == shard]
            if not parts:
                _invalid("导出分片不存在")
        for part in parts:
            with self._read(self.storage_root, Path(dataset_id) / part["name"], self.limits.max_storage_bytes) as (handle, _):
                digest = hashlib.sha256()
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                if "sha256:" + digest.hexdigest() != part["sha256"]:
                    raise DatasetError("dataset_changed", "导出分片校验失败")
                handle.seek(0)
                while chunk := handle.read(1024 * 1024):
                    yield chunk
