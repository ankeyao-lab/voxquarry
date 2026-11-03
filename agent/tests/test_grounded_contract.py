from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent.contracts import AgentError
from agent.tests import test_readonly_qa as readonly_tests
from agent.tests.helpers import GROUP_ID, METRIC_ID, REPORT_ID


class GroundedAnswerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.schema = json.loads((root / "schemas/voxquarry-wenquan-grounded-answer-v1.schema.json").read_text())
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
        cls.citation_validator = Draft202012Validator(json.loads(
            (root.parent / "reporting/schemas/zhuojian-citation-v1.schema.json").read_text()
        ))

    def setUp(self) -> None:
        # Reuse the real read-only pipeline fixture, not a hand-written success
        # template: SQLite persistence, reporting tools, and citation re-read all run.
        self.flow = readonly_tests.ReadonlyQATests()
        self.flow.setUp()
        self.addCleanup(self.flow.tearDown)

    def answered_metadata(self) -> dict:
        self.flow.use_runtime(readonly_tests.StaticRuntime(readonly_tests.selection(
            "metric:" + METRIC_ID, "finding:pain", "table:pain_points:0", "scope", "quality",
            groups=(GROUP_ID,),
        )))
        run, message = self.flow.submit("概括指标、发现、表格、范围、限制并给出原文。")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(message["answer_status"], "answered")
        return message["metadata"]

    def assert_rejected(self, metadata: dict) -> None:
        self.assertTrue(list(self.validator.iter_errors(metadata)), "invalid answer metadata passed")

    def test_actual_six_claim_types_and_presentation_citations_conform(self) -> None:
        metadata = self.answered_metadata()
        self.validator.validate(metadata)
        self.assertEqual({claim["claim_type"] for claim in metadata["claims"]}, {
            "metric", "saved_finding", "saved_table_row", "scope", "data_quality", "evidence",
        })
        citation_ids = {citation["citation_id"] for citation in metadata["citations"]}
        for claim in metadata["claims"]:
            self.assertTrue(set(claim["citation_ids"]).issubset(citation_ids))
            self.assertEqual(claim["report_id"], metadata["report_id"])
            self.assertEqual(claim["snapshot_id"], metadata["snapshot"]["snapshot_id"])
        for citation in metadata["citations"]:
            source_citation = {key: value for key, value in citation.items() if key != "href"}
            self.citation_validator.validate(source_citation)
            self.assertTrue(citation["href"].startswith("/"))

    def test_evidence_only_answer_may_have_no_fact_ids(self) -> None:
        self.flow.use_runtime(readonly_tests.StaticRuntime(readonly_tests.selection(groups=(GROUP_ID,))))
        run, message = self.flow.submit("给我动力相关原文例子。")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(message["metadata"]["fact_ids"], [])
        self.validator.validate(message["metadata"])

    def test_usage_details_and_nullable_model(self) -> None:
        metadata = self.answered_metadata()
        metadata.update({"model": None, "model_calls": 2})
        metadata["usage"] = {
            "prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 5},
        }
        self.validator.validate(metadata)
        metadata["usage"]["completion_tokens_details"]["reasoning_content"] = "model thought"
        self.assert_rejected(metadata)

    def test_actual_missing_sidecar_answer_preserves_unavailable_status(self) -> None:
        self.flow.reporting.evidence.path_for(REPORT_ID).unlink()
        run, message = self.flow.submit()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(message["metadata"]["evidence_status"], "unavailable")
        self.assertNotIn("evidence", {c["claim_type"] for c in message["metadata"]["claims"]})
        self.validator.validate(message["metadata"])

    def test_no_empty_claims_citations_hashes_or_invented_body_fields(self) -> None:
        valid = self.answered_metadata()
        mutations = {
            "empty claims": lambda m: m.update(claims=[]),
            "empty citations": lambda m: m.update(citations=[]),
            "empty claim citations": lambda m: m["claims"][0].update(citation_ids=[]),
            "bad source hash": lambda m: m["claims"][0].update(source_sha256="not-a-hash"),
            "model body": lambda m: m.update(answer="invented model answer"),
            "claim model body": lambda m: m["claims"][0].update(model_text="99.9% invented"),
            "model reasoning": lambda m: m.update(reasoning_content="private thought"),
            "wrong report state": lambda m: m["report"].update(status="failed"),
            "unknown evidence status": lambda m: m.update(evidence_status="available"),
            "negative usage": lambda m: m["usage"].update(total_tokens=-1),
            "invalid model calls": lambda m: m.update(model_calls=3),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                metadata = copy.deepcopy(valid)
                mutate(metadata)
                self.assert_rejected(metadata)

    def test_claim_pointer_types_and_evidence_fingerprint_are_required(self) -> None:
        valid = self.answered_metadata()
        for claim_type in ("metric", "saved_finding", "saved_table_row", "scope", "data_quality", "evidence"):
            with self.subTest(claim_type=claim_type):
                metadata = copy.deepcopy(valid)
                claim = next(c for c in metadata["claims"] if c["claim_type"] == claim_type)
                claim["source_pointer"] = {"url": "https://untrusted.invalid/arbitrary"}
                self.assert_rejected(metadata)
        for mutation in ("quote hash", "group id", "external href"):
            with self.subTest(mutation=mutation):
                metadata = copy.deepcopy(valid)
                citation = next(c for c in metadata["citations"] if c["citation_type"] == "evidence")
                if mutation == "quote hash":
                    citation["quote_sha256"] = None
                elif mutation == "group id":
                    citation["evidence_group_id"] = None
                else:
                    citation["href"] = "https://untrusted.invalid/"
                self.assert_rejected(metadata)

    def test_failed_run_is_not_a_success_contract(self) -> None:
        self.flow.use_runtime(readonly_tests.StaticRuntime(error=AgentError(
            "llm_timeout", "合成超时", http_status=504, retryable=True,
        )))
        run, message = self.flow.submit()
        self.assertEqual(run["status"], "failed")
        self.assertNotIn("contract", message["metadata"])
        self.assert_rejected(message["metadata"])

    def test_failure_during_final_persistence_does_not_keep_success_contract(self) -> None:
        original_finish = self.flow.qa.turns.finish
        for failure in (AgentError("synthetic_finish_failure", "合成结束故障"), RuntimeError("synthetic finish failure")):
            with self.subTest(error_type=type(failure).__name__):
                def fail_success_once(*args: object, **kwargs: object) -> dict:
                    if kwargs["status"] == "completed":
                        raise failure
                    return original_finish(*args, **kwargs)

                with patch.object(self.flow.qa.turns, "finish", side_effect=fail_success_once):
                    run, message = self.flow.submit()
                self.assertEqual(run["status"], "failed")
                self.assertNotIn("contract", message["metadata"])
                self.assert_rejected(message["metadata"])


if __name__ == "__main__":
    unittest.main()
