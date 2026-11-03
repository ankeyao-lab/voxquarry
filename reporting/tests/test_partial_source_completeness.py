from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from reporting.analysis import build_report
from reporting.evidence_store import csv_lines, ndjson_lines
from reporting.markdown_export import report_to_markdown
from reporting.source import (
    MarketContractError,
    build_review_fact,
    catalog,
    materialize_snapshot,
)
from reporting.source_completeness import (
    DECLARED_INCOMPLETE_STOP_REASON,
    EXTERNAL_IMPORT_SCHEMA_VERSION,
    EXTERNAL_IMPORT_VALIDATION_SCHEMA_VERSION,
    SourceCompletenessError,
    enrich_evidence_rows,
    source_completeness_audit,
)
from scripts.build_reporting_snapshot_v2 import build_snapshot


CURRENT_FIELDS = [
    "channel",
    "product_id",
    "product_title",
    "product_url",
    "review_id",
    "display_site",
    "source_site",
    "source_product_id",
    "market_region",
    "market_region_evidence",
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
    "review_word_count",
    "is_eligible",
    "is_valid_text_review",
    "exclusion_reason",
]
AUTO_MISSING = object()


def review_row(review_id: str) -> dict[str, str]:
    return {
        "channel": "homedepot",
        "product_id": "hd-product",
        "product_title": "Fixture Pressure Washer",
        "product_url": "https://example.test/product",
        "review_id": review_id,
        "display_site": "homedepot.com",
        "source_site": "homedepot.com",
        "source_product_id": "hd-product",
        "market_region": "North America",
        "market_region_evidence": "source_market",
        "is_syndicated": "false",
        "submission_time": "2026-08-01",
        "title": "Fixture",
        "is_recommended": "true",
        "user_nickname": "fixture",
        "review_text": "The pressure is useful and setup was straightforward.",
        "rating": "5",
        "verified_purchaser": "",
        "where_purchase": "",
        "age": "",
        "review_word_count": "8",
        "is_eligible": "true",
        "is_valid_text_review": "true",
        "exclusion_reason": "",
    }


def governed_partial_report(
    *,
    declared: int = 5,
    collected: int = 3,
    status: str = "PARTIAL_SUCCESS",
    reconciled: bool = False,
    stop_reason: str | None = DECLARED_INCOMPLETE_STOP_REASON,
    missing: int | None | object = AUTO_MISSING,
) -> dict:
    report = {
        "validation_schema_version": EXTERNAL_IMPORT_VALIDATION_SCHEMA_VERSION,
        "channel": "homedepot",
        "status": status,
        "product": {
            "id": "hd-product",
            "title": "Synthetic pressure washer",
            "url": "https://www.homedepot.com/p/synthetic/hd-product",
            "brand": "Fixture Brand",
            "model": "PW-1",
            "category": "Pressure Washer",
            "market_region": "North America",
        },
        "source_total": declared,
        "source_records_collected": collected,
        "source_unique_reviews": collected,
        "source_reconciled": reconciled,
        "unique_reviews": collected,
        "eligible_reviews": collected,
        "excluded_reviews": 0,
        "valid_text_reviews": collected,
        "ratings_only": 0,
        "market_region": "North America",
        "notes": {
            "import_schema_version": EXTERNAL_IMPORT_SCHEMA_VERSION,
            "method": "governed external review import",
        },
        "artifacts": {
            "csv": "homedepot/reviews.csv",
            "raw_jsonl": "homedepot/reviews.raw.jsonl",
            "report": "homedepot/validation_report.json",
        },
        "completed_at": "2026-08-17T00:00:00Z",
    }
    if missing is AUTO_MISSING:
        report["source_missing_records"] = declared - collected
    elif missing is not None:
        report["source_missing_records"] = missing
    if stop_reason is not None:
        report["stop_reason"] = stop_reason
    return report


