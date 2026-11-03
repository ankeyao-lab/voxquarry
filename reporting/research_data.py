"""Read-only research over a report and its frozen, full Review Fact artifact.

This reader never rebuilds a snapshot or falls back to evidence/active collection
data. Text is the complete *normalized* title/body used by reporting, not the
connector's original response bytes. All searches and aggregates scan the full
verified population before pagination.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import stat
import zlib
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote

from reporting.source import canonical_json


SOURCE_CONTRACT = "zhuojian-research-source/v1"
MAX_PAGE_SIZE = 100
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_REVIEW_PAGE_BYTES = 650 * 1024
MAX_SINGLE_REVIEW_BYTES = 750 * 1024
MAX_RESEARCH_RESPONSE_BYTES = 1024 * 1024
_SNAPSHOT_ID = re.compile(r"^snap_[0-9a-f]{24}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
FILTER_KEYS = {"ratings", "has_text", "channels", "brands", "models", "date_start", "date_end"}
QUERY_KEYS = {"report_id", "review_ids", "query", "filters", "population", "term_groups", "exclude_terms", "match_mode"}
GROUP_FIELDS = {"rating", "channel", "brand", "model", "submission_month", "has_text"}
_HAN_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")


class ResearchDataError(ValueError):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def _invalid(message: str) -> None:
    raise ResearchDataError("invalid_request", message, 422)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def _json(value: str | bytes) -> Any:
    return json.loads(value, object_pairs_hook=_strict_object, parse_constant=_reject_constant)


def _read_at(directory_fd: int, name: str, maximum: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ResearchDataError("research_source_unavailable", "快照文件类型或大小不受支持；未读取部分数据冒充完整数据")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            value = handle.read(maximum + 1)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if len(value) > maximum or identity(before) != identity(after) or identity(after) != identity(current):
            raise ResearchDataError("research_source_changed", "读取期间快照文件发生变化，请重新开始本次研究")
        return value
    finally:
        os.close(fd)


def _open_frozen(root: Path, snapshot_id: str) -> tuple[bytes, int, list[int]]:
    # Keep directories and the fact FD held across both passes; no verified path
    # is reopened. Model inputs never select filesystem paths.
    fds: list[int] = []
    fact_fd: int | None = None
    try:
        fds.append(os.open(root.resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        fds.append(os.open("snapshots", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        fds.append(os.open(snapshot_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        manifest = _read_at(fds[-1], "manifest.json", 4 * 1024 * 1024)
        fact_fd = os.open("review_fact.preview.jsonl.gz", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
        return manifest, fact_fd, fds
    except (OSError, ResearchDataError) as exc:
        if fact_fd is not None:
            os.close(fact_fd)
        for fd in reversed(fds):
            os.close(fd)
        if isinstance(exc, ResearchDataError):
            raise
        raise ResearchDataError("research_source_unavailable", "该报告的冻结评论文件缺失或不可安全读取；不能用精选证据或当前采集数据替代") from exc


def report_provenance(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "report_id": report["report_id"],
        "snapshot_id": report.get("data_snapshot_id"),
        "analysis_version": report.get("analysis_version"),
        "report_sha256": _digest(report),
        "scope_sha256": _digest(report.get("scope") or {}),
        "fact_file_sha256": None,
        "logical_content_sha256": (report.get("hashes") or {}).get("data"),
        "manifest_sha256": None,
        "record_count": None,
        "entity_count": None,
        "written_entity_count": None,
        "content_kind": "saved_report_json",
        "normalization": "报告保存的 JSON；不截断字段内容",
    }


def research_source(provenance: Mapping[str, Any], source_type: str, selector: dict[str, Any], content: Any) -> dict[str, Any]:
    identity = {
        "contract": SOURCE_CONTRACT,
        "source_type": source_type,
        **{key: provenance[key] for key in ("report_id", "snapshot_id", "analysis_version", "report_sha256", "scope_sha256", "fact_file_sha256")},
        "content_sha256": _digest(content),
        "selector": selector,
    }
    source_id = "rsrc_" + _digest(identity).split(":")[1][:24]
    return {
        **identity,
        "source_id": source_id,
        "stable_uri": f"voxquarry://zhuojian/reports/{quote(str(provenance['report_id']), safe='')}/research/{source_id}",
    }


class FrozenReviewData:
    """Two-pass held-FD reader; only identity indexes survive across rows.

    Construction verifies all compressed bytes. iter_facts validates every row,
    logical hash and declared population before a tool may publish its result.
    Consumers must exhaust the iterator and close the reader (use ``with``).
    """

    def __init__(self, root: Path, report: Mapping[str, Any]):
        self.report = report
        self._handle: Any = None
        self._directories: list[int] = []
        self.provenance: dict[str, Any] = {}
        snapshot_id = report.get("data_snapshot_id")
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise ResearchDataError("research_source_unavailable", "报告没有合法的冻结快照标识")
        manifest_bytes, fact_fd, self._directories = _open_frozen(root, snapshot_id)
        try:
            self._handle = os.fdopen(fact_fd, "rb")
            self._initial_stat = os.fstat(fact_fd)
            if not stat.S_ISREG(self._initial_stat.st_mode) or self._initial_stat.st_size > MAX_COMPRESSED_BYTES:
                raise ResearchDataError("research_source_unavailable", "快照类型或大小超过安全读取容量，未返回部分数据")
            manifest = _json(manifest_bytes)
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be object")
            hashes = manifest.get("hashes") or {}
            if (
                manifest.get("schema_version") not in {"snapshot-manifest/v1", "snapshot-manifest/v2"}
                or manifest.get("snapshot_id") != snapshot_id
                or not _SHA256.fullmatch(str(hashes.get("input_manifest") or ""))
                or snapshot_id != "snap_" + str(hashes.get("input_manifest", "")).removeprefix("sha256:")[:24]
                or (manifest.get("schema_version") == "snapshot-manifest/v2" and manifest.get("content_hash") != hashes.get("input_manifest"))
                or manifest.get("scope") != report.get("scope")
                or hashes.get("logical_content") != (report.get("hashes") or {}).get("data")
                or not _SHA256.fullmatch(str(hashes.get("logical_content") or ""))
            ):
                raise ValueError("report/manifest binding mismatch")
            physical_hash = hashlib.sha256()
            bytes_read = 0
            while chunk := self._handle.read(1024 * 1024):
                bytes_read += len(chunk)
                if bytes_read > MAX_COMPRESSED_BYTES:
                    raise ResearchDataError("research_source_unavailable", "完整快照超过安全读取容量，未返回部分数据")
                physical_hash.update(chunk)
            self._ensure_unchanged()
            if hashes.get("preview_fact") != "sha256:" + physical_hash.hexdigest():
                raise ValueError("compressed artifact hash mismatch")
            self.manifest = manifest
            self._base_provenance = {
                **report_provenance(report),
                "fact_file_sha256": "sha256:" + physical_hash.hexdigest(),
                "manifest_sha256": _bytes_digest(manifest_bytes),
                "content_kind": "snapshot_review_fact",
                "normalization": "完整标准化 Review Fact JSON；标题和正文仅做空白标准化、不截断；不是连接器原始响应字节",
            }
        except Exception as exc:
            self.close()
            if isinstance(exc, ResearchDataError):
                raise
            raise ResearchDataError("research_source_invalid", "报告、快照范围或物理内容校验不一致，已停止读取") from exc

    def __enter__(self) -> FrozenReviewData:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        for fd in reversed(self._directories):
            os.close(fd)
        self._directories = []

    def _ensure_unchanged(self) -> None:
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        try:
            current = os.stat("review_fact.preview.jsonl.gz", dir_fd=self._directories[-1], follow_symlinks=False)
            if signature(current) != signature(self._initial_stat) or signature(os.fstat(self._handle.fileno())) != signature(self._initial_stat):
                raise ResearchDataError("research_source_changed", "读取期间评论快照发生变化，请重新开始研究")
        except OSError as exc:
            raise ResearchDataError("research_source_changed", "读取期间评论快照发生变化，请重新开始研究") from exc

    def _validate_fact(self, fact: Any, seen: set[str], ranks: dict[str, int]) -> None:
        if not isinstance(fact, dict):
            raise ValueError("fact must be object")
        occurrence = fact.get("review_occurrence_id")
        rank = fact.get("canonical_entity_rank")
        if (
            not isinstance(occurrence, str) or not occurrence or occurrence in seen
            or not isinstance(fact.get("review_title"), str)
            or not isinstance(fact.get("review_text"), str)
            or not isinstance(fact.get("has_text"), bool)
            or not isinstance(fact.get("rating_valid"), bool)
            or fact.get("business_key_status") not in {"complete", "incomplete"}
            or isinstance(rank, bool) or not isinstance(rank, int) or rank < 1
            or not _SHA256.fullmatch(str(fact.get("row_content_sha256") or ""))
        ):
            raise ValueError("invalid fact identity/content")
        for fact_key, manifest_key in (("fact_schema_version", "analysis_input_version"), ("parser_version", "parser_version"), ("normalization_version", "normalization_version")):
            if not fact.get(fact_key) or fact[fact_key] != self.manifest.get(manifest_key):
                raise ValueError("fact version mismatch")
        if any(not isinstance(fact.get(key), str) for key in ("channel", "brand", "model")):
            raise ValueError("invalid grouping field")
        submission_date = fact.get("submission_date")
        if submission_date is not None and (not isinstance(submission_date, str) or date.fromisoformat(submission_date).isoformat() != submission_date):
            raise ValueError("invalid submission date")
        if fact["has_text"] != bool(fact["review_text"]):
            raise ValueError("text flag mismatch")
        if fact["rating_valid"] and (type(fact.get("rating")) is not int or not 1 <= fact["rating"] <= 5):
            raise ValueError("invalid rating")
        if not fact["rating_valid"] and fact.get("rating") is not None:
            raise ValueError("rating flag mismatch")
        if fact.get("is_recommended") is not None and type(fact["is_recommended"]) is not bool:
            raise ValueError("invalid recommendation")
        if fact["business_key_status"] == "complete":
            entity = fact.get("review_entity_id")
            if not isinstance(entity, str) or not entity:
                raise ValueError("missing entity")
            ranks[entity] += 1
            if rank != ranks[entity] or fact.get("is_duplicate_occurrence") is not (rank > 1):
                raise ValueError("invalid canonical rank")
        elif fact.get("review_entity_id") is not None or rank != 1 or fact.get("is_duplicate_occurrence") is not False:
            raise ValueError("invalid unknown-identity rank")
        seen.add(occurrence)

    def iter_facts(self) -> Iterator[dict[str, Any]]:
        self._ensure_unchanged()
        self._handle.seek(0)
        self.provenance = {}
        seen: set[str] = set()
        ranks: dict[str, int] = defaultdict(int)
        logical_hash = hashlib.sha256()
        byte_count = record_count = written_count = entity_count = written_entities = 0
        try:
            with gzip.GzipFile(fileobj=self._handle, mode="rb") as zipped:
                while line := zipped.readline(MAX_RECORD_BYTES + 1):
                    byte_count += len(line)
                    if len(line) > MAX_RECORD_BYTES or byte_count > MAX_FACT_BYTES:
                        raise ResearchDataError("research_source_unavailable", "完整评论或单条记录超过安全读取容量，未返回部分数据")
                    fact = _json(line.decode("utf-8"))
                    self._validate_fact(fact, seen, ranks)
                    if record_count:
                        logical_hash.update(b"\n")
                    logical_hash.update(fact["row_content_sha256"].encode("ascii"))
                    record_count += 1
                    written_count += fact["has_text"]
                    entity = fact["business_key_status"] == "incomplete" or fact["canonical_entity_rank"] == 1
                    entity_count += entity
                    written_entities += entity and fact["has_text"]
                    yield fact
            self._ensure_unchanged()
            counts = self.manifest.get("record_counts") or {}
            expected_counts = {"occurrences": record_count, "unique_entities": len(ranks), "written_reviews": written_count}
            if "sha256:" + logical_hash.hexdigest() != self.manifest["hashes"]["logical_content"] or any(type(counts.get(key)) is not int or counts[key] != value for key, value in expected_counts.items()):
                raise ValueError("snapshot counts/logical hash mismatch")
            expected_samples = {"occurrences": record_count, "review_entities": entity_count, "written_review_entities": written_entities}
            samples = self.report.get("samples") or {}
            if any(key in samples and samples[key] != value for key, value in expected_samples.items()):
                raise ValueError("report samples mismatch")
            self.provenance = {**self._base_provenance, "record_count": record_count, "entity_count": entity_count, "written_entity_count": written_entities}
            self.provenance["source_fingerprint"] = _digest(self.provenance)
        except (ValueError, TypeError, KeyError, AttributeError, OSError, EOFError, zlib.error) as exc:
            if isinstance(exc, ResearchDataError):
                raise
            raise ResearchDataError("research_source_invalid", "评论内容、逻辑哈希或完整性计数不一致，已停止读取") from exc


def page_args(payload: Mapping[str, Any]) -> tuple[int, int]:
    offset, limit = payload.get("offset", 0), payload.get("limit", 100)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        _invalid("offset 必须是非负整数，limit 必须是 1–100 的整数")
    return offset, limit


def page_result(offset: int, limit: int, total: int, returned: int) -> dict[str, Any]:
    next_offset = offset + returned if offset + returned < total else None
    return {"offset": offset, "limit": limit, "returned": returned, "total": total, "next_offset": next_offset, "has_more": next_offset is not None}


def _strings(value: Any, label: str, *, maximum: int = 50, item_length: int = 200) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(item, str) or not item.strip() or len(item) > item_length for item in value):
        _invalid(f"{label} 必须是最多 {maximum} 个非空字符串")
    return list(dict.fromkeys(value))


def normalize_query(payload: Mapping[str, Any]) -> dict[str, Any]:
    match_mode = payload.get("match_mode", "substring")
    if match_mode not in ("substring", "word"):
        _invalid("match_mode 必须是 substring 或 word")
    population = payload.get("population", "entities")
    if population not in ("entities", "occurrences"):
        _invalid("population 必须是 entities 或 occurrences")
    query = payload.get("query", "")
    if not isinstance(query, str) or len(query) > 500:
        _invalid("query 必须是最多 500 字符的词面检索字符串")
    review_ids = _strings(payload.get("review_ids", []), "review_ids", maximum=100)
    if any(not re.fullmatch(r"(?:occ|rev)_[A-Za-z0-9_-]{1,128}", value) for value in review_ids):
        _invalid("review_ids 仅接受稳定的 occ_ 出现记录 ID 或 rev_ 实体 ID")
    filters = payload.get("filters", {})
    if not isinstance(filters, dict) or set(filters) - FILTER_KEYS:
        _invalid("filters 包含不支持的筛选条件")
    normalized_filters: dict[str, Any] = {}
    for key, value in filters.items():
        if key == "ratings":
            if not isinstance(value, list) or len(value) > 5 or any(type(item) is not int or not 1 <= item <= 5 for item in value):
                _invalid("ratings 必须为 1–5 星的整数数组")
            normalized_filters[key] = sorted(set(value))
        elif key == "has_text":
            if type(value) is not bool:
                _invalid("has_text 必须为布尔值")
            normalized_filters[key] = value
        elif key in {"channels", "brands", "models"}:
            normalized_filters[key] = sorted(_strings(value, key, maximum=20))
        else:
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError("not ISO date")
            except ValueError:
                _invalid("日期必须为 YYYY-MM-DD")
            normalized_filters[key] = value
    if normalized_filters.get("date_start", "") > normalized_filters.get("date_end", "9999-12-31"):
        _invalid("date_start 不能晚于 date_end")
    term_groups = payload.get("term_groups", [])
    if not isinstance(term_groups, list) or len(term_groups) > 8:
        _invalid("term_groups 最多 8 组")
    groups: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for group in term_groups:
        if not isinstance(group, dict) or set(group) != {"label", "terms"}:
            _invalid("term_groups 每组仅允许 label 和 terms")
        label = group["label"]
        if not isinstance(label, str) or not label.strip() or len(label) > 100 or label in seen_labels:
            _invalid("term_groups label 必须非空且唯一")
        terms = _strings(group["terms"], "terms", maximum=20)
        if not terms:
            _invalid("每组 terms 不能为空")
        groups.append({"label": label, "terms": terms})
        seen_labels.add(label)
    normalized = {"population": population, "query": query, "review_ids": sorted(review_ids), "filters": normalized_filters, "term_groups": groups, "exclude_terms": _strings(payload.get("exclude_terms", []), "exclude_terms", maximum=20)}
    # Preserve the exact legacy query/data/source hashes for absent or explicit
    # substring mode. Only the new word semantics add an identity field.
    if match_mode == "word":
        normalized["match_mode"] = match_mode
    return normalized


def _literal_matcher(term: str, match_mode: str) -> Callable[[str], bool]:
    folded = term.casefold()
    if match_mode != "word" or (_HAN_CHARACTER.search(term) and not any(char.isspace() for char in term)):
        return lambda text: folded in text
    # Only literal input is accepted. Unicode letter/digit/underscore boundaries
    # do not stem words or tokenize Chinese; phrase whitespace remains literal.
    pattern = re.compile(r"(?<!\w)" + re.escape(folded) + r"(?!\w)")
    return lambda text: pattern.search(text) is not None


def _matches_filters(fact: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    for key, value in filters.items():
        if key == "ratings" and (not fact["rating_valid"] or fact.get("rating") not in value):
            return False
        if key == "has_text" and fact["has_text"] != value:
            return False
        if key in {"channels", "brands", "models"} and fact.get({"channels": "channel", "brands": "brand", "models": "model"}[key]) not in value:
            return False
        if key == "date_start" and (not fact.get("submission_date") or fact["submission_date"] < value):
            return False
        if key == "date_end" and (not fact.get("submission_date") or fact["submission_date"] > value):
            return False
    return True


def scan_reviews(source: FrozenReviewData, query: Mapping[str, Any], on_match: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Visit the full population; retain no Fact rows other than the callback's page."""
    ids = set(query["review_ids"])
    found_ids: set[str] = set()
    group_counts = {group["label"]: 0 for group in query["term_groups"]}
    match_mode = query.get("match_mode", "substring")
    terms = {query["query"], *query["exclude_terms"]}
    terms.update(term for group in query["term_groups"] for term in group["terms"])
    matchers = {term: _literal_matcher(term, match_mode) for term in terms}
    population_count = base_count = matched_count = 0
    for fact in source.iter_facts():
        if query["population"] == "entities" and fact["business_key_status"] != "incomplete" and fact["canonical_entity_rank"] != 1:
            continue
        population_count += 1
        identifiers = {fact["review_occurrence_id"], fact.get("review_entity_id")}
        found_ids.update(ids.intersection(identifiers))
        if (ids and not ids.intersection(identifiers)) or not _matches_filters(fact, query["filters"]):
            continue
        # A literal phrase must occur inside one field, not across title/body.
        texts = (fact["review_title"].casefold(), fact["review_text"].casefold())
        contains = lambda term: any(matchers[term](text) for text in texts)
        if (query["query"] and not contains(query["query"])) or any(contains(term) for term in query["exclude_terms"]):
            continue
        base_count += 1
        all_groups_match = True
        for group in query["term_groups"]:
            hit = any(contains(term) for term in group["terms"])
            group_counts[group["label"]] += hit
            all_groups_match = all_groups_match and hit
        if all_groups_match:
            matched_count += 1
            on_match(fact)
    # iter_facts only establishes provenance after the last row and all integrity
    # checks. Nothing has been returned to the caller while the scan was running.
    if not source.provenance:
        raise ResearchDataError("research_source_invalid", "完整评论扫描未完成")
    return {
        "population": query["population"], "population_count": population_count,
        "base_match_count": base_count,
        "group_match_counts": [{"label": label, "count": count} for label, count in group_counts.items()],
        "intersection_count": matched_count, "missing_review_ids": sorted(ids - found_ids),
        "search_semantics": (
            "标题/正文不区分大小写的字面词或短语边界匹配；边界按 Unicode 字母、数字及下划线判定，不做词形归一，turn 不命中 return/turning；含汉字且无空白的词条仍按子串匹配，短语空白按字面匹配。query、词组及排除词使用同一模式；组内 OR、组间 AND。各组计数使用相同基础筛选人口；这只是词面线索，不代表语义分类准确，零匹配不能证明相关语义不存在，星级不等于主题情感。"
            if match_mode == "word" else
            "标题/正文不区分大小写的词面子串匹配；组内 OR、组间 AND。各组计数使用相同基础筛选人口；零匹配不能证明相关语义不存在，星级不等于主题情感。"
        ),
        "scan_complete": True,
    }


