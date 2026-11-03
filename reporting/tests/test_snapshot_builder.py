from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from reporting.source import MarketContractError, build_review_fact, catalog
from scripts.build_reporting_snapshot_v2 import (
    backup_database,
    build_snapshot,
    raw_origin_index,
    raw_review_ids,
    sha256_file,
)


LEGACY_FIELDS = [
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
]
CURRENT_FIELDS = LEGACY_FIELDS + [
    "display_site",
    "origin_source_name",
    "origin_source_url",
    "origin_source_site",
    "market_region",
    "market_region_evidence",
    "review_word_count",
    "is_valid_text_review",
    "is_eligible",
    "exclusion_reason",
]
TRUE_EMPTY_SOURCE_FIELDS = [
    "channel",
    "product_id",
    "review_id",
    "review_text",
    "rating",
    "market_region",
    "review_word_count",
    "is_valid_text_review",
    "is_eligible",
    "exclusion_reason",
]
LEGACY_TRUE_EMPTY_SOURCE_FIELDS = [
    "channel",
    "product_id",
    "review_id",
    "review_text",
    "rating",
]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def row(review_id: str, text: str, *, syndicated: str = "False") -> dict[str, str]:
    return {
        "channel": "ace",
        "product_id": "fan-product",
        "product_title": "Fan",
        "product_url": "https://www.acehardware.com/fan",
        "review_id": review_id,
        "source_site": "acehardware.com",
        "source_product_id": "fan-product",
        "is_syndicated": syndicated,
        "submission_time": "2026-07-01",
        "title": "",
        "is_recommended": "",
        "user_nickname": "",
        "review_text": text,
        "rating": "5",
        "verified_purchaser": "",
        "where_purchase": "",
        "age": "",
    }


