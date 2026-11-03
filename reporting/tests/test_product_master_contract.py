from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from argparse import Namespace
from pathlib import Path
from unittest import mock

from reporting.product_master import (
    HASH_ALGORITHM,
    ProductMasterError,
    canonical_json,
    load_product_master,
    resolve_voltage_platform_scope,
)
from reporting.source import catalog, load_jobs
from scripts.build_reporting_snapshot_v2 import build_snapshot


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def record(
    *,
    model_id: str,
    brand_id: str,
    brand: str,
    platform_id: str,
    voltage: str = "18V",
    category: str = "string_trimmer",
    eligible: bool = True,
    reason: str | None = None,
) -> dict:
    row = {
        "model_id": model_id,
        "entity_version": 3,
        "product_kind": "single_product",
        "category": {
            "category_id": f"cat_{category}",
            "code": category,
            "name": "String Trimmer" if category == "string_trimmer" else "Cross-category Bundle",
            "name_zh": "打草机" if category == "string_trimmer" else "跨品类组套",
        },
        "brand": {"brand_id": brand_id, "canonical_name": brand},
        "model_number": model_id[-4:].upper(),
        "product_name": None,
        "energy_type": "DC",
        "voltage_platform": {
            "voltage_platform_id": platform_id,
            "brand_id": brand_id,
            "canonical_voltage": voltage,
            "voltage_value": int(voltage.removesuffix("V")),
            "voltage_unit": "V",
            "identity_version": "brand-voltage/v1",
        },
        "voltage_platform_null_reason": None,
        "bundle": None,
        "analysis_eligibility": {
            "eligible": eligible,
            "reason": reason,
            "policy_version": "retail-analysis-eligibility/v1",
        },
        "review_status": "approved",
        "reviewed_at": "2026-08-14T01:00:00+00:00",
    }
    row["record_snapshot_hash"] = "sha256:" + digest(row)
    return row


def projection_payload(records: list[dict]) -> dict:
    ordered = sorted(records, key=lambda row: (
        row["category"]["code"],
        row["brand"]["canonical_name"].casefold(),
        row["model_number"].casefold(),
        row["model_id"],
    ))
    scope = {
        "review_status": ["approved", "published"],
        "category_code": None,
        "model_ids": [],
        "voltage_platform_ids": [],
    }
    payload = {
        "schema_version": "retail-product-master/v2",
        "snapshot_id": "",
        "snapshot_generated_at": "2026-08-14T01:00:00+00:00",
        "hash_algorithm": HASH_ALGORITHM,
        "authority": "chengmu",
        "scope": scope,
        "ordering": "category.code, brand.canonical_name(casefold), model_number(casefold), model_id",
        "pagination": {"mode": "complete", "total_records": len(ordered)},
        "records": ordered,
    }
    payload["snapshot_id"] = "pms_" + digest({
        "schema_version": payload["schema_version"],
        "authority": payload["authority"],
        "scope": scope,
        "records": ordered,
    })
    return payload


def task_binding(
    row: dict,
    *,
    job_id: str = "job-1",
    snapshot_id: str = "pms_" + "a" * 64,
) -> dict:
    """Mirror Connector 8e9aea's persisted jobs row contract."""
    return {
        "id": job_id,
        "product_master_schema_version": "retail-product-master/v2",
        "product_master_snapshot_id": snapshot_id,
        "product_master_model_id": row["model_id"],
        "product_master_entity_version": row["entity_version"],
        "product_master_record_snapshot_hash": row["record_snapshot_hash"],
        "product_master_record_json": json.dumps(
            row, ensure_ascii=False, separators=(",", ":")
        ),
    }


