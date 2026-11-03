from __future__ import annotations

import json
import unittest
import tempfile
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from reporting.controlled_energy import (
    ControlledEnergyError,
    controlled_energy_fact,
    energy_catalog,
    resolve_controlled_energy_scope,
)
from reporting.evidence_store import EvidenceRepository, evidence_manifest

if find_spec("fastapi") is not None:
    from fastapi.testclient import TestClient

    from reporting.app import create_app
else:
    TestClient = None  # type: ignore[assignment,misc]
    create_app = None  # type: ignore[assignment,misc]

if find_spec("jsonschema") is not None:
    from jsonschema import Draft202012Validator
else:
    Draft202012Validator = None  # type: ignore[assignment,misc]


def energy_job(
    job_id: str,
    energy_type: str | None,
    *,
    brand: str = "EGO",
    voltage: str | None = None,
    readiness: str = "ready",
    reason: str | None = None,
) -> dict:
    row = {
        "id": job_id,
        "category": "Pressure Washer",
        "brand": brand,
        "product_observation_hash": "sha256:" + "a" * 64,
    }
    if energy_type is None:
        return row
    row.update({
        "energy_schema_version": "voxquarry.task-energy/v1",
        "energy_type": energy_type,
        "energy_readiness_status": readiness,
        "energy_evidence_source": "source_observation",
        "energy_canonical_voltage": voltage,
        "energy_unknown_reason": reason,
    })
    if energy_type in {"battery", "other"} and voltage and readiness == "ready":
        row.update({
            "voltage_platform_schema_version": "voxquarry.voltage-platform/v1",
            "voltage_platform_status": "known",
            "voltage_platform_key": f"{brand.casefold()}|{voltage}",
            "voltage_platform_brand": brand,
            "voltage_platform_voltage": voltage,
            "voltage_platform_label": f"{brand} {voltage}",
            "voltage_platform_evidence_source": "source_observation",
            "voltage_platform_null_reason": None,
        })
    elif energy_type in {"corded", "fuel"}:
        row.update({
            "voltage_platform_schema_version": "voxquarry.voltage-platform/v1",
            "voltage_platform_status": "non_dc",
            "voltage_platform_key": None,
            "voltage_platform_brand": brand,
            "voltage_platform_voltage": None,
            "voltage_platform_label": "非 DC",
            "voltage_platform_evidence_source": "source_observation",
            "voltage_platform_null_reason": "not_dc",
        })
    else:
        row.update({
            "voltage_platform_schema_version": "voxquarry.voltage-platform/v1",
            "voltage_platform_status": "unknown",
            "voltage_platform_key": None,
            "voltage_platform_brand": brand,
            "voltage_platform_voltage": None,
            "voltage_platform_label": None,
            "voltage_platform_evidence_source": "source_observation",
            "voltage_platform_null_reason": "voltage_unknown",
        })
    return row


def source_snapshot() -> dict:
    return {
        "snapshot_id": "prod_v2_" + "2" * 24,
        "schema_version": "voxquarry-reporting-snapshot/v2",
        "content_sha256": "sha256:" + "8" * 64,
    }


class ControlledEnergyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.battery = energy_job("b", "battery", voltage="56V")
        self.corded = energy_job("c", "corded")
        self.fuel = energy_job("f", "fuel")
        self.other = energy_job("o", "other")
        self.hybrid = energy_job(
            "h", "other", brand="Greenworks", voltage="60V"
        )
        self.legacy = energy_job("l", None)

    def test_governed_codes_and_legacy_null_are_projected_without_inference(self) -> None:
        self.assertEqual(controlled_energy_fact(self.battery)["energy_type"], "battery")
        self.assertEqual(controlled_energy_fact(self.corded)["canonical_voltage"], None)
        legacy = controlled_energy_fact(self.legacy)
        self.assertEqual(legacy["energy_type"], "unknown")
        self.assertEqual(legacy["contract_status"], "legacy_null")
        self.assertEqual(legacy["unknown_reason"], "legacy_null")

    def test_catalog_keeps_other_and_unknown_visible_with_audit_counts(self) -> None:
        rows = energy_catalog([
            self.battery, self.corded, self.fuel, self.other, self.legacy
        ])
        by_code = {row["energy_type"]: row for row in rows}
        self.assertEqual(set(by_code), {"battery", "corded", "fuel", "other", "unknown"})
        self.assertEqual(by_code["unknown"]["legacy_null_task_count"], 1)
        self.assertEqual(by_code["battery"]["governed_task_count"], 1)

    def test_hybrid_other_keeps_verified_voltage_platform(self) -> None:
        fact = controlled_energy_fact(self.hybrid)
        self.assertEqual(fact["energy_type"], "other")
        self.assertEqual(fact["canonical_voltage"], "60V")

    def test_battery_accepts_governed_dual_voltage_platform(self) -> None:
        dual = energy_job(
            "dual", "battery", brand="Milwaukee", voltage="12V/18V"
        )
        fact = controlled_energy_fact(dual)
        self.assertEqual(fact["canonical_voltage"], "12V/18V")

    def test_historical_other_non_dc_remains_readable_without_snapshot_rewrite(self) -> None:
        historical = energy_job("historical", "other")
        historical.update({
            "voltage_platform_status": "non_dc",
            "voltage_platform_label": "非 DC",
            "voltage_platform_null_reason": "not_dc",
        })
        fact = controlled_energy_fact(historical)
        self.assertEqual(fact["energy_type"], "other")
        self.assertIsNone(fact["canonical_voltage"])

        contradictory = {**historical, "energy_canonical_voltage": "60V"}
        with self.assertRaises(ControlledEnergyError):
            controlled_energy_fact(contradictory)

    def test_invalid_schema_invariants_hash_or_platform_fail_closed(self) -> None:
        battery_missing_voltage = energy_job(
            "battery-missing",
            "battery",
            readiness="metadata_incomplete",
            reason="battery_voltage_required",
        )
        invalid = [
            {**self.battery, "energy_schema_version": "voxquarry.task-energy/v0"},
            {**self.battery, "energy_canonical_voltage": "M18"},
            {**self.battery, "energy_unknown_reason": "battery_voltage_required"},
            {**self.battery, "product_observation_hash": None},
            {**self.battery, "voltage_platform_voltage": "40V"},
            {**self.corded, "energy_canonical_voltage": "120V"},
            {**self.fuel, "voltage_platform_status": "known"},
            {
                **battery_missing_voltage,
                "voltage_platform_status": "known",
                "voltage_platform_key": "ego|56V",
                "voltage_platform_voltage": "56V",
                "voltage_platform_label": "EGO 56V",
                "voltage_platform_null_reason": None,
            },
            {
                **battery_missing_voltage,
                "voltage_platform_status": "non_dc",
                "voltage_platform_label": "非 DC",
                "voltage_platform_null_reason": "not_dc",
            },
            {**self.legacy, "energy_type": "fuel"},
        ]
        for row in invalid:
            with self.subTest(row=row):
                with self.assertRaises(ControlledEnergyError):
                    controlled_energy_fact(row)

        fact = controlled_energy_fact(battery_missing_voltage)
        self.assertEqual(fact["readiness_status"], "metadata_incomplete")
        self.assertEqual(fact["unknown_reason"], "battery_voltage_required")

    def test_pressure_washer_scope_requires_one_code_and_freezes_source_audit(self) -> None:
        jobs = [self.battery, self.corded, self.fuel, self.legacy]
        resolved = resolve_controlled_energy_scope(
            {
                "category_id": "pressure_washer",
                "job_ids": ["b"],
                "energy_type": "battery",
                "energy": {"energy_type": "fuel"},
                "energy_refs": [{"job_id": "forged"}],
            },
            jobs,
            reportable_job_ids={"b", "c", "f", "l"},
            source_snapshot={
                "snapshot_id": "prod_v2_" + "1" * 24,
                "schema_version": "voxquarry-reporting-snapshot/v2",
                "content_sha256": "sha256:" + "9" * 64,
            },
        )
        self.assertEqual(resolved["energy"], {
            "schema_version": "voxquarry.task-energy/v1",
            "energy_type": "battery",
            "label": "电池",
        })
        self.assertEqual([row["job_id"] for row in resolved["energy_refs"]], ["b"])
        self.assertEqual(resolved["energy_selection"]["category_reportable_job_count"], 4)
        self.assertEqual(resolved["energy_selection"]["excluded_other_energy_job_count"], 3)
        self.assertEqual(
            resolved["energy_selection"]["source_snapshot"]["snapshot_id"],
            "prod_v2_" + "1" * 24,
        )

    def test_all_energy_scope_includes_every_code_and_discloses_distribution(self) -> None:
        jobs = [
            {**self.battery, "unique_reviews": 7},
            {**self.corded, "unique_reviews": 11},
            {**self.fuel, "unique_reviews": 13},
            {**self.other, "unique_reviews": 3},
            {**self.legacy, "unique_reviews": 2},
        ]
        resolved = resolve_controlled_energy_scope(
            {
                "category_id": "pressure_washer",
                "job_ids": ["b", "c", "f", "l", "o"],
                "energy_type": "all",
            },
            jobs,
            reportable_job_ids={"b", "c", "f", "l", "o"},
            source_snapshot=source_snapshot(),
        )
        self.assertEqual(resolved["energy"]["label"], "全部")
        self.assertEqual(len(resolved["energy_refs"]), 5)
        self.assertEqual(
            resolved["energy_selection"]["category_energy_task_counts"],
            {"corded": 1, "battery": 1, "fuel": 1, "other": 1, "unknown": 1},
        )
        self.assertEqual(
            resolved["energy_selection"]["category_energy_review_counts"],
            {"corded": 11, "battery": 7, "fuel": 13, "other": 3, "unknown": 2},
        )
        self.assertEqual(
            resolved["energy_selection"]["excluded_other_energy_job_count"], 0
        )

    def test_all_energy_scope_keeps_pre_contract_partial_rows_as_legacy_unknown(self) -> None:
        pre_contract = {
            **self.legacy,
            "id": "pre-contract",
            "energy_type": "unknown",
            "unique_reviews": 17,
        }
        resolved = resolve_controlled_energy_scope(
            {
                "category_id": "pressure_washer",
                "job_ids": ["b", "pre-contract"],
                "energy_type": "all",
            },
            [self.battery, pre_contract],
            reportable_job_ids={"b", "pre-contract"},
            source_snapshot=source_snapshot(),
        )
        refs = {row["job_id"]: row for row in resolved["energy_refs"]}
        self.assertEqual(refs["pre-contract"]["contract_status"], "legacy_null")
        self.assertEqual(refs["pre-contract"]["unknown_reason"], "legacy_null")
        self.assertEqual(
            resolved["energy_selection"]["category_energy_task_counts"]["unknown"],
            1,
        )
        self.assertEqual(
            resolved["energy_selection"]["category_energy_review_counts"]["unknown"],
            17,
        )

        with self.assertRaises(ControlledEnergyError):
            resolve_controlled_energy_scope(
                {
                    "category_id": "pressure_washer",
                    "job_ids": ["pre-contract"],
                    "energy_type": "unknown",
                },
                [pre_contract],
                reportable_job_ids={"pre-contract"},
                source_snapshot=source_snapshot(),
            )

    def test_same_full_snapshot_resolves_three_disjoint_energy_scopes(self) -> None:
        jobs = [self.battery, self.corded, self.fuel]
        source_snapshot_identity = source_snapshot()
        resolved = []
        for code, job_id in (("battery", "b"), ("corded", "c"), ("fuel", "f")):
            resolved.append(resolve_controlled_energy_scope(
                {
                    "category_id": "pressure_washer",
                    "job_ids": [job_id],
                    "energy_type": code,
                },
                jobs,
                reportable_job_ids={"b", "c", "f"},
                source_snapshot=source_snapshot_identity,
            ))
        self.assertEqual(
            {row["energy_selection"]["source_snapshot"]["snapshot_id"] for row in resolved},
            {"prod_v2_" + "2" * 24},
        )
        self.assertEqual(
            {row["energy_refs"][0]["job_id"] for row in resolved},
            {"b", "c", "f"},
        )

    def test_three_energy_reports_keep_disjoint_queryable_evidence_sidecars(self) -> None:
        jobs = [self.battery, self.corded, self.fuel]
        with tempfile.TemporaryDirectory() as directory:
            repository = EvidenceRepository(Path(directory))
            for code, job_id in (("battery", "b"), ("corded", "c"), ("fuel", "f")):
                scope = resolve_controlled_energy_scope(
                    {
                        "category_id": "pressure_washer",
                        "job_ids": [job_id],
                        "energy_type": code,
                    },
                    jobs,
                    reportable_job_ids={"b", "c", "f"},
                    source_snapshot=source_snapshot(),
                )
                report_id = f"rpt_{code}"
                evidence = [{
                    "evidence_id": f"ev_{code}",
                    "evidence_type": "review_quote",
                    "review_entity_id": f"re_{code}",
                    "review_id": f"review-{code}",
                    "channel": "synthetic",
                    "brand": "Fixture",
                    "model": f"PW-{code}",
                    "market_region": "North America",
                    "market_region_evidence": "synthetic_fixture",
                    "display_site": "example.test",
                    "source_site": "example.test",
                    "rating": 5,
                    "submission_date": "2026-08-27",
                    "kind": "energy:scope",
                    "label": scope["energy"]["label"],
                    "polarity": "positive",
                    "quote": f"Synthetic {code} pressure washer evidence.",
                    "matched_text": code,
                    "source_url": "https://example.test/review",
                }]
                groups = {
                    "evg_energy": {
                        "path": f"energy.{code}",
                        "label": scope["energy"]["label"],
                        "evidence_ids": [f"ev_{code}"],
                        "representative_evidence_ids": [f"ev_{code}"],
                    }
                }
                repository.store(
                    report_id,
                    evidence,
                    groups,
                    expected_manifest=evidence_manifest(evidence, groups),
                )
                page = repository.query(report_id, "evg_energy")
                self.assertEqual(page["page"]["total_evidence_rows"], 1)
                self.assertEqual(page["items"][0]["matched_text"], code)
                self.assertEqual(
                    scope["energy_selection"]["source_snapshot"],
                    source_snapshot(),
                )

    @unittest.skipIf(Draft202012Validator is None, "jsonschema unavailable")
    def test_report_scope_schema_validates_new_energy_audit_and_old_reports(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "report-v1.schema.json"
        )
        scope_schema = json.loads(schema_path.read_text(encoding="utf-8"))[
            "properties"
        ]["scope"]
        validator = Draft202012Validator(scope_schema)
        governed = resolve_controlled_energy_scope(
            {
                "category_id": "pressure_washer",
                "categories": ["Pressure Washer"],
                "job_ids": ["b"],
                "energy_type": "battery",
            },
            [self.battery],
            reportable_job_ids={"b"},
            source_snapshot=source_snapshot(),
        )
        validator.validate(governed)
        validator.validate({"categories": ["Pressure Washer"]})
        invalid = json.loads(json.dumps(governed))
        invalid["energy_selection"]["source_snapshot"]["snapshot_id"] = "filtered"
        self.assertTrue(list(validator.iter_errors(invalid)))

    def test_missing_or_mismatched_energy_selection_fails_closed(self) -> None:
        with self.assertRaisesRegex(ControlledEnergyError, "必须且只能选择"):
            resolve_controlled_energy_scope(
                {"category_id": "pressure_washer", "job_ids": ["b"]},
                [self.battery],
                source_snapshot=source_snapshot(),
            )
        with self.assertRaisesRegex(ControlledEnergyError, "全量 Snapshot"):
            resolve_controlled_energy_scope(
                {
                    "category_id": "pressure_washer",
                    "job_ids": ["b"],
                    "energy_type": "battery",
                },
                [self.battery],
                source_snapshot={},
            )

    @unittest.skipIf(TestClient is None, "FastAPI runtime dependency unavailable")
    def test_create_api_resolves_authoritative_energy_and_rejects_forgery(self) -> None:
        jobs = [self.battery, self.corded, self.fuel]
        tasks = [
            {**row, "category_id": "pressure_washer"}
            for row in jobs
        ]
        source_catalog = {
            "tasks": tasks,
            "source_snapshot": {
                "snapshot_id": "prod_v2_" + "2" * 24,
                "schema_version": "voxquarry-reporting-snapshot/v2",
                "content_sha256": "sha256:" + "8" * 64,
            },
            "categories": ["Pressure Washer"],
            "brands": ["EGO"],
            "channels": [],
            "models": [],
            "market_regions": [],
            "defaults": {"scope": {}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            with patch(
                "reporting.app.build_catalog_payload", return_value=source_catalog
            ):
                app = create_app(snapshot, root / "var")
            app.state.reporting.start_job = lambda _job_id: None
            payload = {
                "scope": {
                    "category_id": "pressure_washer",
                    "categories": ["Pressure Washer"],
                    "job_ids": ["b"],
                    "energy_type": "battery",
                    "energy": {"energy_type": "fuel"},
                    "energy_refs": [{"job_id": "forged"}],
                }
            }
            with (
                patch("reporting.app.load_jobs", return_value=jobs),
                patch("reporting.app.catalog", return_value=source_catalog) as read_catalog,
                patch("reporting.app.validate_scope_job_ids") as validate_selection,
                TestClient(app) as client,
            ):
                response = client.post("/reporting/api/v1/reports", json=payload)
                self.assertEqual(response.status_code, 200)
                read_catalog.assert_called_once()
                self.assertIs(validate_selection.call_args.kwargs["source_jobs"], jobs)
                self.assertIs(validate_selection.call_args.kwargs["catalog_payload"], source_catalog)
                stored = app.state.reporting.store.get_job(response.json()["job_id"])
                self.assertEqual(stored["request"]["scope"]["energy_type"], "battery")
                self.assertEqual(
                    stored["request"]["scope"]["energy_refs"][0]["job_id"], "b"
                )
                self.assertEqual(
                    stored["request"]["scope"]["energy_selection"]["source_snapshot"]["snapshot_id"],
                    "prod_v2_" + "2" * 24,
                )

                missing = client.post(
                    "/reporting/api/v1/reports",
                    json={
                        "scope": {
                            "category_id": "pressure_washer",
                            "categories": ["Pressure Washer"],
                            "job_ids": ["b"],
                        }
                    },
                )
                self.assertEqual(missing.status_code, 422)

                mismatch = client.post(
                    "/reporting/api/v1/reports",
                    json={
                        "scope": {
                            "category_id": "pressure_washer",
                            "categories": ["Pressure Washer"],
                            "job_ids": ["b"],
                            "energy_type": "fuel",
                        }
                    },
                )
                self.assertEqual(mismatch.status_code, 422)
        with self.assertRaisesRegex(ControlledEnergyError, "不属于所选能源"):
            resolve_controlled_energy_scope(
                {
                    "category_id": "pressure_washer",
                    "job_ids": ["b"],
                    "energy_type": "fuel",
                },
                [self.battery],
                source_snapshot=source_snapshot(),
            )
        with self.assertRaisesRegex(ControlledEnergyError, "仅适用于"):
            resolve_controlled_energy_scope(
                {
                    "category_id": "leaf_blower",
                    "job_ids": ["b"],
                    "energy_type": "battery",
                },
                [self.battery],
            )


if __name__ == "__main__":
    unittest.main()
