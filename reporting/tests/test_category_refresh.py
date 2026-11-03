from pathlib import Path
import unittest
from unittest.mock import patch
import copy
import json
import sqlite3
import tempfile

from reporting.category_vocabulary import category_id_for
from reporting.contracts import ContractError, normalize_request, default_scope
from reporting.product_form import product_form_fact
from reporting.source import validate_scope_job_ids
from reporting.product_insight import product_insight_support


class CategoryRefreshTests(unittest.TestCase):
    def test_legacy_sprayer_request_preserves_form_scope(self):
        request=normalize_request({"scope":{"categories":["Handheld Sprayer"]}}, defaults=default_scope(), analysis_version="test")
        self.assertEqual(request["scope"]["categories"], ["Sprayer"])
        self.assertEqual(request["scope"]["product_form_factors"], ["handheld"])

    def test_rating_only_population_does_not_inflate_text_readiness(self):
        support = product_insight_support({"scope":{"categories":["Sprayer"]}}, valid_text_reviews=90, eligible_reviews=700)
        self.assertTrue(support["can_generate"])
        self.assertEqual(support["status"], "quantitative")
        self.assertEqual(support["text_readiness_status"], "exploratory")
        self.assertEqual(support["valid_text_reviews"], 90)

    def test_sprayer_forms_share_category_and_fans_stay_separate(self):
        for name, form in [("Handheld Sprayer", "handheld"), ("Backpack Sprayer", "backpack")]:
            self.assertEqual(category_id_for(name), "sprayer")
            self.assertEqual(product_form_fact({"category": name})["value"], form)
        self.assertNotEqual(category_id_for("Fan"), category_id_for("Misting Fan"))
        for name in ["Sprayer", "Fan", "Misting Fan"]:
            self.assertEqual(product_form_fact({"category": name, "review_text": "I carried the handheld unit"})["value"], "unknown")

    def test_form_conflicts_and_unsupported_form_filters_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            product_form_fact({"category": "Backpack Sprayer", "product_form_factor": "handheld", "product_form_evidence_source": "catalog_migration"})
        with self.assertRaisesRegex(ValueError, "conflicts"):
            product_form_fact({"category": "Sprayer", "product_form_source_category": "Backpack Sprayer", "product_form_factor": "handheld", "product_form_evidence_source": "catalog_migration"})
        with self.assertRaises(ContractError):
            normalize_request({"scope": {"categories": ["Misting Fan"], "product_form_factors": ["handheld"]}}, defaults=default_scope(), analysis_version="test")
        with self.assertRaises(ContractError):
            normalize_request({"scope": {"categories": ["Sprayer"], "product_form_factors": ["invented"]}}, defaults=default_scope(), analysis_version="test")

    def test_rating_only_population_generates_a_real_report_without_text_claims(self):
        from reporting.tests.test_reporting import fixture_facts, manifest
        from reporting.analysis import build_report
        from reporting.product_insight import build_product_insight_bundle
        facts = []
        for index in range(501):
            fact = copy.deepcopy(fixture_facts()[0])
            fact.update(review_entity_id=f"e-{index}", review_occurrence_id=f"o-{index}",
                        has_text=False, review_text="", title="", category="Sprayer", category_id="sprayer")
            facts.append(fact)
        report = build_report(facts, manifest(facts))
        report["scope"]["categories"] = ["Sprayer"]
        report["scope"]["category_id"] = "sprayer"
        report, *_ = build_product_insight_bundle(facts, report, enforce_readiness=True)
        self.assertEqual(report["product_insight_availability"]["status"], "quantitative")
        self.assertEqual(report["product_insight_availability"]["valid_text_reviews"], 0)
        self.assertEqual(report["product_insight_availability"]["eligible_reviews"], 501)

    def test_every_audited_category_over_500_is_publishable(self):
        audit = json.loads((Path(__file__).parent / "fixtures/category-admission-20261012.json").read_text())
        for row in audit["categories"]:
            with self.subTest(category=row["category"]):
                self.assertGreater(row["deduplicated_reviews"], 500)
                requested = {"categories": [row["category"]]}
                # Category coverage must also respect its existing energy contract.
                if row["category"] == "Pressure Washer":
                    requested["energy_type"] = "battery"
                scope = normalize_request({"scope": requested}, defaults=default_scope(), analysis_version="test")["scope"]
                support = product_insight_support({"scope": scope}, valid_text_reviews=row["deduplicated_text_reviews"], eligible_reviews=row["deduplicated_reviews"])
                self.assertTrue(support["can_generate"])

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_master_migration_preserves_ids_raw_results_and_fan_boundaries(self):
        from scripts.prepare_category_form_migration import prepare, digest
        from collector.master_data import MasterDataStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            master = MasterDataStore(source, initialize=True)
            names = ["Sprayer", "Backpack Sprayer", "Handheld Sprayer", "Handheld Spreader", "Fan", "Misting Fan"]
            entities = {name: master.create("categories", {"code": name.lower().replace(" ", "_"), "name": name}, actor="test") for name in names}
            with sqlite3.connect(source / "jobs.sqlite3") as conn:
                conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,category TEXT,channel TEXT,result_json TEXT)")
                conn.executemany("INSERT INTO jobs VALUES (?, 'completed', ?, 'fixture', ?)", [(str(i), name, '{"untouched":true}') for i, name in enumerate(names)])
                conn.execute("CREATE TABLE master_categories(id INTEGER PRIMARY KEY,name TEXT,created_at TEXT,updated_at TEXT)")
                conn.executemany("INSERT INTO master_categories VALUES (?,?, 'before', 'before')", list(enumerate(names)))
            before_bytes = (source / "jobs.sqlite3").read_bytes()
            result = prepare(source, root / "candidate")
            for filename, check in result["output_databases"].items():
                self.assertEqual(check["sha256"], digest(root / "candidate" / filename))
            self.assertEqual((source / "jobs.sqlite3").read_bytes(), before_bytes)
            self.assertEqual(result["job_count_after"], 6)
            migrated = MasterDataStore(root / "candidate")
            spreader = migrated.resolve("categories", "Spreader")["record"]
            self.assertEqual(spreader["id"], entities["Handheld Spreader"]["id"])
            self.assertEqual(spreader["code"], "handheld_spreader")
            for name in ("Fan", "Misting Fan"):
                self.assertEqual(migrated.resolve("categories", name)["record"]["id"], entities[name]["id"])
            with sqlite3.connect(root / "candidate/jobs.sqlite3") as conn:
                rows = conn.execute("SELECT category,product_form_factor,result_json FROM jobs ORDER BY id").fetchall()
            self.assertEqual(rows[1][:2], ("Sprayer", "backpack"))
            self.assertEqual(rows[2][:2], ("Sprayer", "handheld"))
            self.assertEqual(rows[3][:2], ("Spreader", "handheld"))
            self.assertTrue(all(row[2] == '{"untouched":true}' for row in rows))
            self.assertIsNone(rows[4][1])
            self.assertIsNone(rows[5][1])

    def test_explicit_task_ids_cannot_cross_form_filter(self):
        payload={"tasks": [{"id": "one", "category_id": "sprayer", "product_form_factor": "backpack"}]}
        with self.assertRaisesRegex(ValueError, "product_form"):
            validate_scope_job_ids(Path("unused"), {"category_id":"sprayer", "job_ids":["one"], "product_form_factors":["handheld"]}, source_jobs=[{"id":"one"}], catalog_payload=payload)


if __name__ == "__main__":
    unittest.main()
