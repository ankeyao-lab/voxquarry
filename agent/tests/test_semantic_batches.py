from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent.contracts import AgentError, sha256_value
from agent.semantic_batches import SemanticBatchModel, SemanticBatchStore, PROMPT_VERSION, validate_classification
from agent.store import WenquanStore
from agent.tests.helpers import REPORT_ID, SNAPSHOT_ID, identity


def review(number, entity=None, text="It stopped after 2 years. My old mower was good. I suspect airflow. If runtime were short, I would buy a spare battery."):
    fact = {"review_occurrence_id": f"occ_{number}", "review_entity_id": entity or f"rev_{number}",
            "review_title": "My experience", "review_text": text, "rating": 5, "job_id": "private-ingestion-job"}
    return {**fact, "record_id": fact["review_occurrence_id"], "fact_sha256": sha256_value(fact), "source_id": f"source_{number}"}


def classification(reviews, labels=None):
    return {"results": [{"record_id": r["record_id"], "status": "classified", "unknown_reason": "",
                         "labels": copy.deepcopy(labels if labels is not None else [{"category_id": "motor", "polarity": "negative",
                         "experience": "actual", "quote": "It stopped after 2 years.", "context": "Current mower, after 2 years."}])} for r in reviews]}


class SemanticBatchesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WenquanStore(Path(self.temp.name) / "data" / "wenquan.sqlite3")
        self.store.initialize()
        self.actor = identity()
        self.session_id = self.store.create_session(self.actor, "Semantic test")["session_id"]
        self.batches = SemanticBatchStore(self.store)
        self.query = {"population": "entities", "query": "", "review_ids": [], "filters": {"has_text": True},
                      "term_groups": [], "exclude_terms": []}
        self.report = {"report_id": REPORT_ID, "analysis_version": "synthetic/v1", "status": "completed"}
        self.snapshot = {"snapshot_id": SNAPSHOT_ID, "sha256": sha256_value("frozen")}
        self.provenance = {"report_id": REPORT_ID, "snapshot_id": SNAPSHOT_ID,
                           "analysis_version": "synthetic/v1", "source_fingerprint": sha256_value("fixture")}
        self.spec = {"question": "Which faults are described, under what conditions?", "report": self.report, "snapshot": self.snapshot,
                     "query": self.query, "categories": [{"category_id": "motor", "definition": "Current mower stopping or motor performance."},
                     {"category_id": "runtime", "definition": "Battery runtime."}], "model": "test/model",
                     "prompt_version": PROMPT_VERSION, "source_provenance": self.provenance, "query_sha256": sha256_value(self.query)}
        self.job = self.batches.create_job(actor=self.actor, session_id=self.session_id, spec=self.spec)
        self.args = {"actor": self.actor, "session_id": self.session_id, "job_id": self.job["job_id"]}

    def page(self, rows, offset=0, total=None, limit=24):
        total = offset + len(rows) if total is None else total
        end = offset + len(rows)
        more = end < total
        return {"report": copy.deepcopy(self.report), "snapshot": copy.deepcopy(self.snapshot),
                "data": {"provenance": copy.deepcopy(self.provenance), "query": copy.deepcopy(self.query),
                         "query_sha256": sha256_value(self.query), "items": copy.deepcopy(rows), "sources": []},
                "page": {"offset": offset, "returned": len(rows), "limit": limit, "total": total,
                         "next_offset": end if more else None, "has_more": more}}

    def save(self, rows, offset=0, total=None, labels=None):
        return self.batches.save_page(**self.args, result=self.page(rows, offset, total), classification=classification(rows, labels))

    def test_typographic_quote_is_saved_as_actual_original_without_changing_input(self):
        rows = [review(1, text="It's hard to turn only while self-propel is on.")]
        labels = [{"category_id": "motor", "polarity": "negative", "experience": "actual",
                   "quote": "It’s hard to turn only while self-propel is on.", "context": "Only with self-propel on."}]
        value = classification(rows, labels)
        checked = validate_classification(value, rows, self.spec["categories"])
        self.assertEqual(checked["results"][0]["labels"][0]["quote"], rows[0]["review_text"])
        self.assertIn("It’s", value["results"][0]["labels"][0]["quote"])
        self.batches.save_page(**self.args, result=self.page(rows), classification=value)
        summary = self.batches.summary(**self.args)
        self.assertEqual(summary["categories"][0]["members"][0]["quote"], rows[0]["review_text"])
        wrong = copy.deepcopy(value)
        wrong["results"][0]["labels"][0]["quote"] = "It’s hard to turn while self-propel is on."
        with self.assertRaises(AgentError):
            validate_classification(wrong, rows, self.spec["categories"])

    def test_counts_dedupe_entities_and_separate_experiences(self):
        rows = [review(1, "same"), review(2, "same"), review(3)]
        labels = [
            {"category_id": "motor", "polarity": "negative", "experience": "actual", "quote": "It stopped after 2 years.", "context": "After 2 years, current mower."},
            {"category_id": "motor", "polarity": "positive", "experience": "comparison", "quote": "My old mower was good.", "context": "Old mower comparison."},
            {"category_id": "motor", "polarity": "negative", "experience": "uncertain", "quote": "I suspect airflow.", "context": "User suspects airflow, not confirmed."},
            {"category_id": "runtime", "polarity": "neutral", "experience": "hypothetical", "quote": "If runtime were short, I would buy a spare battery.", "context": "Hypothetical short runtime, not experienced."},
        ]
        self.save(rows[:2], total=3, labels=labels)
        self.save(rows[2:], offset=2, total=3, labels=labels)
        summary = self.batches.summary(**self.args)
        motor, runtime = summary["categories"]
        self.assertEqual(motor["actual_negative_count"], 2)
        self.assertEqual(motor["actual_positive_count"], 0)
        self.assertEqual(motor["counts"]["comparison"]["positive"], 2)
        self.assertEqual(motor["counts"]["uncertain"]["negative"], 2)
        self.assertEqual(runtime["counts"]["hypothetical"]["neutral"], 2)
        self.assertEqual(summary["classified_entities"], 2)
        self.assertEqual(summary["processed_records"], 3)
        self.assertTrue(summary["complete"])
        self.assertEqual(len(motor["members"]), 9)
        self.assertEqual(motor["members"][0]["record_id"], "occ_1")
        self.assertIn("2 years", motor["members"][0]["context"])

    def test_duplicate_page_is_idempotent_and_original_data_remains(self):
        rows = [review(1)]
        result = self.page(rows)
        frozen = copy.deepcopy(result)
        first = self.batches.save_page(**self.args, result=result, classification=classification(rows))
        again = self.batches.save_page(**self.args, result=result, classification={"results": []})
        self.assertEqual(first, again)
        self.assertEqual(result, frozen)
        saved = self.batches.prepare_page(**self.args, result=result)
        self.assertTrue(saved["already_saved"])
        self.assertEqual(saved["classification"], classification(rows))
        with self.store._connect() as db:
            data = json.loads(db.execute("SELECT page_json FROM semantic_pages").fetchone()[0])
        self.assertEqual(data["data"]["items"], rows)

    def test_revision_is_immutable_across_resume_and_new_calls(self):
        self.save([review(1)], total=2)
        before = self.batches.summary(**self.args, revision=1)
        resumed = SemanticBatchStore(WenquanStore(self.store.database_path))
        self.assertEqual(resumed.load_job(**self.args, spec=self.spec)["next_offset"], 1)
        resumed.save_page(**self.args, result=self.page([review(2)], 1, 2), classification=classification([review(2)]))
        resumed.record_call(**self.args, attempt_id="attempt-1")
        resumed.record_call(**self.args, attempt_id="attempt-1")
        self.assertEqual(resumed.summary(**self.args, revision=1), before)
        self.assertEqual(resumed.load_job(**self.args)["model_calls"], 1)
        self.assertEqual(self.batches.create_job(actor=self.actor, session_id=self.session_id, spec=self.spec)["job_id"], self.job["job_id"])
        after = resumed.summary(**self.args)
        self.assertEqual(after["revision"], 2)
        self.assertEqual(after["through_offset"], 2)
        self.assertNotEqual(after["summary_sha256"], before["summary_sha256"])
        self.assertEqual(after["pages"][1]["source_request"]["payload"]["offset"], 1)
        self.assertEqual(after["pages"][1]["source_sha256"], sha256_value(self.page([review(2)], 1, 2)["data"]))

    def test_missing_duplicate_or_fabricated_results_do_not_advance(self):
        rows = [review(1), review(2)]
        good = classification(rows)
        invalid = [dict(results=good["results"][:1]), dict(results=[good["results"][0]] * 2)]
        for field, value in (("quote", "Fabricated claim."), ("category_id", "invented"), ("experience", "fact")):
            bad = copy.deepcopy(good)
            bad["results"][0]["labels"][0][field] = value
            invalid.append(bad)
        for bad in invalid:
            with self.subTest(bad=bad), self.assertRaises(AgentError):
                self.batches.save_page(**self.args, result=self.page(rows), classification=bad)
            self.assertEqual(self.batches.load_job(**self.args)["revision"], 0)
        self.save(rows)
        self.assertTrue(self.batches.load_job(**self.args)["complete"])

    def test_quote_must_belong_to_correct_record_and_conditions_are_preserved(self):
        rows = [review(1), review(2, text="The battery lasted 50 minutes while alternating medium and high blade speed.")]
        result = classification(rows)
        with self.assertRaises(AgentError):
            self.batches.save_page(**self.args, result=self.page(rows), classification=result)
        result["results"][1]["labels"] = [{"category_id": "runtime", "polarity": "neutral", "experience": "actual",
            "quote": rows[1]["review_text"], "context": "50 minutes, alternating medium and high blade speed."}]
        self.batches.save_page(**self.args, result=self.page(rows), classification=result)
        self.assertIn("alternating medium and high", self.batches.summary(**self.args)["categories"][1]["members"][0]["context"])

    def test_pagination_hash_and_total_inconsistency(self):
        rows = [review(1)]
        for field, value in (("offset", 1), ("returned", 2), ("next_offset", 2), ("has_more", False), ("limit", 100)):
            result = self.page(rows, total=2)
            result["page"][field] = value
            with self.subTest(field=field), self.assertRaises(AgentError):
                self.batches.prepare_page(**self.args, result=result)
        result = self.page(rows)
        result["data"]["items"][0]["review_text"] = "Changed body"
        with self.assertRaises(AgentError):
            self.batches.prepare_page(**self.args, result=result)
        self.save(rows, total=2)
        with self.assertRaises(AgentError):
            self.batches.prepare_page(**self.args, result=self.page([review(2)], 1, 3))
        with self.assertRaises(AgentError):
            self.save(rows, offset=1, total=2)
        changed = self.page([review(9)], total=2)
        with self.assertRaises(AgentError):
            self.batches.prepare_page(**self.args, result=changed)

    def test_source_report_query_rule_model_and_prompt_drift_rejected(self):
        for key in ("question", "model", "prompt_version", "categories", "report", "snapshot", "query"):
            spec = copy.deepcopy(self.spec)
            if key == "categories":
                spec[key][0]["definition"] += " Changed."
            elif isinstance(spec[key], dict):
                spec[key]["changed"] = True
            else:
                spec[key] += " changed"
            with self.subTest(key=key), self.assertRaises(AgentError):
                self.batches.load_job(**self.args, spec=spec)
        for key in ("report", "snapshot", "provenance", "query", "query_sha256"):
            result = self.page([review(1)])
            target = result if key in {"report", "snapshot"} else result["data"]
            if isinstance(target[key], dict):
                target[key]["changed"] = True
            else:
                target[key] = sha256_value("wrong")
            with self.subTest(key=key), self.assertRaises(AgentError):
                self.batches.prepare_page(**self.args, result=result)

    def test_actor_session_and_revoked_report_permissions_are_enforced(self):
        other_session = self.store.create_session(self.actor, "Other session")["session_id"]
        for actor, session_id in ((identity("user-b"), self.session_id), (self.actor, other_session),
                                  (replace(self.actor, actor_id="other-actor"), self.session_id),
                                  (replace(self.actor, authority_id="other-authority"), self.session_id),
                                  (replace(self.actor, allowed_report_ids=frozenset()), self.session_id)):
            args = {**self.args, "actor": actor, "session_id": session_id}
            for method in (self.batches.load_job, self.batches.summary):
                with self.subTest(actor=actor.actor_id, session=session_id), self.assertRaises(AgentError):
                    method(**args)

    def test_unknown_and_no_mention_are_distinct(self):
        rows = [review(1), review(2)]
        classified = classification(rows, [])
        classified["results"][0].update(status="unknown", unknown_reason="Cannot identify which mower.")
        self.batches.save_page(**self.args, result=self.page(rows), classification=classified)
        summary = self.batches.summary(**self.args)
        self.assertEqual(summary["unknown_entities"], 1)
        self.assertEqual(summary["unknown"][0]["record_id"], "occ_1")
        self.assertEqual(summary["categories"][0]["actual_negative_count"], 0)

    def test_empty_page_completes_without_model_request(self):
        class Runtime:
            def complete(self, **kwargs):
                raise AssertionError("empty population needs no model")
        model = SemanticBatchModel(Runtime())
        empty = self.page([])
        result = model.classify_page(job=self.job, result=empty)
        self.batches.save_page(**self.args, result=empty, classification=result)
        summary = self.batches.summary(**self.args)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["total"], 0)

    def test_model_uses_one_request_full_text_projection_and_exact_contract(self):
        rows = [review(1)]
        observed = []
        class Runtime:
            def complete(self, **kwargs):
                observed.append(kwargs)
                return {"choices": [{"message": {"content": json.dumps(classification(rows))}}]}
        model = SemanticBatchModel(Runtime())
        diagnostics = {}
        result = model.classify_page(job=self.job, result=self.page(rows), diagnostics=diagnostics)
        self.assertEqual(result, classification(rows))
        self.assertEqual(len(observed), 1)
        self.assertTrue(diagnostics["request_started"])
        context = json.loads(observed[0]["messages"][1]["content"])
        self.assertEqual(context["reviews"][0]["data"]["review_text"], rows[0]["review_text"])
        self.assertNotIn("job_id", context["reviews"][0]["data"])
        self.assertEqual(observed[0]["tools"], [])

    def test_query_cannot_contain_report_selection_sql_or_pagination(self):
        for key in ("report_id", "sql", "offset", "limit", "cursor"):
            spec = copy.deepcopy(self.spec)
            spec["query"][key] = "forbidden"
            with self.subTest(key=key), self.assertRaises(AgentError):
                self.batches.create_job(actor=self.actor, session_id=self.session_id, spec=spec)

    def test_initial_query_hash_must_match_actual_request_and_defaults(self):
        spec = copy.deepcopy(self.spec)
        spec["query"]["filters"]["ratings"] = [1]
        with self.assertRaises(AgentError):
            self.batches.create_job(actor=self.actor, session_id=self.session_id, spec=spec)
        spec = copy.deepcopy(self.spec)
        spec["query"] = {"filters": {"has_text": True}, "match_mode": "substring"}
        job = self.batches.create_job(actor=self.actor, session_id=self.session_id, spec=spec)
        self.batches.prepare_page(**{**self.args, "job_id": job["job_id"]}, result=self.page([review(1)]))

    def test_model_and_prompt_drift_fail_before_provider_and_never_retry(self):
        calls = []
        class Runtime:
            _settings = SimpleNamespace(model="different-model")
            def complete(self, **kwargs):
                calls.append(kwargs)
                raise AgentError("llm_timeout", "Test timeout")
        runtime = Runtime()
        model = SemanticBatchModel(runtime)
        with self.assertRaises(AgentError):
            model.classify_page(job=self.job, result=self.page([review(1)]))
        self.assertEqual(calls, [])
        runtime._settings = SimpleNamespace(model=self.spec["model"])
        changed = copy.deepcopy(self.job)
        changed["spec"]["prompt_version"] = "another-version"
        with self.assertRaises(AgentError):
            model.classify_page(job=changed, result=self.page([review(1)]))
        self.assertEqual(calls, [])
        with self.assertRaises(AgentError):
            model.classify_page(job=self.job, result=self.page([review(1)]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.batches.load_job(**self.args)["revision"], 0)

    def test_batch_limits_and_zero_progress_page(self):
        with self.assertRaises(AgentError):
            self.batches.prepare_page(**self.args, result=self.page([review(n) for n in range(33)], limit=32))
        with self.assertRaises(AgentError):
            self.batches.prepare_page(**self.args, result=self.page([], total=3))

    def test_unknown_review_identity_is_explicit_not_presented_as_unique_people(self):
        raw = review(1)
        raw["review_entity_id"] = None
        raw["fact_sha256"] = sha256_value({k: v for k, v in raw.items() if k not in {"record_id", "fact_sha256", "source_id"}})
        self.save([raw])
        summary = self.batches.summary(**self.args)
        self.assertEqual(summary["unknown_identity_records"], ["occ_1"])
        self.assertEqual(summary["categories"][0]["unknown_identity_counts"]["actual"]["negative"], 1)
        self.assertIn("不是人数", summary["count_meaning"])

    def test_diagnostics_distinguish_schema_fields_from_missing_reviews(self):
        rows = [review(1), review(2)]
        bad = classification(rows)
        del bad["results"][0]["unknown_reason"]
        diagnostics = {"status": "received"}
        with self.assertRaises(AgentError) as error:
            validate_classification(bad, rows, self.spec["categories"], diagnostics)
        self.assertEqual(error.exception.code, "llm_contract_invalid")
        self.assertEqual(diagnostics["status"], "invalid")
        self.assertEqual(diagnostics["failure_stage"], "semantic_classification_shape")
        self.assertEqual(diagnostics["classification_coverage"]["matched_records"], 2)
        self.assertEqual(diagnostics["classification_coverage"]["missing_record_ids"], [])
        self.assertEqual(diagnostics["schema_errors"], [{"path": ["results", 0], "validator": "required", "missing_fields": ["unknown_reason"]}])

    def test_coverage_diagnostics_never_accept_missing_duplicate_or_unknown_ids(self):
        rows = [review(1), review(2)]
        good = classification(rows)
        cases = [good["results"][:1], [good["results"][0], good["results"][0]],
                 [{**good["results"][0], "record_id": "untrusted-secret-value"}, good["results"][1]]]
        for returned in cases:
            diagnostics = {}
            with self.subTest(returned=returned), self.assertRaises(AgentError):
                validate_classification({"results": returned}, rows, self.spec["categories"], diagnostics)
            self.assertEqual(diagnostics["failure_stage"], "semantic_classification_coverage")
            self.assertEqual(diagnostics["classification_coverage"]["expected_records"], 2)
            self.assertEqual(diagnostics["classification_coverage"]["returned_records"], len(returned))
            self.assertNotIn("untrusted-secret-value", json.dumps(diagnostics))
        self.assertEqual(diagnostics["classification_coverage"]["unexpected_record_count"], 1)

    def test_quote_context_and_unknown_reason_failures_have_safe_precise_paths(self):
        rows = [review(1), review(2, text="A different review body.")]
        cases = [
            ("semantic_classification_evidence", {"quote": "Untrusted fabricated private text."}),
            ("semantic_classification_context", {"context": "   "}),
        ]
        for stage, change in cases:
            value = classification(rows[:1])
            value["results"][0]["labels"][0].update(change)
            diagnostics = {}
            with self.subTest(stage=stage), self.assertRaises(AgentError):
                validate_classification(value, rows[:1], self.spec["categories"], diagnostics)
            self.assertEqual(diagnostics["failure_stage"], stage)
            self.assertEqual(diagnostics["schema_errors"][0]["record_id"], "occ_1")
            self.assertNotIn("Untrusted fabricated private text", json.dumps(diagnostics))
            self.assertNotIn(rows[0]["review_text"], json.dumps(diagnostics))
        value = classification(rows)
        diagnostics = {}
        with self.assertRaises(AgentError):
            validate_classification(value, rows, self.spec["categories"], diagnostics)
        self.assertEqual(diagnostics["schema_errors"][0]["path"], ["results", 1, "labels", 0, "quote"])
        value = classification(rows[:1])
        value["results"][0]["status"] = "unknown"
        with self.assertRaises(AgentError):
            validate_classification(value, rows[:1], self.spec["categories"], diagnostics)
        self.assertEqual(diagnostics["failure_stage"], "semantic_classification_unknown")

    def test_diagnostics_are_bounded_and_do_not_echo_invalid_field_values(self):
        rows = [review(index) for index in range(24)]
        bad = classification(rows)
        for row in bad["results"]:
            row.pop("status")
            row["unexpected-secret-field"] = "private-value"
        diagnostics = {}
        with self.assertRaises(AgentError):
            validate_classification(bad, rows, self.spec["categories"], diagnostics)
        self.assertEqual(len(diagnostics["schema_errors"]), 8)
        self.assertNotIn("unexpected-secret-field", json.dumps(diagnostics))
        self.assertNotIn("private-value", json.dumps(diagnostics))
        self.assertEqual(diagnostics["classification_coverage"]["matched_records"], 24)

    def test_one_call_format_repair_uses_previous_as_untrusted_data_and_revalidates(self):
        rows = [review(1)]
        bad = classification(rows)
        del bad["results"][0]["unknown_reason"]
        good = classification(rows)
        captured = []
        responses = [bad, good]
        class Runtime:
            def complete(self, **kwargs):
                captured.append(json.loads(kwargs["messages"][1]["content"]))
                return {"choices": [{"message": {"content": json.dumps(responses.pop(0))}}]}
        model = SemanticBatchModel(Runtime())
        diagnostics = {}
        with self.assertRaises(AgentError):
            model.classify_page(job=self.job, result=self.page(rows), diagnostics=diagnostics)
        self.assertEqual(len(captured), 1)
        self.assertEqual(diagnostics["status"], "invalid")
        self.assertEqual(diagnostics["failure_stage"], "semantic_classification_shape")
        self.assertEqual(diagnostics["classification_batch"], {"job_id": self.job["job_id"], "offset": 0,
            "expected_records": 1, "source_sha256": sha256_value(self.page(rows)["data"])})
        repaired = model.classify_page(job=self.job, result=self.page(rows), previous=bad,
            format_feedback={"schema_errors": diagnostics["schema_errors"]})
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[1]["format_repair"]["previous"], bad)
        self.assertEqual(captured[1]["reviews"][0]["data"]["review_text"], rows[0]["review_text"])
        self.assertEqual(repaired, good)
        # Recovery lives in the caller; a rejected response never writes a page.
        self.assertEqual(self.batches.load_job(**self.args)["revision"], 0)


if __name__ == "__main__":
    unittest.main()
