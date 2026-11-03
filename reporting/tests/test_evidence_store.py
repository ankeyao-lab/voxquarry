from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import tracemalloc
import unittest
from pathlib import Path

from reporting.evidence_store import (
    EvidenceRepository,
    csv_lines,
    evidence_manifest,
    ndjson_lines,
)
from reporting.source import canonical_json, sha256_text


def evidence_rows() -> list[dict]:
    return [
        {
            "evidence_id": "ev_1",
            "evidence_type": "review_quote",
            "review_entity_id": "re_1",
            "review_id": "1",
            "channel": "ego",
            "brand": "EGO",
            "model": "LB7654",
            "market_region": "Europe",
            "market_region_evidence": "source_market",
            "display_site": "egopowerplus.co.uk",
            "source_site": "egopowerplus.co.uk",
            "rating": 5,
            "submission_date": "2026-01-01",
            "kind": "aspect:power",
            "label": "power",
            "polarity": "positive",
            "quote": "Moves wet leaves without slowing down.",
            "matched_text": "wet leaves",
            "source_url": "https://example.test/1",
        },
        {
            "evidence_id": "ev_2",
            "evidence_type": "review_quote",
            "review_entity_id": "re_2",
            "review_id": "2",
            "channel": "homedepot",
            "brand": "EGO",
            "model": "LB7654",
            "market_region": "North America",
            "market_region_evidence": "source_market",
            "display_site": "homedepot.com",
            "source_site": "homedepot.com",
            "rating": 2,
            "submission_date": "2026-01-02",
            "kind": "aspect:runtime",
            "label": "runtime",
            "polarity": "negative",
            "quote": "The battery dies before I finish the driveway.",
            "matched_text": "battery dies",
            "source_url": "https://example.test/2",
        },
        {
            "evidence_id": "ev_3",
            "evidence_type": "source_field",
            "review_entity_id": "re_3",
            "review_id": "3",
            "channel": "ego",
            "brand": "EGO",
            "model": "LB5300",
            "market_region": "North America",
            "market_region_evidence": "source_market",
            "display_site": "egopowerplus.com",
            "source_site": "egopowerplus.com",
            "rating": 4,
            "submission_date": "2026-01-03",
            "kind": "dimension:explicit_age",
            "label": "65+",
            "polarity": None,
            "quote": "age=65orOver",
            "matched_text": "65orOver",
            "source_field": "age",
            "source_value": "65orOver",
        },
    ]