class PartialSourceFixture:
    def __init__(
        self,
        root: Path,
        *,
        declared: int = 5,
        collected: int = 3,
        job_status: str = "partial_success",
        validation_status: str = "PARTIAL_SUCCESS",
        reconciled: bool = False,
        stop_reason: str | None = DECLARED_INCOMPLETE_STOP_REASON,
        missing: int | None | object = AUTO_MISSING,
    ) -> None:
        self.root = root
        self.job_id = "partial-hd"
        database = (
            root
            / "data"
            / "backups"
            / "jobs-consistent-snapshot-partial.sqlite3"
        )
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
                    active_run_path TEXT,
                    result_json TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.job_id,
                    job_status,
                    "homedepot",
                    "Fixture Brand",
                    "Pressure Washer",
                    "PW-1",
                    "North America",
                    "https://example.test/product",
                    "2026-08-01T00:00:00Z",
                    "2026-08-02T00:00:00Z",
                    None,
                    "",
                    json.dumps(
                        {
                            "status": "completed",
                            "source_reconciled": True,
                            "stop_reason": "",
                        }
                    ),
                ),
            )
        (root / "data" / "jobs.sqlite3").symlink_to(
            Path("backups") / database.name
        )
        run_dir = root / "data" / "runs" / self.job_id / "homedepot"
        run_dir.mkdir(parents=True)
        rows = [review_row(f"review-{index}") for index in range(collected)]
        with (run_dir / "reviews.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=CURRENT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        (run_dir / "reviews.raw.jsonl").write_text(
            "".join(
                json.dumps({"id": row["review_id"]}) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
        report = governed_partial_report(
            declared=declared,
            collected=collected,
            status=validation_status,
            reconciled=reconciled,
            stop_reason=stop_reason,
            missing=missing,
        )
        (run_dir / "validation_report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )


class PartialSourceCompletenessTests(unittest.TestCase):
    def test_partial_only_is_audited_and_disclosed_everywhere(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = PartialSourceFixture(root)
            facts, quality, _ = build_review_fact(root, {})
            audit = quality["source_completeness"][0]
            self.assertEqual(audit["job_id"], fixture.job_id)
            self.assertEqual(audit["declared_source_total"], 5)
            self.assertEqual(audit["collected_source_unique_reviews"], 3)
            self.assertEqual(audit["missing_review_count"], 2)
            self.assertFalse(audit["source_reconciled"])
            self.assertTrue(audit["is_limited_source"])
            self.assertEqual(
                audit["validation_schema_version"],
                EXTERNAL_IMPORT_VALIDATION_SCHEMA_VERSION,
            )
            self.assertEqual(
                audit["import_schema_version"],
                EXTERNAL_IMPORT_SCHEMA_VERSION,
            )

            catalog_payload = catalog(root)
            self.assertEqual(
                catalog_payload["tasks"][0]["source_completeness"], audit
            )

            facts, manifest = materialize_snapshot(
                root, {}, root / "runtime"
            )
            self.assertEqual(
                manifest["quality_summary"]["source_completeness"],
                [audit],
            )
            report = build_report(facts, manifest, report_name="Partial")
            self.assertEqual(report["quality"]["source_completeness"], [audit])
            self.assertEqual(report["scope"]["source_completeness"], [audit])
            limitation = report["limitations"][0]
            self.assertEqual(limitation["code"], "limited_source_sample")
            self.assertIn("已收集 3/已声明 5/缺 2", limitation["message"])
            markdown = report_to_markdown(report)
            self.assertIn("有限来源样本：已收集 3/已声明 5/缺 2", markdown)

    def test_partial_contract_is_narrow_and_fail_closed(self) -> None:
        valid_report = governed_partial_report()
        job = {
            "id": "partial",
            "status": "partial_success",
            "channel": "homedepot",
        }
        audit = source_completeness_audit(
            job,
            valid_report,
            csv_eligible_rows=3,
            raw_source_rows=3,
        )
        self.assertEqual(audit["missing_review_count"], 2)

        invalid = (
            ("completed", valid_report, "任务状态"),
            (
                "partial_success",
                {**valid_report, "status": "COMPLETE"},
                "validation status",
            ),
            (
                "partial_success",
                {**valid_report, "source_total": 2},
                "declared_source_total",
            ),
            (
                "partial_success",
                {**valid_report, "source_reconciled": True},
                "source_reconciled",
            ),
            (
                "partial_success",
                {key: value for key, value in valid_report.items() if key != "stop_reason"},
                "stop_reason",
            ),
            (
                "partial_success",
                {
                    **valid_report,
                    "stop_reason": "quota_exhausted",
                },
                "stop_reason",
            ),
            (
                "partial_success",
                {**valid_report, "source_missing_records": 1},
                "source_missing_records",
            ),
            (
                "partial_success",
                {
                    **valid_report,
                    "validation_schema_version": "unknown/v1",
                },
                "validation_schema_version",
            ),
            (
                "partial_success",
                {
                    **valid_report,
                    "notes": {
                        **valid_report["notes"],
                        "import_schema_version": "unknown/v1",
                    },
                },
                "import_schema_version",
            ),
            (
                "partial_success",
                {**valid_report, "source_records_collected": 2},
                "source_records_collected",
            ),
            (
                "partial_success",
                {
                    key: value
                    for key, value in valid_report.items()
                    if key != "source_missing_records"
                },
                "source_missing_records",
            ),
            (
                "partial_success",
                {**valid_report, "channel": "amazon_live"},
                "does not match the task",
            ),
        )
        for job_status, report, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(SourceCompletenessError, message):
                    source_completeness_audit(
                        {**job, "status": job_status},
                        report,
                        csv_eligible_rows=3,
                        raw_source_rows=3,
                    )
        with self.assertRaisesRegex(
            SourceCompletenessError, "raw source rows"
        ):
            source_completeness_audit(
                job,
                valid_report,
                csv_eligible_rows=3,
                raw_source_rows=2,
            )

    def test_snapshot_builder_preserves_partial_and_hashes_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            PartialSourceFixture(source)
            first = build_snapshot(
                Namespace(
                    source_root=source,
                    output_root=root / "first",
                    connector_commit="connector",
                    reporting_commit="reporting",
                )
            )
            copied = first["selection"]["copied_artifacts"][0]
            self.assertTrue(copied["source_completeness"]["is_limited_source"])
            report_path = root / "first" / copied["artifacts"][1]
            transformed = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(transformed["status"], "PARTIAL_SUCCESS")
            self.assertEqual(transformed["source_total"], 5)
            self.assertEqual(transformed["source_unique_reviews"], 3)
            self.assertFalse(transformed["source_reconciled"])

            second_source = root / "source-two"
            PartialSourceFixture(second_source, declared=6)
            second = build_snapshot(
                Namespace(
                    source_root=second_source,
                    output_root=root / "second",
                    connector_commit="connector",
                    reporting_commit="reporting",
                )
            )
            self.assertNotEqual(first["content_sha256"], second["content_sha256"])

    def test_snapshot_builder_hashes_governed_energy_without_inference(self) -> None:
        def add_energy_contract(source: Path, energy_type: str) -> None:
            database = next((source / "data" / "backups").glob("*.sqlite3"))
            columns = (
                "energy_schema_version",
                "energy_type",
                "energy_readiness_status",
                "energy_evidence_source",
                "energy_canonical_voltage",
                "energy_unknown_reason",
                "product_observation_hash",
                "voltage_platform_schema_version",
                "voltage_platform_status",
                "voltage_platform_key",
                "voltage_platform_brand",
                "voltage_platform_voltage",
                "voltage_platform_label",
                "voltage_platform_evidence_source",
                "voltage_platform_null_reason",
            )
            with sqlite3.connect(database) as connection:
                for column in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
                connection.execute(
                    """
                    UPDATE jobs SET
                      energy_schema_version = 'voxquarry.task-energy/v1',
                      energy_type = ?,
                      energy_readiness_status = 'ready',
                      energy_evidence_source = 'synthetic_fixture',
                      energy_canonical_voltage = NULL,
                      energy_unknown_reason = NULL,
                      product_observation_hash = ?,
                      voltage_platform_schema_version = 'voxquarry.voltage-platform/v1',
                      voltage_platform_status = 'non_dc',
                      voltage_platform_key = NULL,
                      voltage_platform_brand = 'Fixture Brand',
                      voltage_platform_voltage = NULL,
                      voltage_platform_label = '非 DC',
                      voltage_platform_evidence_source = 'synthetic_fixture',
                      voltage_platform_null_reason = 'not_dc'
                    """,
                    (energy_type, "sha256:" + "a" * 64),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fuel_source = root / "fuel-source"
            PartialSourceFixture(fuel_source)
            add_energy_contract(fuel_source, "fuel")
            fuel = build_snapshot(
                Namespace(
                    source_root=fuel_source,
                    output_root=root / "fuel",
                    connector_commit="connector",
                    reporting_commit="reporting",
                )
            )
            self.assertEqual(
                fuel["selection"]["controlled_energy"],
                {
                    "schema_version": "voxquarry.task-energy/v1",
                    "authority": "wotu_task_fact",
                    "reportable_job_count": 1,
                    "energy_type_counts": {"fuel": 1},
                    "contract_status_counts": {"verified": 1},
                    "legacy_null_display": "unknown",
                    "inference_policy": "never_infer",
                },
            )

            corded_source = root / "corded-source"
            PartialSourceFixture(corded_source)
            add_energy_contract(corded_source, "corded")
            corded = build_snapshot(
                Namespace(
                    source_root=corded_source,
                    output_root=root / "corded",
                    connector_commit="connector",
                    reporting_commit="reporting",
                )
            )
            self.assertNotEqual(fuel["content_sha256"], corded["content_sha256"])

    def test_snapshot_builder_rejects_incomplete_battery_with_known_platform_before_artifact_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            PartialSourceFixture(source)
            database = next((source / "data" / "backups").glob("*.sqlite3"))
            columns = (
                "energy_schema_version",
                "energy_type",
                "energy_readiness_status",
                "energy_evidence_source",
                "energy_canonical_voltage",
                "energy_unknown_reason",
                "product_observation_hash",
                "voltage_platform_schema_version",
                "voltage_platform_status",
                "voltage_platform_key",
                "voltage_platform_brand",
                "voltage_platform_voltage",
                "voltage_platform_label",
                "voltage_platform_evidence_source",
                "voltage_platform_null_reason",
            )
            with sqlite3.connect(database) as connection:
                for column in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
                connection.execute(
                    """
                    UPDATE jobs SET
                      energy_schema_version = 'voxquarry.task-energy/v1',
                      energy_type = 'battery',
                      energy_readiness_status = 'metadata_incomplete',
                      energy_evidence_source = 'synthetic_fixture',
                      energy_canonical_voltage = NULL,
                      energy_unknown_reason = 'battery_voltage_required',
                      product_observation_hash = ?,
                      voltage_platform_schema_version = 'voxquarry.voltage-platform/v1',
                      voltage_platform_status = 'known',
                      voltage_platform_key = 'fixture-brand|56V',
                      voltage_platform_brand = 'Fixture Brand',
                      voltage_platform_voltage = '56V',
                      voltage_platform_label = 'Fixture Brand 56V',
                      voltage_platform_evidence_source = 'synthetic_fixture',
                      voltage_platform_null_reason = NULL
                    """,
                    ("sha256:" + "a" * 64,),
                )

            output = root / "snapshot"
            with patch(
                "scripts.build_reporting_snapshot_v2.transform_run_artifacts"
            ) as transform:
                with self.assertRaisesRegex(
                    ValueError, "battery 缺电压与电压平台不一致"
                ):
                    build_snapshot(
                        Namespace(
                            source_root=source,
                            output_root=output,
                            connector_commit="connector",
                            reporting_commit="reporting",
                        )
                    )
                transform.assert_not_called()
            self.assertFalse(output.exists())

    def test_hd12_synthetic_batch_enters_snapshot_with_exact_declared_gap(self) -> None:
        """Production counts, synthetic identities/content; no production package is read."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            database = (
                source
                / "data"
                / "backups"
                / "jobs-consistent-snapshot-hd12.sqlite3"
            )
            database.parent.mkdir(parents=True)
            collected_per_job = [4_475] * 12
            gaps = [37] * 11 + [33]
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
                        active_run_path TEXT,
                        result_json TEXT
                    )
                    """
                )
                for job_index, (collected, gap) in enumerate(
                    zip(collected_per_job, gaps), start=1
                ):
                    job_id = f"synthetic-hd-partial-{job_index:02d}"
                    product_id = f"synthetic-product-{job_index:02d}"
                    declared = collected + gap
                    report = governed_partial_report(
                        declared=declared,
                        collected=collected,
                    )
                    report["product"] = {
                        **report["product"],
                        "id": product_id,
                        "model": f"SYN-{job_index:02d}",
                        "url": f"https://example.test/{product_id}",
                    }
                    run_dir = (
                        source
                        / "data"
                        / "runs"
                        / job_id
                        / "homedepot"
                    )
                    run_dir.mkdir(parents=True)
                    with (run_dir / "reviews.csv").open(
                        "w", encoding="utf-8", newline=""
                    ) as handle:
                        writer = csv.DictWriter(handle, fieldnames=CURRENT_FIELDS)
                        writer.writeheader()
                        for row_index in range(collected):
                            row = review_row(
                                f"synthetic-{job_index:02d}-{row_index:05d}"
                            )
                            row.update(
                                {
                                    "product_id": product_id,
                                    "source_product_id": product_id,
                                    "product_url": f"https://example.test/{product_id}",
                                }
                            )
                            writer.writerow(row)
                    with (run_dir / "reviews.raw.jsonl").open(
                        "w", encoding="utf-8"
                    ) as handle:
                        for row_index in range(collected):
                            handle.write(
                                json.dumps(
                                    {
                                        "id": (
                                            f"synthetic-{job_index:02d}-"
                                            f"{row_index:05d}"
                                        )
                                    }
                                )
                                + "\n"
                            )
                    (run_dir / "validation_report.json").write_text(
                        json.dumps(report), encoding="utf-8"
                    )
                    connection.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id,
                            "partial_success",
                            "homedepot",
                            "Fixture Brand",
                            "Pressure Washer",
                            f"SYN-{job_index:02d}",
                            "North America",
                            f"https://example.test/{product_id}",
                            "2026-08-18T00:00:00Z",
                            "2026-08-18T00:01:00Z",
                            None,
                            f"{job_id}/homedepot",
                            json.dumps({"homedepot": report}),
                        ),
                    )
            (source / "data" / "jobs.sqlite3").symlink_to(
                Path("backups") / database.name
            )
            manifest = build_snapshot(
                Namespace(
                    source_root=source,
                    output_root=root / "snapshot",
                    connector_commit="connector",
                    reporting_commit="reporting",
                )
            )
            copied = manifest["selection"]["copied_artifacts"]
            audits = [row["source_completeness"] for row in copied]
            self.assertEqual(manifest["selection"]["reportable_jobs"], 12)
            self.assertEqual(len(audits), 12)
            self.assertTrue(all(row["is_limited_source"] for row in audits))
            self.assertEqual(
                sum(row["collected_source_unique_reviews"] for row in audits),
                53_700,
            )
            self.assertEqual(
                sum(row["declared_source_total"] for row in audits),
                54_140,
            )
            self.assertEqual(
                sum(row["missing_review_count"] for row in audits),
                440,
            )

    def test_physical_validation_overrides_stale_job_result_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = PartialSourceFixture(root)
            database = root / "data" / "jobs.sqlite3"
            with sqlite3.connect(database) as connection:
                cached = json.loads(
                    connection.execute(
                        "SELECT result_json FROM jobs WHERE id = ?",
                        (fixture.job_id,),
                    ).fetchone()[0]
                )
            self.assertTrue(cached["source_reconciled"])
            facts, quality, _ = build_review_fact(root, {})
            self.assertEqual(len(facts), 3)
            self.assertFalse(
                quality["source_completeness"][0]["source_reconciled"]
            )
            self.assertEqual(
                quality["source_completeness"][0]["stop_reason"],
                DECLARED_INCOMPLETE_STOP_REASON,
            )

    def test_invalid_partial_is_rejected_before_report_generation(self) -> None:
        variants = (
            {"reconciled": True},
            {"stop_reason": None},
            {"stop_reason": "bounded source denial"},
            {"declared": 2, "collected": 3},
            {"validation_status": "COMPLETE"},
        )
        for index, options in enumerate(variants):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                PartialSourceFixture(root, **options)
                with self.assertRaises((SourceCompletenessError, MarketContractError)):
                    build_review_fact(root, {})

    def test_evidence_exports_carry_the_same_source_limit_audit(self) -> None:
        audit = source_completeness_audit(
            {
                "id": "partial-hd",
                "status": "partial_success",
                "channel": "homedepot",
            },
            governed_partial_report(),
        )
        evidence = {
            "evidence_id": "ev_partial",
            "evidence_type": "review_quote",
            "job_id": "partial-hd",
            "quote": "Useful pressure.",
        }
        enriched = list(enrich_evidence_rows([evidence], [audit]))
        self.assertEqual(enriched[0]["source_completeness_status"], "limited")
        self.assertEqual(enriched[0]["missing_review_count"], 2)
        csv_text = b"".join(csv_lines(enriched)).decode("utf-8-sig")
        self.assertIn("job_id", csv_text.splitlines()[0])
        self.assertIn("missing_review_count", csv_text.splitlines()[0])
        self.assertIn(
            "source_completeness_validation_schema_version",
            csv_text.splitlines()[0],
        )
        self.assertIn("partial-hd", csv_text)
        ndjson = json.loads(b"".join(ndjson_lines(enriched)).decode("utf-8"))
        self.assertEqual(ndjson["declared_source_total"], 5)
        self.assertEqual(
            ndjson["source_completeness_validation_status"],
            "PARTIAL_SUCCESS",
        )
        self.assertFalse(ndjson["source_reconciled"])

        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "source-completeness-audit-v1.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            set(audit),
        )
        self.assertTrue(set(audit) <= set(schema["properties"]))
        invalid = {**audit, "source_reconciled": True}
        with self.assertRaisesRegex(SourceCompletenessError, "source_reconciled"):
            source_completeness_audit(
                {
                    "id": "partial-hd",
                    "status": "partial_success",
                    "channel": "homedepot",
                },
                governed_partial_report(
                    reconciled=invalid["source_reconciled"]
                ),
            )

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.


if __name__ == "__main__":
    unittest.main()