def _cursor(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(canonical_json(value).encode("utf-8")).decode("ascii")


def _resolve_cursor(payload: Mapping[str, Any], query_sha256: str) -> tuple[int, int, str | None]:
    offset, limit = page_args(payload)
    expected_fingerprint = None
    if "cursor" in payload:
        if "offset" in payload:
            _invalid("cursor 和 offset 不能同时使用")
        raw = payload["cursor"]
        try:
            if not isinstance(raw, str) or len(raw) > 2000:
                raise ValueError("invalid cursor")
            cursor = _json(base64.b64decode(raw.encode("ascii"), altchars=b"-_", validate=True))
            if not isinstance(cursor, dict) or set(cursor) != {"source_fingerprint", "query_sha256", "offset"}:
                raise ValueError("invalid cursor")
            if cursor["query_sha256"] != query_sha256:
                _invalid("翻页时筛选条件与第一页不同。请原样保留第一页的筛选和词组，或不带cursor从第一页重新读取。")
            expected_fingerprint = cursor["source_fingerprint"]
            if not isinstance(expected_fingerprint, str) or not _SHA256.fullmatch(expected_fingerprint):
                raise ValueError("invalid fingerprint")
            offset = cursor["offset"]
            if type(offset) is not int or offset < 0:
                raise ValueError("invalid offset")
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, ResearchDataError):
                raise
            _invalid("cursor 无效")
    return offset, limit, expected_fingerprint