class EvidenceStoreTests(unittest.TestCase):
    def test_facets_preserve_sql_counts_without_rescanning_group_for_each_field(self) -> None:
        from unittest.mock import patch
        rows = [{**evidence_rows()[0], "evidence_id": f"ev_{i}",
                 "model": f"model-{i:03d}", "brand": "EGO" if i % 2 else None,
                 "rating": i % 5 + 1} for i in range(120)]
        with tempfile.TemporaryDirectory() as directory:
            repository = EvidenceRepository(Path(directory))
            repository.store("rpt_facets", rows, {"evg_facets": {
                "evidence_ids": [row["evidence_id"] for row in rows]}})
            fields = ("channel", "brand", "model", "market_region", "rating", "polarity", "evidence_type")
            with repository._connect("rpt_facets") as connection:
                expected = {field: [dict(row) for row in connection.execute(
                    f"SELECT COALESCE(CAST(e.{field} AS TEXT), 'unknown') AS value, COUNT(*) AS count "
                    "FROM evidence_group_member gm JOIN evidence e ON e.evidence_id=gm.evidence_id "
                    "WHERE gm.evidence_group_id=? GROUP BY value ORDER BY count DESC,value LIMIT 100",
                    ("evg_facets",))] for field in fields}
            queries = []
            connect = repository._connect
            def traced(report_id):
                connection = connect(report_id)
                connection.set_trace_callback(queries.append)
                return connection
            with patch.object(repository, "_connect", side_effect=traced):
                result = repository.query("rpt_facets", "evg_facets", brand="EGO", limit=2)
            self.assertEqual(result["facets"], expected)  # Full group, independent of current filter/page.
            self.assertEqual(len(result["facets"]["model"]), 100)
            self.assertLessEqual(sum("JOIN evidence e" in sql for sql in queries), 4)

    def test_large_manifest_hash_avoids_full_json_allocation(self) -> None:
        rows = [
            {
                "evidence_id": f"ev_{i:06d}",
                "review_entity_id": f"re_{i}",
                "quote": "synthetic evidence " * 230,
            }
            for i in range(8000)
        ]
        expected = sha256_text(canonical_json({
            "store_version": "report-evidence-sqlite/1.0.0",
            "evidence": rows,
            "groups": [],
        }))
        tracemalloc.start()
        try:
            result = evidence_manifest(rows, {})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result["logical_content_sha256"], f"sha256:{expected}")
        self.assertEqual(result["evidence_rows"], 8000)
        self.assertLess(peak, 8 * 1024**2)

    def test_manifest_and_queries_are_stable_and_paginated(self) -> None:
        rows = evidence_rows()
        groups = {
            "evg_runtime": {
                "path": "aspect.runtime.negative",
                "label": "续航 · 负向",
                "evidence_ids": ["ev_2", "ev_1"],
                "representative_evidence_ids": ["ev_2"],
            },
            "evg_age": {
                "path": "dimension.explicit_age.65+",
                "label": "明确年龄 · 65+",
                "evidence_ids": ["ev_3"],
                "representative_evidence_ids": ["ev_3"],
            },
        }
        first = evidence_manifest(rows, groups)
        second = evidence_manifest(reversed(rows), groups)
        self.assertEqual(first, second)

        with tempfile.TemporaryDirectory() as directory:
            repository = EvidenceRepository(Path(directory))
            stored = repository.store(
                "rpt_fixture",
                rows,
                groups,
                expected_manifest=first,
            )
            self.assertEqual(stored["evidence_rows"], 3)
            page = repository.query(
                "rpt_fixture", "evg_runtime", limit=1
            )
            self.assertEqual(page["page"]["total_evidence_rows"], 2)
            self.assertEqual(page["page"]["returned"], 1)
            self.assertEqual(page["page"]["next_offset"], 1)
            self.assertFalse(page["access"]["representative_only"])
            self.assertTrue(page["access"]["facets_included"])
            next_page = repository.query(
                "rpt_fixture",
                "evg_runtime",
                offset=1,
                limit=1,
                include_facets=False,
            )
            self.assertEqual(next_page["page"]["total_evidence_rows"], 2)
            self.assertEqual(next_page["items"][0]["evidence_id"], "ev_1")
            self.assertEqual(next_page["facets"], {})
            self.assertFalse(next_page["access"]["facets_included"])
            filtered = repository.query(
                "rpt_fixture",
                "evg_runtime",
                channel="ego",
            )
            self.assertEqual(filtered["page"]["total_evidence_rows"], 1)
            self.assertEqual(filtered["items"][0]["evidence_id"], "ev_1")
            searched = repository.query(
                "rpt_fixture",
                "evg_runtime",
                q="driveway",
            )
            self.assertEqual(
                [item["evidence_id"] for item in searched["items"]],
                ["ev_2"],
            )
            regional = repository.query(
                "rpt_fixture",
                "evg_runtime",
                market_region="Europe",
            )
            self.assertEqual(
                [item["evidence_id"] for item in regional["items"]],
                ["ev_1"],
            )
            self.assertEqual(
                page["facets"]["market_region"],
                [
                    {"value": "Europe", "count": 1},
                    {"value": "North America", "count": 1},
                ],
            )
            self.assertEqual(
                repository.get("rpt_fixture", "ev_3")["source_field"],
                "age",
            )

    def test_stream_exports_preserve_utf8_and_rows(self) -> None:
        rows = evidence_rows()[:2]
        ndjson = b"".join(ndjson_lines(rows)).decode("utf-8")
        ndjson_rows = [json.loads(line) for line in ndjson.splitlines()]
        self.assertEqual(
            [row["evidence_id"] for row in ndjson_rows], ["ev_1", "ev_2"]
        )
        for row in ndjson_rows:
            for field in (
                "market_region",
                "market_region_evidence",
                "channel",
                "display_site",
                "source_site",
            ):
                self.assertIn(field, row)
        csv = b"".join(csv_lines(rows)).decode("utf-8-sig")
        self.assertIn("evidence_id,evidence_type", csv)
        self.assertIn(
            "market_region,market_region_evidence,channel,display_site,source_site",
            csv,
        )
        self.assertIn("Moves wet leaves", csv)

    def test_legacy_sidecar_remains_readable_without_fabricated_region(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = EvidenceRepository(Path(directory))
            path = repository.path_for("rpt_legacy")
            payload = {
                "evidence_id": "ev_legacy",
                "evidence_type": "review_quote",
                "review_entity_id": "legacy-entity",
                "review_id": "legacy-review",
                "channel": "ego",
                "brand": "EGO",
                "model": "LB1",
                "rating": 5,
                "submission_date": "2023-01-01",
                "kind": "aspect:power",
                "label": "power",
                "polarity": "positive",
                "quote": "Legacy evidence remains readable.",
                "matched_text": "readable",
            }
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE evidence (
                        evidence_id TEXT PRIMARY KEY,
                        evidence_type TEXT NOT NULL,
                        review_entity_id TEXT,
                        review_id TEXT,
                        channel TEXT,
                        brand TEXT,
                        model TEXT,
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
                        PRIMARY KEY (evidence_group_id, evidence_id)
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "ev_legacy",
                        "review_quote",
                        "legacy-entity",
                        "legacy-review",
                        "ego",
                        "EGO",
                        "LB1",
                        5,
                        "2023-01-01",
                        "aspect:power",
                        "power",
                        "positive",
                        "Legacy evidence remains readable.",
                        "readable",
                        None,
                        json.dumps(payload),
                    ),
                )
                connection.execute(
                    "INSERT INTO evidence_group VALUES (?,?,?,?)",
                    ("evg_legacy", "aspect.power", "Power", 1),
                )
                connection.execute(
                    "INSERT INTO evidence_group_member VALUES (?,?,?)",
                    ("evg_legacy", "ev_legacy", 1),
                )

            page = repository.query("rpt_legacy", "evg_legacy")
            self.assertEqual(
                [row["evidence_id"] for row in page["items"]],
                ["ev_legacy"],
            )
            self.assertNotIn("market_region", page["facets"])
            filtered = repository.query(
                "rpt_legacy",
                "evg_legacy",
                market_region="Europe",
            )
            self.assertEqual(filtered["page"]["total_evidence_rows"], 0)
            self.assertEqual(filtered["items"], [])

    def test_stream_iterator_can_move_between_asgi_worker_threads(
        self,
    ) -> None:
        rows = evidence_rows()[:2]
        groups = {
            "evg_runtime": {
                "path": "aspect.runtime",
                "label": "续航",
                "evidence_ids": ["ev_1", "ev_2"],
                "representative_evidence_ids": ["ev_1"],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            repository = EvidenceRepository(Path(directory))
            repository.store("rpt_fixture", rows, groups)
            iterator = repository.iter_filtered(
                "rpt_fixture", "evg_runtime"
            )
            ready = threading.Event()
            finished = threading.Event()
            results: list[str] = []
            errors: list[BaseException] = []

            def first_worker() -> None:
                try:
                    results.append(next(iterator)["evidence_id"])
                    ready.set()
                    finished.wait(timeout=5)
                except BaseException as exc:
                    errors.append(exc)
                    ready.set()

            def second_worker() -> None:
                ready.wait(timeout=5)
                try:
                    results.append(next(iterator)["evidence_id"])
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            first = threading.Thread(target=first_worker)
            second = threading.Thread(target=second_worker)
            first.start()
            second.start()
            first.join(timeout=5)
            second.join(timeout=5)
            iterator.close()

        self.assertEqual(errors, [])
        self.assertEqual(results, ["ev_1", "ev_2"])


if __name__ == "__main__":
    unittest.main()