class ReportingSnapshotBuilderTests(unittest.TestCase):
    def test_backup_database_accepts_an_immutable_read_only_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "handoff"
            source_dir.mkdir()
            source = source_dir / "jobs.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO jobs VALUES ('job-1')")
            source.chmod(0o444)
            source_dir.chmod(0o555)
            try:
                real_connect = sqlite3.connect
                with mock.patch(
                    "scripts.build_reporting_snapshot_v2.sqlite3.connect",
                    side_effect=real_connect,
                ) as connect:
                    self.assertEqual(
                        backup_database(source, root / "copy.sqlite3"), 1
                    )
                self.assertIn("immutable=1", str(connect.call_args_list[0].args[0]))
            finally:
                source_dir.chmod(0o755)

    def test_backup_database_includes_uncheckpointed_wal_without_source_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "jobs.sqlite3"
            target = root / "jobs-consistent.sqlite3"
            wal = Path(f"{source}-wal")
            writer = sqlite3.connect(source)
            try:
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal",
                )
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
                writer.executemany(
                    "INSERT INTO jobs VALUES (?)",
                    ((f"job-{index:03d}",) for index in range(888)),
                )
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute("INSERT INTO jobs VALUES ('272e555c87a9')")
                writer.commit()
                self.assertGreater(wal.stat().st_size, 0)
                self.assertEqual(
                    writer.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                    889,
                )

                before = {
                    path: (sha256_file(path), path.stat().st_mtime_ns)
                    for path in (source, wal)
                }
                real_connect = sqlite3.connect
                with mock.patch(
                    "scripts.build_reporting_snapshot_v2.sqlite3.connect",
                    side_effect=real_connect,
                ) as connect:
                    self.assertEqual(backup_database(source, target), 889)

                source_uri = str(connect.call_args_list[0].args[0])
                self.assertIn("mode=ro", source_uri)
                self.assertNotIn("immutable=1", source_uri)
                with sqlite3.connect(target) as copied:
                    self.assertEqual(
                        copied.execute("PRAGMA integrity_check").fetchone()[0],
                        "ok",
                    )
                    self.assertEqual(
                        copied.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                        889,
                    )
                    self.assertEqual(
                        copied.execute(
                            "SELECT COUNT(*) FROM jobs WHERE id = ?",
                            ("272e555c87a9",),
                        ).fetchone()[0],
                        1,
                    )
                self.assertEqual(
                    {
                        path: (sha256_file(path), path.stat().st_mtime_ns)
                        for path in (source, wal)
                    },
                    before,
                )
            finally:
                writer.close()

    def test_raw_origin_index_supports_ryobi_guid_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "reviews.raw.jsonl"
            raw_path.write_text(
                json.dumps(
                    {
                        "id": "ryobi-internal-id",
                        "guid": "csv-review-guid",
                        "SourceClient": "ryobi",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            index = raw_origin_index(raw_path.with_name("reviews.csv"))
            self.assertEqual(
                set(index),
                {"ryobi-internal-id", "csv-review-guid"},
            )
            self.assertEqual(index["csv-review-guid"]["origin_source_name"], "ryobi")
            self.assertEqual(raw_review_ids({"uuid": "not-governed"}), ())

    def test_migrates_legacy_and_includes_short_and_rating_only_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            database = source / "data" / "jobs.sqlite3"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE jobs (
                        id TEXT,
                        status TEXT,
                        channel TEXT,
                        brand TEXT,
                        category TEXT,
                        model TEXT,
                        market_region TEXT,
                        target_url TEXT,
                        created_at TEXT,
                        finished_at TEXT,
                        superseded_by TEXT,
                        active_run_path TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            "legacy-fan",
                            "completed",
                            "ace",
                            "Brand A",
                            "Jobsite Fan",
                            "LEGACY",
                            "North America",
                            "https://www.acehardware.com/fan",
                            "2026-07-01",
                            "2026-07-02",
                            None,
                            "",
                        ),
                        (
                            "current-fan",
                            "completed",
                            "amazon_reviews_2023",
                            "Brand B",
                            "Fan",
                            "CURRENT",
                            "North America",
                            "https://www.amazon.com/dp/B000FAN",
                            "2026-07-01",
                            "2026-07-03",
                            None,
                            "",
                        ),
                        (
                            "older-blower",
                            "completed",
                            "lowes",
                            "SKIL",
                            "Leaf Blower/Vacuum",
                            "BL0650C-13",
                            "North America",
                            "https://www.lowes.com/pd/SKIL-BL0650C-13/empty",
                            "2026-07-01",
                            "2026-07-02",
                            None,
                            "",
                        ),
                        (
                            "04efd58f6de8",
                            "completed",
                            "lowes",
                            "SKIL",
                            "Leaf Blower/Vacuum",
                            "BL0650C-13",
                            "North America",
                            "https://www.lowes.com/pd/SKIL-BL0650C-13/empty",
                            "2026-08-01",
                            "2026-08-02",
                            None,
                            "",
                        ),
                        (
                            "5fe461903438",
                            "completed",
                            "lowes",
                            "Sun Joe",
                            "Leaf Blower",
                            "SBJ597E",
                            "North America",
                            "https://www.lowes.com/pd/Sun-Joe-SBJ597E/empty",
                            "2026-07-23",
                            "2026-07-24",
                            None,
                            "",
                        ),
                        (
                            "62512c8aae41",
                            "completed",
                            "lowes",
                            "WORX",
                            "Inflator",
                            "",
                            "North America",
                            "https://www.lowes.com/pd/WORX-Inflator/empty",
                            "2026-07-23",
                            "2026-07-24",
                            None,
                            "",
                        ),
                        *[
                            (
                                job_id,
                                "completed",
                                "lowes",
                                "Test Brand",
                                "Leaf Blower",
                                f"INCOMPLETE-{index}",
                                "North America",
                                f"https://www.lowes.com/pd/{job_id}/empty",
                                "2026-07-23",
                                "2026-07-24",
                                None,
                                "",
                            )
                            for index, job_id in enumerate(
                                (
                                    "85e85b359cc0",
                                    "8d9a16902b87",
                                    "0f245c1ef63c",
                                ),
                                start=1,
                            )
                        ],
                    ],
                )

            legacy_dir = source / "data" / "runs" / "legacy-fan" / "ace"
            legacy_rows = [
                row("short", "Works"),
                row("rating-only", ""),
                row("ego-origin", "This official review is syndicated.", syndicated="True"),
            ]
            write_csv(legacy_dir / "reviews.csv", LEGACY_FIELDS, legacy_rows)
            (legacy_dir / "reviews.raw.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"Id": "short"}),
                        json.dumps(
                            {
                                "id": "rating-only-internal",
                                "guid": "rating-only",
                            }
                        ),
                        json.dumps(
                            {
                                "Id": "ego-origin",
                                "SourceClient": "egopowerplus",
                                "SyndicationSource": {
                                    "Name": "egopowerplus.com",
                                    "ContentLink": "https://egopowerplus.com/fan",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (legacy_dir / "validation_report.json").write_text(
                json.dumps(
                    {
                        "status": "COMPLETE",
                        "source_total": 3,
                        "unique_reviews": 3,
                        "product": {"id": "legacy-fan"},
                    }
                ),
                encoding="utf-8",
            )

            current_dir = (
                source
                / "data"
                / "runs"
                / "current-fan"
                / "amazon_reviews_2023"
            )
            current_rows = [
                {
                    **row("current-short", "Good"),
                    "channel": "amazon_reviews_2023",
                    "display_site": "amazon.com",
                    "origin_source_name": "",
                    "origin_source_url": "",
                    "origin_source_site": "",
                    "market_region": "North America",
                    "market_region_evidence": "source_market",
                    "review_word_count": "1",
                    "is_valid_text_review": "False",
                    "is_eligible": "True",
                    "exclusion_reason": "",
                },
                {
                    **row("current-rating", ""),
                    "channel": "amazon_reviews_2023",
                    "display_site": "amazon.com",
                    "origin_source_name": "",
                    "origin_source_url": "",
                    "origin_source_site": "",
                    "market_region": "North America",
                    "market_region_evidence": "source_market",
                    "review_word_count": "0",
                    "is_valid_text_review": "False",
                    "is_eligible": "True",
                    "exclusion_reason": "",
                },
            ]
            write_csv(current_dir / "reviews.csv", CURRENT_FIELDS, current_rows)
            (current_dir / "validation_report.json").write_text(
                json.dumps(
                    {
                        "status": "COMPLETE",
                        "source_total": 2,
                        "source_unique_reviews": 2,
                        "unique_reviews": 2,
                        "eligible_reviews": 2,
                        "excluded_reviews": 0,
                        "valid_text_reviews": 0,
                        "market_region": "North America",
                        "archive_through": "2023-09-30",
                        "product": {
                            "id": "current-fan",
                            "market_region": "North America",
                            "archive_through": "2023-09-30",
                        },
                    }
                ),
                encoding="utf-8",
            )

            older_dir = source / "data" / "runs" / "older-blower" / "lowes"
            write_csv(
                older_dir / "reviews.csv",
                CURRENT_FIELDS,
                [
                    {
                        **row("older-review", "Older review must not return"),
                        "channel": "lowes",
                        "display_site": "lowes.com",
                        "origin_source_name": "",
                        "origin_source_url": "",
                        "origin_source_site": "",
                        "market_region": "North America",
                        "market_region_evidence": "source_market",
                        "review_word_count": "5",
                        "is_valid_text_review": "True",
                        "is_eligible": "True",
                        "exclusion_reason": "",
                    }
                ],
            )
            (older_dir / "validation_report.json").write_text(
                json.dumps(
                    {
                        "status": "COMPLETE",
                        "source_total": 1,
                        "source_unique_reviews": 1,
                        "unique_reviews": 1,
                        "eligible_reviews": 1,
                        "excluded_reviews": 0,
                        "valid_text_reviews": 1,
                        "market_region": "North America",
                        "product": {
                            "id": "skil-bl0650c-13",
                            "market_region": "North America",
                        },
                    }
                ),
                encoding="utf-8",
            )

            empty_dir = source / "data" / "runs" / "04efd58f6de8" / "lowes"
            write_csv(
                empty_dir / "reviews.csv",
                TRUE_EMPTY_SOURCE_FIELDS,
                [],
            )
            (empty_dir / "reviews.raw.jsonl").write_text("", encoding="utf-8")
            (empty_dir / "validation_report.json").write_text(
                json.dumps(
                    {
                        "channel": "lowes",
                        "status": "COMPLETE",
                        "source_total": 0,
                        "source_unique_reviews": 0,
                        "source_reconciled": True,
                        "unique_reviews": 0,
                        "eligible_reviews": 0,
                        "excluded_reviews": 0,
                        "valid_text_reviews": 0,
                        "ratings_only": 0,
                        "market_region": "North America",
                        "notes": {"empty_source_confirmed": True},
                        "product": {
                            "id": "skil-bl0650c-13",
                            "market_region": "North America",
                        },
                    }
                ),
                encoding="utf-8",
            )

            def write_legacy_empty_run(
                job_id: str,
                product_id: str,
                *,
                status: str,
                confirmed: bool,
            ) -> None:
                run_dir = source / "data" / "runs" / job_id / "lowes"
                write_csv(
                    run_dir / "reviews.csv",
                    LEGACY_TRUE_EMPTY_SOURCE_FIELDS,
                    [],
                )
                (run_dir / "reviews.raw.jsonl").write_text("", encoding="utf-8")
                notes = {"official_lowes_rnr_api": True}
                if confirmed:
                    notes["empty_source_confirmed"] = True
                (run_dir / "validation_report.json").write_text(
                    json.dumps(
                        {
                            "channel": "lowes",
                            "status": status,
                            "source_total": 0,
                            "unique_reviews": 0,
                            "ratings_only": 0,
                            "duplicates_removed": 0,
                            "notes": notes,
                            "product": {"id": product_id},
                        }
                    ),
                    encoding="utf-8",
                )

            write_legacy_empty_run(
                "5fe461903438",
                "sun-joe-sbj597e",
                status="COMPLETE",
                confirmed=True,
            )
            write_legacy_empty_run(
                "62512c8aae41",
                "worx-inflator-unspecified",
                status="COMPLETE",
                confirmed=True,
            )
            for incomplete_job_id in (
                "85e85b359cc0",
                "8d9a16902b87",
                "0f245c1ef63c",
            ):
                write_legacy_empty_run(
                    incomplete_job_id,
                    f"incomplete-{incomplete_job_id}",
                    status="INCOMPLETE_SOURCE_LIMIT",
                    confirmed=False,
                )

            output = root / "snapshot"
            try:
                manifest = build_snapshot(
                    Namespace(
                        source_root=source,
                        output_root=output,
                        connector_commit="connector-test",
                        reporting_commit="reporting-test",
                    )
                )
                self.assertEqual(manifest["selection"]["reportable_jobs"], 2)
                self.assertEqual(
                    manifest["selection"]["non_reportable_jobs"],
                    3,
                )
                exclusions = {
                    item["job_id"]: item
                    for item in manifest["selection"]["non_reportable_exclusions"]
                }
                self.assertEqual(
                    set(exclusions),
                    {"04efd58f6de8", "5fe461903438", "62512c8aae41"},
                )
                self.assertTrue(
                    {
                        "85e85b359cc0",
                        "8d9a16902b87",
                        "0f245c1ef63c",
                    }.isdisjoint(exclusions)
                )
                exclusion = exclusions["04efd58f6de8"]
                self.assertEqual(exclusion["job_id"], "04efd58f6de8")
                self.assertEqual(
                    exclusion["product_identity"],
                    "skil-bl0650c-13",
                )
                self.assertEqual(
                    exclusion["reason_code"],
                    "verified_true_empty_source",
                )
                self.assertEqual(
                    {item["role"] for item in exclusion["source_artifacts"]},
                    {
                        "standard_reviews",
                        "validation_report",
                        "raw_origin_evidence",
                    },
                )
                self.assertTrue(
                    all(
                        item["sha256"].startswith("sha256:")
                        for item in exclusion["source_artifacts"]
                    )
                )
                legacy_exclusion = exclusions["5fe461903438"]
                self.assertEqual(
                    legacy_exclusion["reconciliation_mode"],
                    "legacy_zero_reconciled",
                )
                self.assertEqual(
                    exclusions["62512c8aae41"]["reconciliation_mode"],
                    "legacy_zero_reconciled",
                )
                self.assertEqual(
                    set(legacy_exclusion["missing_validation_fields"]),
                    {
                        "source_reconciled",
                        "source_unique_reviews",
                        "eligible_reviews",
                        "excluded_reviews",
                        "valid_text_reviews",
                    },
                )
                self.assertEqual(
                    legacy_exclusion["derived_reconciliation"],
                    {
                        "source_reconciled": True,
                        "source_unique_reviews": 0,
                        "eligible_reviews": 0,
                        "excluded_reviews": 0,
                        "valid_text_reviews": 0,
                    },
                )
                self.assertEqual(
                    manifest["selection"]["source_contract_counts"],
                    {"current": 1, "legacy": 1},
                )
                self.assertEqual(
                    manifest["selection"]["output_contract"],
                    "review-fact/v2",
                )
                self.assertEqual(
                    manifest["selection"]["text_validity_reconciliation"],
                    {
                        "policy": (
                            "is_valid_text_review := "
                            "bool(trim(review_text))"
                        ),
                        "jobs_with_mismatch": 1,
                        "mismatch_rows": 1,
                    },
                )
                transformed = {
                    item["job_id"]: item
                    for item in manifest["selection"]["copied_artifacts"]
                }
                self.assertEqual(
                    transformed["current-fan"][
                        "source_text_validity_mismatch_rows"
                    ],
                    1,
                )
                self.assertTrue(
                    all(
                        source["sha256"].startswith("sha256:")
                        for source in transformed["current-fan"][
                            "source_artifacts"
                        ]
                    )
                )
                self.assertEqual(
                    manifest["reconciliation"]["output_contract_counts"],
                    {"current": 2},
                )
                self.assertEqual(
                    manifest["reconciliation"]["source_unique_reviews"], 5
                )
                self.assertEqual(
                    manifest["reconciliation"]["eligible_reviews"], 4
                )
                self.assertEqual(
                    manifest["reconciliation"]["written_reviews"], 2
                )
                tasks = catalog(output)["tasks"]
                self.assertEqual({task["category_id"] for task in tasks}, {"fan"})
                self.assertEqual(
                    {task["market_contract_mode"] for task in tasks},
                    {"current"},
                )
                self.assertEqual(sum(task["eligible_reviews"] for task in tasks), 4)
                self.assertNotIn(
                    "older-blower",
                    {task["id"] for task in tasks},
                )
                facts, quality, _ = build_review_fact(output, {})
                self.assertEqual(
                    {fact["review_id"] for fact in facts},
                    {
                        "short",
                        "rating-only",
                        "current-short",
                        "current-rating",
                    },
                )
                self.assertEqual(quality["written_occurrences"], 2)
                self.assertEqual(quality["non_text_records"], 2)
                self.assertEqual(quality["excluded_ineligible_records"], 1)
            finally:
                if output.exists():
                    for path in output.rglob("*"):
                        os.chmod(path, 0o755 if path.is_dir() else 0o644)
                    os.chmod(output, 0o755)

    def test_true_empty_source_exclusion_remains_fail_closed(self) -> None:
        invalid_cases = {
            "not-confirmed": {"empty_source_confirmed": False},
            "missing-source-reconciled": {"remove_field": "source_reconciled"},
            "not-reconciled": {"source_reconciled": False},
            "nonzero-source-total": {"source_total": 1},
            "nonzero-source": {"source_unique_reviews": 1},
            "nonzero-unique": {"unique_reviews": 1},
            "nonzero-eligible": {"eligible_reviews": 1},
            "nonzero-excluded": {"excluded_reviews": 1},
            "nonzero-valid-text": {"valid_text_reviews": 1},
            "nonzero-ratings-only": {"ratings_only": 1},
            "not-complete": {"status": "PARTIAL_SUCCESS"},
            "csv-data-row": {"csv_data_row": True},
            "raw-data-row": {"raw_data_row": True},
            "missing-raw": {"missing_raw": True},
        }
        for case_name, overrides in invalid_cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source"
                database = source / "data" / "jobs.sqlite3"
                database.parent.mkdir(parents=True)
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        """
                        CREATE TABLE jobs (
                            id TEXT,
                            status TEXT,
                            channel TEXT,
                            brand TEXT,
                            category TEXT,
                            model TEXT,
                            market_region TEXT,
                            target_url TEXT,
                            created_at TEXT,
                            finished_at TEXT,
                            superseded_by TEXT,
                            active_run_path TEXT
                        )
                        """
                    )
                    connection.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            "invalid-empty",
                            "completed",
                            "lowes",
                            "SKIL",
                            "Leaf Blower/Vacuum",
                            "BL0650C-13",
                            "North America",
                            "https://www.lowes.com/pd/SKIL-BL0650C-13/empty",
                            "2026-08-01",
                            "2026-08-02",
                            None,
                            "",
                        ),
                    )

                run_dir = source / "data" / "runs" / "invalid-empty" / "lowes"
                csv_rows = (
                    [
                        {
                            field: (
                                "unexpected-review"
                                if field == "review_id"
                                else "North America"
                                if field == "market_region"
                                else ""
                            )
                            for field in TRUE_EMPTY_SOURCE_FIELDS
                        }
                    ]
                    if overrides.get("csv_data_row")
                    else []
                )
                write_csv(
                    run_dir / "reviews.csv",
                    TRUE_EMPTY_SOURCE_FIELDS,
                    csv_rows,
                )
                if not overrides.get("missing_raw"):
                    raw_text = (
                        json.dumps({"id": "unexpected-review"}) + "\n"
                        if overrides.get("raw_data_row")
                        else ""
                    )
                    (run_dir / "reviews.raw.jsonl").write_text(
                        raw_text,
                        encoding="utf-8",
                    )
                report = {
                    "channel": "lowes",
                    "status": "COMPLETE",
                    "source_total": 0,
                    "source_unique_reviews": 0,
                    "source_reconciled": True,
                    "unique_reviews": 0,
                    "eligible_reviews": 0,
                    "excluded_reviews": 0,
                    "valid_text_reviews": 0,
                    "ratings_only": 0,
                    "market_region": "North America",
                    "notes": {"empty_source_confirmed": True},
                    "product": {
                        "id": "skil-bl0650c-13",
                        "market_region": "North America",
                    },
                }
                if "empty_source_confirmed" in overrides:
                    report["notes"]["empty_source_confirmed"] = overrides[
                        "empty_source_confirmed"
                    ]
                for field in (
                    "status",
                    "source_reconciled",
                    "source_total",
                    "source_unique_reviews",
                    "unique_reviews",
                    "eligible_reviews",
                    "excluded_reviews",
                    "valid_text_reviews",
                    "ratings_only",
                ):
                    if field in overrides:
                        report[field] = overrides[field]
                if overrides.get("remove_field"):
                    report.pop(str(overrides["remove_field"]), None)
                (run_dir / "validation_report.json").write_text(
                    json.dumps(report),
                    encoding="utf-8",
                )

                with self.assertRaises((RuntimeError, MarketContractError)):
                    build_snapshot(
                        Namespace(
                            source_root=source,
                            output_root=root / "snapshot",
                            connector_commit="connector-test",
                            reporting_commit="reporting-test",
                        )
                    )

    def test_legacy_true_empty_source_compatibility_is_narrow(self) -> None:
        invalid_cases = {
            "missing-source-total": {"remove_field": "source_total"},
            "missing-unique": {"remove_field": "unique_reviews"},
            "nonzero-source-total": {"source_total": 1},
            "nonzero-unique": {"unique_reviews": 1},
            "nonzero-ratings-only": {"ratings_only": 1},
            "nonzero-duplicates": {"duplicates_removed": 1},
            "partial-v2-field": {"source_reconciled": False},
            "not-complete": {"status": "INCOMPLETE_SOURCE_LIMIT"},
            "missing-legacy-header": {"missing_csv_field": "rating"},
        }
        for case_name, overrides in invalid_cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source"
                database = source / "data" / "jobs.sqlite3"
                database.parent.mkdir(parents=True)
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        """
                        CREATE TABLE jobs (
                            id TEXT,
                            status TEXT,
                            channel TEXT,
                            brand TEXT,
                            category TEXT,
                            model TEXT,
                            market_region TEXT,
                            target_url TEXT,
                            created_at TEXT,
                            finished_at TEXT,
                            superseded_by TEXT,
                            active_run_path TEXT
                        )
                        """
                    )
                    connection.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            "5fe461903438",
                            "completed",
                            "lowes",
                            "Sun Joe",
                            "Leaf Blower",
                            "SBJ597E",
                            "North America",
                            "https://www.lowes.com/pd/Sun-Joe-SBJ597E/empty",
                            "2026-07-23",
                            "2026-07-24",
                            None,
                            "",
                        ),
                    )

                run_dir = source / "data" / "runs" / "5fe461903438" / "lowes"
                csv_fields = [
                    field
                    for field in LEGACY_TRUE_EMPTY_SOURCE_FIELDS
                    if field != overrides.get("missing_csv_field")
                ]
                write_csv(run_dir / "reviews.csv", csv_fields, [])
                (run_dir / "reviews.raw.jsonl").write_text("", encoding="utf-8")
                report = {
                    "channel": "lowes",
                    "status": "COMPLETE",
                    "source_total": 0,
                    "unique_reviews": 0,
                    "ratings_only": 0,
                    "duplicates_removed": 0,
                    "notes": {
                        "empty_source_confirmed": True,
                        "official_lowes_rnr_api": True,
                    },
                    "product": {"id": "sun-joe-sbj597e"},
                }
                for field in (
                    "status",
                    "source_total",
                    "unique_reviews",
                    "ratings_only",
                    "duplicates_removed",
                    "source_reconciled",
                ):
                    if field in overrides:
                        report[field] = overrides[field]
                if overrides.get("remove_field"):
                    report.pop(str(overrides["remove_field"]), None)
                (run_dir / "validation_report.json").write_text(
                    json.dumps(report),
                    encoding="utf-8",
                )

                with self.assertRaises(RuntimeError):
                    build_snapshot(
                        Namespace(
                            source_root=source,
                            output_root=root / "snapshot",
                            connector_commit="connector-test",
                            reporting_commit="reporting-test",
                        )
                    )


if __name__ == "__main__":
    unittest.main()
