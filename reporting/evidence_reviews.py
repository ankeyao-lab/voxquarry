"""Read full comments from the report's immutable Review Fact, never live data.

The optional presentation fields do not change persisted evidence, quotes,
group membership or metrics. Cache only requested identities (one bounded API
page), keyed by the immutable artifact hash and current filesystem identity.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from reporting.evidence_review_index import indexed_comments


def _identity(row: dict[str, Any]) -> tuple[str, str]:
    for field in ("review_occurrence_id", "review_entity_id"):
        if row.get(field):
            return field, str(row[field])
    return "", ""


def _signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@lru_cache(maxsize=64)
def _read_comments(
    path: Path, expected_hash: str, signature: tuple[int, int, int, int],
    identities: tuple[tuple[str, str], ...],
) -> dict[tuple[str, str], set[tuple[str, str]]]:
    wanted = set(identities)
    comments: dict[tuple[str, str], set[tuple[str, str]]] = {}
    with path.open("rb") as raw:
        if "sha256:" + hashlib.file_digest(raw, "sha256").hexdigest() != expected_hash:
            raise ValueError("Review Fact hash mismatch")
        try:
            comments = indexed_comments(raw, expected_hash, path.parents[2] / "evidence-review-cache", identities)
        except (OSError, ValueError, sqlite3.Error, KeyError, TypeError):
            # A missing/unwritable disposable cache does not hide valid source
            # text. Keep the previous full-scan behavior as the fallback.
            comments = None
        if comments is not None:
            if _signature(path) != signature:
                raise ValueError("Review Fact changed while reading")
            return comments
        comments = {}
        raw.seek(0)
        with gzip.open(raw, "rt", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                fact = json.loads(line)
                for field in ("review_occurrence_id", "review_entity_id"):
                    key = field, str(fact.get(field) or "")
                    if key in wanted:
                        text = str(fact.get("review_text") or "")
                        title = str(fact.get("review_title") or "")
                        if text or title:
                            comments.setdefault(key, set()).add((title, text))
    if _signature(path) != signature:
        raise ValueError("Review Fact changed while reading")
    return comments


def prepare_review_text(var_root: Path, report: dict[str, Any]) -> None:
    """Prepare the derived index before making a newly generated report ready."""
    snapshot_id = str(report.get("data_snapshot_id") or "")
    if not re.fullmatch(r"snap_[0-9a-f]{24}", snapshot_id):
        return
    root = (var_root / "snapshots").resolve()
    directory = (root / snapshot_id).resolve()
    path = (directory / "review_fact.preview.jsonl.gz").resolve()
    manifest_path = (directory / "manifest.json").resolve()
    if directory.parent != root or path.parent != directory or manifest_path.parent != directory:
        raise ValueError("invalid Review Fact path")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = str((manifest.get("hashes") or {}).get("preview_fact") or "")
    if manifest.get("snapshot_id") != snapshot_id or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise ValueError("invalid Review Fact manifest")
    signature = _signature(path)
    with path.open("rb") as raw:
        if "sha256:" + hashlib.file_digest(raw, "sha256").hexdigest() != expected:
            raise ValueError("Review Fact hash mismatch")
        indexed_comments(raw, expected, var_root / "evidence-review-cache", ())
    if _signature(path) != signature:
        raise ValueError("Review Fact changed while preparing text index")


def enrich_review_text(
    var_root: Path, report: dict[str, Any], rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add full title/body only when a stable identity resolves unambiguously."""
    items = [dict(row) for row in rows]
    reviews = [row for row in items if row.get("evidence_type") != "source_field"]
    for row in reviews:
        row["review_text_status"] = "unavailable"
    identities = tuple(sorted({_identity(row) for row in reviews} - {("", "")}))
    snapshot_id = str(report.get("data_snapshot_id") or "")
    if not identities or not re.fullmatch(r"snap_[0-9a-f]{24}", snapshot_id):
        return items
    try:
        root = (var_root / "snapshots").resolve()
        directory = (root / snapshot_id).resolve()
        fact_path = (directory / "review_fact.preview.jsonl.gz").resolve()
        manifest_path = (directory / "manifest.json").resolve()
        if directory.parent != root or any(p.parent != directory for p in (fact_path, manifest_path)):
            return items
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_hash = str((manifest.get("hashes") or {}).get("preview_fact") or "")
        if manifest.get("snapshot_id") != snapshot_id or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_hash):
            return items
        comments = _read_comments(fact_path, expected_hash, _signature(fact_path), identities)
    except (OSError, EOFError, ValueError, TypeError, AttributeError, zlib.error):
        return items
    for row in reviews:
        matches = comments.get(_identity(row), set())
        if len(matches) > 1:
            quote = " ".join(str(row.get("quote") or row.get("snippet") or "").split())
            matches = {
                (title, text) for title, text in matches
                if quote and quote in " ".join(f"{title}. {text}".split())
            }
        if len(matches) != 1:
            continue
        title, text = next(iter(matches))
        row.update(
            review_title=title, review_text=text, review_text_status="available",
            review_text_snapshot_id=snapshot_id,
        )
    return items
