from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reporting.source import (
    UNSPECIFIED_MODEL,
    catalog,
    discover_selected_runs,
    validate_scope_job_ids,
)
class CreationCatalogTests(unittest.TestCase):
    def test_request_local_catalog_reuse_preserves_scope_rejections(self):
        jobs = [{"id": "one"}, {"id": "unreportable"}]
        payload = {"tasks": [{"id": "one", "category_id": "leaf_blower", "energy_type": "battery"}]}
        with patch("reporting.source.load_jobs", side_effect=AssertionError("duplicate source scan")), patch("reporting.source.catalog", side_effect=AssertionError("duplicate catalog scan")):
            validate_scope_job_ids(Path("unused"), {"category_id": "leaf_blower", "job_ids": ["one"]}, source_jobs=jobs, catalog_payload=payload)
            for selection, message in [("missing", "unknown job_ids"), ("unreportable", "not reportable")]:
                with self.assertRaisesRegex(ValueError, message):
                    validate_scope_job_ids(Path("unused"), {"category_id": "leaf_blower", "job_ids": [selection]}, source_jobs=jobs, catalog_payload=payload)
            with self.assertRaisesRegex(ValueError, "do not belong to category_id"):
                validate_scope_job_ids(Path("unused"), {"category_id": "snow_blower", "job_ids": ["one"]}, source_jobs=jobs, catalog_payload=payload)

    def test_catalog_discloses_unverified_product_master_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_root = Path(temporary_directory)
            backup_root = snapshot_root / "data" / "backups"
            backup_root.mkdir(parents=True)
            database = backup_root / "jobs-consistent-snapshot-test.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE jobs (status TEXT, channel TEXT, brand TEXT, "
                    "category TEXT, model TEXT)"
                )
            payload = catalog(snapshot_root)
            self.assertEqual(
                payload["product_master_contract"],
                {
                    "status": "unavailable",
                    "schema_version": "retail-product-master/v2",
                    "reason": "authoritative_product_master_projection_missing",
                },
            )
            self.assertEqual(payload["voltage_platforms"], [])

    def test_wotu_platform_catalog_does_not_require_chengmu_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_root = Path(temporary_directory)
            backup_root = snapshot_root / "data" / "backups"
            backup_root.mkdir(parents=True)
            database = backup_root / "jobs-consistent-snapshot-test.sqlite3"
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
                        target_url TEXT,
                        created_at TEXT,
                        finished_at TEXT,
                        superseded_by TEXT,
                        product_observation_hash TEXT,
                        voltage_platform_schema_version TEXT,
                        voltage_platform_status TEXT,
                        voltage_platform_key TEXT,
                        voltage_platform_brand TEXT,
                        voltage_platform_voltage TEXT,
                        voltage_platform_label TEXT,
                        voltage_platform_evidence_source TEXT,
                        voltage_platform_null_reason TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "ego-56", "completed", "ego", "EGO",
                        "Leaf Blower", "LB1", "https://example.test/lb1",
                        "2026-08-18", "2026-08-18", None,
                        "sha256:" + "1" * 64,
                        "voxquarry.voltage-platform/v1", "known",
                        "ego|56V", "EGO", "56V", "EGO 56V",
                        "source_observation", None,
                    ),
                )
            run = snapshot_root / "data" / "runs" / "ego-56" / "ego"
            run.mkdir(parents=True)
            (run / "validation_report.json").write_text(
                '{"status":"COMPLETE","product":{"id":"LB1"},'
                '"source_total":1,"unique_reviews":1}',
                encoding="utf-8",
            )
            (run / "reviews.csv").write_text(
                "channel,product_id,review_id\nego,LB1,review-1\n",
                encoding="utf-8",
            )

            payload = catalog(snapshot_root)
            self.assertEqual(payload["product_master_contract"]["status"], "unavailable")
            self.assertEqual(
                payload["voltage_platforms"],
                [{
                    "schema_version": "voxquarry.voltage-platform/v1",
                    "platform_key": "ego|56V",
                    "brand": "EGO",
                    "canonical_voltage": "56V",
                    "display_name": "EGO 56V",
                }],
            )
            self.assertEqual(payload["tasks"][0]["voltage_platform_key"], "ego|56V")

    def test_catalog_is_rebuilt_from_latest_readable_source_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_root = Path(temporary_directory)
            backup_root = snapshot_root / "data" / "backups"
            backup_root.mkdir(parents=True)
            database = backup_root / "jobs-consistent-snapshot-test.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE jobs (
                        status TEXT,
                        channel TEXT,
                        brand TEXT,
                        category TEXT,
                        model TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?)",
                    ("completed", "ego", "EGO", "割草机", "LM2135SP"),
                )

            first = catalog(snapshot_root)
            self.assertEqual(first["categories"], ["Walk-Behind Mower"])

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?)",
                    ("completed", "ego", "EGO", "吹风机", "LB7654"),
                )

            second = catalog(snapshot_root)
            self.assertEqual(
                second["categories"],
                ["Leaf Blower", "Walk-Behind Mower"],
            )
            self.assertEqual(
                second["models_by_category"]["Leaf Blower"], ["LB7654"]
            )
            self.assertEqual(second["tasks"], [])

    def test_complete_run_precedes_newer_partial_and_partial_is_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_root = Path(temporary_directory)
            backup_root = snapshot_root / "data" / "backups"
            backup_root.mkdir(parents=True)
            database = backup_root / "jobs-consistent-snapshot-test.sqlite3"
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
                        target_url TEXT,
                        created_at TEXT,
                        finished_at TEXT,
                        superseded_by TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            "full-old", "completed", "ego", "EGO", "吹风机",
                            "LB1", "https://example.test/lb1", "2026-01-01",
                            "2026-01-01", None,
                        ),
                        (
                            "partial-new", "partial_success", "ego", "EGO",
                            "吹风机", "LB1", "https://example.test/lb1",
                            "2026-02-01", "2026-02-01", None,
                        ),
                        (
                            "partial-only", "partial_success", "ego", "EGO",
                            "吹风机", "LB2", "https://example.test/lb2",
                            "2026-03-01", "2026-03-01", None,
                        ),
                    ],
                )
            reports = {
                "full-old": ("LB1", "COMPLETE"),
                "partial-new": ("LB1", "PARTIAL_SUCCESS"),
                "partial-only": ("LB2", "PARTIAL_SUCCESS"),
            }
            for job_id, (product_id, status) in reports.items():
                run_dir = snapshot_root / "data" / "runs" / job_id / "ego"
                run_dir.mkdir(parents=True)
                (run_dir / "validation_report.json").write_text(
                    (
                        '{"status":"%s","product":{"id":"%s"},'
                        '"unique_reviews":1,"source_total":2}'
                    ) % (status, product_id),
                    encoding="utf-8",
                )
                (run_dir / "reviews.csv").write_text(
                    "channel,product_id,review_id\n"
                    f"ego,{product_id},{job_id}-review\n",
                    encoding="utf-8",
                )

            selected, quality = discover_selected_runs(snapshot_root, {})
            self.assertEqual(
                {run["job"]["id"] for run in selected},
                {"full-old", "partial-only"},
            )
            self.assertEqual(quality["eligible_runs"], 2)

            exact, _ = discover_selected_runs(
                snapshot_root, {"job_ids": ["partial-only"]}
            )
            self.assertEqual(
                [run["job"]["id"] for run in exact],
                ["partial-only"],
            )
            task_catalog = catalog(snapshot_root)
            self.assertEqual(
                {task["id"] for task in task_catalog["tasks"]},
                {"full-old", "partial-only"},
            )
            self.assertEqual(
                {task["category_id"] for task in task_catalog["tasks"]},
                {"leaf_blower"},
            )
            validate_scope_job_ids(
                snapshot_root,
                {
                    "category_id": "leaf_blower",
                    "job_ids": ["full-old", "partial-only"],
                },
            )
            with self.assertRaisesRegex(
                ValueError, "do not belong to category_id"
            ):
                validate_scope_job_ids(
                    snapshot_root,
                    {
                        "category_id": "snow_blower",
                        "job_ids": ["full-old"],
                    },
                )
            with self.assertRaisesRegex(ValueError, "unknown job_ids"):
                validate_scope_job_ids(
                    snapshot_root,
                    {
                        "category_id": "leaf_blower",
                        "job_ids": ["missing-job"],
                    },
                )
            with self.assertRaisesRegex(ValueError, "not reportable"):
                validate_scope_job_ids(
                    snapshot_root,
                    {
                        "category_id": "leaf_blower",
                        "job_ids": ["partial-new"],
                    },
                )

            # An absent model is an explicit, selectable catalog dimension.
            # It is intentionally not converted into a fabricated Review Fact
            # model value; only the scope/catalog representation changes.
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        "model-unknown", "completed", "ego", "EGO",
                        "吹风机", "", "https://example.test/lb-unknown",
                        "2026-04-01", "2026-04-01", None,
                    ),
                )
            unknown_run_dir = snapshot_root / "data" / "runs" / "model-unknown" / "ego"
            unknown_run_dir.mkdir(parents=True)
            (unknown_run_dir / "validation_report.json").write_text(
                '{"status":"COMPLETE","product":{"id":"LB-UNKNOWN"},'
                '"unique_reviews":1,"source_total":1}',
                encoding="utf-8",
            )
            (unknown_run_dir / "reviews.csv").write_text(
                "channel,product_id,review_id\n"
                "ego,LB-UNKNOWN,unknown-review\n",
                encoding="utf-8",
            )

            unknown_only, _ = discover_selected_runs(
                snapshot_root, {"models": [UNSPECIFIED_MODEL]}
            )
            self.assertEqual(
                [run["job"]["id"] for run in unknown_only], ["model-unknown"]
            )
            refreshed_catalog = catalog(snapshot_root)
            self.assertIn(UNSPECIFIED_MODEL, refreshed_catalog["models"])
            self.assertIn(
                UNSPECIFIED_MODEL,
                refreshed_catalog["models_by_category"]["Leaf Blower"],
            )


if __name__ == "__main__":
    unittest.main()
