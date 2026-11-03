from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator

from reporting.source import canonical_json
from reporting.json_stream import canonical_json_sha256


EVIDENCE_STORE_VERSION = "report-evidence-sqlite/1.0.0"
MAX_PAGE_SIZE = 100


class EvidenceStoreError(ValueError):
    """Raised when an immutable evidence artifact is invalid."""


def _normalized_group(
    group_id: str, raw: dict[str, Any]
) -> dict[str, Any]:
    evidence_ids = list(
        dict.fromkeys(str(value) for value in raw.get("evidence_ids") or [])
    )
    representative_ids = list(
        dict.fromkeys(
            str(value) for value in raw.get("representative_evidence_ids") or []
        )
    )
    return {
        "evidence_group_id": group_id,
        "path": str(raw.get("path") or group_id),
        "label": str(raw.get("label") or raw.get("path") or group_id),
        "evidence_ids": evidence_ids,
        "representative_evidence_ids": representative_ids,
    }


def evidence_manifest(
    evidence_rows: Iterable[dict[str, Any]],
    evidence_groups: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows = sorted(
        (dict(row) for row in evidence_rows),
        key=lambda row: str(row["evidence_id"]),
    )
    groups = [
        _normalized_group(group_id, raw)
        for group_id, raw in sorted(evidence_groups.items())
    ]
    evidence_ids = {str(row["evidence_id"]) for row in rows}
    missing = sorted(
        {
            evidence_id
            for group in groups
            for evidence_id in group["evidence_ids"]
            if evidence_id not in evidence_ids
        }
    )
    if missing:
        raise EvidenceStoreError(
            f"证据组引用不存在的 evidence_id：{', '.join(missing[:5])}"
        )
    group_rows = sum(len(group["evidence_ids"]) for group in groups)
    unique_entities = {
        str(row.get("review_entity_id") or row.get("review_id") or "")
        for row in rows
        if row.get("review_entity_id") or row.get("review_id")
    }
    logical = {
        "store_version": EVIDENCE_STORE_VERSION,
        "evidence": rows,
        "groups": groups,
    }
    return {
        "store_version": EVIDENCE_STORE_VERSION,
        "evidence_rows": len(rows),
        "unique_review_entities": len(unique_entities),
        "evidence_groups": len(groups),
        "group_memberships": group_rows,
        "logical_content_sha256": (
            f"sha256:{canonical_json_sha256(logical)}"
        ),
    }


class EvidenceRepository:
    """Immutable, report-scoped evidence sidecars.

    The repository never reads or writes the scraper database. Each report is
    backed by one SQLite file under the reporting runtime directory.
    """

    def __init__(self, root: Path):
        self.root = root
        self.evidence_dir = root / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, report_id: str) -> Path:
        return self.evidence_dir / f"{report_id}.sqlite3"

    def _connect(self, report_id: str) -> sqlite3.Connection:
        path = self.path_for(report_id)
        if not path.exists():
            raise FileNotFoundError(report_id)
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.execute("PRAGMA query_only = ON")
        # Keep the B-tree working set in a bounded cache when a large group
        # joins many rows in an immutable, payload-heavy evidence database.
        connection.execute("PRAGMA cache_size = -65536")
        connection.row_factory = sqlite3.Row
        return connection

    def store(
        self,
        report_id: str,
        evidence_rows: Iterable[dict[str, Any]],
        evidence_groups: dict[str, dict[str, Any]],
        *,
        expected_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rows = sorted(
            (dict(row) for row in evidence_rows),
            key=lambda row: str(row["evidence_id"]),
        )
        groups = {
            group_id: _normalized_group(group_id, raw)
            for group_id, raw in sorted(evidence_groups.items())
        }
        manifest = evidence_manifest(rows, groups)
        if (
            expected_manifest
            and expected_manifest.get("logical_content_sha256")
            != manifest["logical_content_sha256"]
        ):
            raise EvidenceStoreError("证据边车逻辑哈希与报告清单不一致")

        target = self.path_for(report_id)
        if target.exists():
            with self._connect(report_id) as existing:
                row = existing.execute(
                    "SELECT value FROM metadata WHERE key = ?",
                    ("logical_content_sha256",),
                ).fetchone()
            if row and row["value"] == manifest["logical_content_sha256"]:
                return {
                    **manifest,
                    "physical_file_sha256": _file_sha256(target),
                    "bytes": target.stat().st_size,
                }
            raise EvidenceStoreError(
                f"不可变证据边车已存在且内容不同：{report_id}"
            )

        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{report_id}-",
            suffix=".sqlite3",
            dir=self.evidence_dir,
            delete=False,
        )
        temporary = Path(handle.name)
        handle.close()
        try:
            connection = sqlite3.connect(temporary)
            try:
                connection.executescript(
                    """
                    PRAGMA journal_mode = OFF;
                    PRAGMA synchronous = OFF;
                    PRAGMA temp_store = MEMORY;
                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE evidence (
                        evidence_id TEXT PRIMARY KEY,
                        evidence_type TEXT NOT NULL,
                        review_entity_id TEXT,
                        review_id TEXT,
                        channel TEXT,
                        brand TEXT,
                        model TEXT,
                        market_region TEXT,
                        market_region_evidence TEXT,
                        display_site TEXT,
                        source_site TEXT,
                        rating INTEGER,
                        submission_date TEXT,
                        kind TEXT,
                        label TEXT,
                        polarity TEXT,
                        quote TEXT NOT NULL,
                        matched_text TEXT,
                        source_url TEXT,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE evidence_group (
                        evidence_group_id TEXT PRIMARY KEY,
                        path TEXT NOT NULL,
                        label TEXT NOT NULL,
                        representative_count INTEGER NOT NULL
                    );
                    CREATE TABLE evidence_group_member (
                        evidence_group_id TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        stable_rank INTEGER NOT NULL,
                        PRIMARY KEY (evidence_group_id, evidence_id),
                        FOREIGN KEY (evidence_group_id)
                            REFERENCES evidence_group(evidence_group_id),
                        FOREIGN KEY (evidence_id)
                            REFERENCES evidence(evidence_id)
                    );
                    CREATE INDEX idx_evidence_channel
                        ON evidence(channel);
                    CREATE INDEX idx_evidence_model
                        ON evidence(model);
                    CREATE INDEX idx_evidence_market_region
                        ON evidence(market_region);
                    CREATE INDEX idx_evidence_rating
                        ON evidence(rating);
                    CREATE INDEX idx_evidence_kind_label
                        ON evidence(kind, label);
                    CREATE INDEX idx_group_member_rank
                        ON evidence_group_member(
                            evidence_group_id, stable_rank, evidence_id
                        );
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO evidence (
                        evidence_id, evidence_type, review_entity_id,
                        review_id, channel, brand, model, rating,
                        market_region, market_region_evidence,
                        display_site, source_site,
                        submission_date, kind, label, polarity, quote,
                        matched_text, source_url, payload_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        (
                            str(row["evidence_id"]),
                            str(row.get("evidence_type") or "review_quote"),
                            row.get("review_entity_id"),
                            row.get("review_id"),
                            row.get("channel"),
                            row.get("brand"),
                            row.get("model"),
                            row.get("rating"),
                            row.get("market_region"),
                            row.get("market_region_evidence"),
                            row.get("display_site"),
                            row.get("source_site"),
                            row.get("submission_date"),
                            row.get("kind"),
                            row.get("label"),
                            row.get("polarity"),
                            str(row.get("quote") or row.get("snippet") or ""),
                            row.get("matched_text"),
                            row.get("source_url"),
                            canonical_json(row),
                        )
                        for row in rows
                    ),
                )
                for group_id, group in groups.items():
                    connection.execute(
                        """
                        INSERT INTO evidence_group (
                            evidence_group_id, path, label,
                            representative_count
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            group_id,
                            group["path"],
                            group["label"],
                            len(group["representative_evidence_ids"]),
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO evidence_group_member (
                            evidence_group_id, evidence_id, stable_rank
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            (group_id, evidence_id, rank)
                            for rank, evidence_id in enumerate(
                                group["evidence_ids"], start=1
                            )
                        ),
                    )
                connection.executemany(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    (
                        (key, str(value))
                        for key, value in {
                            **manifest,
                            "report_id": report_id,
                        }.items()
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

        return {
            **manifest,
            "physical_file_sha256": _file_sha256(target),
            "bytes": target.stat().st_size,
        }

    def get(self, report_id: str, evidence_id: str) -> dict[str, Any] | None:
        with self._connect(report_id) as connection:
            row = connection.execute(
                "SELECT payload_json FROM evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def group(self, report_id: str, group_id: str) -> dict[str, Any] | None:
        with self._connect(report_id) as connection:
            row = connection.execute(
                """
                SELECT evidence_group_id, path, label, representative_count
                FROM evidence_group
                WHERE evidence_group_id = ?
                """,
                (group_id,),
            ).fetchone()
            if not row:
                return None
            totals = connection.execute(
                """
                SELECT COUNT(*) AS evidence_rows,
                    COUNT(DISTINCT COALESCE(
                        NULLIF(e.review_entity_id, ''),
                        NULLIF(e.review_id, ''),
                        e.evidence_id
                    )) AS unique_review_entities
                FROM evidence_group_member gm
                JOIN evidence e ON e.evidence_id = gm.evidence_id
                WHERE gm.evidence_group_id = ?
                """,
                (group_id,),
            ).fetchone()
        return {
            **dict(row),
            "evidence_rows": totals["evidence_rows"],
            "unique_review_entities": totals["unique_review_entities"],
        }

    def list_groups(
        self,
        report_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        bounded_offset = max(0, int(offset))
        bounded_limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        with self._connect(report_id) as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM evidence_group"
                ).fetchone()["total"]
            )
            rows = connection.execute(
                """
                SELECT g.evidence_group_id, g.path, g.label,
                    g.representative_count,
                    COUNT(gm.evidence_id) AS evidence_rows,
                    COUNT(DISTINCT COALESCE(
                        NULLIF(e.review_entity_id, ''),
                        NULLIF(e.review_id, ''),
                        e.evidence_id
                    )) AS unique_review_entities
                FROM evidence_group g
                LEFT JOIN evidence_group_member gm
                    ON gm.evidence_group_id = g.evidence_group_id
                LEFT JOIN evidence e ON e.evidence_id = gm.evidence_id
                GROUP BY g.evidence_group_id, g.path, g.label,
                    g.representative_count
                ORDER BY g.evidence_group_id
                LIMIT ? OFFSET ?
                """,
                (bounded_limit, bounded_offset),
            ).fetchall()
        next_offset = (
            bounded_offset + bounded_limit
            if bounded_offset + bounded_limit < total
            else None
        )
        return {
            "report_id": report_id,
            "items": [dict(row) for row in rows],
            "page": {
                "offset": bounded_offset,
                "limit": bounded_limit,
                "returned": len(rows),
                "total": total,
                "next_offset": next_offset,
                "has_more": next_offset is not None,
            },
        }

    def is_group_member(
        self, report_id: str, group_id: str, evidence_id: str
    ) -> bool:
        with self._connect(report_id) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM evidence_group_member
                WHERE evidence_group_id = ? AND evidence_id = ?
                """,
                (group_id, evidence_id),
            ).fetchone()
        return row is not None

    def query(
        self,
        report_id: str,
        group_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        channel: str | None = None,
        brand: str | None = None,
        model: str | None = None,
        market_region: str | None = None,
        rating: int | None = None,
        polarity: str | None = None,
        evidence_type: str | None = None,
        q: str | None = None,
        include_facets: bool = True,
    ) -> dict[str, Any]:
        bounded_offset = max(0, int(offset))
        bounded_limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        with self._connect(report_id) as connection:
            available_fields = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(evidence)"
                ).fetchall()
            }
            where, values = _query_filters(
                group_id,
                channel=channel,
                brand=brand,
                model=model,
                market_region=market_region,
                rating=rating,
                polarity=polarity,
                evidence_type=evidence_type,
                q=q,
                available_fields=available_fields,
            )
            metadata = connection.execute(
                "SELECT evidence_group_id, path, label, representative_count "
                "FROM evidence_group WHERE evidence_group_id = ?", (group_id,)
            ).fetchone()
            if metadata is None:
                raise KeyError(group_id)
            fields = [field for field in ("channel", "brand", "model", "market_region", "rating", "polarity", "evidence_type")
                      if include_facets and field in available_fields]
            counts = {field: Counter() for field in fields}
            columns = "".join(f", COALESCE(CAST(e.{field} AS TEXT), 'unknown') AS {field}" for field in fields)
            group_entities, selected_entities = set(), set()
            group_count = selected_count = 0
            # One streaming join supplies whole-group counts, filtered counts
            # and all requested facets. Preserve SQL filter/CAST semantics and
            # avoid loading large payload_json values except for the page.
            for row in connection.execute(
                "SELECT COALESCE(NULLIF(e.review_entity_id, ''), NULLIF(e.review_id, ''), e.evidence_id) AS entity, "
                f"CASE WHEN {where} THEN 1 ELSE 0 END AS selected{columns} "
                "FROM evidence_group_member gm JOIN evidence e ON e.evidence_id = gm.evidence_id "
                "WHERE gm.evidence_group_id = ?", (*values, group_id)
            ):
                group_count += 1
                group_entities.add(row["entity"])
                if row["selected"]:
                    selected_count += 1
                    selected_entities.add(row["entity"])
                for field in fields:
                    counts[field][row[field]] += 1
            group = {**dict(metadata), "evidence_rows": group_count, "unique_review_entities": len(group_entities)}
            totals = {"evidence_rows": selected_count, "unique_review_entities": len(selected_entities)}
            facets = {field: [{"value": value, "count": count}
                              for value, count in sorted(counts[field].items(), key=lambda item: (-item[1], item[0]))[:100]]
                      for field in fields}
            rows = connection.execute(
                f"""
                SELECT e.payload_json
                FROM evidence_group_member gm
                JOIN evidence e ON e.evidence_id = gm.evidence_id
                WHERE {where}
                ORDER BY gm.stable_rank, e.evidence_id
                LIMIT ? OFFSET ?
                """,
                (*values, bounded_limit, bounded_offset),
            ).fetchall()

        total = int(totals["evidence_rows"])
        next_offset = (
            bounded_offset + bounded_limit
            if bounded_offset + bounded_limit < total
            else None
        )
        return {
            "report_id": report_id,
            "group": group,
            "items": [json.loads(row["payload_json"]) for row in rows],
            "page": {
                "offset": bounded_offset,
                "limit": bounded_limit,
                "returned": len(rows),
                "total_evidence_rows": total,
                "total_unique_review_entities": int(
                    totals["unique_review_entities"]
                ),
                "next_offset": next_offset,
                "has_more": next_offset is not None,
            },
            "access": {
                "mode": "full_evidence_sidecar",
                "representative_only": False,
                "facets_included": bool(include_facets),
            },
            "facets": facets,
        }

    def iter_filtered(
        self,
        report_id: str,
        group_id: str,
        **filters: Any,
    ) -> Iterator[dict[str, Any]]:
        connection = self._connect(report_id)
        try:
            available_fields = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(evidence)"
                ).fetchall()
            }
            where, values = _query_filters(
                group_id,
                available_fields=available_fields,
                **filters,
            )
            cursor = connection.execute(
                f"""
                SELECT e.payload_json
                FROM evidence_group_member gm
                JOIN evidence e ON e.evidence_id = gm.evidence_id
                WHERE {where}
                ORDER BY gm.stable_rank, e.evidence_id
                """,
                values,
            )
            for row in cursor:
                yield json.loads(row["payload_json"])
        finally:
            connection.close()


def _query_filters(
    group_id: str,
    *,
    channel: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    market_region: str | None = None,
    rating: int | None = None,
    polarity: str | None = None,
    evidence_type: str | None = None,
    q: str | None = None,
    available_fields: set[str] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    clauses = ["gm.evidence_group_id = ?"]
    values: list[Any] = [group_id]
    for field, value in (
        ("channel", channel),
        ("brand", brand),
        ("model", model),
        ("market_region", market_region),
        ("rating", rating),
        ("polarity", polarity),
        ("evidence_type", evidence_type),
    ):
        if value is None or value == "":
            continue
        if available_fields is not None and field not in available_fields:
            clauses.append("1 = 0")
            continue
        clauses.append(f"e.{field} = ?")
        values.append(value)
    normalized_query = " ".join(str(q or "").split()).strip()
    if normalized_query:
        clauses.append(
            "(e.quote LIKE ? ESCAPE '\\' OR e.matched_text LIKE ? ESCAPE '\\')"
        )
        escaped = (
            normalized_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        values.extend((f"%{escaped}%", f"%{escaped}%"))
    return " AND ".join(clauses), tuple(values)


def ndjson_lines(rows: Iterable[dict[str, Any]]) -> Iterator[bytes]:
    for row in rows:
        yield (
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")


def csv_lines(rows: Iterable[dict[str, Any]]) -> Iterator[bytes]:
    fields = (
        "evidence_id",
        "evidence_type",
        "job_id",
        "review_entity_id",
        "review_id",
        "market_region",
        "market_region_evidence",
        "channel",
        "display_site",
        "source_site",
        "brand",
        "model",
        "rating",
        "submission_date",
        "kind",
        "label",
        "polarity",
        "quote",
        "matched_text",
        "source_url",
        "source_completeness_status",
        "source_completeness_collection_status",
        "source_completeness_validation_status",
        "source_completeness_validation_schema_version",
        "source_completeness_import_schema_version",
        "declared_source_total",
        "collected_source_unique_reviews",
        "missing_review_count",
        "source_reconciled",
        "source_completeness_stop_reason",
        "non_reportable_exclusion_policy",
        "non_reportable_excluded_records",
        "non_reportable_exclusion_message",
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    yield buffer.getvalue().encode("utf-8-sig")
    buffer.seek(0)
    buffer.truncate(0)
    for row in rows:
        writer.writerow(row)
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
