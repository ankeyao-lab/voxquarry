from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from retail_landscape.bootstrap import (
    bootstrap_review_results,
    bootstrap_snapshot,
    bootstrap_verified_wdv_facts,
    energy_and_voltage,
    trustworthy_snapshot_model,
)
from retail_landscape.category_registry import finalize_category_manifest
from retail_landscape.field_catalog import fields_for, has_field_profile, uncovered_aliases
from retail_landscape.store import (
    ReadOnlyStorePreconditionError,
    RetailStore,
    content_hash,
    normalize_voltage,
    product_master_record_invariant_errors,
)
from scripts.build_retail_product_master_acceptance_fixture import (
    build_acceptance_fixture,
    synthetic_57_category_manifest,
)
from scripts.initialize_retail_product_master_store import initialize_with_audit
from scripts.import_retail_category_catalog import import_catalog_with_audit
from scripts.prepare_retail_category_catalog_manifest import (
    prepare_category_catalog_manifest,
)
from scripts.restore_retail_product_master_store import restore_database


FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RetailLandscapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="retail-landscape-")
        self.root = Path(self.temp.name)
        self.store = RetailStore(self.root / "retail.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_excel_field_inventory_is_fully_mapped(self) -> None:
        inventory = json.loads((FIXTURES / "excel-field-inventory-v1.json").read_text(encoding="utf-8"))
        for category_code, aliases in inventory.items():
            self.assertEqual(uncovered_aliases(category_code, set(aliases)), set(), category_code)
        self.assertGreaterEqual(len(fields_for("wet_dry_vacuum")), 40)
        self.assertGreaterEqual(len(fields_for("fan")), 50)
        self.assertGreaterEqual(len(fields_for("pressure_washer")), 65)

    def test_only_wet_dry_vacuum_is_enabled_for_mvp(self) -> None:
        categories = {item["code"]: item for item in self.store.list_categories()}
        self.assertEqual(categories["wet_dry_vacuum"]["enabled"], 1)
        self.assertEqual(categories["fan"]["enabled"], 0)
        self.assertEqual(categories["light"]["enabled"], 0)

    def test_snapshot_bootstrap_links_all_wdv_tasks_to_stable_model_ids(self) -> None:
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(json.dumps({
            "generated_at": "2026-08-05T02:57:09+00:00",
            "jobs": [
                {"id": "one", "channel": "ryobi", "status": "completed", "target_url": "https://www.ryobitools.com/products/PCL733B", "target_key": "one", "brand": "RYOBI", "model": "PCL733B", "category": "Wet/Dry Vacuum", "market_region": "North America", "active_result_status": "completed", "last_attempt_status": "completed"},
                {"id": "two", "channel": "homedepot", "status": "partial_success", "target_url": "https://www.homedepot.com/p/PCL733B/2", "target_key": "two", "brand": "RYOBI", "model": "PCL733B", "category": "Wet/Dry Vacuum", "market_region": "North America", "active_result_status": "partial", "last_attempt_status": "partial"},
                {"id": "fan", "channel": "ryobi", "status": "completed", "target_url": "https://example.test/PCL811B", "target_key": "fan", "brand": "RYOBI", "model": "PCL811B", "category": "Fan", "market_region": "North America", "active_result_status": "completed", "last_attempt_status": "completed"},
            ],
        }), encoding="utf-8")
        self.assertEqual(bootstrap_snapshot(self.store, snapshot), {"products": 1, "listings": 2})
        products = self.store.list_products("wet_dry_vacuum")
        self.assertEqual(len(products), 1)
        self.assertTrue(products[0]["id"].startswith("pvr_"))
        self.assertIsNone(products[0]["product_name"])
        self.assertEqual(products[0]["task_count"], 2)
        self.assertEqual(products[0]["energy_type"], "DC")
        self.assertEqual(products[0]["voltage_platform"], "18V")
        self.assertTrue(all(item["source_type"] == "connector_task_listing" for item in products[0]["listings"]))
        self.assertEqual(self.store.list_products("fan"), [])

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_sanitized_review_result_handoff_updates_existing_task_only(self) -> None:
        task_snapshot = self.root / "tasks.json"
        task_snapshot.write_text(json.dumps({
            "generated_at": "2026-08-05T00:00:00+00:00",
            "jobs": [{
                "id": "count-task", "channel": "ryobi", "status": "completed",
                "target_url": "https://www.ryobitools.com/products/PCL733B",
                "target_key": "count", "brand": "RYOBI", "model": "PCL733B",
                "category": "Wet/Dry Vacuum", "market_region": "North America",
            }],
        }), encoding="utf-8")
        bootstrap_snapshot(self.store, task_snapshot)
        result_snapshot = self.root / "results.json"
        result_snapshot.write_text(json.dumps({
            "schema_version": "voxquarry.retail-review-count-handoff/v1",
            "generated_at": "2026-08-06T00:00:00+00:00",
            "records": [
                {"task_id": "count-task", "unique_reviews": 321, "source_total": 321, "result_status": "COMPLETE", "finished_at": "2026-08-05T23:00:00+00:00"},
                {"task_id": "not-linked", "unique_reviews": 4, "source_total": 4, "result_status": "COMPLETE"},
            ],
        }), encoding="utf-8")
        result = bootstrap_review_results(self.store, result_snapshot)
        product = self.store.list_products("wet_dry_vacuum")[0]
        self.assertEqual(result, {"updated": 1, "skipped": 0, "unmatched": 1})
        self.assertEqual(product["review_count"], 321)
        self.assertEqual(product["review_count_status"], "complete")

    def test_energy_and_voltage_are_normalized_without_ac_platforms(self) -> None:
        self.assertEqual(normalize_voltage("56 volt", "DC"), "56V")
        self.assertIsNone(normalize_voltage("120V", "AC"))
        self.assertEqual(energy_and_voltage("Test", "AC-1", "https://example.test/120-volt-vac"), ("AC", None))
        with self.assertRaisesRegex(ValueError, "标准电压"):
            normalize_voltage("high", "DC")

    def test_model_crud_supports_regions_retailers_notes_listings_and_numeric_specs(self) -> None:
        product = self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "QA", "model_number": "MODEL-1",
            "energy_type": "DC", "voltage_platform": " 18 volts ", "notes": "自由备注",
            "source_url": "https://www.ryobitools.com/products/model-1", "source_type": "official_product_page",
            "source_snapshot_hash": "sha256:test", "observed_at": "2026-08-05T00:00:00+00:00",
            "market_region_ids": ["mr_na", "mr_eu"],
            "retailer_ids": ["rtl_ryobi"],
            "listings": [{"retailer": "待识别", "online_url": "https://www.ryobitools.com/products/model-1", "review_status": "pending_review"}],
            "specs": {"dust_capacity": {"raw_value": "2", "normalized_value": 2, "unit_id": "unit_gal", "review_status": "pending_review", "source_url": "https://www.ryobitools.com/products/model-1", "source_type": "official_product_page", "source_snapshot_hash": "sha256:fact", "observed_at": "2026-08-05T00:00:00+00:00"}},
        })
        self.assertEqual(product["voltage_platform"], "18V")
        self.assertEqual(product["notes"], "自由备注")
        self.assertEqual({item["id"] for item in product["markets"]}, {"mr_na", "mr_eu"})
        self.assertEqual(product["listings"][0]["retailer_id"], "rtl_ryobi")
        self.assertEqual(product["specs"]["dust_capacity"]["numeric_value"], 2.0)
        updated = self.store.update_product(product["id"], {"energy_type": "AC", "voltage_platform": "120V", "notes": "已修改", "listings": []})
        self.assertIsNone(updated["voltage_platform"])
        self.assertEqual(updated["notes"], "已修改")
        self.assertEqual(updated["listings"], [])

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_retail_price_validation_and_schema_defaults(self) -> None:
        with self.store.connect() as conn:
            price_columns = {
                row["name"]: dict(row)
                for row in conn.execute("PRAGMA table_info(price_observations)").fetchall()
            }
            listing_columns = {
                row["name"]: dict(row)
                for row in conn.execute("PRAGMA table_info(product_listings)").fetchall()
            }
            spec_columns = {
                row["name"]: dict(row)
                for row in conn.execute("PRAGMA table_info(spec_values)").fetchall()
            }
        self.assertEqual(price_columns["review_status"]["dflt_value"], "'draft'")
        self.assertIn("source_type", listing_columns)
        self.assertIn("source_type", spec_columns)
        base = {
            "category_code": "wet_dry_vacuum", "brand": "QA",
            "listings": [{"retailer": "Lowe's", "online_url": "https://lowes.com/price"}],
        }
        cases = [
            ({"normalized_amount": -1, "currency_code": "USD", "raw_value": "-1"}, "不能为负数"),
            ({"normalized_amount": 1, "currency_code": "US", "raw_value": "1"}, "三位 ISO"),
            ({"normalized_amount": 1, "currency_code": "USD", "raw_value": "1", "review_status": "published"}, "阶段 1"),
        ]
        for index, (price, message) in enumerate(cases):
            payload = json.loads(json.dumps(base))
            payload["model_number"] = f"BAD-PRICE-{index}"
            payload["listings"][0]["prices"] = [price]
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, message):
                self.store.create_product(payload)

    def test_matrix_specs_require_numeric_values_and_master_units(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须是数字"):
            self.store.create_product({"category_code": "wet_dry_vacuum", "brand": "QA", "model_number": "TEXT", "specs": {"dust_capacity": {"normalized_value": "two", "unit_id": "unit_gal"}}})
        with self.assertRaisesRegex(ValueError, "必须选择全局单位"):
            self.store.save_parameter("wet_dry_vacuum", {"label": "自定义参数", "value_type": "number"})

    def test_images_are_attachments_and_one_hero_is_enforced(self) -> None:
        product = self.store.create_product({"category_code": "wet_dry_vacuum", "brand": "QA", "model_number": "ASSET-1"})
        with self.assertRaisesRegex(ValueError, "附件接口"):
            self.store.update_product(product["id"], {"assets": [{"asset_role": "hero", "source_url": "https://example.test/hero.jpg"}]})
        product = self.store.add_attachment_asset(product["id"], storage_path=f"{product['id']}/one.webp", original_filename="one.webp", content_hash="sha256:one", file_size=1000, make_hero=True)
        product = self.store.add_attachment_asset(product["id"], storage_path=f"{product['id']}/two.webp", original_filename="two.webp", content_hash="sha256:two", file_size=1000, make_hero=True)
        self.assertEqual(sum(asset["asset_role"] == "hero" for asset in product["assets"]), 1)
        self.assertEqual(sum(asset["asset_role"] == "gallery" for asset in product["assets"]), 1)
        self.assertTrue(all(asset["source_snapshot_hash"] for asset in product["assets"]))

    def test_model_review_uses_identity_and_evidence_blockers_only(self) -> None:
        product = self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "QA",
            "model_number": "REVIEW-1", "energy_type": "AC",
            "source_url": "https://example.test/review-1",
            "source_type": "official_product_page",
            "source_snapshot_hash": "sha256:review-1",
            "observed_at": "2026-08-11T00:00:00+00:00",
        })
        self.assertEqual(product["review_blockers"], [])
        self.assertIn("hero_asset", product["enrichment_gaps"])
        self.assertIn("launch_region", product["enrichment_gaps"])
        reviewed = self.store.review_product(product["id"], approved=True, reviewer="qa")
        self.assertEqual(reviewed["review_status"], "approved")
        self.assertEqual(reviewed["review_blockers"], [])
        self.assertEqual(reviewed["review_decisions"][0]["decision"], "approve")
        reopened = self.store.review_product(product["id"], approved=False, reviewer="", note="补证据")
        self.assertEqual(reopened["review_status"], "pending_review")
        reopen_decision = next(item for item in reopened["review_decisions"] if item["decision"] == "reopen")
        self.assertEqual(reopen_decision["reviewer"], "local_preview")

        unsourced = self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "QA",
            "model_number": "REVIEW-UNSOURCED",
        })
        self.assertEqual(unsourced["review_blockers"], ["source_evidence"])
        with self.assertRaisesRegex(ValueError, "真实性或身份阻断"):
            self.store.review_product(unsourced["id"], approved=True)

        dc_without_voltage = self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "QA",
            "model_number": "REVIEW-DC", "energy_type": "DC",
            "source_url": "https://example.test/review-dc",
            "source_type": "official_product_page",
            "source_snapshot_hash": "sha256:review-dc",
            "observed_at": "2026-08-11T00:00:00+00:00",
        })
        self.assertEqual(dc_without_voltage["review_blockers"], [])
        self.assertIn("voltage_platform", dc_without_voltage["enrichment_gaps"])
        approved_dc = self.store.review_product(
            dc_without_voltage["id"], approved=True, reviewer="qa",
        )
        projection = self.store.product_master_projection(model_ids=[approved_dc["id"]])
        self.assertIsNone(projection["records"][0]["voltage_platform"])
        self.assertEqual(
            projection["records"][0]["voltage_platform_null_reason"],
            "voltage_unverified",
        )

        fact_without_type = self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "QA",
            "model_number": "REVIEW-FACT-SOURCE",
            "source_url": "https://example.test/review-fact",
            "source_type": "official_product_page",
            "source_snapshot_hash": "sha256:review-fact",
            "observed_at": "2026-08-11T00:00:00+00:00",
            "specs": {"dust_capacity": {
                "normalized_value": 2, "unit_id": "unit_gal",
                "source_url": "https://example.test/review-fact",
                "source_snapshot_hash": "sha256:review-fact-spec",
                "observed_at": "2026-08-11T00:00:00+00:00",
            }},
        })
        preview = self.store.review_preview(fact_without_type["id"])
        self.assertEqual(preview["review_blockers"], ["fact_evidence"])
        self.assertEqual(preview["review_blocker_details"]["fact_evidence"], [{
            "code": "dust_capacity", "label": "尘桶容量",
            "missing_fields": ["source_type"],
        }])

    def test_review_preview_and_bulk_review_are_model_level(self) -> None:
        first = self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "QA", "model_number": "BULK-1",
            "notes": "首个备注",
            "source_url": "https://example.test/bulk-1", "source_type": "official_product_page",
            "source_snapshot_hash": "sha256:bulk-1", "observed_at": "2026-08-11T00:00:00+00:00",
            "market_region_ids": ["mr_na"],
            "retailer_ids": ["rtl_homedepot"],
            "listings": [{
                "retailer_id": "rtl_homedepot", "retailer": "Home Depot",
                "market_region": "North America", "online_url": "https://www.homedepot.com/p/test",
                "source_type": "retailer_listing",
                "source_snapshot_hash": "sha256:bulk-listing-1",
                "observed_at": "2026-08-11T00:00:00+00:00",
            }],
        })
        second = self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "QA", "model_number": "BULK-2",
            "source_url": "https://example.test/bulk-2", "source_type": "official_product_page",
            "source_snapshot_hash": "sha256:bulk-2", "observed_at": "2026-08-11T00:00:00+00:00",
        })
        preview = self.store.review_preview(first["id"])
        self.assertTrue(any(item["field"] == "brand" for item in preview["new"]))
        additions = {item["field"]: item for item in preview["new"]}
        self.assertEqual(additions["markets"]["label"], "上市地区")
        self.assertEqual(additions["markets"]["after"], "北美")
        self.assertEqual(additions["retailers"]["after"], "Home Depot")
        self.assertIn("Home Depot · North America", additions["listings"]["after"])
        self.assertNotIn("{", additions["listings"]["after"])
        self.assertNotIn('"channel"', additions["listings"]["after"])
        updated = self.store.update_product(first["id"], {"notes": "修改后的备注"})
        self.assertEqual(updated["review_status"], "pending_review")
        preview = self.store.review_preview(first["id"])
        self.assertTrue(any(item["field"] == "notes" for item in preview["modified"]))
        self.assertEqual(preview["review_blockers"], [])
        self.assertIn("hero_asset", preview["enrichment_gaps"])
        result = self.store.bulk_review([first["id"], second["id"]], approved=True)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["review_blocker_count"], 0)
        self.assertTrue(all(item["review_status"] == "approved" for item in result["products"]))
        reopened = self.store.bulk_review([first["id"], second["id"]], approved=False)
        self.assertTrue(all(item["review_status"] == "pending_review" for item in reopened["products"]))

    def test_product_search_is_case_insensitive_and_matches_model_substrings(self) -> None:
        self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "DEWALT",
            "model_number": "DCV580H", "product_name": "20V MAX Wet Dry Vacuum",
        })
        self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "RIDGID",
            "model_number": "HD1200", "product_name": "12 Gallon Vacuum",
        })
        results = self.store.list_products("wet_dry_vacuum", q="dcV")
        self.assertEqual([item["model_number"] for item in results], ["DCV580H"])

    def test_category_voltage_count_uses_distinct_brand_voltage_pairs(self) -> None:
        for model, voltage in (("V18", "18V"), ("V18B", "18V"), ("V40", "40V")):
            self.store.create_product({
                "category_code": "wet_dry_vacuum", "brand": "QA",
                "model_number": model, "energy_type": "DC", "voltage_platform": voltage,
            })
        self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "QB",
            "model_number": "V18", "energy_type": "DC", "voltage_platform": "18V",
        })
        category = next(item for item in self.store.list_categories() if item["code"] == "wet_dry_vacuum")
        brand = next(item for item in self.store.brand_summary() if item["brand"] == "QA")
        self.assertEqual(category["voltage_platform_count"], 3)
        self.assertEqual(brand["voltage_platform_count"], 2)
        self.assertEqual(brand["voltage_platforms"], ["18V", "40V"])

    def test_persisted_brand_and_voltage_platform_identities_survive_rename(self) -> None:
        def create(brand: str, model: str, voltage: str) -> dict:
            return self.store.create_product({
                "category_code": "wet_dry_vacuum", "brand": brand,
                "model_number": model, "energy_type": "DC",
                "voltage_platform": voltage,
            })

        a18 = create("Brand A", "A-18", "18V")
        a18_equivalent = create("brand a", "A-18-EQ", "18 volts")
        a20 = create("Brand A", "A-20", "20 V")
        b18 = create("Brand B", "B-18", "18 V")

        self.assertEqual(a18["brand_id"], a18_equivalent["brand_id"])
        self.assertEqual(a18["voltage_platform_id"], a18_equivalent["voltage_platform_id"])
        self.assertNotEqual(a18["voltage_platform_id"], a20["voltage_platform_id"])
        self.assertNotEqual(a18["voltage_platform_id"], b18["voltage_platform_id"])

        brand_id = a18["brand_id"]
        platform_id = a18["voltage_platform_id"]
        renamed = self.store.rename_brand(brand_id, "Brand A Professional", aliases=["BRAND-A"])
        self.assertEqual(renamed["id"], brand_id)
        after = self.store.get_product(a18["id"])
        self.assertEqual(after["brand_id"], brand_id)
        self.assertEqual(after["voltage_platform_id"], platform_id)
        self.assertEqual(after["brand"], "Brand A Professional")

        alias_product = create("BRAND-A", "A-18-ALIAS", "18.0 volts")
        self.assertEqual(alias_product["brand_id"], brand_id)
        self.assertEqual(alias_product["voltage_platform_id"], platform_id)
        self.assertEqual(alias_product["brand"], "Brand A Professional")
        with self.assertRaisesRegex(ValueError, "另一稳定品牌 ID"):
            self.store.rename_brand(brand_id, "Brand B")
        with self.store.connect() as conn:
            conflict_count = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_type='brand' "
                "AND entity_id=? AND action='rename_conflict'",
                (brand_id,),
            ).fetchone()[0]
        self.assertEqual(conflict_count, 1)

    def test_legacy_v15_database_migrates_to_persisted_master_identities(self) -> None:
        path = self.root / "legacy-v15.sqlite3"
        migrations = PROJECT_ROOT / "retail_landscape" / "migrations"
        with sqlite3.connect(path) as conn:
            for migration in sorted(migrations.glob("000[1-6]_*.sql")):
                conn.executescript(migration.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.stem, "2026-08-14T00:00:00+00:00"),
                )
            conn.execute(
                """
                INSERT INTO categories(id, code, name, name_zh, description, enabled)
                VALUES ('cat_wet_dry_vacuum', 'wet_dry_vacuum', 'Wet/Dry Vacuum',
                        '干湿两用吸尘器', '', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO products(
                  id, category_id, brand, model_number, energy_type,
                  voltage_platform, review_status, created_at, updated_at
                ) VALUES (
                  'pvr_legacy00000000000000', 'cat_wet_dry_vacuum', 'Legacy Brand',
                  'LEG-18', 'DC', '18 V', 'approved',
                  '2026-08-14T00:00:00+00:00', '2026-08-14T00:00:00+00:00'
                )
                """
            )
        audit = initialize_with_audit(path, backup_dir=self.root / "legacy-backups")
        self.assertEqual(audit["schema_migrations"], audit["expected_schema_migrations"])
        self.assertTrue(Path(str(audit["backup_path"])).is_file())
        migrated = RetailStore(path, runtime_mode="read_only")
        product = migrated.get_product("pvr_legacy00000000000000")
        self.assertTrue(product["brand_id"].startswith("brd_"))
        self.assertTrue(product["voltage_platform_id"].startswith("vpf_"))
        self.assertEqual(product["voltage_platform_identity"]["standard_voltage"], "18V")
        with migrated.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertTrue(conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version='0007_product_master_identity'"
            ).fetchone())

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_product_master_v2_invalid_fixture_matches_runtime_and_schema_invariants(self) -> None:
        def deep_patch(target: dict, patch: dict) -> None:
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    deep_patch(target[key], value)
                else:
                    target[key] = copy.deepcopy(value)

        example = json.loads((
            PROJECT_ROOT / "retail_landscape" / "schemas" / "examples" /
            "retail-product-master-v2.example.json"
        ).read_text(encoding="utf-8"))
        fixture = json.loads((
            FIXTURES / "product-master-v2-invalid-invariants.json"
        ).read_text(encoding="utf-8"))
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                record = copy.deepcopy(example["records"][0])
                deep_patch(record, case["patch"])
                errors = product_master_record_invariant_errors(record)
                self.assertIn(case["expected_error"], errors)

        schema = json.loads((
            PROJECT_ROOT / "retail_landscape" / "schemas" /
            "retail-product-master-v2.schema.json"
        ).read_text(encoding="utf-8"))
        evidence = schema["$defs"]["evidence"]["properties"]
        self.assertEqual(evidence["source_url"]["type"], ["string", "null"])
        self.assertEqual(evidence["source_type"]["type"], ["string", "null"])
        self.assertEqual(evidence["source_snapshot_hash"]["type"], ["string", "null"])
        self.assertEqual(evidence["observed_at"]["type"], ["string", "null"])

    def test_child_fact_statuses_cannot_bypass_preview_review_gate(self) -> None:
        bypass_payloads = [
            {"listings": [{"retailer": "Lowe's", "online_url": "https://lowes.com/bypass", "review_status": "published"}]},
            {"listings": [{"retailer": "Lowe's", "online_url": "https://lowes.com/bypass-price", "prices": [{"raw_value": "$1", "normalized_amount": 1, "currency_code": "USD", "review_status": "published"}]}]},
            {"specs": {"dust_capacity": {"normalized_value": 2, "unit_id": "unit_gal", "review_status": "approved"}}},
            {"assets": [{"asset_role": "hero", "source_url": "https://example.test/hero.jpg", "review_status": "published"}]},
        ]
        for index, child in enumerate(bypass_payloads):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "阶段 1 仅允许 draft/pending_review"):
                self.store.create_product({"category_code": "wet_dry_vacuum", "brand": "GATE", "model_number": f"GATE-{index}", **child})

    # Deployment-specific seed or retired frontend is outside the public package.

    def test_snapshot_model_quality_gate_and_listing_default(self) -> None:
        self.assertIsNone(trustworthy_snapshot_model("STEALTHSONIC-QUIET-16-GALLONS-6-5-HP-CORDED-WET-DRY-SHOP-VACUUM-WITH-ACCESSORIES-INCLUDED"))
        with self.store.connect() as conn:
            columns = {row["name"]: dict(row) for row in conn.execute("PRAGMA table_info(product_listings)").fetchall()}
        self.assertEqual(columns["review_status"]["dflt_value"], "'draft'")


    def test_read_only_runtime_never_mutates_an_initialized_database(self) -> None:
        product = self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "Runtime QA", "model_number": "RO-1",
            "energy_type": "DC", "voltage_platform": "18 V",
            "source_url": "https://example.test/runtime-ro-1",
            "source_type": "official_product_page",
            "source_snapshot_hash": "sha256:runtime-ro-1",
            "observed_at": "2026-08-17T00:00:00+00:00",
        })
        self.store.review_product(product["id"], approved=True, reviewer="qa")
        database = self.root / "retail.sqlite3"
        before = hashlib.sha256(database.read_bytes()).hexdigest()

        readonly = RetailStore(database, runtime_mode="read_only")
        projection = readonly.product_master_projection(model_ids=[product["id"]])
        self.assertEqual([record["model_id"] for record in projection["records"]], [product["id"]])
        after = hashlib.sha256(database.read_bytes()).hexdigest()
        self.assertEqual(before, after)

        # The one-shot initializer remains safe to retry: no migration/seed/
        # backfill work is left on the runtime path and a no-op retry is byte-stable.
        RetailStore.initialize_database(database)
        self.assertEqual(after, hashlib.sha256(database.read_bytes()).hexdigest())

    def test_read_only_runtime_fails_closed_for_uninitialized_or_unseeded_database(self) -> None:
        uninitialized = self.root / "uninitialized.sqlite3"
        sqlite3.connect(uninitialized).close()
        before = hashlib.sha256(uninitialized.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ReadOnlyStorePreconditionError, "required schema"):
            RetailStore(uninitialized, runtime_mode="read_only")
        self.assertEqual(before, hashlib.sha256(uninitialized.read_bytes()).hexdigest())

        seeded = self.root / "missing-seed.sqlite3"
        RetailStore.initialize_database(seeded)
        with sqlite3.connect(seeded) as conn:
            conn.execute("DELETE FROM retailers WHERE code='home_depot'")
        before_seed_check = hashlib.sha256(seeded.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ReadOnlyStorePreconditionError, "retailers.missing=home_depot"):
            RetailStore(seeded, runtime_mode="read_only")
        self.assertEqual(before_seed_check, hashlib.sha256(seeded.read_bytes()).hexdigest())

        with sqlite3.connect(self.root / "retail.sqlite3") as conn:
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES ('9999_unknown', '2026-08-17')"
            )
        mismatched = self.root / "retail.sqlite3"
        mismatch_hash = hashlib.sha256(mismatched.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ReadOnlyStorePreconditionError, "unexpected=9999_unknown"):
            RetailStore(mismatched, runtime_mode="read_only")
        self.assertEqual(mismatch_hash, hashlib.sha256(mismatched.read_bytes()).hexdigest())

    def test_product_master_get_etag_and_404_are_preserved_in_read_only_runtime(self) -> None:
        product = self.store.create_product({
            "category_code": "wet_dry_vacuum", "brand": "Runtime QA", "model_number": "HTTP-RO-1",
            "energy_type": "AC",
            "source_url": "https://example.test/runtime-http-ro-1",
            "source_type": "official_product_page",
            "source_snapshot_hash": "sha256:runtime-http-ro-1",
            "observed_at": "2026-08-17T00:00:00+00:00",
        })
        self.store.review_product(product["id"], approved=True, reviewer="qa")
        database = self.root / "retail.sqlite3"
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        readonly = RetailStore(database, runtime_mode="read_only")
        status, payload, headers = readonly.product_master_http_response()
        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        self.assertIn("ETag", headers)
        not_modified, empty_payload, not_modified_headers = readonly.product_master_http_response(
            if_none_match=headers["ETag"]
        )
        self.assertEqual(not_modified, 304)
        self.assertIsNone(empty_payload)
        self.assertEqual(not_modified_headers, headers)
        missing_status, missing_payload, _ = readonly.product_master_http_response(
            model_ids=["pvr_missing000000000000"]
        )
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing_payload["error"]["code"], "master_identity_not_found")
        schema = json.loads((
            PROJECT_ROOT / "retail_landscape" / "schemas" /
            "retail-product-master-v2.schema.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())

    def test_synthetic_acceptance_fixture_and_filesystem_read_only_gate(self) -> None:
        database = self.root / "product-master-acceptance.sqlite3"
        result = build_acceptance_fixture(database)
        self.assertEqual(len(result["model_ids"]), 5)
        self.assertEqual(result["category_import"]["imported_category_count"], 57)
        self.assertEqual(result["assertions"], {
            "different_brand_18v_platforms": True,
            "same_brand_different_voltage_platforms": True,
            "cross_category_ineligible": True,
            "cross_category_reason": "cross_category_bundle",
        })
        database.chmod(0o440)
        readonly = RetailStore(
            database, runtime_mode="read_only", require_filesystem_read_only=True,
            required_category_manifest_id=result["category_manifest_id"],
        )
        self.assertEqual(len(readonly.product_master_projection()["records"]), 5)
        with self.assertRaisesRegex(ReadOnlyStorePreconditionError, "required_manifest_missing"):
            RetailStore(
                database,
                runtime_mode="read_only",
                require_filesystem_read_only=True,
                required_category_manifest_id="ccm_" + "0" * 64,
            )
        database.chmod(0o640)
        with self.assertRaisesRegex(ReadOnlyStorePreconditionError, "filesystem write bits"):
            RetailStore(
                database, runtime_mode="read_only", require_filesystem_read_only=True,
            )

    def test_governed_category_registry_imports_57_and_preserves_core_identities(self) -> None:
        core_before = {
            item["code"]: (item["id"], item["name"], item["name_zh"])
            for item in self.store.list_categories()
        }
        field_ids_before = {
            code: [item["id"] for item in self.store.field_definitions(code)]
            for code in core_before
        }
        manifest = synthetic_57_category_manifest()
        result = self.store.import_category_catalog(manifest, actor="registry_test")
        self.assertEqual(result["imported_category_count"], 57)
        self.assertEqual(result["created"], 53)
        self.assertEqual(result["matched"], 4)
        self.assertEqual(len(self.store.list_categories()), len(core_before) + result["created"])
        for code, identity in core_before.items():
            category = self.store.category(code)
            self.assertEqual(
                (category["id"], category["name"], category["name_zh"]), identity,
            )
            self.assertEqual(
                [item["id"] for item in self.store.field_definitions(code)],
                field_ids_before[code],
            )
        self.assertFalse(has_field_profile("wotu_synthetic_governed_category_001"))
        self.assertEqual(fields_for("wotu_synthetic_governed_category_001"), [])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM category_source_mappings "
                "WHERE source_system='wotu' AND active=1"
            ).fetchone()[0], 57)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM category_catalog_imports"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE entity_type='category_catalog_import' AND action='imported'"
            ).fetchone()[0], 1)
            stored_source = json.loads(conn.execute(
                "SELECT source_record_json FROM category_source_mappings "
                "WHERE source_system='wotu' AND source_category_id='fixture-wotu-001'"
            ).fetchone()[0])
            self.assertEqual(stored_source["source_category_id"], "fixture-wotu-001")
            self.assertEqual(stored_source["source_metadata"], {})
        before_replay = hashlib.sha256((self.root / "retail.sqlite3").read_bytes()).hexdigest()
        replay = self.store.import_category_catalog(manifest, actor="registry_test")
        self.assertEqual(replay["status"], "already_applied")
        self.assertEqual(
            hashlib.sha256((self.root / "retail.sqlite3").read_bytes()).hexdigest(),
            before_replay,
        )
        before_rename = self.store.resolve_category_identity(
            source_system="wotu", source_category_id="fixture-wotu-001",
        )
        renamed = copy.deepcopy(manifest)
        renamed["categories"][4]["source_name"] = "Renamed governed category 001"
        renamed["categories"][4]["source_updated_at"] = "2026-08-18T00:00:00+00:00"
        renamed["source_reference"] = "synthetic-fixture://wotu/master_categories/renamed"
        renamed["source_snapshot_hash"] = "sha256:" + "b" * 64
        renamed["observed_at"] = "2026-08-18T00:00:00+00:00"
        renamed["approved_at"] = "2026-08-18T00:00:00+00:00"
        renamed = finalize_category_manifest(renamed, content_hash=content_hash)
        self.store.import_category_catalog(renamed, actor="registry_test")
        after_rename = self.store.resolve_category_identity(
            source_system="wotu", source_category_id="fixture-wotu-001",
            source_name="Renamed governed category 001",
        )
        self.assertEqual(after_rename["id"], before_rename["id"])
        self.assertEqual(after_rename["code"], before_rename["code"])

    def test_operations_catalog_is_prepared_without_guessing_identity_or_losing_evidence(self) -> None:
        raw = {
            "schema_version": "voxquarry.production-wotu-category-catalog/v1",
            "artifact_id": "production-wotu-category-catalog-fixture",
            "as_of_utc": "2026-08-17T12:08:35+00:00",
            "approval_id": "fixture-approval",
            "candidate_sha": "a" * 40,
            "mapping_policy": "fixture exact bindings only",
            "source_database_sha256": "c" * 64,
            "source_environment": "production",
            "source_query_version": "fixture/v1",
            "summary": {"rows": 3},
            "records": [
                {
                    "source_record": {
                        "id": "master-1", "name": "Wet/Dry Vacuum",
                        "created_at": "2020-01-01T00:00:00+00:00",
                        "updated_at": "2026-08-17T00:00:00+00:00",
                    },
                    "aliases": [], "alias_source": "production_schema_has_no_alias",
                    "coverage": {"jobs_total": 28}, "issues": [],
                    "candidate_8d459_exact_binding_support": True,
                    "candidate_catalog_evidence": {
                        "category_id": "cat_wet_dry_vacuum",
                        "code": "wet_dry_vacuum", "name": "Wet/Dry Vacuum",
                    },
                },
                {
                    "source_record": {
                        "id": "master-2", "name": "Governed Generic Category",
                        "created_at": "2020-01-01T00:00:00+00:00",
                        "updated_at": "2026-08-17T00:00:00+00:00",
                    },
                    "aliases": [], "alias_source": "production_schema_has_no_alias",
                    "coverage": {"jobs_total": 7}, "issues": ["no_field_profile"],
                    "candidate_8d459_exact_binding_support": False,
                    "candidate_catalog_evidence": None,
                },
                {
                    "source_record": {
                        "id": "master-3", "name": "Misting Fan",
                        "created_at": "2020-01-01T00:00:00+00:00",
                        "updated_at": "2026-08-17T00:00:00+00:00",
                    },
                    "aliases": [], "alias_source": "production_schema_has_no_alias",
                    "coverage": {"jobs_total": 3}, "issues": [],
                    "candidate_8d459_exact_binding_support": False,
                    "candidate_catalog_evidence": None,
                },
            ],
        }
        prepared = prepare_category_catalog_manifest(
            raw,
            source_file_sha256="a" * 64,
            approved_by="operations_fixture_approval",
            approved_at="2026-08-17T12:08:35+00:00",
        )
        self.assertNotIn("canonical_code", prepared["categories"][1])
        self.assertNotIn("category_id", prepared["categories"][1])
        self.assertEqual(
            prepared["categories"][1]["source_metadata"]["coverage"]["jobs_total"], 7,
        )
        self.assertEqual(
            prepared["categories"][1]["source_metadata"]["operations_source_record"]["id"],
            "master-2",
        )
        self.store.import_category_catalog(prepared, actor="registry_test")
        identity = self.store.resolve_category_identity(
            source_system="wotu", source_category_id="master-2",
            source_name="Governed Generic Category",
        )
        self.assertEqual(identity["code"], "wotu_governed_generic_category")
        misting = self.store.resolve_category_identity(
            source_system="wotu", source_category_id="master-3",
            source_name="Misting Fan",
        )
        self.assertEqual(misting["code"], "wotu_misting_fan")
        self.assertNotEqual(misting["id"], self.store.category("fan")["id"])
        with self.store.connect() as conn:
            stored = json.loads(conn.execute(
                "SELECT source_record_json FROM category_source_mappings "
                "WHERE source_system='wotu' AND source_category_id='master-2'"
            ).fetchone()[0])
        self.assertEqual(stored["source_metadata"]["coverage"]["jobs_total"], 7)

    def test_controlled_category_catalog_import_writes_backup_and_audit(self) -> None:
        manifest = synthetic_57_category_manifest()
        manifest_path = self.root / "external-category-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8",
        )
        audit = import_catalog_with_audit(
            self.root / "retail.sqlite3",
            manifest_path,
            actor="registry_import_job",
            backup_dir=self.root / "catalog-backups",
        )
        self.assertEqual(audit["manifest_id"], manifest["manifest_id"])
        self.assertEqual(audit["expected_category_count"], 57)
        self.assertEqual(audit["wotu_mapping_count"], 57)
        self.assertEqual(audit["integrity_check"], "ok")
        self.assertTrue(Path(str(audit["backup_path"])).is_file())
        self.assertEqual(
            audit["backup_sha256"],
            hashlib.sha256(Path(str(audit["backup_path"])).read_bytes()).hexdigest(),
        )

    def test_category_registry_conflicts_fail_closed_with_zero_writes(self) -> None:
        base = synthetic_57_category_manifest()

        def resign(changed: dict[str, object]) -> dict[str, object]:
            return finalize_category_manifest(changed, content_hash=content_hash)

        cases: list[tuple[str, dict[str, object]]] = []
        duplicate_slug = copy.deepcopy(base)
        duplicate_slug["categories"][4]["canonical_code"] = "wotu_collision"
        duplicate_slug["categories"][5]["canonical_code"] = "wotu_collision"
        cases.append(("duplicate canonical_code", resign(duplicate_slug)))
        duplicate_name = copy.deepcopy(base)
        duplicate_name["categories"][5]["canonical_name"] = duplicate_name["categories"][4]["source_name"]
        cases.append(("duplicate normalized category name", resign(duplicate_name)))
        alias_collision = copy.deepcopy(base)
        alias_collision["categories"][5]["aliases"] = ["  WET/DRY   VACUUM "]
        cases.append(("duplicate normalized category alias", resign(alias_collision)))

        for expected, manifest in cases:
            with self.subTest(expected=expected):
                before = hashlib.sha256((self.root / "retail.sqlite3").read_bytes()).hexdigest()
                with self.assertRaisesRegex(ValueError, expected):
                    self.store.import_category_catalog(manifest, actor="registry_test")
                self.assertEqual(
                    hashlib.sha256((self.root / "retail.sqlite3").read_bytes()).hexdigest(),
                    before,
                )

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_dedicated_read_service_declares_only_health_schema_and_models_gets(self) -> None:
        source = (
            PROJECT_ROOT / "retail_landscape" / "product_master_read_service.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(source.count("@app.get("), 3)
        self.assertNotIn("@app.post(", source)
        self.assertNotIn("@app.put(", source)
        self.assertNotIn("@app.delete(", source)
        self.assertIn('docs_url=None', source)
        self.assertIn('redoc_url=None', source)
        self.assertIn('openapi_url=None', source)
        self.assertIn('require_filesystem_read_only=True', source)

    def test_explicit_initializer_is_audited_backed_up_and_idempotent(self) -> None:
        database = self.root / "explicit-init.sqlite3"
        first = initialize_with_audit(database)
        self.assertEqual(first["schema_migrations"], first["expected_schema_migrations"])
        self.assertEqual(first["integrity_check"], "ok")
        self.assertIsNone(first["backup_path"])

        before_retry = hashlib.sha256(database.read_bytes()).hexdigest()
        second = initialize_with_audit(database, backup_dir=self.root / "backups")
        self.assertEqual(second["database_sha256_before"], before_retry)
        self.assertEqual(second["database_sha256_after"], before_retry)
        backup = Path(str(second["backup_path"]))
        self.assertTrue(backup.is_file())
        self.assertEqual(second["backup_sha256"], hashlib.sha256(backup.read_bytes()).hexdigest())
        with sqlite3.connect(f"{backup.resolve().as_uri()}?mode=ro", uri=True) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                len(first["expected_schema_migrations"]),
            )

        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO categories(id, code, name, name_zh, enabled) "
                "VALUES ('cat_restore_test', 'restore_test', 'Restore Test', '恢复测试', 0)"
            )
        mutated_hash = hashlib.sha256(database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "current database hash"):
            restore_database(database, backup, expected_current_sha256="0" * 64)
        restored_hash = restore_database(
            database, backup, expected_current_sha256=mutated_hash,
        )
        self.assertEqual(restored_hash, second["backup_sha256"])
        with sqlite3.connect(database) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM categories WHERE code='restore_test'").fetchone()[0],
                0,
            )

    # Deployment-specific seed or retired frontend is outside the public package.




if __name__ == "__main__":
    unittest.main()