def query_reviews(source: FrozenReviewData, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    query = normalize_query(payload)
    query_sha256 = _digest(query)
    offset, limit, expected_fingerprint = _resolve_cursor(payload, query_sha256)
    page_facts: list[dict[str, Any]] = []
    matched_position = 0
    retained_bytes = 0
    byte_limited = False

    def retain_page(fact: dict[str, Any]) -> None:
        nonlocal matched_position, retained_bytes, byte_limited
        if offset <= matched_position and len(page_facts) < limit and not byte_limited:
            # Budget for both the complete Fact and its derived item/source
            # identity. Never cut body/title or skip a record to fit a page.
            estimated_bytes = len(canonical_json(fact).encode("utf-8")) + 2048
            if page_facts and retained_bytes + estimated_bytes > MAX_REVIEW_PAGE_BYTES:
                byte_limited = True
            elif estimated_bytes > MAX_SINGLE_REVIEW_BYTES:
                raise ResearchDataError("report_review_too_large", "这条完整评论超过问答单次传输容量；可从报告的评论 JSON 导出读取，正文未被截断")
            else:
                retained_bytes += estimated_bytes
                page_facts.append(fact)
        matched_position += 1

    coverage = scan_reviews(source, query, retain_page)
    if expected_fingerprint is not None and expected_fingerprint != source.provenance["source_fingerprint"]:
        raise ResearchDataError("research_source_changed", "翻页来源发生变化，请从第一页重新读取")
    items, sources = [], []
    for fact in page_facts:
        ref = research_source(source.provenance, "review", {"review_occurrence_id": fact["review_occurrence_id"]}, fact)
        items.append({**fact, "record_id": fact["review_occurrence_id"], "fact_sha256": _digest(fact), "source_id": ref["source_id"]})
        sources.append(ref)
    page = page_result(offset, limit, coverage["intersection_count"], len(items))
    return {
        "provenance": source.provenance, "query": query, "query_sha256": query_sha256,
        **coverage, "items": items, "sources": sources,
        "page_limit_reason": "bytes" if byte_limited else "items" if page["has_more"] else None,
        "next_cursor": _cursor({"source_fingerprint": source.provenance["source_fingerprint"], "query_sha256": query_sha256, "offset": page["next_offset"]}) if page["has_more"] else None,
    }, page


def _empty_summary() -> dict[str, int]:
    return {key: 0 for key in ("count", "written_count", "valid_rating_count", "unknown_rating_count", "rating_sum", "positive_rating_count", "negative_rating_count", "recommendation_numerator", "recommendation_denominator", "unknown_identity_count")}


def _add_summary(summary: dict[str, int], fact: Mapping[str, Any]) -> None:
    summary["count"] += 1
    summary["written_count"] += fact["has_text"]
    summary["unknown_identity_count"] += fact["business_key_status"] == "incomplete"
    if fact["rating_valid"]:
        rating = fact["rating"]
        summary["valid_rating_count"] += 1
        summary["rating_sum"] += rating
        summary["positive_rating_count"] += rating >= 4
        summary["negative_rating_count"] += rating <= 2
    else:
        summary["unknown_rating_count"] += 1
    if fact.get("is_recommended") is not None:
        summary["recommendation_denominator"] += 1
        summary["recommendation_numerator"] += fact["is_recommended"]


def _finish_summary(summary: dict[str, int]) -> dict[str, Any]:
    return {**summary, "average_rating": summary["rating_sum"] / summary["valid_rating_count"] if summary["valid_rating_count"] else None}


def aggregate_reviews(source: FrozenReviewData, payload: Mapping[str, Any]) -> dict[str, Any]:
    query = normalize_query(payload)
    group_by = payload.get("group_by", "rating")
    if not isinstance(group_by, str) or group_by not in GROUP_FIELDS:
        _invalid("group_by 不在支持的聚合字段中")
    summary = _empty_summary()
    grouped: dict[Any, dict[str, int]] = defaultdict(_empty_summary)
    record_ids_hash = hashlib.sha256(b"[")
    matched_count = 0

    def accumulate(fact: dict[str, Any]) -> None:
        nonlocal matched_count
        if group_by == "submission_month":
            key = fact["submission_date"][:7] if fact.get("submission_date") else None
        elif group_by == "rating":
            key = fact.get("rating") if fact["rating_valid"] else None
        else:
            key = fact.get(group_by)
        _add_summary(summary, fact)
        _add_summary(grouped[key], fact)
        if matched_count:
            record_ids_hash.update(b",")
        record_ids_hash.update(canonical_json(fact["review_occurrence_id"]).encode("utf-8"))
        matched_count += 1

    coverage = scan_reviews(source, query, accumulate)
    record_ids_hash.update(b"]")
    groups = [{"key": key, **_finish_summary(counts)} for key, counts in grouped.items()]
    groups.sort(key=lambda row: (-row["count"], canonical_json(row["key"])))
    result = {**coverage, "summary": _finish_summary(summary), "group_by": group_by, "groups": groups, "matched_record_ids_sha256": "sha256:" + record_ids_hash.hexdigest()}
    ref = research_source(source.provenance, "review_aggregate", {**query, "group_by": group_by}, result)
    return {
        "provenance": source.provenance, "query": query, "query_sha256": _digest(query),
        **result, "sources": [ref],
        "definitions": {
            "population": "entities 保留完整业务键的首个出现记录，并将身份未知记录单列计入；occurrences 保留所有出现记录。",
            "average_rating": "rating_sum / valid_rating_count；无有效评分时为 null",
            "positive_rating_count": "4–5 星记录数；不代表具体主题获得正向评价",
            "negative_rating_count": "1–2 星记录数；不代表具体主题受到投诉",
            "recommendation_rate": "recommendation_numerator / recommendation_denominator；只含明确推荐或不推荐记录",
            "group_order": "count 降序，计数相同按 JSON key 升序；不是语义重要性排名",
        },
    }


def _descriptor(path: str, value: Any) -> dict[str, Any]:
    kind = "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "scalar"
    return {"section": path, "type": kind, "length": len(value) if isinstance(value, (dict, list, str)) else None}


def report_section(report: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    section = payload.get("section", "index")
    if not isinstance(section, str) or len(section) > 1000:
        _invalid("section 必须是 index 或 JSON pointer")
    offset, limit = page_args(payload)
    provenance = report_provenance(report)
    provenance["source_fingerprint"] = _digest(provenance)
    descriptors = []
    if section == "index":
        for key, value in report.items():
            pointer = "/" + key.replace("~", "~0").replace("/", "~1")
            descriptors.append(_descriptor(pointer, value))
            if isinstance(value, dict):
                descriptors.extend(_descriptor(pointer + "/" + child.replace("~", "~0").replace("/", "~1"), item) for child, item in value.items())
        content = {"top_keys": list(report), "descriptors": descriptors}
        page = None
    else:
        if section and not section.startswith("/"):
            _invalid("section 必须为以 / 开头的 JSON pointer；空字符串表示报告根节点")
        content: Any = report
        try:
            for raw_token in section.split("/")[1:] if section else []:
                if re.search(r"~(?![01])", raw_token):
                    _invalid("JSON pointer 转义无效")
                token = raw_token.replace("~1", "/").replace("~0", "~")
                if isinstance(content, dict):
                    content = content[token]
                elif isinstance(content, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
                    content = content[int(token)]
                else:
                    raise KeyError(token)
        except (KeyError, IndexError) as exc:
            raise ResearchDataError("report_section_not_found", "报告中没有该 JSON 节点", 404) from exc
        if isinstance(content, dict):
            descriptors = [_descriptor(section + "/" + key.replace("~", "~0").replace("/", "~1"), value) for key, value in content.items()]
        if isinstance(content, list):
            total = len(content)
            content = content[offset:offset + limit]
            page = page_result(offset, limit, total, len(content))
        else:
            if offset:
                _invalid("只有数组节点支持 offset；对象可通过子节点 pointer 继续读取")
            page = None
    selector = {"section": section, "offset": offset, "limit": limit}
    ref = research_source(provenance, "report_section", selector, content)
    return {"provenance": provenance, "section": section, "content": content, "descriptors": descriptors, "sources": [ref]}, page
