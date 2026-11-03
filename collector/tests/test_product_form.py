"""Synthetic form contract tests across import artifacts and task persistence."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from collector.product_form import CATEGORY_FORMS, LABELS, product_form
from collector.product_metadata import normalize_product_metadata
from collector.external_import import validate_import_bundle, verify_import_artifacts, write_import_artifacts, ExternalImportError
from collector.store import Store, EXTERNAL_IMPORT_ACTION
from scripts.test_external_review_import import browser_bundle


class ProductFormTests(unittest.TestCase):
    def test_collector_and_reporting_contracts_match(self):
        from reporting.product_form import CATEGORY_FORMS as REPORT_FORMS, FORM_LABELS, product_form_fact
        self.assertEqual(CATEGORY_FORMS, REPORT_FORMS)
        self.assertEqual(LABELS, FORM_LABELS)
        for category in [*CATEGORY_FORMS, "Sprayer", "Fan", "Misting Fan"]:
            self.assertEqual(product_form(category), product_form_fact({"category": category}))

    def test_new_task_preserves_form_when_category_is_unified(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DATA_DIR": directory}):
            store = Store()
            store.create_job("one", "scrape", "homedepot", category="Backpack Sprayer")
            job = store.get_job("one")
            self.assertEqual(job["category"], "Sprayer")
            self.assertEqual(job["product_form_factor"], "backpack")
            self.assertEqual(job["product_form_source_category"], "Backpack Sprayer")

    def test_legacy_category_import_preserves_hash_and_form_through_publish(self):
        for category, family, form in [("Backpack Sprayer", "Sprayer", "backpack"), ("Handheld Sprayer", "Sprayer", "handheld"), ("Handheld Spreader", "Spreader", "handheld")]:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DATA_DIR": directory}):
                payload = browser_bundle()
                payload["product"]["category"] = category
                original = copy.deepcopy(payload)
                validated = validate_import_bundle(payload)
                self.assertEqual(payload, original)
                self.assertEqual(validated["task_product_metadata"]["category"], family)
                self.assertEqual(validated["product"]["product_form_factor"], form)
                stage = Path(directory) / "stage"
                report = write_import_artifacts(stage, validated)
                verified = verify_import_artifacts(stage)
                self.assertEqual(verified["report"], report)
                store = Store()
                store.create_job("one", EXTERNAL_IMPORT_ACTION, "homedepot", target_url=payload["target_url"], brand="DEWALT", model="EXAMPLE-1", category=family)
                result = store.publish_external_import("one", channel="homedepot", target_url=payload["target_url"], brand="DEWALT", model="EXAMPLE-1", category=family,
                    market_region="North America", source_product_id=payload["product"]["id"], result={"homedepot": report}, staged_run_dir=stage)
                self.assertEqual(result["product_form_factor"], form)
                self.assertEqual(result["product_form_source_category"], category)

    def test_source_metadata_form_claim_and_conflict(self):
        payload = browser_bundle()
        payload["product"].update(category="Sprayer", product_form_factor="backpack")
        with self.assertRaisesRegex(ExternalImportError, "requires"):
            validate_import_bundle(payload)
        url = payload["target_url"]
        evidence = {"schema_version": "voxquarry.source-product-metadata/v1", "channel": "homedepot", "source_url": url, "final_url": url,
            "source_sha256": "a"*64, "identity": {"method": "jsonld_exact_product", "canonical_url": url, "product_id": payload["product"]["id"], "variant_id": None},
            "fields": {"product_form_factor": {"source": "specifications.form", "raw": "Backpack", "value": "backpack"}}, "gaps": [], "autofill_fields": ["product_form_factor"]}
        payload["product"]["source_metadata_evidence"] = evidence
        validated = validate_import_bundle(payload)
        self.assertEqual(validated["task_product_metadata"]["product_form"]["evidence_source"], "source_product_page")
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            write_import_artifacts(run, validated)
            verify_import_artifacts(run)
        payload["product"]["category"] = "Misting Fan"
        with self.assertRaisesRegex(ExternalImportError, "Fan"):
            validate_import_bundle(payload)

    def test_form_does_not_come_from_review_or_product_title(self):
        metadata = normalize_product_metadata(target_url="https://example.test/p", category="Sprayer", product_title="Backpack Sprayer")
        self.assertEqual(metadata["product_form_factor"], "unknown")
        payload = browser_bundle()
        payload["product"]["category"] = "Sprayer"
        payload["reviews"][0]["review_text"] = "This handheld sprayer has straps."
        self.assertEqual(validate_import_bundle(payload)["task_product_metadata"]["product_form_factor"], "unknown")

    def test_historical_v2_artifacts_with_no_form_remain_verifiable(self):
        payload = browser_bundle()
        payload["product"]["category"] = "Backpack Sprayer"
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            write_import_artifacts(run, validate_import_bundle(payload))
            for filename in ("selection_manifest.json", "validation_report.json"):
                path = run / filename
                data = json.loads(path.read_text())
                for field in ("product_form", "product_form_factor", "product_form_source_category"):
                    data["product"].pop(field)
                data["product"]["category"] = "Backpack Sprayer"
                path.write_text(json.dumps(data))
            verify_import_artifacts(run, allow_legacy_declared_incomplete=True)

    def test_source_category_claim_survives_artifact_normalization(self):
        payload = browser_bundle()
        payload["product"]["category"] = "Backpack Sprayer"
        url = payload["target_url"]
        payload["product"]["source_metadata_evidence"] = {
            "schema_version": "voxquarry.source-product-metadata/v1", "channel": "homedepot",
            "source_url": url, "final_url": url, "source_sha256": "a" * 64,
            "identity": {"method": "jsonld_exact_product", "canonical_url": url,
                         "product_id": payload["product"]["id"], "variant_id": None},
            "fields": {"category": {"source": "specifications.category", "raw": "Backpack Sprayer", "value": "Backpack Sprayer"}},
            "gaps": [], "autofill_fields": ["category"],
        }
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            write_import_artifacts(run, validate_import_bundle(payload))
            verified = verify_import_artifacts(run)
            self.assertEqual(verified["validated"]["product"]["product_form_factor"], "backpack")
        payload["product"]["product_form_source_category"] = "Handheld Sprayer"
        with self.assertRaisesRegex(ExternalImportError, "category"):
            validate_import_bundle(payload)

    def test_one_artifact_cannot_omit_form_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            write_import_artifacts(run, validate_import_bundle(browser_bundle()))
            path = run / "selection_manifest.json"
            data = json.loads(path.read_text())
            for field in ("product_form", "product_form_factor", "product_form_source_category"):
                data["product"].pop(field)
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ExternalImportError, "form fields disagree"):
                verify_import_artifacts(run)


if __name__ == "__main__":
    unittest.main()
