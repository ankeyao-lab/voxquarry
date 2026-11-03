from __future__ import annotations

import copy
import json
import unittest
import time
from types import SimpleNamespace
from unittest.mock import Mock

from agent.contracts import AgentError
from agent.question_tasks import explicit_requirements, retained_coverage
from agent.research_grounding import (
    GroundedResearchModel, finish_grounded_answer, pointer_value, render_statements, validate_findings, preserve_checked_findings, review_units, review_sources,
)
from agent.tests import test_report_research as fixtures


def fact(source="R1", **changes):
    row = {"finding_id": "F1", "kind": "experience", "statement": "有用户反映机器使用2年后停机。",
           "context": "当前机器，使用2年；不是早期故障。", "evidence": [{"source_id": source, "quote": "It stopped after 2 years."}]}
    row.update(changes)
    return row


def review(ids, key="finding_id", failures=None):
    failures = failures or {}
    return {"items": [{key: key_id, "status": "revise" if key_id in failures else "pass", "feedback": failures.get(key_id, "")} for key_id in ids],
            "completion_feedback": []}


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"R1": {"kind": "review", "record_id": "r1", "text": "It stopped after 2 years. I suspect airflow.",
                                "quote_text": "It stopped after 2 years. I suspect airflow."},
                        "T1": {"kind": "report", "text": json.dumps({"content": [{"factor_id": "power", "mention_count": 10,
                            "positive_count": 9, "positive_share": .9, "rated_mentions": 10}]})}}
        self.findings = [fact()]

    def test_exact_evidence_and_duplicate_identity_count(self):
        row = fact(evidence=[{"source_id": "R1", "quote": "It stopped after 2 years."}] * 2)
        source_copy = copy.deepcopy(self.sources)
        self.sources["R2"] = {**self.sources["R1"]}
        row["evidence"].append({"source_id": "R2", "quote": "It stopped after 2 years."})
        checked = validate_findings({"type": "findings", "findings": [row]}, self.sources)
        self.assertEqual(checked["findings"][0]["cited_review_count"], 1)
        self.assertNotIn("cited_review_count", row)
        self.assertEqual(self.sources["R1"], source_copy["R1"])
        row["cited_review_count"] = 3
        self.assertEqual(validate_findings({"type": "findings", "findings": [row]}, self.sources)["findings"][0]["cited_review_count"], 1)

    def test_local_revision_cannot_rewrite_or_omit_passed_facts(self):
        first, second = fact(), fact(finding_id="F2", statement="另一项已确认事实。")
        changed = fact(statement="故障发生在第一天。")
        third = fact(finding_id="F3", statement="正在修正的发现。")
        result = preserve_checked_findings([changed, third], [first, second])
        self.assertEqual(result, [first, third, second])
        self.assertEqual(changed["statement"], "故障发生在第一天。")

    def test_fabricated_translation_or_wrong_pointer_rejected(self):
        invalid_refs = [{"source_id": "R1", "quote": "It failed after 1 week."},
                        {"source_id": "R1", "quote": "两年后坏了"},
                        {"source_id": "R1", "pointer": ""},
                        {"source_id": "T1", "pointer": "/content/1"},
                        {"source_id": "T1", "pointer": "/content/-1"},
                        {"source_id": "T1", "pointer": "/_evidence_semantics"}]
        for ref in invalid_refs:
            with self.subTest(ref=ref), self.assertRaises(AgentError):
                validate_findings({"type": "findings", "findings": [fact(evidence=[ref])]}, self.sources)
        valid = fact(kind="statistic", evidence=[{"source_id": "T1", "pointer": "/content/0"}])
        self.assertEqual(validate_findings({"type": "findings", "findings": [valid]}, self.sources)["findings"][0]["cited_review_count"], 0)
        self.assertEqual(pointer_value({"a/b": {"~key": [4]}}, "/a~1b/~0key/0"), 4)

    def test_absolute_report_pointer_resolves_only_exact_section(self):
        self.sources["T1"]["text"] = json.dumps({"section": "/tables", "content": [{"rows": [{"count": 7}]}]})
        row = fact(kind="statistic", evidence=[{"source_id": "T1", "pointer": "/tables/0/rows/0/count"}])
        result = validate_findings({"type": "findings", "findings": [row]}, self.sources)
        self.assertEqual(result["findings"][0]["evidence"][0]["pointer"], "/content/0/rows/0/count")
        row["evidence"][0]["pointer"] = "/tables/1/rows/0/count"
        with self.assertRaises(AgentError):
            validate_findings({"type": "findings", "findings": [row]}, self.sources)

    def test_rewrite_numeric_drift_rejected_but_layout_allowed(self):
        for text in ("使用1年后停机。", "2名用户反馈停机，约50%遇到。"):
            with self.subTest(text=text), self.assertRaises(AgentError):
                render_statements({"statements": [{"text": text, "finding_ids": ["F1"]}], "unanswered": []}, self.findings, self.sources)
        result = render_statements({"statements": [{"text": "1. 有用户反映使用2年后停机。", "finding_ids": ["F1"]}], "unanswered": []}, self.findings, self.sources)
        self.assertIn("使用2年", result["answer"])
        self.assertEqual(result["citations"], [{"source_id": "R1"}])

    def test_source_mapping_cannot_be_changed_by_writer(self):
        for row in ({"text": "已确认。", "finding_ids": ["F9"]},
                    {"text": "已确认。[T1]", "finding_ids": ["F1"]},
                    {"text": "详见 https://example.org", "finding_ids": ["F1"]}):
            with self.subTest(row=row), self.assertRaises(AgentError):
                render_statements({"statements": [row], "unanswered": []}, self.findings, self.sources)

    def test_reviewer_must_check_every_id_once(self):
        runtime = Mock()
        model = GroundedResearchModel(runtime)
        for rows in ([{"finding_id": "F1", "status": "pass", "feedback": ""}] * 2,
                     [{"finding_id": "F1", "status": "pass", "feedback": "needs changes"}, {"finding_id": "F2", "status": "pass", "feedback": ""}]):
            runtime.complete.return_value = fixtures.Runtime(lambda c, i: {"items": rows, "completion_feedback": []}).complete(messages=[{"content": "{}"}])
            with self.assertRaises(AgentError):
                model.review_findings(findings=[fact(), fact(finding_id="F2")], question="故障", sources=self.sources)

    def test_finding_review_retains_full_counterexample_sources_and_semantics(self):
        self.sources["R2"] = {"kind": "review", "text": "This same grass type cuts well."}
        runtime = fixtures.Runtime(lambda c, i: review([u["finding_id"] for u in c["review_units"]]))
        findings = self.findings + [fact(finding_id="F2", kind="statistic", evidence=[{"source_id": "T1", "pointer": "/content/0"}])]
        GroundedResearchModel(runtime).review_findings(findings=findings, question="哪些问题", sources=self.sources)
        context = runtime.contexts[0]
        self.assertEqual({s["source_id"] for s in context["sources"]}, set(self.sources))
        metric = next(s for s in context["sources"] if s["source_id"] == "T1")["excerpts"][0]["data"][0]
        self.assertIn("whole_product_rating", metric["_evidence_semantics"])

    def test_aggregate_cell_review_retains_competing_groups_and_definitions(self):
        raw = {"group_by": "topic", "metric": "keyword_mentions", "groups": [{"topic": "A", "count": 2}, {"topic": "B", "count": 9}]}
        sources = {"T1": {"kind": "aggregation", "text": json.dumps(raw)}}
        findings = [fact(kind="statistic", statement="A有2条，是最多的组。", evidence=[{"source_id": "T1", "pointer": "/groups/0/count"}])]
        result = review_sources(findings, sources)
        self.assertEqual(result[0]["excerpts"][0]["data"], raw)

    def test_mixed_paragraph_cannot_hide_bad_headline_or_context(self):
        findings = [fact(statement="续航评价两极分化：136条提及中121条给整机高星。", context="用户遇到50分钟续航；症状不同，所以机制不同。")]
        def callback(context, _index):
            units = context["review_units"]
            self.assertEqual(context["parents"], findings)
            failures = {u["finding_id"]: "星级不能证明功能评价分布" for u in units if "两极分化" in u["focus"]}
            failures.update({u["finding_id"]: "症状不能证明机制" for u in units if "机制" in u["focus"]})
            return review([u["finding_id"] for u in units], failures=failures)
        result = GroundedResearchModel(fixtures.Runtime(callback)).review_findings(findings=findings, question="结论", sources=self.sources)
        self.assertEqual(result["items"][0]["status"], "revise")
        self.assertIn("星级不能", result["items"][0]["feedback"])
        self.assertIn("症状不能", result["items"][0]["feedback"])
        for field in ("statement", "context"):
            units = [u for u in review_units(findings, "finding_id", (field,))]
            self.assertEqual("".join(u["focus"] for u in units), findings[0][field])
            for u in units:
                self.assertEqual(u["focus"], findings[0][field][u["start"]:u["end"]])

    def test_draft_prose_cannot_bypass_finding_stage(self):
        for status in ("answered", "insufficient_evidence", "out_of_scope"):
            runtime = fixtures.Runtime(lambda c, i: {"type": "answer", "status": status, "answer": "可复现。[R1]", "citations": ["R1"], "limitations": []})
            with self.subTest(status=status), self.assertRaises(AgentError):
                GroundedResearchModel(runtime).next_action(question="主要问题", sources=self.sources, report_context={}, history=[], observations=[], tools=[])

    def test_semantic_group_uses_entities_not_occurrences(self):
        self.sources = {sid: {"kind": "review", "record_id": sid, "text": json.dumps({"review_entity_id": "same-review", "review_text": "Motor stopped."}),
                             "quote_text": "Motor stopped."} for sid in ("occ1", "occ2")}
        members = [{"source_id": sid, "quote": "Motor stopped.", "experience": "actual"} for sid in self.sources]
        findings = [fact(statement="已列出的原文中有{count:G1}条说电机停机。", context="这里只统计列出的评论。",
                         evidence=[{"source_id": sid, "quote": "Motor stopped."} for sid in self.sources])]
        action = validate_findings({"type": "findings", "findings": findings,
            "review_groups": [{"group_id": "G1", "label": "电机停机", "members": members}]}, self.sources)
        self.assertEqual(action["review_groups"][0]["count"], 1)
        self.assertEqual(action["findings"][0]["cited_review_count"], 1)
        self.assertIn("有1条", action["findings"][0]["statement"])

    def written(self, text="有用户报告使用2年后停机。", sid="P1", task_ids=None, quote=None):
        return {"statement_id": sid, "text": text, "task_ids": task_ids or ["Q1"],
                "finding_ids": ["F1"], "evidence": [{"source_id": "R1", "quote": quote or "It stopped after 2 years."}]}

    def checked(self, ids, failures=None, tasks=None):
        value = review(ids, "statement_id", failures)
        value["task_checks"] = tasks or [{"task_id": "Q1", "status": "answered", "statement_ids": ids, "reason": ""}]
        return value

    def finish(self, model, findings=None, metadata=None, question="故障"):
        model.answer_tasks = explicit_requirements(question)
        return finish_grounded_answer(model=model, findings=findings or self.findings,
            kwargs={"question": question, "sources": self.sources}, invoke=lambda method, **kw: method(**kw),
            checkpoint=lambda a: None, metadata=metadata if metadata is not None else {})[0]

    def test_writer_format_retry_is_bounded_without_repeated_finding_review(self):
        model = Mock()
        model.write_answer.side_effect = [AgentError("llm_contract_invalid", "bad shape"),
            {"statements": [self.written()], "unanswered": []}]
        model.review_writing.return_value = self.checked(["P1"])
        self.assertEqual(self.finish(model)["status"], "answered")
        self.assertEqual(model.write_answer.call_count, 2)
        model.review_findings.assert_not_called()
        self.assertEqual(model.review_writing.call_count, 1)

    def test_corrected_prose_survives_another_paragraph_failure_without_old_error(self):
        self.sources["R1"]["text"] = self.sources["R1"]["quote_text"] = "I curse this mower on every cut. I suspect airflow."
        bad = self.written("每个割季都会遇到问题。", quote="I curse this mower on every cut.")
        fixed = self.written("每次割草都会遇到问题。", quote="I curse this mower on every cut.")
        other = self.written("根因就是气流。", sid="P2", quote="I suspect airflow.")
        model = Mock()
        model.write_answer.side_effect = [{"statements": [bad, other], "unanswered": []},
                                          {"statements": [fixed, other], "unanswered": []}]
        model.review_writing.side_effect = [self.checked(["P1", "P2"], {"P1": "every cut是每次割草", "P2": "仅为猜测"}),
                                           self.checked(["P1", "P2"], {"P2": "仅为猜测"})]
        meta = {}
        action = self.finish(model, findings=[fact(statement="每个割季", context="", evidence=bad["evidence"])], metadata=meta)
        self.assertIn("每次割草", action["answer"])
        self.assertNotIn("每个割季", action["answer"])
        self.assertNotIn("根因就是", action["answer"])
        self.assertEqual(action["status"], "insufficient_evidence")
        self.assertEqual(meta["question_coverage"][0]["status"], "unanswered")

    def test_interrupted_second_review_keeps_only_previously_checked_prose(self):
        model = Mock()
        old = self.written("早期就发生故障。")
        accepted = self.written("用户怀疑气流有关。", sid="P2", quote="I suspect airflow.")
        new = self.written("使用2年后发生故障。")
        model.write_answer.side_effect = [{"statements": [old, accepted], "unanswered": []},
                                          {"statements": [new, accepted], "unanswered": []}]
        model.review_writing.side_effect = [self.checked(["P1", "P2"], {"P1": "不是早期"}),
                                           AgentError("llm_idle_timeout", "timeout")]
        action = self.finish(model)
        self.assertIn("用户怀疑", action["answer"])
        self.assertNotIn("使用2年", action["answer"])
        self.assertNotIn("早期", action["answer"])
        self.assertEqual(action["status"], "insufficient_evidence")

    def test_correct_source_number_can_replace_wrong_finding_number(self):
        original = "Repair took four weeks. Parts took at least one week."
        self.sources["R1"]["text"] = self.sources["R1"]["quote_text"] = original
        row = self.written("整机维修共4周，其中等配件至少1周。", quote=original)
        model = Mock()
        model.write_answer.return_value = {"statements": [row], "unanswered": []}
        model.review_writing.return_value = self.checked(["P1"])
        action = self.finish(model, [fact(statement="等配件4周。", context="", evidence=row["evidence"])])
        self.assertIn("至少1周", action["answer"])
        self.assertEqual(action["status"], "answered")
        row["text"] = "维修花了99周。"
        with self.assertRaises(AgentError):
            self.finish(model)

    def test_writer_has_full_original_counterexample_and_can_change_citation(self):
        self.sources["R2"] = {"kind": "review", "record_id": "r2", "text": "It turns easily with self propel off."}
        tasks = explicit_requirements("转向体验如何？")
        runtime = fixtures.Runtime(lambda c, i: {"statements": [{"statement_id": "P1", "text": "另一位用户关闭自走后转向轻松。",
            "task_ids": [t["task_id"] for t in tasks], "evidence": [{"source_id": "R2", "quote": self.sources["R2"]["text"]}]}], "unanswered": []})
        result = GroundedResearchModel(runtime).write_answer(findings=self.findings, question="转向体验如何？", sources=self.sources)
        context = runtime.contexts[0]
        self.assertEqual({row["source_id"] for row in context["sources"]}, {"R1", "R2"})
        action = render_statements(result, self.findings, self.sources)
        self.assertEqual(action["citations"], [{"source_id": "R2"}])

    def test_complete_original_supports_number_outside_short_citation_quote(self):
        original = "Repair was slow. Parts took a week. My lawn is .2 acres. I mow every twenty two days."
        self.sources["R1"].update(text=original, quote_text=original)
        row = self.written("等配件1周，草坪0.2英亩，每22天割草。", quote="Repair was slow.")
        rendered = render_statements({"statements": [row], "unanswered": []}, self.findings, self.sources)
        self.assertIn("22天", rendered["answer"])
        row["text"] = "等配件99周。"
        with self.assertRaises(AgentError):
            render_statements({"statements": [row], "unanswered": []}, self.findings, self.sources)

    def test_displayed_rounding_ratio_and_rating_scale_do_not_invent_counts(self):
        self.sources["T1"]["text"] = json.dumps({"negative": 46, "total": 158, "negative_rate": .291139, "average_rating": 2.891304})
        row = self.written("46/158条为负面（29.1%）；平均2.89星。好评指4–5星。")
        row["evidence"] = [{"source_id": "T1", "pointer": ""}]
        render_statements({"statements": [row], "unanswered": []}, self.findings, self.sources)
        for text in ("负面占30.0%。", "共47条负面。", "99/158条负面。", "5条投诉，4–5星是好评。"):
            row["text"] = text
            with self.subTest(text=text), self.assertRaises(AgentError):
                render_statements({"statements": [row], "unanswered": []}, self.findings, self.sources)

    def test_bad_paragraph_does_not_block_independent_review_of_valid_prose(self):
        rows = [self.written("有用户使用2年后停机。"), self.written("有99条投诉。", sid="P2")]
        runtime = fixtures.Runtime(lambda c, i: {"statements": rows, "unanswered": []})
        model = GroundedResearchModel(runtime)
        diagnostics = {}
        result = model.write_answer(findings=self.findings, question="有哪些问题？", sources=self.sources, diagnostics=diagnostics)
        self.assertEqual([row["statement_id"] for row in result["statements"]], ["P1"])
        self.assertEqual(diagnostics["paragraph_errors"][0]["numbers_without_source"], ["99"])
        self.assertIn("P2", model.writing_feedback[0])

    def test_unasked_caveat_does_not_replace_task_coverage(self):
        model = Mock()
        model.write_answer.return_value = {"statements": [self.written()], "unanswered": ["无法估计总体故障率。"]}
        model.review_writing.return_value = self.checked(["P1"])
        action = self.finish(model)
        self.assertEqual(action["status"], "answered")
        self.assertNotIn("故障率", action["answer"])
        model.review_writing.return_value = self.checked(["P1"], tasks=[{"task_id": "Q1", "status": "unanswered",
            "statement_ids": [], "reason": "没有回答所问的故障率。"}])
        self.assertEqual(self.finish(model)["status"], "insufficient_evidence")

    def test_no_checked_prose_never_falls_back_to_research_notes(self):
        model = Mock()
        model.write_answer.side_effect = AgentError("llm_idle_timeout", "timeout")
        with self.assertRaises(AgentError):
            self.finish(model)
        model.review_findings.assert_not_called()

    def test_removing_optional_wrong_paragraph_still_completes_summary(self):
        valid = self.written()
        optional = self.written("根因就是气流。", sid="P2", quote="I suspect airflow.")
        internal = "P2.1/Q1 INTERNAL_REVIEW: 不能把用户猜测写成确定根因。"
        def callback(context, _index):
            if "retained_answer" in context:
                self.assertEqual(context["retained_answer"], [{key: valid[key] for key in ("statement_id", "text", "task_ids")}])
                self.assertFalse({"sources", "findings", "review_units"} & context.keys())
                return {"items": [], "completion_feedback": [], "task_checks": [
                    {"task_id": "Q1", "status": "answered", "statement_ids": ["P1"], "reason": ""}]}
            if "review_units" in context:
                self.assertNotIn("findings", context)
                return review([u["statement_id"] for u in context["review_units"]], "statement_id",
                    failures={u["statement_id"]: internal for u in context["review_units"] if u["parent_id"] == "P2"})
            return {"statements": [valid, optional], "unanswered": []}
        runtime = fixtures.Runtime(callback)
        metadata = {}
        result = self.finish(GroundedResearchModel(runtime), metadata=metadata, question="总结这份报告")
        self.assertEqual(result["status"], "answered")
        self.assertIn("使用2年后停机", result["answer"])
        self.assertNotIn("根因", result["answer"])
        self.assertNotIn("INTERNAL_REVIEW", result["answer"])
        self.assertEqual(result["limitations"], [])
        self.assertEqual(len(runtime.contexts), 3)
        self.assertEqual(metadata["question_coverage"][0]["statement_ids"], ["P1"])
        self.assertEqual(len(metadata["coverage_checks"]), 1)

    def test_explicit_missing_scenario_stays_partial_without_publishing_review_feedback(self):
        question = "有哪些负面评价，发生在什么场景？"
        row = self.written("有用户报告机器停机。", task_ids=["Q1", "Q3"])
        internal = "Q4/P9 INTERNAL_REVIEW: 请恢复已删除场景段，核对原文后重写。"
        tasks = [{"task_id": task["task_id"], "status": "answered" if task["task_id"] == "Q3" else "unanswered",
                  "statement_ids": ["P1"] if task["task_id"] == "Q3" else [],
                  "reason": "" if task["task_id"] == "Q3" else internal} for task in explicit_requirements(question)]
        def callback(context, _index):
            if "retained_answer" in context:
                return {"items": [], "completion_feedback": [], "task_checks": copy.deepcopy(tasks)}
            if "review_units" in context:
                return review([u["statement_id"] for u in context["review_units"]], "statement_id")
            return {"statements": [row], "unanswered": [internal]}
        runtime = fixtures.Runtime(callback)
        metadata = {}
        result = self.finish(GroundedResearchModel(runtime), metadata=metadata, question=question)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIn("机器停机", result["answer"])
        self.assertIn("场景", " ".join(result["limitations"]))
        for hidden in ("Q4", "P9", "INTERNAL_REVIEW", "请恢复", "重写"):
            self.assertNotIn(hidden, result["answer"] + " ".join(result["limitations"]))
        self.assertEqual(next(t for t in metadata["question_coverage"] if t["task_id"] == "Q4")["status"], "unanswered")
        self.assertEqual(sum("retained_answer" in context for context in runtime.contexts), 1)

    def test_final_review_sees_only_its_cited_complete_originals_without_old_findings(self):
        self.sources["R2"] = {"kind": "review", "record_id": "r2", "text": "UNRELATED_ORIGINAL: my old mower failed."}
        self.sources["R1"]["in_model"] = False
        stale = fact(source="R2", statement="STALE_FINDING: 当前机器故障。", evidence=[{"source_id": "R2", "quote": "my old mower failed."}])
        def callback(context, _index):
            self.assertNotIn("findings", context)
            self.assertNotIn("STALE_FINDING", json.dumps(context))
            self.assertEqual([source["source_id"] for source in context["sources"]], ["R1"])
            self.assertEqual(context["sources"][0]["text"], self.sources["R1"]["text"])
            self.assertIn("I suspect airflow.", context["sources"][0]["text"])
            self.assertNotIn("UNRELATED_ORIGINAL", json.dumps(context))
            self.assertEqual(context["parents"][0]["evidence"], self.written()["evidence"])
            return self.checked([u["statement_id"] for u in context["review_units"]], tasks=[
                {"task_id": "Q1", "status": "answered", "statement_ids": ["P1"], "reason": ""}])
        result = GroundedResearchModel(fixtures.Runtime(callback)).review_writing(findings=[stale],
            writing={"statements": [self.written()], "unanswered": []}, question="故障", sources=self.sources)
        self.assertEqual(result["items"][0]["status"], "pass")

    def test_answered_coverage_can_include_explanation_and_is_normalized(self):
        supplied = {"items": [], "completion_feedback": [], "task_checks": [
            {"task_id": "Q1", "status": "answered", "statement_ids": ["P1"], "reason": "这段已直接说明用户遇到的故障。"}]}
        runtime = fixtures.Runtime(lambda context, _index: supplied)
        diagnostics = {}
        result = GroundedResearchModel(runtime).review_coverage(question="故障", statements=[self.written()], diagnostics=diagnostics)
        self.assertEqual(result["task_checks"], [{"task_id": "Q1", "status": "answered", "statement_ids": ["P1"], "reason": ""}])
        self.assertEqual(diagnostics["status"], "valid")
        self.assertEqual(len(runtime.contexts), 1)
        self.assertTrue(supplied["task_checks"][0]["reason"])

    def test_coverage_cannot_use_deleted_unknown_or_sentence_ids_as_retained_paragraphs(self):
        for ids in (["P1", "P2"], ["P9"], ["P1.1"], ["P1", "P1"]):
            with self.subTest(ids=ids):
                runtime = fixtures.Runtime(lambda context, _index: {"items": [], "completion_feedback": [], "task_checks": [
                    {"task_id": "Q1", "status": "answered", "statement_ids": ids, "reason": "内容完整。"}]})
                diagnostics = {}
                with self.assertRaises(AgentError) as failure:
                    GroundedResearchModel(runtime).review_coverage(question="故障", statements=[self.written()], diagnostics=diagnostics)
                self.assertEqual(failure.exception.code, "llm_contract_invalid")
                self.assertEqual(failure.exception.format_feedback, diagnostics["schema_errors"])
                self.assertTrue(any(row["path"][:3] == ["task_checks", 0, "statement_ids"] for row in diagnostics["schema_errors"]))
                self.assertNotIn("内容完整", json.dumps(diagnostics, ensure_ascii=False))

    def test_duplicate_item_review_reports_missing_and_duplicate_ids(self):
        runtime = fixtures.Runtime(lambda context, _index: review(["F1", "F1"]))
        diagnostics = {}
        with self.assertRaises(AgentError) as failure:
            GroundedResearchModel(runtime)._item_review("synthetic check", {}, ["F1", "F2"],
                "finding_id", diagnostics, None)
        self.assertEqual(failure.exception.format_feedback, [
            {"path": ["items"], "missing_ids": ["F2"], "duplicate_ids": ["F1"]}])

    def test_written_bare_source_ids_are_rejected_but_product_names_are_not(self):
        self.sources["S20"] = copy.deepcopy(self.sources["R1"])
        for identifier in ("R1", "S20"):
            with self.subTest(identifier=identifier), self.assertRaises(AgentError):
                render_statements({"statements": [self.written(f"{identifier}提到使用2年后停机。")], "unanswered": []}, self.findings, self.sources)
        result = render_statements({"statements": [self.written("LM2236SP使用2年后停机。")], "unanswered": []}, self.findings, self.sources)
        self.assertIn("LM2236SP", result["answer"])

    def test_many_public_gaps_fit_contract_without_losing_retained_prose(self):
        from agent.research_grounding import _public_gap_reasons
        reasons = _public_gap_reasons([{"task_id": f"Q{i}"} for i in range(1, 10)])
        self.assertEqual(len(reasons), 4)
        result = render_statements({"statements": [self.written()], "unanswered": reasons}, self.findings, self.sources)
        self.assertIn("使用2年后停机", result["answer"])
        for gap in ("优点", "缺点", "场景", "排序", "统计", "旧机器", "假设"):
            self.assertIn(gap, result["answer"])


