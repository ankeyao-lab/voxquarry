from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from reporting.category_vocabulary import (
    CATEGORY_RESOLUTION_SCHEMA_VERSION,
    canonical_category_name,
    category_id_for,
    category_resolution,
)
from reporting.controlled_energy import (
    controlled_energy_fact,
    resolve_controlled_energy_scope,
)
from reporting.source import catalog
from reporting.tests.test_partial_source_completeness import PartialSourceFixture
from scripts.build_reporting_snapshot_v2 import build_snapshot

if find_spec("jsonschema") is not None:
    from jsonschema import Draft202012Validator
else:
    Draft202012Validator = None  # type: ignore[assignment,misc]


ALIASES = (
    "Gas Pressure Washer",
    "Electric Pressure Washer",
    "Cordless Pressure Washer",
)


def governed_job(
    job_id: str,
    source_category: str,
    energy_type: str,
) -> dict[str, object]:
    brand = "Fixture Brand"
    voltage = "56V" if energy_type == "battery" else None
    row: dict[str, object] = {
        "id": job_id,
        "category": source_category,
        "brand": brand,
        "product_observation_hash": "sha256:" + "a" * 64,
        "energy_schema_version": "voxquarry.task-energy/v1",
        "energy_type": energy_type,
        "energy_readiness_status": "ready",
        "energy_evidence_source": "synthetic_fixture",
        "energy_canonical_voltage": voltage,
        "energy_unknown_reason": None,
        "voltage_platform_schema_version": "voxquarry.voltage-platform/v1",
        "voltage_platform_brand": brand,
        "voltage_platform_evidence_source": "synthetic_fixture",
    }
    if energy_type == "battery":
        row.update(
            {
                "voltage_platform_status": "known",
                "voltage_platform_key": "fixture-brand|56V",
                "voltage_platform_voltage": "56V",
                "voltage_platform_label": "Fixture Brand 56V",
                "voltage_platform_null_reason": None,
            }
        )
    else:
        row.update(
            {
                "voltage_platform_status": "non_dc",
                "voltage_platform_key": None,
                "voltage_platform_voltage": None,
                "voltage_platform_label": "非 DC",
                "voltage_platform_null_reason": "not_dc",
            }
        )
    return row


