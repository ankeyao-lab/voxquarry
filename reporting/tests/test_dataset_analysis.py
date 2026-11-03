import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from reporting.dataset_analysis import analyze_dataset
from reporting.product_insight import report_readiness_status
from reporting.research_definition import get_research_definition
from reporting.source import canonical_json
from reporting.tests.test_market_region import SnapshotFixture, _row
from reporting.tests.test_partial_source_completeness import governed_partial_report, review_row
from reporting.wenquan_datasets import DatasetError, WenquanDatasets


class DatasetAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = SnapshotFixture(self.root / "source")
        self.backend = WenquanDatasets(self.fixture.root, self.root / "datasets")

    def freeze(self, rows, job="one"):
        self.fixture.add_run(job, "North America", rows)
        return self.backend.open({"job_ids": [job]})

    def test_small_brief_runs_registered_rules_without_publishing_or_changing_source(self):
        text = "I clear wet leaves every week. The battery runtime is terrible."
        manifest = self.freeze([_row("review-one", "North America", text=text)])
        before = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob("*") if p.is_file()}
        definition = get_research_definition("leaf_blower")
        result = analyze_dataset(self.backend, manifest["dataset_id"], definition["definition_id"])
        after = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, {k: v for k, v in after.items() if k in before})
        self.assertEqual(len(set(after) - set(before)), 1)  # Derived cache only.
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["sample"]["written_entities"], 1)
        self.assertEqual(result["readiness"]["formal_sample_status"], "exploratory")
        self.assertFalse(result["readiness"]["formal_report_created"])
        self.assertFalse(result["readiness"]["formal_sample_threshold_met"])
        self.assertEqual(result["manifest_sha256"], manifest["manifest_sha256"])
        self.assertEqual(result["definition_id"], definition["definition_id"])
        self.assertEqual(result["atomic_scripts"]["script_count"], 28)
        self.assertEqual(result["atomic_scripts"]["ledger"]["record_count"], 28)
        self.assertNotIn("relative_path", result["atomic_scripts"]["ledger"])
        self.assertFalse(result["atomic_scripts"]["ledger"]["stored"])
        self.assertTrue(result["evidence"])
        # High-star complaints remain explicit-negative clues, not low stars.
        self.assertTrue(any(r["explicit_negative_entities"] > 0 for r in result["aspect_experience"]))
        self.assertTrue(all(r["low_star_entities"] == 0 for r in result["aspect_experience"]))
        for evidence in result["evidence"]:
            original = self.backend.query(manifest["dataset_id"], review_ids=[evidence["record_id"]])["items"]
            self.assertEqual(len(original), 1)
            self.assertEqual(original[0]["review_text"], text)
            self.assertTrue(evidence["full_review_required"])
        payload = {k: v for k, v in result.items() if k != "analysis_sha256"}
        self.assertEqual(result["analysis_sha256"], "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest())

    def test_analysis_reuses_verified_cache_and_recomputes_corrupt_result(self):
        manifest = self.freeze([_row("r1", "North America")])
        first = analyze_dataset(self.backend, manifest['dataset_id'])
        with patch('reporting.dataset_analysis.run_atomic_scripts', side_effect=AssertionError('must reuse')):
            self.assertEqual(analyze_dataset(self.backend, manifest['dataset_id']), first)
        cache = self.backend.storage_root / manifest['dataset_id'] / (first['analysis_id'] + '.json')
        cache.write_text('{"analysis_id":"incorrect"}')
        self.assertEqual(analyze_dataset(self.backend, manifest['dataset_id']), first)

    def test_zero_text_returns_sample_and_no_invented_experience(self):
        manifest = self.freeze([_row("no-text", "North America", text="", valid_text="false")])
        result = analyze_dataset(self.backend, manifest["dataset_id"])
        self.assertEqual(result["status"], "no_review_text")
        self.assertEqual(result["readiness"]["formal_sample_status"], "no_data")
        self.assertFalse(result["readiness"]["experience_analysis_available"])
        self.assertEqual(result["sample"]["rating_n"], 1)
        self.assertIsNone(result["atomic_scripts"])
        self.assertEqual(result["aspect_experience"], [])
        self.assertEqual(result["evidence"], [])

    def test_zero_eligible_rows_keep_collection_product_identity(self):
        manifest = self.freeze([])
        result = analyze_dataset(self.backend, manifest["dataset_id"])
        self.assertEqual(result["status"], "no_review_text")
        self.assertEqual(result["sample"]["review_entities"], 0)
        self.assertEqual(result["category_id"], "leaf_blower")
        self.assertEqual(result["products"][0]["identity_source"], "collection_metadata")
        self.assertTrue(result["products"][0]["product_url"])

    def test_other_category_or_obsolete_definition_is_rejected(self):
        manifest = self.freeze([_row("r1", "North America")])
        for definition in (get_research_definition("walk_behind_mower")["definition_id"], "rdef_" + "0" * 64):
            with self.assertRaises(DatasetError) as raised:
                analyze_dataset(self.backend, manifest["dataset_id"], definition)
            self.assertEqual(raised.exception.code, "analysis_definition_mismatch")

    def test_product_binding_comes_from_actual_dataset_not_external_input(self):
        one = self.freeze([_row("one", "North America")])
        two = self.freeze([_row("two", "North America")], "two")
        first = analyze_dataset(self.backend, one["dataset_id"])
        second = analyze_dataset(self.backend, two["dataset_id"])
        self.assertNotEqual(first["analysis_id"], second["analysis_id"])
        self.assertEqual(first["products"][0]["source_product_id"], "source-one")
        self.assertEqual(second["products"][0]["source_product_id"], "source-two")
        self.assertTrue(first["analysis_scope"]["single_product_source"])

    def test_formal_report_and_private_dataset_select_the_same_product_reviews(self):
        from reporting.contracts import default_scope, normalize_scope
        from reporting.source import build_review_fact
        self.fixture.add_run('mixed', 'North America', [_row('target', 'North America'), _row('other', 'North America')])
        scope = {'job_ids': ['mixed'], 'source_product_ids': ['source-target']}
        dataset = self.backend.open(scope)
        rows = self.backend.query(dataset['dataset_id'])['items']
        facts, _, _ = build_review_fact(self.fixture.root, normalize_scope(scope, default_scope()))
        self.assertEqual([row['source_product_id'] for row in rows], ['source-target'])
        self.assertEqual([row['source_product_id'] for row in facts], ['source-target'])

    def test_multiple_product_sources_are_explicit_and_not_called_a_single_product(self):
        manifest = self.freeze([_row("one", "North America"), _row("two", "North America")])
        result = analyze_dataset(self.backend, manifest["dataset_id"])
        self.assertEqual(result["analysis_scope"]["product_source_count"], 2)
        self.assertFalse(result["analysis_scope"]["single_product_source"])
        self.assertEqual({p["source_product_id"] for p in result["products"]}, {"source-one", "source-two"})

    def test_canonical_dedup_precedes_script_counts(self):
        row = _row("same", "North America")
        self.fixture.add_run("one", "North America", [row])
        self.fixture.add_run("two", "North America", [{**row, "channel": "lowes"}], channel="lowes")
        manifest = self.backend.open()
        result = analyze_dataset(self.backend, manifest["dataset_id"])
        self.assertEqual(manifest["record_counts"]["occurrences"], 2)
        self.assertEqual(result["sample"]["review_entities"], 1)
        self.assertEqual(result["atomic_scripts"]["written_entities"], 1)

    def test_mixed_category_dataset_is_not_given_one_profile(self):
        self.fixture.add_run("one", "North America", [_row("one", "North America")])
        self.fixture.add_run("two", "North America", [_row("two", "North America")])
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE jobs SET category='Walk-Behind Mower' WHERE id='two'")
        manifest = self.backend.open()
        with self.assertRaises(DatasetError) as raised:
            analyze_dataset(self.backend, manifest["dataset_id"])
        self.assertEqual(raised.exception.code, "analysis_category_ambiguous")

    def test_verified_partial_collection_remains_partial_in_brief(self):
        self.fixture.add_run("partial", "North America", [review_row("r1"), review_row("r2"), review_row("r3")],
                             channel="homedepot", report_overrides=governed_partial_report())
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE jobs SET status='partial_success'")
        manifest = self.backend.open()
        result = analyze_dataset(self.backend, manifest["dataset_id"])
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["source_complete"])
        self.assertEqual(result["source_completeness"][0]["missing_review_count"], 2)
        self.assertEqual(result["sample"]["review_entities"], 3)
        self.assertFalse(result["readiness"]["formal_report_created"])

    def test_crossing_formal_sample_threshold_still_does_not_publish_report(self):
        manifest = self.freeze([_row(str(n), "North America", text="Battery works well.") for n in range(500)])
        result = analyze_dataset(self.backend, manifest["dataset_id"])
        self.assertEqual(result["readiness"]["formal_sample_status"], "pilot")
        self.assertTrue(result["readiness"]["formal_sample_threshold_met"])
        self.assertFalse(result["readiness"]["formal_report_created"])

    def test_capacity_and_changed_dataset_fail_without_partial_analysis(self):
        manifest = self.freeze([_row("r1", "North America")])
        with patch("reporting.dataset_analysis.MAX_ANALYSIS_BYTES", 1):
            with self.assertRaises(DatasetError) as raised:
                analyze_dataset(self.backend, manifest["dataset_id"])
            self.assertEqual(raised.exception.code, "analysis_capacity_exceeded")
        path = self.backend.storage_root / manifest["dataset_id"] / "reviews.sqlite3"
        path.parent.chmod(0o700)
        path.chmod(0o600)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE facts SET payload='{}'")
        with self.assertRaises(DatasetError) as raised:
            analyze_dataset(self.backend, manifest["dataset_id"])
        self.assertEqual(raised.exception.code, "dataset_changed")

    def test_formal_readiness_is_not_lowered_by_brief_analysis(self):
        manifest = self.freeze([_row(str(n), "North America", text="Battery works well.") for n in range(499)])
        result = analyze_dataset(self.backend, manifest["dataset_id"])
        self.assertEqual(result["sample"]["written_entities"], 499)
        self.assertEqual(result["readiness"]["formal_sample_status"], "exploratory")
        self.assertEqual(report_readiness_status(500), "pilot")
        self.assertEqual(result["readiness"]["formal_report_thresholds"], {"pilot": 500, "limited": 1000, "full": 3000, "exploratory": 1})


if __name__ == "__main__":
    unittest.main()