class BatchedGroundingTests(unittest.TestCase):
    def setUp(self):
        self.sources = {f"R{i}": {"kind": "review", "record_id": f"r{i}",
            "text": f"The motor stopped. Full conditions for product {chr(64+i)}.",
            "quote_text": f"The motor stopped. Full conditions for product {chr(64+i)}."} for i in range(1, 4)}
        self.findings = [fact(statement="电机停机。", context="", evidence=[{"source_id": "R1", "quote": "The motor stopped."}])]
        self.rows = [{"statement_id": f"P{i}", "text": "用户报告电机停机。" * units,
            "task_ids": ["Q1"], "evidence": [{"source_id": f"R{i}", "quote": "The motor stopped."}]} for i, units in enumerate((5, 10, 2), 1)]
        self.checkpoints = []
        self.invocations = []

    def finish(self, model, *, resume=None, budget=14):
        model.answer_tasks = [{"task_id": "Q1", "question": "逐项说明这些问题"}]
        self.invocations = []
        def invoke(method, **kwargs):
            if len(self.invocations) >= budget:
                raise AgentError("research_budget_exhausted", "budget")
            self.invocations.append(method.__name__)
            return method(**kwargs)
        self.metadata = {}
        return finish_grounded_answer(model=model, findings=self.findings, kwargs={"question": "逐项说明这些问题", "sources": self.sources},
            invoke=invoke, checkpoint=lambda value: self.checkpoints.append(copy.deepcopy(value)), metadata=self.metadata, resume=resume)[0]

    def respond(self, context, _index):
        if "retained_answer" in context:
            ids = [row["statement_id"] for row in context["retained_answer"]]
            complete = len(ids) == len(self.rows)
            return {"items": [], "completion_feedback": [], "task_checks": [{"task_id": "Q1",
                "status": "answered" if complete else "unanswered", "statement_ids": ids if complete else [],
                "reason": "" if complete else "还有明确要求说明的问题尚未回答。"}]}
        if context.get("review_batch"):
            self.assertLessEqual(len(context["review_units"]), 12)
            self.assertEqual({u["parent_id"] for u in context["review_units"]}, {row["statement_id"] for row in context["parents"]})
            self.assertEqual({source["source_id"] for source in context["sources"]},
                {ref["source_id"] for row in context["parents"] for ref in row["evidence"]})
            self.assertNotIn("task_checks", context["response_contract"]["schema"]["properties"])
            self.assertNotIn("findings", context)
            for source in context["sources"]:
                self.assertIn("Full conditions", source["text"])
            return review([u["statement_id"] for u in context["review_units"]], "statement_id")
        return {"statements": copy.deepcopy(self.rows), "unanswered": []}

    def test_every_unit_reviewed_once_in_bounded_calls_and_coverage_uses_retained_whole_answer(self):
        runtime = fixtures.Runtime(self.respond)
        action = self.finish(GroundedResearchModel(runtime))
        self.assertEqual(action["status"], "answered")
        batches = [c for c in runtime.contexts if c.get("review_batch")]
        self.assertEqual([len(c["review_units"]) for c in batches], [12, 5])
        ids = [u["statement_id"] for c in batches for u in c["review_units"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {u["statement_id"] for u in review_units(self.rows, "statement_id", ("text",))})
        self.assertEqual(self.invocations, ["write_answer", "review_writing_batch", "review_writing_batch", "review_coverage"])
        self.assertEqual([row["statement_id"] for row in runtime.contexts[-1]["retained_answer"]], ["P1", "P2", "P3"])
        self.assertEqual(self.checkpoints[-1]["review_contract"], "wenquan-writing-batches/v1")

    def test_later_truncation_keeps_only_complete_paragraph_and_resume_skips_done_units(self):
        batch_count = 0
        def callback(context, index):
            nonlocal batch_count
            if context.get("review_batch"):
                batch_count += 1
                if batch_count == 2:
                    raise AgentError("llm_response_incomplete", "length")
            return self.respond(context, index)
        action = self.finish(GroundedResearchModel(fixtures.Runtime(callback)))
        self.assertEqual(action["status"], "insufficient_evidence")
        self.assertEqual([r["statement_id"] for r in self.metadata["grounded_statements"]["statements"]], ["P1"])
        saved = self.checkpoints[-1]
        self.assertIsNotNone(saved["pending_writing"])
        self.assertEqual(len(saved["paragraph_reviews"]["P2"]["unit_checks"]), 7)
        runtime = fixtures.Runtime(self.respond)
        resumed = self.finish(GroundedResearchModel(runtime), resume=saved)
        self.assertEqual(resumed["status"], "answered")
        self.assertEqual(self.invocations, ["review_writing_batch", "review_coverage"])
        self.assertEqual([u["statement_id"] for u in runtime.contexts[0]["review_units"]], ["P2.8", "P2.9", "P2.10", "P3.1", "P3.2"])
        self.assertNotIn("R1", {source["source_id"] for source in runtime.contexts[0]["sources"]})

    def test_shared_budget_stops_before_hidden_request_and_preserves_checkpoint(self):
        runtime = fixtures.Runtime(self.respond)
        action = self.finish(GroundedResearchModel(runtime), budget=2)
        self.assertEqual(action["status"], "insufficient_evidence")
        self.assertEqual(len(runtime.contexts), 2)
        self.assertEqual(self.invocations, ["write_answer", "review_writing_batch"])
        self.assertEqual(self.metadata["partial_reason_code"], "research_budget_exhausted")
        self.assertEqual([row["statement_id"] for row in self.checkpoints[-1]["checked_statements"]], ["P1"])
        self.assertIsNotNone(self.checkpoints[-1]["pending_writing"])

    def test_incomplete_single_parent_cannot_publish_any_of_its_passed_sentences(self):
        self.rows = [self.rows[0]]
        self.rows[0]["text"] = "用户报告电机停机。" * 13
        def callback(context, index):
            if context.get("review_batch") and context["review_units"][0]["statement_id"] == "P1.13":
                raise AgentError("llm_response_incomplete", "length")
            return self.respond(context, index)
        with self.assertRaises(AgentError):
            self.finish(GroundedResearchModel(fixtures.Runtime(callback)))
        self.assertEqual(self.checkpoints[-1]["checked_statements"], [])
        self.assertEqual(len(self.checkpoints[-1]["paragraph_reviews"]["P1"]["unit_checks"]), 12)
        runtime = fixtures.Runtime(self.respond)
        action = self.finish(GroundedResearchModel(runtime), resume=self.checkpoints[-1])
        self.assertEqual(action["status"], "answered")
        self.assertEqual([u["statement_id"] for u in runtime.contexts[0]["review_units"]], ["P1.13"])

    def test_omitted_review_unit_never_advances_checkpoint(self):
        def callback(context, index):
            result = self.respond(context, index)
            if context.get("review_batch"):
                result["items"].pop()
            return result
        with self.assertRaises(AgentError):
            self.finish(GroundedResearchModel(fixtures.Runtime(callback)))
        self.assertEqual(self.invocations, ["write_answer", "review_writing_batch", "review_writing_batch"])
        self.assertEqual(self.checkpoints[-1]["checked_statements"], [])
        self.assertEqual(self.checkpoints[-1]["review_batches"], [])
        self.assertTrue(all(not state["unit_checks"] for state in self.checkpoints[-1]["paragraph_reviews"].values()))

    def test_changed_source_invalidates_cached_units_before_resume(self):
        self.finish(GroundedResearchModel(fixtures.Runtime(self.respond)), budget=2)
        saved = self.checkpoints[-1]
        self.sources["R2"]["text"] += " New comparison context."
        runtime = fixtures.Runtime(self.respond)
        action = self.finish(GroundedResearchModel(runtime), resume=saved)
        self.assertEqual(action["status"], "answered")
        ids = [u["statement_id"] for c in runtime.contexts if c.get("review_batch") for u in c["review_units"]]
        self.assertEqual(ids, [f"P2.{i}" for i in range(1, 11)] + ["P3.1", "P3.2"])
        self.assertEqual(self.checkpoints[-1]["paragraph_reviews"]["P1"], saved["paragraph_reviews"]["P1"])

    def test_unchanged_approved_paragraphs_not_reviewed_again_during_correction(self):
        self.rows = [self.rows[0], self.rows[2]]
        self.rows[1]["text"] = "一定是电机本身损坏。"
        def callback(context, index):
            result = self.respond(context, index)
            if context.get("review_batch"):
                for item in result["items"]:
                    unit = next(u for u in context["review_units"] if u["statement_id"] == item["statement_id"])
                    if "一定" in unit["focus"]:
                        item.update(status="revise", feedback="原文未说明确定根因。")
            elif "retained_answer" not in context and "revision" in context:
                result["statements"][1]["text"] = "用户报告电机停机。"
            return result
        runtime = fixtures.Runtime(callback)
        action = self.finish(GroundedResearchModel(runtime))
        self.assertEqual(action["status"], "answered")
        batches = [c for c in runtime.contexts if c.get("review_batch")]
        self.assertEqual(len(batches), 2)
        self.assertEqual({u["parent_id"] for u in batches[1]["review_units"]}, {"P3"})
        self.assertNotIn("一定", action["answer"])

    def test_old_contract_approvals_are_rewritten_not_reused(self):
        saved = {"checked_statements": [{**self.rows[0], "text": "R1证明全部故障。"}],
                 "checked_review": {"task_checks": [{"task_id": "Q1", "status": "answered", "statement_ids": ["P1"], "reason": ""}]}}
        runtime = fixtures.Runtime(self.respond)
        action = self.finish(GroundedResearchModel(runtime), resume=saved)
        self.assertEqual(action["status"], "answered")
        self.assertEqual(self.invocations[0], "write_answer")
        self.assertNotIn("全部故障", action["answer"])
        self.assertNotIn("R1证明", json.dumps(runtime.contexts, ensure_ascii=False))

    def test_model_change_does_not_reuse_previous_model_approval(self):
        runtime = fixtures.Runtime(self.respond)
        runtime._settings = SimpleNamespace(model="synthetic-A", base_url="https://example.invalid/v1")
        self.finish(GroundedResearchModel(runtime), budget=2)
        saved = self.checkpoints[-1]
        changed = fixtures.Runtime(self.respond)
        changed._settings = SimpleNamespace(model="synthetic-B", base_url="https://example.invalid/v1")
        action = self.finish(GroundedResearchModel(changed), resume=saved)
        self.assertEqual(action["status"], "answered")
        self.assertEqual(self.invocations, ["write_answer", "review_writing_batch", "review_writing_batch", "review_coverage"])
        self.assertEqual(sum(len(c["review_units"]) for c in changed.contexts if c.get("review_batch")), 17)


class GroundedIntegrationTests(unittest.TestCase):
    model_class = GroundedResearchModel
    setUp = fixtures.ReportResearchTests.setUp
    tearDown = fixtures.ReportResearchTests.tearDown
    submit = fixtures.ReportResearchTests.submit

    def callback(self, context, _index):
        if "required_tasks" in context:
            return {"tasks": []}
        if "retained_answer" in context:
            return {"items": [], "completion_feedback": [], "task_checks": [
                {"task_id": task["task_id"], "status": "answered", "reason": "",
                 "statement_ids": [row["statement_id"] for row in context["retained_answer"] if task["task_id"] in row["task_ids"]]}
                for task in context["answer_tasks"]]}
        if "review_units" in context:
            key = "statement_id" if "statement_id" in context["review_units"][0] else "finding_id"
            value = review([r[key] for r in context["review_units"]], key)
            if not context.get("review_batch"):
                value["task_checks"] = [{"task_id": t["task_id"], "status": "answered", "reason": "",
                    "statement_ids": [r["statement_id"] for r in context["parents"]]} for t in context["answer_tasks"]]
            return value
        if "findings" in context:
            return {"statements": [{"statement_id": "P1", "text": "有用户认为当前机器动力表现好；坏掉的是之前那台机器。",
                "task_ids": [t["task_id"] for t in context["answer_tasks"]], "evidence": context["findings"][0]["evidence"],
                "finding_ids": ["F1"]}], "unanswered": []}
        source = next((s for s in context["sources"] if s["kind"] == "review"), None)
        if source is None:
            return fixtures.tool(offset=0, limit=10)
        return {"type": "findings", "findings": [fact(source["source_id"], statement="有用户认为当前机器动力表现好。",
            context="旧割草机故障，不是当前机器故障。", evidence=[{"source_id": source["source_id"], "quote": fixtures.REVIEW["review_text"]}])]}

    def test_real_flow_retrieval_then_findings_review_then_writing_and_review(self):
        self.runtime.callback = self.callback
        run, message = self.submit("主要负面评价是什么，发生在什么场景？")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["metadata"]["model_calls"], 6)
        self.assertIn("坏掉的是之前", message["content"])
        self.assertEqual([c["stage"] for c in message["metadata"]["grounding_checks"]], ["writing_batch"])
        self.assertTrue(message["metadata"]["research_findings"])
        citation = message["metadata"]["citations"][0]
        self.assertEqual(self.qa.source(self.actor, self.sid, citation["citation_id"])["review"]["review_text"], fixtures.REVIEW["review_text"])

    def test_wrong_final_meaning_is_repaired_and_checked_again(self):
        attempts = {"writing": 0, "review": 0}
        def callback(context, index):
            if "review_units" in context and "statement_id" in context["review_units"][0]:
                attempts["review"] += 1
                value = self.callback(context, index)
                if attempts["review"] == 1:
                    value["items"][0].update(status="revise", feedback="旧机器故障不能写成当前机器故障")
                return value
            if "findings" in context and "parents" not in context:
                attempts["writing"] += 1
                if attempts["writing"] == 1:
                    value = self.callback(context, index)
                    value["statements"][0]["text"] = "当前机器已发生故障。"
                    return value
            return self.callback(context, index)
        self.runtime.callback = callback
        run, message = self.submit("这条评论到底是哪台机器坏了？")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(attempts, {"writing": 2, "review": 2})
        self.assertNotIn("当前机器已发生故障", message["content"])

    def test_source_drift_still_blocks_reviewed_prose(self):
        self.runtime.callback = self.callback
        self.transport.mode = "drift"
        run, message = self.submit("这条评论是哪台机器坏了？")
        self.assertNotEqual(run["status"], "completed")

    def test_program_rankings_keep_zero_model_calls(self):
        self.runtime.callback = lambda c, i: self.fail("ranking should not call model")
        run, message = self.submit("只列报告已有的pros and cons各前10主题和数量")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["metadata"]["model_calls"], 0)

    def test_ordinary_top10_reads_originals_and_carries_both_rankings_through_writing_and_review(self):
        seen = {"research": 0, "writing": 0, "review": 0, "coverage": 0}
        def callback(context, index):
            if "required_tasks" in context:
                return self.callback(context, index)
            if "retained_answer" in context:
                seen["coverage"] += 1
                return {"items": [], "completion_feedback": [], "task_checks": [
                    {"task_id": task["task_id"], "status": "unanswered", "statement_ids": [],
                     "reason": "其余优缺点尚未取得可支持的原文。"} for task in context["answer_tasks"]]}
            stage = "review" if "review_units" in context else "writing" if "findings" in context else "research"
            seen[stage] += 1
            if stage == "review":
                self.assertTrue(context["review_batch"])
                self.assertNotIn("findings", context)
                self.assertEqual({s["source_id"] for s in context["sources"]},
                    {ref["source_id"] for row in context["parents"] for ref in row["evidence"]})
                return self.callback(context, index)
            rankings = context.get("report_rankings") or [row["report_ranking_context"]
                for row in context["observations"] if "report_ranking_context" in row]
            self.assertEqual(len(rankings), 1)
            self.assertEqual([len(table["rows"]) for table in rankings[0]["rankings"]], [10, 10])
            self.assertEqual(rankings[0]["rankings"][1]["rows"][0]["label"], "自走按钮")
            self.assertEqual(rankings[0]["rankings"][1]["rows"][0]["count"], 3)
            self.assertIn("不是星级", rankings[0]["basis"])
            by_id = {source["source_id"]: source for source in context["sources"]}
            for sid in rankings[0]["source_ids"]:
                self.assertIn(sid, by_id)
                self.assertEqual(len(by_id[sid]["data"]["content"]), len(fixtures.ASPECT_ROWS))
            value = self.callback(context, index)
            if stage == "writing":
                self.assertIn("主题名称不能代替评价", context["writing_brief"])
                self.assertGreaterEqual(context["response_contract"]["schema"]["properties"]["statements"]["maxItems"], 21)
                value["unanswered"] = ["其余优缺点尚未取得可支持的原文。"]
            return value
        self.runtime.callback = callback
        run, message = self.submit("分别给我排名前10的pros and cons")
        self.assertEqual(run["status"], "blocked", message)
        self.assertTrue(message["metadata"]["partial_answer"])
        self.assertIn("坏掉的是之前", message["content"])
        self.assertNotIn("主题0", message["content"])
        self.assertEqual(seen, {"research": 2, "writing": 2, "review": 1, "coverage": 1})
        self.assertEqual(message["metadata"]["model_calls"], 7)
        self.assertEqual(sum(name == "zhuojian.query_report_reviews@v1" for name, _ in self.transport.payloads), 2)

    def wait_resumed(self, result):
        run_id = result["run"]["run_id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            run = self.store.get_run(self.actor, run_id)
            if run["status"] != "running":
                message = next(m for m in self.store.list_messages(self.actor, self.sid)
                               if m.get("run_id") == run_id and m["role"] == "assistant")
                return run, message
            time.sleep(.01)
        self.fail("resume did not complete")

    def test_failed_writer_resumes_saved_research_after_rechecking_sources(self):
        attempts = {"writing": 0, "findings": 0}
        def callback(context, index):
            if "findings" in context and "parents" not in context:
                attempts["writing"] += 1
                if attempts["writing"] == 1:
                    raise AgentError("llm_idle_timeout", "模型没有响应", http_status=504, retryable=True)
            if "sources" in context and "findings" not in context and "review_units" not in context and any(s["kind"] == "review" for s in context["sources"]):
                attempts["findings"] += 1
            return self.callback(context, index)
        self.runtime.callback = callback
        first, failed = self.submit("这条评论是哪台机器坏了？")
        self.assertEqual(first["status"], "failed", failed)
        self.assertTrue(failed["metadata"]["can_resume"])
        findings_calls = attempts["findings"]
        result = self.qa.resume(self.actor, self.sid, first["run_id"], "resume-same-question")
        resumed, message = self.wait_resumed(result)
        self.assertEqual(resumed["status"], "completed", message)
        self.assertIn("坏掉的是之前", message["content"])
        self.assertEqual(attempts["findings"], findings_calls)
        self.assertEqual(message["metadata"]["model_calls"], 3)
        self.assertEqual(message["metadata"]["cumulative_model_calls"], failed["metadata"]["model_calls"] + 3)
        self.assertTrue(self.qa.resume(self.actor, self.sid, first["run_id"], "resume-same-question")["duplicate"])

    def test_changed_source_blocks_resume_before_new_model_call(self):
        def callback(context, index):
            if "findings" in context and "parents" not in context:
                raise AgentError("llm_idle_timeout", "timeout")
            return self.callback(context, index)
        self.runtime.callback = callback
        first, _ = self.submit("这条评论是哪台机器坏了？")
        self.transport.mode = "drift"
        before = len(self.runtime.contexts)
        result = self.qa.resume(self.actor, self.sid, first["run_id"], "resume-changed-source")
        resumed, message = self.wait_resumed(result)
        self.assertEqual(resumed["status"], "failed")
        self.assertEqual(message["metadata"]["error"]["code"], "source_changed")
        self.assertEqual(len(self.runtime.contexts), before)
        self.assertFalse(message["metadata"]["can_resume"])


if __name__ == "__main__":
    unittest.main()