class PressureWasherCategoryAliasTests(unittest.TestCase):
    def test_cleaned_canonical_tasks_remain_one_category_with_energy_filters(self) -> None:
        jobs = [
            governed_job("corded", "Pressure Washer", "corded"),
            governed_job("battery", "Pressure Washer", "battery"),
            governed_job("fuel", "Pressure Washer", "fuel"),
            governed_job("hybrid", "Pressure Washer", "other"),
        ]
        self.assertEqual(
            {category_id_for(str(row["category"])) for row in jobs},
            {"pressure_washer"},
        )
        resolved = resolve_controlled_energy_scope(
            {
                "category_id": "pressure_washer",
                "job_ids": ["hybrid"],
                "energy_type": "other",
            },
            jobs,
            reportable_job_ids={"corded", "battery", "fuel", "hybrid"},
            source_snapshot={
                "snapshot_id": "prod_v2_" + "1" * 24,
                "schema_version": "voxquarry-reporting-snapshot/v2",
                "content_sha256": "sha256:" + "9" * 64,
            },
        )
        self.assertEqual(resolved["energy"]["energy_type"], "other")
        self.assertEqual([item["job_id"] for item in resolved["energy_refs"]], ["hybrid"])

    def test_exact_governed_aliases_and_original_name_share_stable_identity(self) -> None:
        for source_category in ("Pressure Washer", *ALIASES):
            with self.subTest(source_category=source_category):
                self.assertEqual(
                    category_id_for(source_category),
                    "pressure_washer",
                )
                self.assertEqual(
                    canonical_category_name(source_category),
                    "Pressure Washer",
                )

    def test_resolution_audit_preserves_source_and_versioned_alias_rule(self) -> None:
        source_category = "  Electric Pressure Washer  "
        resolution = category_resolution(source_category)
        self.assertEqual(
            resolution,
            {
                "schema_version": CATEGORY_RESOLUTION_SCHEMA_VERSION,
                "source_category": source_category,
                "normalized_source_category": "electric pressure washer",
                "category_id": "pressure_washer",
                "canonical_category": "Pressure Washer",
                "match_type": "controlled_alias",
            },
        )

    @unittest.skipIf(Draft202012Validator is None, "jsonschema unavailable")
    def test_resolution_audit_matches_the_versioned_schema(self) -> None:
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas"
                / "category-resolution-v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        validator.validate(category_resolution("Gas Pressure Washer"))
        validator.validate(category_resolution("Pressure Washer Attachment"))

    def test_nearby_non_target_categories_remain_unresolved_or_separate(self) -> None:
        unresolved = (
            "Gas Power Washer",
            "Electric Power Washer",
            "Pressure Washer Attachment",
            "Pressure Washer Surface Cleaner",
            "Cordless Cleaner",
            "High Pressure Washer",
        )
        for source_category in unresolved:
            with self.subTest(source_category=source_category):
                self.assertIsNone(category_id_for(source_category))
                self.assertEqual(
                    category_resolution(source_category)["match_type"],
                    "unresolved",
                )
        for source_category in (
            "Power Cleaner",
            "Power Scrubber",
            "Patio Cleaner",
        ):
            with self.subTest(source_category=source_category):
                self.assertNotEqual(
                    category_id_for(source_category),
                    "pressure_washer",
                )

    def test_snapshot_manifest_hashes_source_category_and_resolution_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            PartialSourceFixture(source)
            database = next((source / "data" / "backups").glob("*.sqlite3"))
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE jobs SET category = ?",
                    ("Gas Pressure Washer",),
                )
            task = catalog(source)["tasks"][0]
            self.assertEqual(task["source_category"], "Gas Pressure Washer")
            self.assertEqual(task["category"], "Pressure Washer")
            self.assertEqual(task["category_id"], "pressure_washer")
            self.assertEqual(
                task["category_resolution"],
                category_resolution("Gas Pressure Washer"),
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
            self.assertEqual(len(copied), 1)
            self.assertEqual(copied[0]["category"], "Gas Pressure Washer")
            self.assertEqual(
                copied[0]["category_resolution"],
                category_resolution("Gas Pressure Washer"),
            )
            self.assertEqual(
                manifest["contracts"]["category_resolution"],
                CATEGORY_RESOLUTION_SCHEMA_VERSION,
            )
            self.assertEqual(
                manifest["reconciliation"]["by_category"],
                {
                    "pressure_washer": {
                        "eligible_reviews": 3,
                        "jobs": 1,
                        "source_unique_reviews": 3,
                        "written_reviews": 3,
                    }
                },
            )
            second_source = root / "second-source"
            PartialSourceFixture(second_source)
            second_database = next(
                (second_source / "data" / "backups").glob("*.sqlite3")
            )
            with sqlite3.connect(second_database) as connection:
                connection.execute(
                    "UPDATE jobs SET category = ?",
                    ("Electric Pressure Washer",),
                )
            second_manifest = build_snapshot(
                Namespace(
                    source_root=second_source,
                    output_root=root / "second-snapshot",
                    connector_commit="connector",
                    reporting_commit="reporting",
                )
            )
            self.assertNotEqual(
                manifest["content_sha256"],
                second_manifest["content_sha256"],
            )

    def test_snapshot_identity_directly_binds_resolution_audit_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            PartialSourceFixture(source)
            database = next((source / "data" / "backups").glob("*.sqlite3"))
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE jobs SET category = ?",
                    ("Gas Pressure Washer",),
                )

            def build(name: str) -> dict[str, object]:
                # Package identity includes the timestamped SQLite filename.
                # Hold that independent input fixed so crossing a wall-clock
                # second cannot masquerade as a category-resolution change.
                with patch("scripts.build_reporting_snapshot_v2.datetime") as clock:
                    clock.now.return_value = datetime(2026, 8, 17, tzinfo=timezone.utc)
                    return build_snapshot(
                        Namespace(
                            source_root=source,
                            output_root=root / name,
                            connector_commit="connector",
                            reporting_commit="reporting",
                        )
                    )

            baseline = build("baseline")
            repeated = build("repeated")
            self.assertEqual(
                baseline["content_sha256"],
                repeated["content_sha256"],
            )
            self.assertEqual(baseline["files"], repeated["files"])

            changed_audit = category_resolution("Gas Pressure Washer")
            changed_audit["match_type"] = "canonical_name"
            with patch(
                "scripts.build_reporting_snapshot_v2.category_resolution",
                return_value=changed_audit,
            ):
                audit_changed = build("audit-changed")
            self.assertNotEqual(
                baseline["content_sha256"],
                audit_changed["content_sha256"],
            )
            self.assertEqual(baseline["files"], audit_changed["files"])

            changed_schema = {
                **category_resolution("Gas Pressure Washer"),
                "schema_version": "voxquarry.reporting-category-resolution/v2",
            }
            with (
                patch(
                    "scripts.build_reporting_snapshot_v2."
                    "CATEGORY_RESOLUTION_SCHEMA_VERSION",
                    "voxquarry.reporting-category-resolution/v2",
                ),
                patch(
                    "scripts.build_reporting_snapshot_v2.category_resolution",
                    return_value=changed_schema,
                ),
            ):
                schema_changed = build("schema-changed")
            self.assertNotEqual(
                baseline["content_sha256"],
                schema_changed["content_sha256"],
            )
            self.assertEqual(baseline["files"], schema_changed["files"])

    def test_walmart_68_alias_inventory_supports_three_disjoint_energy_scopes(self) -> None:
        jobs: list[dict[str, object]] = []
        energy_groups: dict[str, list[str]] = {
            "fuel": [],
            "corded": [],
            "battery": [],
        }
        for source_category, energy_type, count in (
            ("Gas Pressure Washer", "fuel", 29),
            ("Electric Pressure Washer", "corded", 29),
            ("Cordless Pressure Washer", "battery", 10),
        ):
            for index in range(count):
                job_id = f"synthetic-walmart-{energy_type}-{index:02d}"
                job = governed_job(job_id, source_category, energy_type)
                jobs.append(job)
                energy_groups[energy_type].append(job_id)

        self.assertEqual(len(jobs), 68)
        self.assertEqual(
            {category_id_for(job["category"]) for job in jobs},
            {"pressure_washer"},
        )
        self.assertEqual(
            {canonical_category_name(job["category"]) for job in jobs},
            {"Pressure Washer"},
        )
        self.assertEqual(
            {controlled_energy_fact(job)["energy_type"] for job in jobs},
            {"fuel", "corded", "battery"},
        )

        snapshot = {
            "snapshot_id": "prod_v2_" + "1" * 24,
            "schema_version": "voxquarry-reporting-snapshot/v2",
            "content_sha256": "sha256:" + "2" * 64,
        }
        reportable_job_ids = {str(job["id"]) for job in jobs}
        scopes = []
        for energy_type in ("fuel", "corded", "battery"):
            scopes.append(
                resolve_controlled_energy_scope(
                    {
                        "category_id": "pressure_washer",
                        "categories": ["Pressure Washer"],
                        "job_ids": energy_groups[energy_type],
                        "energy_type": energy_type,
                    },
                    jobs,
                    reportable_job_ids=reportable_job_ids,
                    source_snapshot=snapshot,
                )
            )

        self.assertEqual(
            [scope["energy_selection"]["selected_job_count"] for scope in scopes],
            [29, 29, 10],
        )
        self.assertEqual(
            {
                scope["energy_selection"]["source_snapshot"]["snapshot_id"]
                for scope in scopes
            },
            {snapshot["snapshot_id"]},
        )
        selected = [
            {reference["job_id"] for reference in scope["energy_refs"]}
            for scope in scopes
        ]
        self.assertFalse(selected[0] & selected[1])
        self.assertFalse(selected[0] & selected[2])
        self.assertFalse(selected[1] & selected[2])
        self.assertEqual(set().union(*selected), reportable_job_ids)


if __name__ == "__main__":
    unittest.main()
