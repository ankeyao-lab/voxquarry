"""Offline integration of research batching, real persistence and citation reads."""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from agent.contracts import AgentError, sha256_value
from agent.research_batches import batch_tool, run_batches, semantic_source
from agent.research_grounding import render_evidence_statements
from agent.research_grounding import review_sources
from agent.research_model import _model_source
from agent.semantic_batches import DEFAULT_BATCH_SIZE, SemanticBatchStore
from agent.tests import test_semantic_batches as fixtures
from reporting.research_data import normalize_query


class Runtime:
    def __init__(self):
        self._settings = SimpleNamespace(model="test/model")
        self.seen = []
        self.failure = None
        self.labels = None

    def complete(self, **kwargs):
        context = json.loads(kwargs["messages"][1]["content"])
        rows = [row["data"] for row in context["reviews"]]
        self.seen.append([row["record_id"] for row in rows])
        if self.failure:
            raise self.failure
        value = fixtures.classification(rows, self.labels)
        return {"model": self._settings.model, "choices": [{"message": {"content": json.dumps(value)}}]}


class ResearchBatchesTests(unittest.TestCase):
    def setUp(self):
        # Reuse the small source contract fixture without inheriting/re-running
        # its separate storage test suite.
        self.fixture = fixtures.SemanticBatchesTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.actor = self.fixture.actor
        self.session_id = self.fixture.session_id
        self.batches = self.fixture.batches
        self.runtime = Runtime()
        self.rows = [fixtures.review(index) for index in range(50)]
        self.remaining = 14
        self.attempts = 0
        self.calls, self.registrations, self.saved, self.events = [], [], [], []
        self.sources = {}
        self.page_size = DEFAULT_BATCH_SIZE
        self.query_args = {"query": {}, "categories": copy.deepcopy(self.fixture.spec["categories"])}

    def call(self, name, payload):
        self.assertEqual(name, "query_report_reviews")
        self.assertEqual(payload["report_id"], self.fixture.report["report_id"])
        self.calls.append(copy.deepcopy(payload))
        offset = payload["offset"]
        limit = min(payload["limit"], self.page_size)
        result = self.fixture.page(self.rows[offset:offset + limit], offset, len(self.rows), payload["limit"])
        result["data"]["query"] = normalize_query(payload)
        result["data"]["query_sha256"] = sha256_value(result["data"]["query"])
        return result

    def register(self, name, payload, result):
        self.registrations.append((name, copy.deepcopy(payload), copy.deepcopy(result)))
        for row in result["data"]["items"]:
            self.sources[row["record_id"]] = {"kind": "review", "record_id": row["record_id"],
                "text": json.dumps(row), "source_request": {"tool": name, "payload": copy.deepcopy(payload)}}

    def invoke(self, method, **kwargs):
        hook = kwargs.pop("_attempt_hook")
        diagnostics = {}
        candidate = {}
        def received(response, _info):
            candidate["content"] = response["choices"][0]["message"]["content"]
        try:
            return method(**kwargs, diagnostics=diagnostics, on_response=received)
        except AgentError as error:
            error.call_diagnostics = diagnostics
            error.raw_candidate = candidate.get("content")
            raise
        finally:
            if diagnostics.get("request_started"):
                self.remaining -= 1
                self.attempts += 1
                hook(f"test-run:{self.attempts}")

    def run_job(self, job_id=None):
        return run_batches(store=self.batches, actor=self.actor, session_id=self.session_id,
            question=self.fixture.spec["question"], report_id=self.fixture.report["report_id"], runtime=self.runtime,
            args=self.query_args, call=self.call, register=self.register, invoke=self.invoke,
            budget_left=lambda: self.remaining, save_job=lambda job, args: self.saved.append((copy.deepcopy(job), copy.deepcopy(args))),
            progress=lambda *args, **kwargs: self.events.append((args, kwargs)), sources=self.sources, job_id=job_id)

    def qa(self):
        def invoke(actor, tool, payload, *, session_id):
            self.assertEqual(actor, self.actor)
            self.assertEqual(session_id, self.session_id)
            return self.call(tool, payload)
        return SimpleNamespace(semantic=self.batches, invoke=invoke)

    def payload(self, job, revision=None):
        return {"report_id": self.fixture.report["report_id"], "job_id": job["job_id"],
                "revision": job["revision"] if revision is None else revision}

    def test_program_reads_every_page_once_and_preserves_full_classification(self):
        job = self.run_job()
        self.assertTrue(job["complete"])
        self.assertEqual((job["next_offset"], job["total"], job["revision"], job["model_calls"]), (50, 50, 3, 3))
        self.assertEqual([call["offset"] for call in self.calls], [0, 24, 48])
        self.assertEqual([len(rows) for rows in self.runtime.seen], [24, 24, 2])
        self.assertEqual([rid for batch in self.runtime.seen for rid in batch], [row["record_id"] for row in self.rows])
        summary = self.batches.summary(actor=self.actor, session_id=self.session_id, job_id=job["job_id"])
        self.assertEqual(summary["categories"][0]["actual_negative_count"], 50)
        self.assertEqual(len(summary["categories"][0]["members"]), 50)
        self.assertEqual(self.remaining, 11)
        self.assertEqual(self.saved[-1][0]["revision"], 3)
        self.assertEqual({rid for rid, source in self.sources.items() if source.get("in_model")}, {"occ_0", "occ_1", "occ_49"})
        self.assertEqual(len(self.sources), 50)

    def test_byte_limited_pages_use_returned_next_offset_not_requested_limit(self):
        self.rows = self.rows[:18]
        self.page_size = 7
        job = self.run_job()
        self.assertEqual([call["offset"] for call in self.calls], [0, 7, 14])
        self.assertEqual([len(rows) for rows in self.runtime.seen], [7, 7, 4])
        self.assertEqual(job["next_offset"], 18)
        self.assertTrue(job["complete"])

    def test_five_calls_are_reserved_and_resume_starts_at_saved_offset(self):
        self.remaining = 6
        first = self.run_job()
        self.assertEqual((first["next_offset"], first["revision"], first["complete"]), (24, 1, False))
        self.assertEqual(self.remaining, 5)
        self.assertEqual([call["offset"] for call in self.calls], [0])
        frozen = self.batches.summary(actor=self.actor, session_id=self.session_id, job_id=first["job_id"], revision=1)
        self.batches = SemanticBatchStore(self.fixture.store)
        self.remaining = 14
        self.calls.clear()
        second = self.run_job(first["job_id"])
        self.assertTrue(second["complete"])
        self.assertEqual([call["offset"] for call in self.calls], [0, 24, 48])
        self.assertEqual([len(rows) for rows in self.runtime.seen], [24, 24, 2])
        self.assertEqual(second["model_calls"], 3)
        self.assertEqual(self.batches.summary(actor=self.actor, session_id=self.session_id, job_id=first["job_id"], revision=1), frozen)
        self.calls.clear()
        self.run_job(second["job_id"])
        self.assertEqual(len(self.runtime.seen), 3)

    def test_exactly_five_calls_do_not_classify_a_page(self):
        self.remaining = 5
        job = self.run_job()
        self.assertEqual(job["revision"], 0)
        self.assertEqual(job["model_calls"], 0)
        self.assertFalse(job["complete"])
        self.assertEqual(self.runtime.seen, [])

    def test_failed_model_attempt_is_counted_without_advancing_and_can_resume(self):
        self.runtime.failure = AgentError("llm_timeout", "Synthetic timeout")
        with self.assertRaises(AgentError):
            self.run_job()
        job = self.saved[-1][0]
        fresh = self.batches.load_job(actor=self.actor, session_id=self.session_id, job_id=job["job_id"])
        self.assertEqual((fresh["revision"], fresh["next_offset"], fresh["model_calls"]), (0, 0, 1))
        self.assertEqual(self.registrations, [])
        self.runtime.failure = None
        done = self.run_job(job["job_id"])
        self.assertTrue(done["complete"])
        self.assertEqual(done["model_calls"], 4)
        self.assertEqual(self.runtime.seen[0], self.runtime.seen[1])

    def test_one_format_repair_uses_previous_and_errors_without_advancing_early(self):
        self.rows = self.rows[:3]
        complete = self.runtime.complete
        contexts = []
        def first_incomplete(**kwargs):
            context = json.loads(kwargs["messages"][1]["content"])
            contexts.append(context)
            result = complete(**kwargs)
            if len(contexts) == 1:
                candidate = json.loads(result["choices"][0]["message"]["content"])
                candidate["results"].pop()
                result["choices"][0]["message"]["content"] = json.dumps(candidate)
            return result
        self.runtime.complete = first_incomplete
        job = self.run_job()
        self.assertEqual(job["model_calls"], 2)
        self.assertEqual(job["revision"], 1)
        self.assertEqual(job["next_offset"], 3)
        self.assertEqual(len(contexts[1]["format_repair"]["previous"]["results"]), 2)
        self.assertTrue(contexts[1]["format_repair"]["feedback"]["classification_coverage"]["missing_record_ids"])

    def test_repeated_format_failure_spends_only_two_calls_and_saves_no_page(self):
        self.runtime.failure = AgentError("llm_contract_invalid", "Invalid synthetic response")
        with self.assertRaises(AgentError):
            self.run_job()
        job = self.batches.load_job(actor=self.actor, session_id=self.session_id, job_id=self.saved[-1][0]["job_id"])
        self.assertEqual((job["model_calls"], job["revision"], job["next_offset"]), (2, 0, 0))

    def test_resume_rejects_changed_model_rules_and_source_before_more_classification(self):
        self.remaining = 6
        first = self.run_job()
        self.remaining = 14
        cases = (
            (self.runtime._settings, "model", "different-model"),
            (self.query_args["categories"][0], "definition", "Another classification rule"),
            (self.fixture.snapshot, "sha256", sha256_value("new snapshot")),
            (self.fixture.provenance, "source_fingerprint", sha256_value("new source")),
        )
        for target, key, changed in cases:
            old = target[key] if isinstance(target, dict) else getattr(target, key)
            if isinstance(target, dict):
                target[key] = changed
            else:
                setattr(target, key, changed)
            try:
                with self.subTest(key=key), self.assertRaises(AgentError):
                    self.run_job(first["job_id"])
                self.assertEqual(len(self.runtime.seen), 1)
            finally:
                if isinstance(target, dict):
                    target[key] = old
                else:
                    setattr(target, key, old)

    def test_resume_verifies_reread_first_page_body_against_saved_hash(self):
        self.remaining = 6
        first = self.run_job()
        self.remaining = 14
        self.rows[0] = fixtures.review(0, text="Changed first page body, despite unchanged source fingerprint.")
        with self.assertRaises(AgentError):
            self.run_job(first["job_id"])
        self.assertEqual(len(self.runtime.seen), 1)

    def test_actual_comparison_hypothesis_counts_are_separate_and_citable(self):
        self.rows = self.rows[:4]
        self.runtime.labels = [
            {"category_id": "motor", "polarity": "negative", "experience": "actual", "quote": "It stopped after 2 years.", "context": "Current mower, after two years."},
            {"category_id": "motor", "polarity": "positive", "experience": "comparison", "quote": "My old mower was good.", "context": "Old mower, not current one."},
            {"category_id": "motor", "polarity": "negative", "experience": "uncertain", "quote": "I suspect airflow.", "context": "User suspicion, not confirmed cause."},
            {"category_id": "runtime", "polarity": "neutral", "experience": "hypothetical", "quote": "If runtime were short, I would buy a spare battery.", "context": "Hypothetical condition, not actual short runtime."},
        ]
        job = self.run_job()
        result = semantic_source(self.qa(), self.actor, self.session_id, self.payload(job))
        summary = result["data"]
        self.assertEqual(summary["categories"][0]["actual_positive_count"], 0)
        self.assertEqual(summary["categories"][0]["actual_negative_count"], 4)
        self.assertEqual(summary["categories"][0]["counts"]["comparison"]["positive"], 4)
        self.assertEqual(summary["categories"][0]["counts"]["uncertain"]["negative"], 4)
        self.assertEqual(summary["categories"][1]["counts"]["hypothetical"]["neutral"], 4)
        sources = {"S1": {"kind": "analysis", "text": json.dumps(summary)}}
        writing = {"statements": [{"statement_id": "P1", "task_ids": ["Q1"],
            "text": "4 条评论描述当前机器停机。另有 4 条旧机正面比较，单独列出。",
            "evidence": [{"source_id": "S1", "pointer": "/categories/0/actual_negative_count"},
                         {"source_id": "S1", "pointer": "/categories/0/counts/comparison/positive"}]}], "unanswered": []}
        answer = render_evidence_statements(writing, [], sources, [{"task_id": "Q1", "question": "实际负面和比较分别有多少条"}])
        self.assertEqual(answer["citations"], [{"source_id": "S1"}])
        self.assertEqual(answer["status"], "answered")

    def test_historical_revision_revalidates_all_its_pages_and_stays_immutable(self):
        job = self.run_job()
        self.calls.clear()
        result = semantic_source(self.qa(), self.actor, self.session_id, self.payload(job, revision=2))
        self.assertEqual([call["offset"] for call in self.calls], [0, 24])
        self.assertEqual(result["data"]["processed_records"], 48)
        self.assertEqual(result["data"]["revision"], 2)
        self.assertFalse(result["data"]["complete"])
        self.assertNotIn("items", result["data"])
        before = copy.deepcopy(result)
        self.batches.record_call(actor=self.actor, session_id=self.session_id, job_id=job["job_id"], attempt_id="later-count-only")
        self.assertEqual(semantic_source(self.qa(), self.actor, self.session_id, self.payload(job, revision=2)), before)

    def test_original_review_pagination_reads_full_text_and_rechecks_every_page(self):
        self.rows[-1] = fixtures.review(49, text="It stopped after 2 years.\n\n" + "Full original body. " * 3000)
        job = self.run_job()
        payload = self.payload(job)
        originals = []
        for offset, returned, next_offset in ((0, 24, 24), (24, 24, 48), (48, 2, None)):
            self.calls.clear()
            result = semantic_source(self.qa(), self.actor, self.session_id, payload, include_reviews=True, offset=offset)
            self.assertEqual([call["offset"] for call in self.calls], [0, 24, 48])
            self.assertEqual(result["page"], {"offset": offset, "returned": returned, "total": 50,
                "has_more": next_offset is not None, "next_offset": next_offset})
            originals.extend(result["data"]["items"])
        self.assertEqual(originals, self.rows)
        self.assertGreater(len(originals[-1]["review_text"]), 50000)
        self.assertEqual(len(self.runtime.seen), 3)

    def test_changed_unshown_page_still_invalidates_original_reader(self):
        job = self.run_job()
        self.rows[49] = fixtures.review(49, text="A changed last page with a new individual hash.")
        with self.assertRaises(AgentError) as caught:
            semantic_source(self.qa(), self.actor, self.session_id, self.payload(job), include_reviews=True, offset=0)
        self.assertEqual(caught.exception.code, "source_changed")

    def test_cancel_during_source_verification_stops_before_next_page(self):
        job = self.run_job()
        self.calls.clear()
        progress = []
        def active():
            if self.calls:
                raise AgentError("run_cancelled", "Stopped", http_status=409)
        with self.assertRaises(AgentError) as caught:
            semantic_source(self.qa(), self.actor, self.session_id, self.payload(job),
                            check_active=active, on_progress=lambda *value: progress.append(value))
        self.assertEqual(caught.exception.code, "run_cancelled")
        self.assertEqual([call["offset"] for call in self.calls], [0])
        self.assertEqual(progress, [])

    def test_source_rejects_wrong_report_and_actor_before_reading_upstream(self):
        job = self.run_job()
        self.calls.clear()
        with self.assertRaises(AgentError):
            semantic_source(self.qa(), self.actor, self.session_id, {**self.payload(job), "report_id": "rpt_other"})
        with self.assertRaises(AgentError):
            semantic_source(self.qa(), fixtures.identity("other-user"), self.session_id, self.payload(job))
        self.assertEqual(self.calls, [])

    def test_batch_tool_removes_pagination_and_does_not_mutate_upstream_schema(self):
        tools = [{"name": "query_report_reviews", "parameters": {"type": "object", "additionalProperties": False,
            "required": ["limit"], "properties": {"query": {"type": "string"}, "offset": {"type": "integer"},
                "limit": {"type": "integer"}, "cursor": {"type": "string"}}}}]
        original = copy.deepcopy(tools)
        schema = batch_tool(tools)["parameters"]
        valid = {"query": {}, "categories": [{"category_id": "runtime", "definition": "Battery runtime."}]}
        self.assertTrue(Draft202012Validator(schema).is_valid(valid))
        for key in ("offset", "limit", "cursor", "report_id", "sql"):
            self.assertFalse(Draft202012Validator(schema).is_valid({**valid, "query": {key: "untrusted"}}))
        self.assertEqual(tools, original)

    def test_summary_model_projection_keeps_filter_paths_and_drops_unbounded_members(self):
        self.query_args["query"] = {"filters": {"ratings": [1]}, "term_groups": [{"label": "motor", "terms": ["stopped"]}]}
        job = self.run_job()
        summary = self.batches.summary(actor=self.actor, session_id=self.session_id, job_id=job["job_id"])
        original = copy.deepcopy(summary)
        sources = {"S1": {"kind": "analysis", "text": json.dumps(summary)}}
        projected = _model_source("S1", sources["S1"])["data"]
        self.assertEqual(projected["spec"]["query"], summary["spec"]["query"])
        self.assertEqual(projected["spec"]["question"], summary["spec"]["question"])
        self.assertEqual(set(projected["spec"]), {"query", "question"})
        self.assertEqual(projected["spec"]["query"]["filters"]["ratings"], [1])
        self.assertNotIn("members", projected["categories"][0])
        self.assertNotIn("pages", projected)
        self.assertEqual(projected["categories"][0]["actual_negative_count"], 50)
        examples = projected["categories"][0]["_representative_examples"]
        self.assertEqual(len(examples), 3)
        self.assertFalse(projected["categories"][0]["_examples_are_complete"])
        for example in examples:
            member_index = int(example["pointer"].rsplit("/", 1)[1])
            member = summary["categories"][0]["members"][member_index]
            self.assertEqual(example["quote"], member["quote"])
            self.assertEqual(example["context"], member["context"])
            self.assertEqual(example["record_id"], member["record_id"])
        finding = {"finding_id": "F1", "evidence": [{"source_id": "S1", "pointer": "/categories/0/actual_negative_count"}]}
        reviewed = review_sources([finding], sources)[0]["data"]
        self.assertEqual(reviewed["spec"]["query"], summary["spec"]["query"])
        self.assertNotIn("members", reviewed["categories"][0])
        self.assertEqual(summary, original)


if __name__ == "__main__":
    unittest.main()
