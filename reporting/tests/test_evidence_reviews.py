from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from reporting.evidence_reviews import enrich_review_text, prepare_review_text, _read_comments


class EvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot_id = "snap_" + "a" * 24
        self.report = {"data_snapshot_id": self.snapshot_id}
        self.directory = self.root / "snapshots" / self.snapshot_id
        self.directory.mkdir(parents=True)
        self.fact = {
            "review_entity_id": "rev_one", "review_occurrence_id": "occ_one",
            "review_title": "Quiet mower", "review_text": "Before.\nWorks well.\nAfter.",
        }
        self.evidence = {"evidence_id": "ev_one", "review_entity_id": "rev_one", "quote": "Works well."}
        self.write_facts([self.fact])

    def write_facts(self, rows):
        path = self.directory / "review_fact.preview.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        (self.directory / "manifest.json").write_text(json.dumps({
            "snapshot_id": self.snapshot_id,
            "hashes": {"preview_fact": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()},
        }))

    def enrich(self, rows=None):
        return enrich_review_text(self.root, self.report, rows or [self.evidence])

    def test_full_body_and_title_from_bound_snapshot_without_mutating_quote(self):
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        item = self.enrich()[0]
        self.assertEqual(item["review_text"], self.fact["review_text"])
        self.assertEqual(item["review_title"], "Quiet mower")
        self.assertEqual(item["quote"], "Works well.")
        self.assertEqual(item["review_text_status"], "available")
        self.assertEqual(item["review_text_snapshot_id"], self.snapshot_id)
        self.assertNotIn("review_text", self.evidence)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})

    def test_same_review_id_cannot_match_different_entity(self):
        self.evidence.update(review_entity_id="rev_missing", review_id="same-id")
        self.fact["review_id"] = "same-id"
        self.write_facts([self.fact])
        self.assertEqual(self.enrich()[0]["review_text_status"], "unavailable")

    def test_occurrence_identity_takes_precedence(self):
        self.evidence["review_occurrence_id"] = "occ_missing"
        self.assertEqual(self.enrich()[0]["review_text_status"], "unavailable")
        self.evidence["review_occurrence_id"] = "occ_one"
        self.assertEqual(self.enrich()[0]["review_text_status"], "available")

    def test_source_field_is_not_presented_as_comment(self):
        row = {**self.evidence, "evidence_type": "source_field"}
        self.assertEqual(self.enrich([row]), [row])

    def test_missing_and_corrupt_snapshot_keep_excerpt_with_explicit_gap(self):
        path = self.directory / "review_fact.preview.jsonl.gz"
        path.write_bytes(b"corrupt")
        self.assertEqual(self.enrich()[0]["review_text_status"], "unavailable")
        path.unlink()
        self.assertEqual(self.enrich()[0]["quote"], "Works well.")
        self.assertNotIn("review_text", self.enrich()[0])

    def test_invalid_manifest_identity_and_path_are_rejected(self):
        self.report["data_snapshot_id"] = "../outside"
        self.assertEqual(self.enrich()[0]["review_text_status"], "unavailable")
        self.report["data_snapshot_id"] = self.snapshot_id
        manifest = self.directory / "manifest.json"
        data = json.loads(manifest.read_text())
        data["snapshot_id"] = "snap_" + "b" * 24
        manifest.write_text(json.dumps(data))
        self.assertEqual(self.enrich()[0]["review_text_status"], "unavailable")

    def test_same_entity_conflict_requires_unique_quote_match(self):
        conflicting = {**self.fact, "review_text": "Different review."}
        self.write_facts([self.fact, conflicting])
        self.assertEqual(self.enrich()[0]["review_text"], self.fact["review_text"])
        self.write_facts([self.fact, {**conflicting, "review_text": "Works well. But different."}])
        self.assertEqual(self.enrich()[0]["review_text_status"], "unavailable")

    def test_identical_syndicated_text_is_not_a_conflict(self):
        self.write_facts([self.fact, {**self.fact, "review_occurrence_id": "occ_two"}])
        self.assertEqual(self.enrich()[0]["review_text_status"], "available")

    def test_title_quote_and_normalized_whitespace_resolve(self):
        self.evidence["quote"] = "Quiet mower"
        self.assertEqual(self.enrich()[0]["review_text_status"], "available")
        self.evidence["quote"] = "Before. Works well."
        self.assertEqual(self.enrich()[0]["review_text_status"], "available")

    def test_cache_never_reuses_different_snapshot_version(self):
        self.assertEqual(self.enrich()[0]["review_text"], self.fact["review_text"])
        self.write_facts([{**self.fact, "review_text": "Works well. Changed."}])
        self.assertEqual(self.enrich()[0]["review_text"], "Works well. Changed.")

    def test_prepared_index_serves_unseen_pages_after_process_cache_clear(self):
        self.write_facts([self.fact, {**self.fact, "review_entity_id": "rev_two", "review_text": "Second body"}])
        prepare_review_text(self.root, self.report)
        _read_comments.cache_clear()
        with patch("reporting.evidence_review_index.gzip.open", side_effect=AssertionError("must not rescan facts")):
            self.assertEqual(self.enrich()[0]["review_text"], self.fact["review_text"])
            self.assertEqual(self.enrich([{**self.evidence, "review_entity_id": "rev_two"}])[0]["review_text"], "Second body")

    def test_corrupt_derived_index_is_rebuilt_from_verified_source(self):
        prepare_review_text(self.root, self.report)
        index = next((self.root / "evidence-review-cache").glob("*.sqlite3"))
        index.write_bytes(b"corrupt derived file")
        _read_comments.cache_clear()
        self.assertEqual(self.enrich()[0]["review_text"], self.fact["review_text"])
        self.assertGreater(index.stat().st_size, 100)

    def test_source_hash_failure_cannot_be_hidden_by_prepared_index(self):
        prepare_review_text(self.root, self.report)
        (self.directory / "review_fact.preview.jsonl.gz").write_bytes(b"corrupted original")
        _read_comments.cache_clear()
        self.assertEqual(self.enrich()[0]["review_text_status"], "unavailable")

    def test_unwritable_index_falls_back_without_changing_comment(self):
        with patch("reporting.evidence_reviews.indexed_comments", side_effect=PermissionError("readonly")):
            self.assertEqual(self.enrich()[0]["review_text"], self.fact["review_text"])

    def test_cache_symlink_is_not_followed_or_replaced(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / "evidence-review-cache").symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.enrich()[0]["review_text"], self.fact["review_text"])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_late_malformed_fact_never_publishes_a_partial_index(self):
        path = self.directory / "review_fact.preview.jsonl.gz"
        with gzip.open(path, "wt") as stream:
            stream.write(json.dumps(self.fact) + "\nnot-json\n")
        manifest = self.directory / "manifest.json"
        value = json.loads(manifest.read_text())
        value["hashes"]["preview_fact"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(value))
        self.assertEqual(self.enrich()[0]["review_text_status"], "unavailable")
        self.assertEqual(list((self.root / "evidence-review-cache").glob("*.sqlite3")), [])

    @unittest.skipUnless(find_spec("fastapi") and find_spec("httpx"), "API test dependencies unavailable")
    def test_all_report_evidence_routes_return_full_comment(self):
        from fastapi.testclient import TestClient
        from reporting.app import create_app

        source = self.root / "source"
        source.mkdir()
        with patch("reporting.app.build_catalog_payload", return_value={"tasks": [], "defaults": {"scope": {}}}):
            app = create_app(source, self.root)
        report_id = "rpt_" + "b" * 24
        self.report.update(report_id=report_id, evidence=[self.evidence])
        app.state.reporting.evidence.store(report_id, [self.evidence], {
            "group": {"label": "Fixture", "evidence_ids": ["ev_one"]},
        })
        with patch.object(app.state.reporting.store, "get_report", return_value=self.report):
            with TestClient(app) as client:
                base = f"/reporting/api/v1/reports/{report_id}"
                with patch("reporting.app.enrich_review_text", side_effect=AssertionError("legacy reads must not scan full facts")):
                    for suffix in ("/evidence/ev_one", "/evidence-groups/group", "/evidence"):
                        response = client.get(base + suffix)
                        self.assertEqual(response.status_code, 200, response.text)
                        item = response.json() if suffix.endswith("ev_one") else response.json()["items"][0]
                        self.assertEqual(item["quote"], self.evidence["quote"])
                        self.assertNotIn("review_text", item)
                individual = client.get(base + "/evidence/ev_one?include_review_text=true")
                grouped = client.get(base + "/evidence-groups/group?include_review_text=true")
                listed = client.get(base + "/evidence?include_review_text=true")
                for response in (individual, grouped, listed):
                    self.assertEqual(response.status_code, 200, response.text)
                for item in [individual.json(), grouped.json()["items"][0], listed.json()["items"][0]]:
                    self.assertEqual(item["review_text"], self.fact["review_text"])
                    self.assertEqual(item["quote"], self.evidence["quote"])
                self.assertEqual(grouped.json()["page"]["total_evidence_rows"], 1)
                self.assertEqual(client.get(base + "/evidence-groups/missing").status_code, 404)


if __name__ == "__main__":
    unittest.main()