class ProductMasterContractTests(unittest.TestCase):
    def write_projection(self, root: Path, records: list[dict]) -> dict:
        payload = projection_payload(records)
        (root / "product-master-v2.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload

    def test_verified_projection_keeps_same_voltage_brands_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                record(
                    model_id="pvr_11111111111111111111",
                    brand_id="brd_11111111111111111111",
                    brand="Milwaukee",
                    platform_id="vpf_11111111111111111111",
                ),
                record(
                    model_id="pvr_22222222222222222222",
                    brand_id="brd_22222222222222222222",
                    brand="DEWALT",
                    platform_id="vpf_22222222222222222222",
                ),
            ]
            self.write_projection(root, records)
            projection = load_product_master(root)
            assert projection is not None
            platforms = projection.platform_catalog()
            self.assertEqual(len(platforms), 2)
            self.assertEqual(
                {item["display_name"] for item in platforms},
                {"Milwaukee 18V", "DEWALT 18V"},
            )

    def test_canonical_hash_matches_chengmu_domain_verified_example(self) -> None:
        official_record = {
            "model_id": "pvr_84a652f682227528adc1",
            "entity_version": 2,
            "product_kind": "single_product",
            "category": {
                "category_id": "cat_wet_dry_vacuum",
                "code": "wet_dry_vacuum",
                "name": "Wet/Dry Vacuum",
                "name_zh": "干湿两用吸尘器",
            },
            "brand": {
                "brand_id": "brd_b8204d319dc0125cad19",
                "canonical_name": "RYOBI",
            },
            "model_number": "PCL733B",
            "product_name": "18V ONE+ 6 Gal. Wet/Dry Vacuum",
            "energy_type": "DC",
            "voltage_platform": {
                "voltage_platform_id": "vpf_4412bee74375a6014f9a",
                "brand_id": "brd_b8204d319dc0125cad19",
                "canonical_voltage": "18V",
                "voltage_value": 18.0,
                "voltage_unit": "V",
                "identity_version": "brand-voltage/v1",
            },
            "voltage_platform_null_reason": None,
            "bundle": None,
            "analysis_eligibility": {
                "eligible": True,
                "reason": None,
                "policy_version": "retail-analysis-eligibility/v1",
            },
            "review_status": "approved",
            "reviewed_at": "2026-08-14T01:00:00+00:00",
        }
        self.assertEqual(
            "sha256:" + digest(official_record),
            "sha256:25b9e4224104d8135f31ef04b913f58e5a1418c3b094e6382e0ba0c3ecf8b3a1",
        )
        official_record["record_snapshot_hash"] = "sha256:" + digest(
            official_record
        )
        scope = {
            "review_status": ["approved", "published"],
            "category_code": "wet_dry_vacuum",
            "model_ids": [],
            "voltage_platform_ids": [],
        }
        self.assertEqual(
            "pms_" + digest({
                "schema_version": "retail-product-master/v2",
                "authority": "chengmu",
                "scope": scope,
                "records": [official_record],
            }),
            "pms_1bc9c639a870456d11d60c3e3084a0f50165cb40eb085119ebeb73c0fca56938",
        )

    def test_catalog_keeps_wotu_tasks_when_optional_chengmu_binding_disagrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = record(
                model_id="pvr_11111111111111111111",
                brand_id="brd_11111111111111111111",
                brand="Milwaukee",
                platform_id="vpf_11111111111111111111",
            )
            self.write_projection(root, [row])
            database = (
                root / "data" / "backups"
                / "jobs-consistent-snapshot-test.sqlite3"
            )
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE jobs (id TEXT, status TEXT, channel TEXT, "
                    "brand TEXT, category TEXT, model TEXT, "
                    "target_url TEXT, created_at TEXT, finished_at TEXT, "
                    "superseded_by TEXT, product_master_schema_version TEXT, "
                    "product_master_snapshot_id TEXT, product_master_model_id TEXT, "
                    "product_master_entity_version INTEGER, "
                    "product_master_record_snapshot_hash TEXT, "
                    "product_master_record_json TEXT)"
                )
                for job_id, category, model in (
                    ("job-1", "String Trimmer", "M18"),
                    ("job-mismatch", "Snow Blower", "M18-BLOWER"),
                ):
                    binding = task_binding(row, job_id=job_id)
                    connection.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id, "completed", "homedepot", "Milwaukee",
                            category, model, f"https://example.test/{job_id}",
                            "2026-08-01", "2026-08-02", None,
                            binding["product_master_schema_version"],
                            binding["product_master_snapshot_id"],
                            binding["product_master_model_id"],
                            binding["product_master_entity_version"],
                            binding["product_master_record_snapshot_hash"],
                            binding["product_master_record_json"],
                        ),
                    )
                connection.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "job-legacy", "completed", "homedepot", "Milwaukee",
                        "String Trimmer", "M18-LEGACY",
                        "https://example.test/job-legacy", "2026-08-01",
                        "2026-08-02", None, None, None, None, None, None, None,
                    ),
                )
                for column in (
                    "voltage_platform_schema_version", "voltage_platform_status",
                    "voltage_platform_key", "voltage_platform_brand",
                    "voltage_platform_voltage", "voltage_platform_label",
                    "voltage_platform_evidence_source", "voltage_platform_null_reason",
                    "product_observation_hash",
                ):
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
                connection.execute(
                    "UPDATE jobs SET voltage_platform_schema_version=?, "
                    "voltage_platform_status='known', voltage_platform_key='milwaukee|18V', "
                    "voltage_platform_brand='Milwaukee', voltage_platform_voltage='18V', "
                    "voltage_platform_label='Milwaukee 18V', "
                    "voltage_platform_evidence_source='source_observation', "
                    "product_observation_hash=? WHERE id='job-1'",
                    ("voxquarry.voltage-platform/v1", "sha256:" + "1" * 64),
                )
                connection.execute(
                    "UPDATE jobs SET voltage_platform_schema_version=?, "
                    "voltage_platform_status='unknown', voltage_platform_brand='Milwaukee', "
                    "voltage_platform_evidence_source='source_observation', "
                    "voltage_platform_null_reason='voltage_unknown', "
                    "product_observation_hash=? WHERE id='job-mismatch'",
                    ("voxquarry.voltage-platform/v1", "sha256:" + "2" * 64),
                )
            for job_id in ("job-1", "job-mismatch", "job-legacy"):
                run = root / "data" / "runs" / job_id / "homedepot"
                run.mkdir(parents=True)
                product_id = {
                    "job-1": "M18",
                    "job-mismatch": "M18-BLOWER",
                    "job-legacy": "M18-LEGACY",
                }[job_id]
                (run / "validation_report.json").write_text(
                    json.dumps({
                        "status": "COMPLETE",
                        "product": {"id": product_id},
                        "source_total": 1,
                        "unique_reviews": 1,
                    }),
                    encoding="utf-8",
                )
                (run / "reviews.csv").write_text(
                    f"channel,product_id,review_id\nhomedepot,{product_id},review-1\n",
                    encoding="utf-8",
                )
            payload = catalog(root)
            self.assertEqual(payload["product_master_contract"]["status"], "verified")
            self.assertEqual(
                [task["id"] for task in payload["tasks"]],
                ["job-1", "job-mismatch", "job-legacy"],
            )
            self.assertEqual(payload["tasks"][0]["model_id"], row["model_id"])
            self.assertEqual(
                payload["tasks"][0]["product_master_binding_status"],
                "verified",
            )
            self.assertEqual(
                payload["tasks"][2]["product_master_binding_status"],
                "legacy_unbound",
            )
            self.assertEqual(
                payload["tasks"][0]["voltage_platform_status"],
                "known",
            )
            self.assertEqual(
                payload["voltage_platforms"][0]["display_name"],
                "Milwaukee 18V",
            )
            self.assertEqual(
                payload["voltage_platform_contract"],
                {
                    "status": "available",
                    "schema_version": "voxquarry.voltage-platform/v1",
                    "authority": "wotu_task_fact",
                    "known_tasks": 1,
                    "unknown_tasks": 2,
                    "non_dc_tasks": 0,
                    "unknown_option_available": True,
                },
            )
            self.assertEqual(
                payload["product_master_rejections"],
                [{
                    "job_id": "job-mismatch",
                    "model_id": row["model_id"],
                    "reason": "master_category_mismatch",
                    "job_category_id": "snow_blower",
                    "master_category_id": "string_trimmer",
                    "policy_version": "retail-analysis-eligibility/v1",
                }],
            )

    def test_scope_freezes_projection_models_and_platform_display(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = record(
                model_id="pvr_11111111111111111111",
                brand_id="brd_11111111111111111111",
                brand="Milwaukee",
                platform_id="vpf_11111111111111111111",
            )
            payload = self.write_projection(root, [row])
            projection = load_product_master(root)
            database = (
                root / "data" / "backups"
                / "jobs-consistent-snapshot-binding.sqlite3"
            )
            database.parent.mkdir(parents=True)
            binding = task_binding(
                row,
                snapshot_id="pms_" + "b" * 64,
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE jobs (id TEXT PRIMARY KEY, "
                    "product_master_schema_version TEXT, "
                    "product_master_snapshot_id TEXT, "
                    "product_master_model_id TEXT, "
                    "product_master_entity_version INTEGER, "
                    "product_master_record_snapshot_hash TEXT, "
                    "product_master_record_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                    tuple(binding[field] for field in (
                        "id",
                        "product_master_schema_version",
                        "product_master_snapshot_id",
                        "product_master_model_id",
                        "product_master_entity_version",
                        "product_master_record_snapshot_hash",
                        "product_master_record_json",
                    )),
                )
            resolved = resolve_voltage_platform_scope(
                projection,
                {
                    "category_id": "string_trimmer",
                    "job_ids": ["job-1"],
                    "voltage_platform_ids": ["vpf_11111111111111111111"],
                },
                load_jobs(root),
            )
            self.assertEqual(
                resolved["product_master"]["snapshot_id"],
                payload["snapshot_id"],
            )
            self.assertEqual(
                resolved["model_refs"],
                [{
                    "job_id": "job-1",
                    "product_master_schema_version": "retail-product-master/v2",
                    "product_master_snapshot_id": "pms_" + "b" * 64,
                    "product_master_model_id": row["model_id"],
                    "product_master_entity_version": 3,
                    "product_master_record_snapshot_hash": row["record_snapshot_hash"],
                }],
            )
            self.assertNotEqual(
                resolved["model_refs"][0]["product_master_snapshot_id"],
                resolved["product_master"]["snapshot_id"],
            )
            self.assertEqual(
                resolved["voltage_platforms"][0]["display_name"],
                "Milwaukee 18V",
            )

    def test_task_binding_drift_and_record_json_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = record(
                model_id="pvr_11111111111111111111",
                brand_id="brd_11111111111111111111",
                brand="Milwaukee",
                platform_id="vpf_11111111111111111111",
            )
            self.write_projection(root, [row])
            projection = load_product_master(root)
            assert projection is not None
            scope = {
                "category_id": "string_trimmer",
                "job_ids": ["job-1"],
                "voltage_platform_ids": ["vpf_11111111111111111111"],
            }
            entity_drift = task_binding(row)
            entity_drift["product_master_entity_version"] = 4
            with self.assertRaisesRegex(ProductMasterError, "版本已漂移") as drift:
                resolve_voltage_platform_scope(projection, scope, [entity_drift])
            self.assertEqual(drift.exception.code, "product_master_binding_drift")

            hash_drift = task_binding(row)
            hash_drift["product_master_record_snapshot_hash"] = "sha256:" + "f" * 64
            with self.assertRaisesRegex(ProductMasterError, "版本已漂移"):
                resolve_voltage_platform_scope(projection, scope, [hash_drift])

            tampered = task_binding(row)
            record_evidence = json.loads(tampered["product_master_record_json"])
            record_evidence["brand"]["canonical_name"] = "Changed"
            tampered["product_master_record_json"] = json.dumps(record_evidence)
            with self.assertRaisesRegex(ProductMasterError, "record_snapshot_hash mismatch"):
                resolve_voltage_platform_scope(projection, scope, [tampered])

    def test_cross_category_task_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = record(
                model_id="pvr_33333333333333333333",
                brand_id="brd_11111111111111111111",
                brand="Milwaukee",
                platform_id="vpf_11111111111111111111",
                category="cross_category_bundle",
                eligible=False,
                reason="cross_category_bundle",
            )
            row["product_kind"] = "bundle"
            row["bundle"] = {
                "scope": "cross_category",
                "primary_model_id": None,
                "members": [],
            }
            row["record_snapshot_hash"] = "sha256:" + digest({
                key: value for key, value in row.items()
                if key != "record_snapshot_hash"
            })
            self.write_projection(root, [row])
            projection = load_product_master(root)
            with self.assertRaisesRegex(ProductMasterError, "不具备量化报告资格"):
                resolve_voltage_platform_scope(
                    projection,
                    {
                        "category_id": "cross_category_bundle",
                        "job_ids": ["job-cross"],
                        "voltage_platform_ids": ["vpf_11111111111111111111"],
                    },
                    [task_binding(row, job_id="job-cross")],
                )

    def test_missing_projection_binding_and_hash_mismatch_fail_closed(self) -> None:
        with self.assertRaisesRegex(ProductMasterError, "正式投影") as unavailable:
            resolve_voltage_platform_scope(
                None,
                {
                    "category_id": "string_trimmer",
                    "job_ids": ["job-1"],
                    "voltage_platform_ids": ["vpf_11111111111111111111"],
                },
                [],
            )
        self.assertEqual(unavailable.exception.code, "product_master_unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = record(
                model_id="pvr_11111111111111111111",
                brand_id="brd_11111111111111111111",
                brand="Milwaukee",
                platform_id="vpf_11111111111111111111",
            )
            payload = self.write_projection(root, [row])
            payload["records"][0]["brand"]["canonical_name"] = "Changed"
            (root / "product-master-v2.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ProductMasterError, "record_snapshot_hash mismatch"):
                load_product_master(root)

    def test_unknown_platform_unbound_job_and_cross_bundle_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = record(
                model_id="pvr_11111111111111111111",
                brand_id="brd_11111111111111111111",
                brand="Milwaukee",
                platform_id="vpf_11111111111111111111",
            )
            self.write_projection(root, [row])
            projection = load_product_master(root)
            base_scope = {
                "category_id": "string_trimmer",
                "job_ids": ["job-1"],
                "voltage_platform_ids": ["vpf_99999999999999999999"],
            }
            with self.assertRaisesRegex(ProductMasterError, "未知 voltage_platform_id") as unknown:
                resolve_voltage_platform_scope(
                    projection, base_scope,
                    [task_binding(row)],
                )
            self.assertEqual(unknown.exception.code, "master_identity_not_found")
            self.assertEqual(unknown.exception.field, "voltage_platform_id")
            base_scope["voltage_platform_ids"] = ["vpf_11111111111111111111"]
            with self.assertRaisesRegex(ProductMasterError, "缺少数据采集 product_master_\*"):
                resolve_voltage_platform_scope(
                    projection, base_scope, [{"id": "job-1"}],
                )

    def test_builder_freezes_verified_projection_into_immutable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            database = source / "data" / "jobs.sqlite3"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
            projection_path = root / "chengmu-product-master.json"
            row = record(
                model_id="pvr_11111111111111111111",
                brand_id="brd_11111111111111111111",
                brand="Milwaukee",
                platform_id="vpf_11111111111111111111",
            )
            payload = projection_payload([row])
            projection_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

            def backup(_source: Path, target: Path) -> int:
                target.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(target) as connection:
                    connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
                return 0

            output = root / "snapshot"
            with (
                mock.patch(
                    "scripts.build_reporting_snapshot_v2.backup_database",
                    side_effect=backup,
                ),
                mock.patch(
                    "scripts.build_reporting_snapshot_v2.copy_reportable_artifacts",
                    return_value=([], []),
                ),
                mock.patch(
                    "scripts.build_reporting_snapshot_v2.validate_snapshot",
                    return_value={"reportable_jobs": 0},
                ),
            ):
                manifest = build_snapshot(Namespace(
                    source_root=source,
                    output_root=output,
                    connector_commit="connector-test",
                    reporting_commit="reporting-test",
                    product_master_projection=projection_path,
                ))
            self.assertEqual(
                manifest["product_master"]["snapshot_id"], payload["snapshot_id"]
            )
            self.assertEqual(
                manifest["contracts"]["product_master"],
                "retail-product-master/v2",
            )
            self.assertEqual(
                load_product_master(output).snapshot_id, payload["snapshot_id"]
            )
            self.assertEqual(
                (output / "product-master-v2.json").stat().st_mode & 0o222, 0
            )

    def test_final_chengmu_invariants_fail_closed(self) -> None:
        member = {
            "role": "accessory",
            "member_model_id": None,
            "member_label": "Accessory",
            "resolution_status": "unresolved",
            "quantity": 1,
            "evidence": {
                "source_url": None,
                "source_type": None,
                "source_snapshot_hash": None,
                "observed_at": None,
            },
        }
        same_category_bundle = {
            "scope": "same_category",
            "primary_model_id": None,
            "members": [],
        }
        cross_category = {
            "category_id": "cat_cross_category_bundle",
            "code": "cross_category_bundle",
            "name": "Cross-category Bundle",
            "name_zh": "跨品类组套",
        }
        ineligible_cross = {
            "eligible": False,
            "reason": "cross_category_bundle",
            "policy_version": "retail-analysis-eligibility/v1",
        }
        invalid_patches = {
            "eligible_true_with_reason": {
                "analysis_eligibility": {
                    "eligible": True,
                    "reason": "cross_category_bundle",
                    "policy_version": "retail-analysis-eligibility/v1",
                },
            },
            "ineligible_without_reason": {
                "analysis_eligibility": {
                    "eligible": False,
                    "reason": None,
                    "policy_version": "retail-analysis-eligibility/v1",
                },
            },
            "single_product_with_bundle": {"bundle": same_category_bundle},
            "single_product_ineligible": {
                "analysis_eligibility": {
                    "eligible": False,
                    "reason": "bundle_primary_unresolved",
                    "policy_version": "retail-analysis-eligibility/v1",
                },
            },
            "cross_category_code_with_same_category_scope": {
                "product_kind": "bundle",
                "category": cross_category,
                "bundle": same_category_bundle,
            },
            "cross_category_scope_with_regular_category": {
                "product_kind": "bundle",
                "bundle": {**same_category_bundle, "scope": "cross_category"},
                "analysis_eligibility": ineligible_cross,
            },
            "cross_category_bundle_marked_eligible": {
                "product_kind": "bundle",
                "category": cross_category,
                "bundle": {**same_category_bundle, "scope": "cross_category"},
            },
            "resolved_member_without_model_id": {
                "product_kind": "bundle",
                "bundle": {
                    **same_category_bundle,
                    "members": [{**member, "resolution_status": "resolved"}],
                },
            },
            "unresolved_member_with_model_id": {
                "product_kind": "bundle",
                "bundle": {
                    **same_category_bundle,
                    "members": [{
                        **member,
                        "member_model_id": "pvr_11111111111111111111",
                    }],
                },
            },
            "unresolved_member_without_label": {
                "product_kind": "bundle",
                "bundle": {
                    **same_category_bundle,
                    "members": [{**member, "member_label": "   "}],
                },
            },
        }

        def merge(target: dict, patch: dict) -> None:
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = deepcopy(value)

        for name, patch in invalid_patches.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                row = record(
                    model_id="pvr_11111111111111111111",
                    brand_id="brd_11111111111111111111",
                    brand="Milwaukee",
                    platform_id="vpf_11111111111111111111",
                )
                merge(row, patch)
                row["record_snapshot_hash"] = "sha256:" + digest({
                    key: value
                    for key, value in row.items()
                    if key != "record_snapshot_hash"
                })
                payload = projection_payload([row])
                (root / "product-master-v2.json").write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                with self.assertRaises(ProductMasterError):
                    load_product_master(root)


if __name__ == "__main__":
    unittest.main()
