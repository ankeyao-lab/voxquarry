from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.contracts import AgentError
from agent.research import ReportResearchQA, model_tools, needs_aspect_baseline
from agent.research_model import ResearchModel
from agent.service import WenquanService
from agent.store import WenquanStore
from agent.tests.helpers import GROUP_ID, QUOTE, REPORT_ID, SNAPSHOT_ID, SyntheticZhuojianTransport, build_reporting_service, identity
from agent.tool_registry import ToolRegistry
from agent.upstream import ToolExecutor


REVIEW = {"record_id": "rev_oldmachine", "review_entity_id": "rev_oldmachine", "rating": 1,
          "review_title": "Changed mower", "review_text": "My old mower broke, so I bought this battery mower. Its power is excellent."}
ASPECT_SECTION = "/product_insight/aspect_experience"
ASPECT_ROWS = [
    {"aspect_id": f"aspect_{index}", "label": f"主题{index}", "review_entities": 10,
     "explicit_positive_entities": 5, "explicit_negative_entities": 2,
     "high_star_entities": 7, "low_star_entities": 3}
    for index in range(20)
] + [{"aspect_id": "self_propel", "label": "自走按钮", "review_entities": 8,
      "explicit_positive_entities": 2, "explicit_negative_entities": 3,
      "high_star_entities": 8, "low_star_entities": 0}]


def answer(context, text="这份报告有 20 条评论，其中 18 条有文字。", kind="report", quote=None):
    source = next(s for s in context["sources"] if s["kind"] == kind)
    reference = {"source_id": source["source_id"]}
    if quote is not None:
        reference["quote"] = quote
    return {"type": "answer", "status": "answered", "answer": text + "[" + source["source_id"] + "]",
            "citations": [reference], "limitations": []}


def tool(name="query_report_reviews", **args):
    return {"type": "tool", "tool_name": name, "args": args}


class Runtime:
    def __init__(self, callback=None):
        self.callback = callback or (lambda context, index: {"status": "pass", "feedback": []} if "answer_action" in context else answer(context))
        self.contexts = []
        self.started, self.release = threading.Event(), threading.Event()
        self.block = False

    def complete(self, **kwargs):
        context = json.loads(kwargs["messages"][-1]["content"])
        self.contexts.append(context)
        self.started.set()
        if self.block:
            self.release.wait(5)
        response = self.callback(context, len(self.contexts))
        return {"model": "synthetic-research", "usage": {"total_tokens": 7},
                "choices": [{"message": {"content": json.dumps(response, ensure_ascii=False)}}]}


class Transport(SyntheticZhuojianTransport):
    def __init__(self, service):
        super().__init__(service)
        self.payloads = []
        self.mode = ""
        self.counts = {}

    def call(self, name, payload, actor, definition):
        self.payloads.append((name, copy.deepcopy(payload)))
        self.counts[name] = self.counts.get(name, 0) + 1
        if name in {"zhuojian.get_report_section@v1", "zhuojian.query_report_reviews@v1", "zhuojian.aggregate_report_reviews@v1"}:
            if self.mode == "missing_raw" and any(part in name for part in ("aggregate_report", "query_report")):
                raise AgentError("research_source_unavailable", "该报告的原始评论尚不可用。", http_status=409)
            result = super().call("zhuojian.get_report@v1", {"report_id": payload["report_id"]}, actor, definition)
            result = copy.deepcopy(result)
            result["tool"] = name
            if result["status"] == "error":
                return result
            if "query_report" in name:
                item = copy.deepcopy(REVIEW)
                if payload.get("offset", 0):
                    item["record_id"] = item["review_entity_id"] = "rev_next"
                    item["review_text"] = "The self propel buttons engage accidentally while turning."
                result["data"] = {"items": [item], "population": "entities"}
                result["page"] = {"offset": payload.get("offset", 0), "limit": 1, "returned": 1,
                                  "total": 2, "next_offset": 1 if not payload.get("offset") else None,
                                  "has_more": not bool(payload.get("offset"))}
            elif "aggregate" in name:
                result["data"] = {"total_entities": 20, "written_entities": 18,
                                  "intersection_count": 2, "group_match_counts": [{"label": "drive", "count": 5}, {"label": "turn", "count": 3}]}
            elif payload.get("section") == ASPECT_SECTION:
                result["data"] = {"section": ASPECT_SECTION, "content": copy.deepcopy(ASPECT_ROWS)}
                result["page"] = {"offset": 0, "limit": payload.get("limit", 100), "returned": len(ASPECT_ROWS),
                                  "total": len(ASPECT_ROWS), "next_offset": None, "has_more": False}
            else:
                result["data"] = {"section": payload.get("section"), "keys": ["metrics", "product_insight", "tables"], "value": {"saved_rule_count": 10}}
            result["data"]["provenance"] = {"report_id": result["report"]["report_id"], "snapshot_id": result["snapshot"]["snapshot_id"], "analysis_version": result["report"]["analysis_version"]}
            if self.mode == "drift" and self.counts[name] > 1:
                result["data"]["tampered"] = True
            if self.mode == "wrong_snapshot":
                result["snapshot"]["snapshot_id"] = "snap_unrelated"
            return result
        return super().call(name, payload, actor, definition)


class ReportResearchTests(unittest.TestCase):
    # These fixtures exercise the legacy/simple-answer protocol and shared
    # source/lifecycle invariants. Grounded production flow has its own fixture.
    model_class = ResearchModel

    def setUp(self):
        model_patch = patch("agent.research.GroundedResearchModel", self.model_class)
        model_patch.start()
        self.addCleanup(model_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = WenquanStore(root / "agent.sqlite3")
        self.store.initialize()
        self.reporting = build_reporting_service(root / "reporting")
        self.transport = Transport(self.reporting)
        registry = ToolRegistry(zhuojian_status="synthetic_test_only", enable_research=True)
        executor = ToolExecutor(registry=registry, transport=self.transport, store=self.store,
                                envelope_schema_path=str(Path(__file__).parents[2] / "reporting/schemas/zhuojian-tool-envelope-v1.schema.json"),
                                allow_synthetic=True)
        self.runtime = Runtime()
        self.service = WenquanService(store=self.store, registry=registry, executor=executor,
                                      planner=SimpleNamespace(runtime=self.runtime), identity_status="synthetic_test_only")
        self.qa = ReportResearchQA(self.service)
        self.actor = identity()
        self.sid = self.service.create_session(self.actor, "Research regression")["session_id"]
        self.sequence = 0

    def tearDown(self):
        self.runtime.release.set()
        self.qa.close()
        self.tmp.cleanup()

    def submit(self, question="这份报告有多少评论？", report_id=REPORT_ID, selection_mode=None):
        self.sequence += 1
        result = self.qa.submit(self.actor, session_id=self.sid, question=question,
                                report_id=report_id, request_id=f"research-{self.sequence}", selection_mode=selection_mode)
        rid = result["run"]["run_id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            run = self.store.get_run(self.actor, rid)
            if run["status"] != "running":
                messages = [m for m in self.store.list_messages(self.actor, self.sid) if m.get("run_id") == rid and m["role"] == "assistant"]
                self.assertEqual(len(messages), 1)
                return run, messages[0]
            time.sleep(0.01)
        self.fail("Research fixture did not finish")

    def test_quantity_is_a_short_actual_answer_without_fact_dump(self):
        run, message = self.submit()
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["content"], "这份报告有 20 条评论，其中 18 条有文字。[1]")
        self.assertEqual(message["metadata"]["contract"], "voxquarry-wenquan-research-answer/v1")
        self.assertNotIn("数据限制", message["content"])
        self.assertEqual(message["metadata"]["model_calls"], 1)
        self.assertTrue(all(s["status"] == "completed" for s in run["steps"]))

    def test_auto_without_attachment_can_generate_once_and_continue_same_question(self):
        calls = []
        candidate = {"candidate_id": "fixture_candidate", "title": "EGO LM2236SP lowes",
                     "scope": {"models": ["LM2236SP"], "brands": ["EGO"], "channels": ["lowes"], "job_ids": ["fixture_job"]}}
        def workflow(operation, payload, actor):
            calls.append((operation, payload))
            if operation == "catalog":
                return {"items": [candidate]}
            self.assertEqual(operation, "ensure")
            return {"job": {"job_id": "fixture_job", "report_id": REPORT_ID, "status": "completed"}, "job_token": "fixture"}
        self.transport.report_workflow = workflow
        self.actor = replace(self.actor, allowed_report_ids=None)
        with patch.dict("os.environ", {"VOXQUARRY_WENQUAN_REPORT_GENERATION_ENABLED": "true"}), patch.object(self.qa, "reports", return_value={"items": []}):
            run, message = self.submit("LM2236SP有多少条评论？", report_id=None, selection_mode="auto")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual([name for name, _ in calls], ["catalog", "ensure"])
        self.assertEqual(message["metadata"]["report_id"], REPORT_ID)
        self.assertIn("报告已生成，继续回答原问题", [s["title"] for s in run["steps"]])
        user = next(m for m in self.store.list_messages(self.actor, self.sid) if m["role"] == "user")
        self.assertEqual(user["metadata"]["resolved_report_id"], REPORT_ID)
        self.assertEqual(user["metadata"]["selection_mode"], "auto")

    def test_live_progress_survives_refresh_before_model_finishes_and_cancel(self):
        self.runtime.block = True
        accepted = self.qa.submit(self.actor, session_id=self.sid, question="评论有多少？", report_id=REPORT_ID, request_id="live-progress")
        self.assertTrue(self.runtime.started.wait(3))
        run_id = accepted["run"]["run_id"]
        before = self.store.get_run(self.actor, run_id)
        events = [s for s in before["steps"] if s["input_summary"].get("live_event")]
        self.assertTrue(any(e["status"] == "completed" for e in events))
        self.assertTrue(any(e["status"] == "running" for e in events))
        self.assertFalse(any(m["role"] == "assistant" for m in self.store.list_messages(self.actor, self.sid)))
        self.qa.cancel(self.actor, self.sid, run_id)
        self.runtime.release.set()
        final = self.store.get_run(self.actor, run_id)
        self.assertEqual(final["status"], "cancelled")
        self.assertFalse(any(s["status"] == "running" for s in final["steps"]))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            saved = self.qa.turns.draft(self.actor, run_id)
            if saved["action"].get("cumulative_model_calls") == 1:
                break
            time.sleep(.01)
        self.assertEqual(saved["action"]["cumulative_model_calls"], 1)

    def test_invalid_format_candidate_is_retained_privately_without_reasoning(self):
        self.runtime.callback = lambda context, index: {"type": "answer", "status": "answered", "answer": "FORMAT_PRIVATE_CANDIDATE", "citations": "invalid", "limitations": []}
        run, message = self.submit()
        self.assertEqual(run["status"], "failed")
        with self.store._connect() as db:
            saved = db.execute("SELECT payload_json FROM research_drafts WHERE run_id=?", (run["run_id"],)).fetchone()[0]
        self.assertIn("FORMAT_PRIVATE_CANDIDATE", saved)
        self.assertNotIn("FORMAT_PRIVATE_CANDIDATE", json.dumps(message))
        self.assertTrue(message["metadata"]["research_diagnostics"]["calls"][-1]["schema_errors"])

    def test_model_has_only_three_research_tools_but_legacy_evidence_still_works(self):
        exposed = model_tools(self.service.registry)
        self.assertEqual({item["name"] for item in exposed}, {
            "get_report_section", "query_report_reviews", "aggregate_report_reviews",
        })
        self.assertEqual(len(exposed), 3)
        self.assertIsNotNone(self.service.registry.get("zhuojian.query_evidence@v1"))
        legacy = self.qa.invoke(
            self.actor, "query_evidence", {"report_id": REPORT_ID, "evidence_group_id": GROUP_ID},
            session_id=self.sid,
        )
        self.assertEqual(legacy["status"], "ok")
        self.assertEqual(legacy["data"]["items"][0]["quote"], QUOTE)

    def test_model_cannot_invoke_legacy_evidence_but_can_repair_to_full_reviews(self):
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return tool("query_evidence", evidence_group_id=GROUP_ID)
            if index == 2:
                self.assertIn("tool_name", context["observations"][-1]["validation_feedback"])
                return tool(query="old mower", limit=1)
            return answer(context, "原文描述旧机器损坏，不能认定当前产品故障。", "review")

        self.runtime.callback = respond
        run, message = self.submit("这条原文说的是当前产品故障吗？")
        self.assertEqual(run["status"], "completed", message)
        self.assertFalse(any(name == "zhuojian.query_evidence@v1" for name, _ in self.transport.payloads))
        self.assertTrue(any(name == "zhuojian.query_report_reviews@v1" for name, _ in self.transport.payloads))
        self.assertEqual(message["metadata"]["research"]["review_records_read"], 1)

    def test_broad_negative_report_only_claim_must_read_full_review_before_delivery(self):
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return answer(context, "已核对原文，主要问题是当前电池故障。")
            if index == 2:
                feedback = context["observations"][-1]["delivery_feedback"]
                self.assertIn("query_report_reviews", " ".join(feedback))
                self.assertFalse(any(source["kind"] == "review" for source in context["sources"]))
                return tool(query="old mower", limit=1)
            return answer(context, "核对后发现这条原文说的是旧机器故障，不能算当前产品电池故障。", "review")

        self.runtime.callback = respond
        run, message = self.submit("主要负面评论是什么？")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["metadata"]["research"]["review_records_read"], 1)
        self.assertTrue(any(check["kind"] == "delivery" and check["feedback"] for check in message["metadata"]["answer_checks"]))
        self.assertEqual(message["metadata"]["citations"][0]["record_id"], REVIEW["record_id"])

    def test_prior_turn_raw_review_cannot_satisfy_current_broad_answer_requirement(self):
        def read_previous(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return tool(query="old mower", limit=1)
            return answer(context, "这条原文描述旧机器损坏。", "review")

        self.runtime.callback = read_previous
        first_run, first_message = self.submit("先核对这条原文")
        self.assertEqual(first_run["status"], "completed", first_message)
        self.assertEqual(first_message["metadata"]["research"]["review_records_read"], 1)
        starting_call = len(self.runtime.contexts)
        starting_read = len(self.transport.payloads)
        self.runtime.callback = lambda context, index: (
            {"status": "pass", "feedback": []} if "answer_action" in context
            else answer(context, "已经核对原文，主要问题是电池故障。")
        )
        run, message = self.submit("主要负面评论是什么？", report_id=None)
        self.assertEqual(run["status"], "failed", message)
        self.assertEqual(message["metadata"]["error"]["code"], "answer_incomplete")
        self.assertEqual(message["metadata"]["citations"], [])
        current_context = self.runtime.contexts[starting_call]
        self.assertTrue(any(source.get("record_id") for turn in current_context["previous_turns"] for source in turn["previous_sources"]))
        self.assertFalse(any(source["kind"] == "review" for source in current_context["sources"]))
        self.assertFalse(any(name == "zhuojian.query_report_reviews@v1" for name, _ in self.transport.payloads[starting_read:]))
        delivery = [check for check in message["metadata"]["answer_checks"] if check["kind"] == "delivery"]
        self.assertEqual(len(delivery), 3)
        self.assertTrue(all(check["feedback"] for check in delivery))

    def test_aspect_baseline_predicate_distinguishes_broad_topics_from_specific_followups(self):
        positive = ["主要负面评论是什么？", "优缺点各前10", "pros & cons 各前10和依据", "最常见的抱怨", "报告主题排名"]
        negative = ["这条电池故障结论是否误读？", "为什么这条不是当前电池故障？", "自走与转向问题有多少重合？", "还有哪些相关原文？", "多少评论？"]
        for question in positive:
            with self.subTest(question=question):
                self.assertTrue(needs_aspect_baseline(question))
        for question in negative:
            with self.subTest(question=question):
                self.assertFalse(needs_aspect_baseline(question))

    def test_broad_negative_question_starts_with_all_aspects_and_can_read_high_star_complaints(self):
        original_call = self.transport.call

        def high_star_review(name, payload, actor, definition):
            result = original_call(name, payload, actor, definition)
            if name == "zhuojian.query_report_reviews@v1":
                result["data"]["items"][0].update(
                    rating=5, review_text="I gave it five stars, but the self propel buttons engage accidentally.")
            return result

        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                baseline = next(item["aspect_baseline"] for item in context["observations"] if "aspect_baseline" in item)
                self.assertEqual(baseline["status"], "complete")
                self.assertTrue(baseline["note"])
                source = next(source for source in context["sources"] if source.get("data", {}).get("section") == ASPECT_SECTION)
                self.assertEqual([{k: v for k, v in row.items() if k != "_evidence_semantics"} for row in source["data"]["content"]], ASPECT_ROWS)
                self.assertEqual(source["data"]["content"][-1]["explicit_negative_entities"], 3)
                self.assertEqual(source["data"]["content"][-1]["low_star_entities"], 0)
                return tool(query="self propel", match_mode="word", limit=20)
            raw = next(source for source in context["sources"] if source["kind"] == "review")
            self.assertEqual(raw["data"]["rating"], 5)
            return answer(context, "这条五星评论也抱怨自走按钮误触，负面体验不能只看低星评论。", "review")

        self.runtime.callback = respond
        with patch.object(self.transport, "call", side_effect=high_star_review):
            run, message = self.submit("主要负面评论是什么？")
        self.assertEqual(run["status"], "completed", message)
        baseline_reads = [payload for name, payload in self.transport.payloads if name.endswith("get_report_section@v1") and payload.get("section") == ASPECT_SECTION]
        self.assertEqual(len(baseline_reads), 1)
        self.assertEqual(baseline_reads[0]["limit"], 100)
        raw_reads = [payload for name, payload in self.transport.payloads if name.endswith("query_report_reviews@v1")]
        self.assertTrue(raw_reads)
        self.assertTrue(all("filters" not in payload for payload in raw_reads))

    def test_unavailable_or_incomplete_aspect_baseline_keeps_raw_research_available(self):
        for mode in ("missing", "empty", "partial", "capacity"):
            with self.subTest(mode=mode):
                self.sid = self.service.create_session(self.actor, f"Baseline {mode}")["session_id"]
                original_call = self.transport.call
                starting_call = len(self.runtime.contexts)

                def baseline_variant(name, payload, actor, definition):
                    if name.endswith("get_report_section@v1") and payload.get("section") == ASPECT_SECTION and mode == "missing":
                        raise AgentError("report_section_not_found", "没有此报告章节", http_status=404)
                    result = original_call(name, payload, actor, definition)
                    if name.endswith("get_report_section@v1") and payload.get("section") == ASPECT_SECTION:
                        if mode == "empty":
                            result["data"]["content"] = []
                            result["page"].update(returned=0, total=0)
                        elif mode == "partial":
                            result["page"].update(total=22, next_offset=21, has_more=True)
                        elif mode == "capacity":
                            result["data"]["content"][0]["note"] = "REJECTED_BASELINE_BODY_" + "X" * 520_000
                    return result

                def respond(context, index):
                    if "answer_action" in context:
                        return {"status": "pass", "feedback": []}
                    if index - starting_call == 1:
                        baseline = next(item["aspect_baseline"] for item in context["observations"] if "aspect_baseline" in item)
                        self.assertIn(baseline["status"], {"unavailable", "partial"})
                        self.assertTrue(baseline["note"])
                        self.assertIn("query_report_reviews", [item["name"] for item in context["tools"]])
                        if mode == "capacity":
                            self.assertFalse(any(source.get("data", {}).get("section") == ASPECT_SECTION for source in context["sources"]))
                        return tool(query="old mower", limit=1)
                    return answer(context, "主题基线尚未完整取得；这条原文描述的是旧机器故障，不能推及当前产品。", "review")

                self.runtime.callback = respond
                with patch.object(self.transport, "call", side_effect=baseline_variant):
                    run, message = self.submit("主要负面评论是什么？")
                self.assertEqual(run["status"], "completed", message)
                self.assertNotIn("REJECTED_BASELINE_BODY_", json.dumps(self.runtime.contexts[starting_call:]))

    def test_specific_questions_do_not_add_an_aspect_baseline_read(self):
        questions = ["这条电池故障结论是否误读？", "为什么这条不是当前电池故障？", "自走与转向问题有多少重合？", "还有哪些相关原文？", "多少评论？"]
        for question in questions:
            with self.subTest(question=question):
                before = len(self.transport.payloads)
                run, message = self.submit(question)
                self.assertEqual(run["status"], "completed", message)
                reads = self.transport.payloads[before:]
                self.assertFalse(any(payload.get("section") == ASPECT_SECTION for _, payload in reads))
                self.assertEqual(message["metadata"]["research"]["read_requests"], 3)

    def test_tool_argument_repair_uses_known_schema_without_echoing_invalid_payload(self):
        cases = [
            (tool(filters={"ratings": "PRIVATE_INVALID_RATINGS"}), tool(filters={"ratings": [1, 2]}), "review", ["ratings", "array", "integer", "1", "5"], ["PRIVATE_INVALID_RATINGS"]),
            (tool("aggregate_report_reviews", group_by="PRIVATE_INVALID_GROUP"), tool("aggregate_report_reviews", group_by="rating"), "analysis", ["group_by", "rating", "submission_month"], ["PRIVATE_INVALID_GROUP"]),
            (tool(query="old mower", PRIVATE_UNKNOWN_FIELD="PRIVATE_UNKNOWN_BODY"), tool(query="old mower"), "review", ["query", "仅允许字段"], ["PRIVATE_UNKNOWN_FIELD", "PRIVATE_UNKNOWN_BODY"]),
        ]
        for invalid, repaired, kind, expected_hints, rejected_text in cases:
            with self.subTest(invalid=invalid["args"]):
                self.sid = self.service.create_session(self.actor, "参数修复")["session_id"]
                starting_call = len(self.runtime.contexts)

                def respond(context, index):
                    if "answer_action" in context:
                        return {"status": "pass", "feedback": []}
                    relative_call = index - starting_call
                    if relative_call == 1:
                        return invalid
                    if relative_call == 2:
                        feedback = context["observations"][-1]["validation_feedback"]
                        for expected in expected_hints:
                            self.assertIn(expected, feedback)
                        for secret in rejected_text:
                            self.assertNotIn(secret, feedback)
                        return repaired
                    return answer(context, kind=kind)

                self.runtime.callback = respond
                run, message = self.submit()
                self.assertEqual(run["status"], "completed", message)
                for secret in rejected_text:
                    self.assertNotIn(secret, json.dumps(message["metadata"]))
                    self.assertNotIn(secret, json.dumps(self.runtime.contexts[starting_call:]))

    def test_model_reads_full_original_and_can_correct_rule_report(self):
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return tool(query="old mower")
            return answer(context, "这条评论说的是旧割草机坏了，不能算当前产品的电池故障。", "review", "My old mower broke")
        self.runtime.callback = respond
        run, message = self.submit("这条电池故障结论是否误读了原文？")
        self.assertEqual(run["status"], "completed", message)
        self.assertIn("不能算当前产品", message["content"])
        reference = message["metadata"]["citations"][0]
        raw = self.qa.source(self.actor, self.sid, reference["citation_id"])
        self.assertEqual(raw["review"]["review_text"], REVIEW["review_text"])
        self.assertEqual(message["metadata"]["research"]["review_records_read"], 1)
        self.assertEqual(message["metadata"]["answer_review"]["status"], "pass")

    def test_nonverbatim_preview_is_removed_and_simple_count_requires_semantic_review(self):
        invalid_preview = "PRIVATE_BAD_PREVIEW_SENTINEL"
        for answer_status, run_status in (("answered", "completed"), ("insufficient_evidence", "blocked")):
            with self.subTest(answer_status=answer_status):
                self.sid = self.service.create_session(self.actor, "预览复核")["session_id"]
                starting_call = len(self.runtime.contexts)

                def respond(context, index):
                    if index - starting_call == 1:
                        action = answer(context, quote=invalid_preview)
                        action["status"] = answer_status
                        return action
                    self.assertIn("answer_action", context)
                    self.assertNotIn("quote", context["answer_action"]["citations"][0])
                    feedback = next(item["citation_preview_feedback"] for item in context["observations"] if "citation_preview_feedback" in item)
                    for requirement in ("引号句", "数字", "解释"):
                        self.assertIn(requirement, feedback)
                    self.assertNotIn(invalid_preview, json.dumps(context))
                    return {"status": "pass", "feedback": []}

                self.runtime.callback = respond
                run, message = self.submit()
                self.assertEqual(run["status"], run_status, message)
                self.assertEqual(message["metadata"]["model_calls"], 2)
                self.assertEqual(len(message["metadata"]["citations"]), 1)
                self.assertNotIn("quote", message["metadata"]["citations"][0])
                self.assertIs(message["metadata"]["preview_dropped"], True)
                self.assertEqual(message["metadata"]["preview_dropped_source_ids"], ["S1"])
                self.assertEqual(message["metadata"]["answer_review"]["status"], "pass")
                self.assertNotIn(invalid_preview, json.dumps(message["metadata"]))

    def test_removed_preview_cannot_publish_without_budget_for_required_review(self):
        invalid_preview = "PRIVATE_BAD_PREVIEW_SENTINEL"
        for answer_status in ("answered", "insufficient_evidence"):
            with self.subTest(answer_status=answer_status):
                self.sid = self.service.create_session(self.actor, "预览预算")["session_id"]
                starting_call = len(self.runtime.contexts)

                def respond(context, index):
                    action = answer(context, quote=invalid_preview)
                    action["status"] = answer_status
                    return action

                self.runtime.callback = respond
                with patch("agent.research.MAX_MODEL_CALLS", 1):
                    run, message = self.submit()
                self.assertEqual(run["status"], "failed", message)
                self.assertEqual(message["metadata"]["error"]["code"], "research_budget_exhausted")
                self.assertEqual(message["metadata"]["model_calls"], 1)
                self.assertEqual(len(self.runtime.contexts) - starting_call, 1)
                self.assertNotIn("answer_action", self.runtime.contexts[starting_call])
                self.assertEqual(message["metadata"]["citations"], [])
                self.assertIs(message["metadata"]["preview_dropped"], True)
                self.assertEqual(message["metadata"]["preview_dropped_source_ids"], ["S1"])
                self.assertNotIn(invalid_preview, json.dumps(message["metadata"]))

    def test_twenty_ranked_items_are_not_cut_to_six(self):
        rows = "优点：\n" + "\n".join(f"{i}. 优点{i}" for i in range(1, 11))
        rows += "\n缺点：\n" + "\n".join(f"{i}. 缺点{i}" for i in range(1, 11)) + "\n依据报告内的明确正向和负向提及数分别排序。"
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return tool(query="old mower")
            return answer(context, rows)
        self.runtime.callback = respond
        run, message = self.submit("只列报告已有的优缺点各前10主题和数量")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual([len(table["rows"]) for table in message["metadata"]["rankings"]], [10, 10])
        self.assertEqual(message["metadata"]["model_calls"], 0)
        self.assertIn("并列", message["content"])

    def test_ordinary_ranking_uses_originals_and_computed_context_instead_of_ending_at_labels(self):
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return answer(context, "下面是优点和缺点前十，按提及人数排序。")
            if index == 2:
                self.assertTrue(context["observations"][-1]["delivery_feedback"])
                return tool(query="old mower")
            rows = "## 优点\n" + "\n".join(f"{i}. 优点{i}" for i in range(1, 11))
            rows += "\n## 缺点\n" + "\n".join(f"{i}. 缺点{i}" for i in range(1, 11))
            return answer(context, rows)
        self.runtime.callback = respond
        run, message = self.submit("pros & cons 各前10和依据")
        self.assertEqual(run["status"], "completed", message)
        self.assertNotIn("rankings", message["metadata"])
        ranking = next(row["report_ranking_context"] for row in self.runtime.contexts[0]["observations"] if "report_ranking_context" in row)
        self.assertEqual([len(table["rows"]) for table in ranking["rankings"]], [10, 10])
        self.assertGreater(message["metadata"]["model_calls"], 0)
        self.assertTrue(any(name == "zhuojian.query_report_reviews@v1" for name, _ in self.transport.payloads))

    def test_short_interpretive_answer_still_requires_semantic_review(self):
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return tool(query="old mower", limit=1)
            return answer(context, "原文谈的是旧机器故障，不能算当前产品故障。", "review")

        self.runtime.callback = respond
        run, message = self.submit("主要负面评论是什么？")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["metadata"]["model_calls"], 3)
        self.assertIn("answer_action", self.runtime.contexts[-1])

    def test_oversized_review_page_is_rejected_before_context_and_can_be_reduced(self):
        original_call = self.transport.call
        records = [
            {**REVIEW, "record_id": f"rev_large_{index}", "review_entity_id": f"rev_large_{index}",
             "review_text": f"FULL_REVIEW_{index}_" + chr(65 + index) * 130_000}
            for index in range(4)
        ]

        def large_page(name, payload, actor, definition):
            result = original_call(name, payload, actor, definition)
            if name == "zhuojian.query_report_reviews@v1":
                count = payload["limit"]
                result["data"]["items"] = copy.deepcopy(records[:count])
                result["page"] = {"offset": 0, "limit": count, "returned": count, "total": 4,
                                  "next_offset": count if count < 4 else None, "has_more": count < 4}
            return result

        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return tool(limit=4)
            if index == 2:
                refused = context["observations"][-1]
                self.assertEqual(refused["tool_error"]["code"], "research_context_too_large")
                self.assertTrue(refused["next_step"])
                self.assertFalse(any(source["kind"] == "review" for source in context["sources"]))
                return tool(limit=1)
            review_sources = [source for source in context["sources"] if source["kind"] == "review"]
            self.assertEqual([source["data"]["review_text"] for source in review_sources], [records[0]["review_text"]])
            self.assertEqual([source["source_id"] for source in context["sources"]], [f"S{i}" for i in range(1, 6)])
            return answer(context, "已完整读取第一条评论，其余评论尚未核对。", "review")

        self.runtime.callback = respond
        with patch.object(self.transport, "call", side_effect=large_page):
            run, message = self.submit("读取评论原文并说明已核对范围")
            self.assertEqual(run["status"], "completed", message)
            reference = message["metadata"]["citations"][0]
            self.assertEqual(self.qa.source(self.actor, self.sid, reference["citation_id"])["review"]["review_text"], records[0]["review_text"])
        self.assertEqual(message["metadata"]["research"]["read_requests"], 4)
        self.assertEqual(message["metadata"]["research"]["review_records_read"], 1)
        self.assertNotIn("FULL_REVIEW_1_", json.dumps(self.runtime.contexts))

    def test_single_review_over_context_limit_reports_that_full_check_is_unavailable(self):
        original_call = self.transport.call
        oversized_body = "UNREAD_SINGLE_REVIEW_" + "X" * 600_000

        def large_record(name, payload, actor, definition):
            result = original_call(name, payload, actor, definition)
            if name == "zhuojian.query_report_reviews@v1":
                result["data"]["items"][0]["review_text"] = oversized_body
            return result

        def respond(context, index):
            if index == 1:
                return tool(limit=1)
            refused = context["observations"][-1]
            self.assertEqual(refused["tool_error"]["code"], "research_context_too_large")
            self.assertIn("单条", refused["tool_error"]["message"])
            self.assertIn("完整核对", refused["tool_error"]["message"])
            self.assertFalse(any(source["kind"] == "review" for source in context["sources"]))
            return {"type": "answer", "status": "insufficient_evidence",
                    "answer": "这条评论过长，本轮无法完整核对，不能据此判断故障原因。",
                    "citations": [], "limitations": ["尚未完整核对这条评论。"]}

        self.runtime.callback = respond
        with patch.object(self.transport, "call", side_effect=large_record):
            run, message = self.submit("完整核对这条评论的故障原因")
        self.assertEqual(run["status"], "blocked", message)
        self.assertIn("无法完整核对", message["content"])
        self.assertEqual(message["metadata"]["research"]["read_requests"], 3)
        self.assertEqual(message["metadata"]["research"]["review_records_read"], 0)
        self.assertEqual(message["metadata"]["citations"], [])
        self.assertNotIn("UNREAD_SINGLE_REVIEW_", json.dumps(self.runtime.contexts))

    def test_history_counts_toward_page_admission_and_smaller_page_can_finish(self):
        original_call = self.transport.call
        original_messages = self.store.list_messages
        history = []
        for index in range(6):
            previous_run = f"historical-run-{index}"
            history.extend([
                {"role": "user", "content": "请继续核对上一项。", "run_id": previous_run, "metadata": {}},
                {"role": "assistant", "content": "H" * 19_000, "run_id": previous_run,
                 "metadata": {"report_id": REPORT_ID}},
            ])
        records = [
            {**REVIEW, "record_id": f"rev_history_{index}", "review_entity_id": f"rev_history_{index}",
             "review_text": f"HISTORY_PAGE_{index}_" + "R" * 90_000}
            for index in range(4)
        ]
        self.assertLess(len(json.dumps(records)), 460_000)

        def with_history(*args, **kwargs):
            return history + original_messages(*args, **kwargs)

        def review_page(name, payload, actor, definition):
            result = original_call(name, payload, actor, definition)
            if name == "zhuojian.query_report_reviews@v1":
                count = payload["limit"]
                result["data"]["items"] = copy.deepcopy(records[:count])
                result["page"] = {"offset": 0, "limit": count, "returned": count, "total": 4,
                                  "next_offset": count if count < 4 else None, "has_more": count < 4}
            return result

        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            self.assertEqual(len(context["previous_turns"]), 12)
            if index == 1:
                return tool(limit=4)
            if index == 2:
                refused = context["observations"][-1]["tool_error"]
                self.assertEqual(refused["code"], "research_context_too_large")
                self.assertIn("对话", refused["message"])
                self.assertFalse(any(source["kind"] == "review" for source in context["sources"]))
                return tool(limit=1)
            return answer(context, "本轮完整核对了第一条评论，尚未读取其余三条。", "review")

        self.runtime.callback = respond
        with patch.object(self.store, "list_messages", side_effect=with_history), patch.object(self.transport, "call", side_effect=review_page):
            run, message = self.submit("接着读原文并说明核对范围")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["metadata"]["research"]["read_requests"], 4)
        self.assertEqual(message["metadata"]["research"]["review_records_read"], 1)
        self.assertNotIn("HISTORY_PAGE_1_", json.dumps(self.runtime.contexts))

    def test_persistent_style_suggestion_still_delivers_count_after_semantic_review(self):
        candidate = "这份报告有20条评论实体，其中18条有文字。"

        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 2:
                self.assertTrue(context["observations"][-1]["style_feedback"])
                self.assertEqual(context["tools"], [])
            return answer(context, candidate)

        self.runtime.callback = respond
        run, message = self.submit()
        self.assertEqual(run["status"], "completed", message)
        self.assertIn(candidate, message["content"])
        self.assertEqual(message["metadata"]["model_calls"], 3)
        self.assertIn("answer_action", self.runtime.contexts[-1])
        checks = message["metadata"]["answer_checks"]
        style_checks = [check for check in checks if check["kind"] == "style"]
        self.assertEqual(len(style_checks), 2)
        self.assertTrue(all(check["feedback"] for check in style_checks))
        self.assertTrue(any(check["kind"] == "review" and check["status"] == "pass" for check in checks))
        self.assertTrue(all(set(check) <= {"kind", "feedback", "candidate_chars", "status"} for check in checks))
        self.assertNotIn(candidate, json.dumps(checks, ensure_ascii=False))

    def test_persistent_missing_ranked_items_still_fails_with_delivery_feedback(self):
        candidate = "优点\n1. 动力充足\n缺点\n1. 按钮容易误触"

        def respond(context, index):
            if index == 1:
                return tool(limit=1)
            self.assertNotIn("answer_action", context)
            return answer(context, candidate, "review")

        self.runtime.callback = respond
        run, message = self.submit("结合原文，优缺点各前2，并给出依据")
        self.assertEqual(run["status"], "failed", message)
        self.assertEqual(message["metadata"]["error"]["code"], "answer_incomplete")
        checks = [check for check in message["metadata"]["answer_checks"] if check["kind"] == "delivery"]
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(check["feedback"] and check["candidate_chars"] > 0 for check in checks))
        self.assertTrue(all("前2" in " ".join(check["feedback"]) for check in checks))
        self.assertNotIn(candidate, json.dumps(message["metadata"], ensure_ascii=False))
        self.assertEqual(message["metadata"]["citations"], [])

    def test_failed_semantic_review_preserves_feedback_and_size_without_candidate_text(self):
        candidate = "PRIVATE_CANDIDATE_SENTINEL：这条评论证明当前电池损坏。"
        feedback_text = "原文谈的是旧机器损坏，不能推出当前产品故障。"

        def respond(context, index):
            if "answer_action" in context:
                return {"status": "revise", "feedback": [feedback_text]}
            if "repair_request" in context:
                old = "当前" if "当前" in context["repair_request"]["answer"] else "本机"
                return {"patches": [{"old": old, "new": "本机" if old == "当前" else "当前"}]}
            if index == 1:
                return tool(limit=1)
            return answer(context, candidate, "review")

        self.runtime.callback = respond
        run, message = self.submit("这条原文是否证明当前产品出现电池故障？")
        self.assertEqual(run["status"], "failed", message)
        self.assertEqual(message["metadata"]["error"]["code"], "answer_review_failed")
        checks = message["metadata"]["answer_checks"]
        reviews = [check for check in checks if check["kind"] == "review"]
        self.assertEqual(len(reviews), 3)
        self.assertTrue(all(check["status"] == "revise" and check["feedback"] == [feedback_text] for check in reviews))
        self.assertTrue(all(check["candidate_chars"] == len(candidate + "[S4]") for check in reviews))
        self.assertTrue(all(set(check) <= {"kind", "feedback", "candidate_chars", "status"} for check in checks))
        self.assertNotIn("PRIVATE_CANDIDATE_SENTINEL", json.dumps(message["metadata"], ensure_ascii=False))
        self.assertEqual(message["metadata"]["citations"], [])
        with self.store._connect() as db:
            saved = db.execute("SELECT payload_json FROM research_drafts WHERE run_id=?", (run["run_id"],)).fetchone()
        self.assertIn("PRIVATE_CANDIDATE_SENTINEL", saved[0])

    def test_cross_topic_uses_full_population_aggregate_not_page_size(self):
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return tool("aggregate_report_reviews", term_groups=[{"label": "drive", "terms": ["propel", "drive"]}, {"label": "turn", "terms": ["turn"]}])
            return answer(context, "同时提到自走和转向的有2条；这是关键词交集，还需要读原文判断具体体验。", "analysis")
        self.runtime.callback = respond
        run, message = self.submit("自走和转向的问题有多少重合？")
        self.assertEqual(run["status"], "completed", message)
        payload = next(p for n, p in self.transport.payloads if n.endswith("aggregate_report_reviews@v1") and p.get("term_groups"))
        self.assertEqual(len(payload["term_groups"]), 2)
        self.assertEqual(payload["report_id"], REPORT_ID)

    def test_followup_preserves_original_user_words_and_can_read_next_page(self):
        self.submit("先看自走的问题")
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if not any(s["kind"] == "review" for s in context["sources"]):
                return tool(offset=1, limit=1)
            return answer(context, "另一条说，转弯时容易误触自走按钮。", "review")
        self.runtime.callback = respond
        run, message = self.submit("再看下一条", report_id=None)
        self.assertEqual(run["status"], "completed", message)
        history = next(context["previous_turns"] for context in self.runtime.contexts if context["question"] == "再看下一条")
        self.assertEqual(history[0]["text"], "先看自走的问题")
        self.assertEqual(message["metadata"]["citations"][0]["record_id"], "rev_next")

    def test_selected_report_id_is_not_an_available_model_argument(self):
        self.runtime.callback = lambda context, index: tool(report_id="rpt_other")
        run, message = self.submit()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(message["metadata"]["error"]["code"], "llm_contract_invalid")
        self.assertTrue(all(p["report_id"] == REPORT_ID for _, p in self.transport.payloads))
        self.assertFalse(any("query_report_reviews" in n for n, _ in self.transport.payloads))

    def test_tool_budget_leaves_room_for_draft_review_revision_and_final_review(self):
        reviews = []
        def respond(context, index):
            if "answer_action" in context:
                reviews.append(index)
                return {"status": "revise", "feedback": ["请明确这只是单条原文。"]} if len(reviews) == 1 else {"status": "pass", "feedback": []}
            if "repair_request" in context:
                return {"patches": [{"old": "这条原文", "new": "仅这条原文"}]}
            if index in {1, 3, 5}:
                return tool(filters={"ratings": "invalid"})
            if context["tools"]:
                return tool(query="old mower")
            return answer(context, "这条原文表扬当前产品动力；不能把旧机器损坏当当前产品故障。", "review")
        self.runtime.callback = respond
        run, message = self.submit("继续核对原文直到能明确回答")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["metadata"]["model_calls"], 14)
        self.assertEqual(reviews, [12, 14])

    def test_missing_raw_does_not_block_saved_count_but_cannot_fake_originals(self):
        self.transport.mode = "missing_raw"
        run, message = self.submit()
        self.assertEqual(run["status"], "completed", message)
        self.runtime.callback = lambda context, index: tool(query="old mower")
        run, message = self.submit("读原文核对旧机器故障")
        self.assertEqual(run["status"], "blocked", message)
        self.assertEqual(message["metadata"]["citations"], [])

    def test_snapshot_mismatch_stops_before_sending_data_to_model(self):
        self.transport.mode = "wrong_snapshot"
        run, message = self.submit()
        self.assertEqual(run["status"], "failed", message)
        self.assertEqual(message["metadata"]["error"]["code"], "source_changed")
        self.assertEqual(len(self.runtime.contexts), 0)

    def test_content_drift_with_unchanged_ids_prevents_publication(self):
        self.transport.mode = "drift"
        self.runtime.callback = lambda context, index: answer(context, "共有20条。", "report")
        run, message = self.submit()
        self.assertEqual(run["status"], "failed", message)
        self.assertEqual(message["metadata"]["error"]["code"], "source_changed")

    def test_source_endpoint_rechecks_owner_and_current_report_authority(self):
        _, message = self.submit()
        cid = message["metadata"]["citations"][0]["citation_id"]
        with self.assertRaises(AgentError):
            self.qa.source(identity("another-user"), self.sid, cid)
        denied = replace(self.actor, allowed_report_ids=frozenset())
        with self.assertRaises(AgentError):
            self.qa.source(denied, self.sid, cid)

    def test_cancel_inflight_never_publishes_late_answer_or_runs_more_tools(self):
        self.runtime.block = True
        accepted = self.qa.submit(self.actor, session_id=self.sid, question="主要问题是什么？", report_id=REPORT_ID, request_id="cancel-research")
        self.assertTrue(self.runtime.started.wait(5))
        before = len(self.transport.payloads)
        self.qa.cancel(self.actor, self.sid, accepted["run"]["run_id"])
        self.runtime.release.set()
        self.qa.pool.shutdown(wait=True)
        self.assertEqual(len(self.transport.payloads), before)
        messages = [m for m in self.store.list_messages(self.actor, self.sid) if m["role"] == "assistant"]
        self.assertEqual([m["content"] for m in messages], ["已停止回答。"])

    def test_tool_loop_budget_terminates_without_fabricated_partial_answer(self):
        self.runtime.callback = lambda context, index: tool(offset=index, limit=1)
        with patch("agent.research.MAX_MODEL_CALLS", 2):
            run, message = self.submit()
        self.assertEqual(run["status"], "failed", message)
        self.assertEqual(message["metadata"]["error"]["code"], "research_budget_exhausted")
        self.assertEqual(message["metadata"]["model_calls"], 2)
        self.assertEqual(message["metadata"]["citations"], [])

    def test_bad_format_has_one_bounded_repair_and_preserves_usage(self):
        self.runtime.callback = lambda context, index: {"garbage": True} if index == 1 else answer(context)
        run, message = self.submit()
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["metadata"]["model_calls"], 2)
        self.assertEqual(message["metadata"]["usage"]["total_tokens"], 14)

    def test_tool_then_answer_format_can_each_be_repaired_before_review(self):
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            if index == 1:
                return tool(filters={"ratings": "invalid"})
            if index == 2:
                return tool(query="old mower", limit=1)
            result = answer(context, "原文说旧机器损坏，不能认定当前产品故障。", "review")
            if index == 3:
                result["limitations"] = "invalid"
            return result

        self.runtime.callback = respond
        run, message = self.submit("这条原文说的是当前产品故障吗？")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["metadata"]["model_calls"], 5)
        self.assertEqual(message["metadata"]["usage"]["total_tokens"], 35)
        self.assertEqual(message["metadata"]["research"]["review_records_read"], 1)
        self.assertEqual(message["metadata"]["answer_review"]["status"], "pass")
        feedback = [item for item in self.runtime.contexts[3]["observations"] if "validation_feedback" in item]
        self.assertEqual(len(feedback), 2)
        self.assertFalse(any(payload.get("filters", {}).get("ratings") == "invalid" for _, payload in self.transport.payloads))

    def test_consecutive_format_errors_stop_without_a_valid_action(self):
        cases = {
            "tool": lambda context: tool(filters={"ratings": "invalid"}),
            "answer": lambda context: {**answer(context), "limitations": "invalid"},
        }
        for category, invalid in cases.items():
            with self.subTest(category=category):
                self.sid = self.service.create_session(self.actor, category)["session_id"]
                start = len(self.runtime.contexts)
                self.runtime.callback = lambda context, index: invalid(context)
                run, message = self.submit()
                self.assertEqual(run["status"], "failed", message)
                self.assertEqual(message["metadata"]["error"]["code"], "llm_contract_invalid")
                self.assertEqual(len(self.runtime.contexts) - start, 2)
                self.assertEqual(message["metadata"]["model_calls"], 2)
                self.assertEqual(message["metadata"]["citations"], [])

    def test_unclassified_action_then_invalid_tool_stops_after_one_consecutive_repair(self):
        for malformed in ({"type": "unknown"}, [], "not a JSON action"):
            with self.subTest(malformed=malformed):
                self.sid = self.service.create_session(self.actor, "Unknown action")["session_id"]
                start = len(self.runtime.contexts)

                def respond(context, index):
                    relative = index - start
                    if relative == 1:
                        return malformed
                    if relative == 2:
                        return tool(filters={"ratings": "invalid"})
                    return {**answer(context), "limitations": "invalid"}

                self.runtime.callback = respond
                original_complete = self.runtime.complete

                def complete(**kwargs):
                    response = original_complete(**kwargs)
                    if len(self.runtime.contexts) == start + 1 and isinstance(malformed, str):
                        response["choices"][0]["message"]["content"] = malformed
                    return response

                with patch.object(self.runtime, "complete", side_effect=complete):
                    run, message = self.submit()
                self.assertEqual(run["status"], "failed", message)
                self.assertEqual(message["metadata"]["error"]["code"], "llm_contract_invalid")
                self.assertEqual(message["metadata"]["model_calls"], 2)
                self.assertEqual(message["metadata"]["citations"], [])

    def test_valid_tool_progress_allows_later_independent_format_repairs(self):
        def respond(context, index):
            if "answer_action" in context:
                return {"status": "pass", "feedback": []}
            actions = {
                1: tool(filters={"ratings": "invalid"}),
                2: tool(query="old mower", limit=1),
                3: tool("aggregate_report_reviews", group_by="invalid"),
                4: tool("aggregate_report_reviews", group_by="rating"),
                5: tool("get_report_section", section=3),
                6: tool("get_report_section", section="/metrics"),
            }
            return actions.get(index) or answer(context, "原文说旧机器损坏，不能认定当前产品故障。", "review")

        self.runtime.callback = respond
        run, message = self.submit("这条原文说的是当前产品故障吗？")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["metadata"]["model_calls"], 8)
        self.assertEqual(message["metadata"]["usage"]["total_tokens"], 56)
        self.assertEqual(message["metadata"]["research"]["read_requests"], 6)
        self.assertEqual(message["metadata"]["research"]["review_records_read"], 1)
        self.assertEqual(message["metadata"]["answer_review"]["status"], "pass")
        self.assertTrue(all(payload.get("filters", {}).get("ratings") != "invalid"
                            and payload.get("group_by") != "invalid" and payload.get("section") != 3
                            for _, payload in self.transport.payloads))

    def test_two_format_repairs_do_not_extend_total_call_limit_or_skip_review(self):
        def respond(context, index):
            if index == 1:
                return tool(filters={"ratings": "invalid"})
            if index == 2:
                return tool(query="old mower", limit=1)
            result = answer(context, "原文说的是旧机器损坏。", "review")
            if index == 3:
                result["limitations"] = "invalid"
            return result

        self.runtime.callback = respond
        with patch("agent.research.MAX_MODEL_CALLS", 4):
            run, message = self.submit("这条原文说的是当前产品故障吗？")
        self.assertEqual(run["status"], "failed", message)
        self.assertEqual(message["metadata"]["error"]["code"], "research_budget_exhausted")
        self.assertEqual(message["metadata"]["model_calls"], 4)
        self.assertEqual(len(self.runtime.contexts), 4)
        self.assertEqual(message["metadata"]["citations"], [])
        self.assertEqual(self.runtime.contexts[2]["tools"], [])

    def test_provider_failures_are_not_format_repairs(self):
        for code in ("llm_timeout", "llm_provider_unavailable", "llm_upstream_error", "llm_contract_invalid"):
            with self.subTest(code=code):
                self.sid = self.service.create_session(self.actor, code)["session_id"]
                start = len(self.runtime.contexts)

                def respond(context, index):
                    raise AgentError(code, "Synthetic provider failure", http_status=503, retryable=True)

                self.runtime.callback = respond
                run, message = self.submit()
                self.assertEqual(run["status"], "failed", message)
                self.assertEqual(message["metadata"]["error"]["code"], code)
                self.assertEqual(len(self.runtime.contexts) - start, 1)
                self.assertEqual(message["metadata"]["model_calls"], 1)

    def test_missing_or_unknown_failure_stage_does_not_receive_a_format_repair(self):
        for stage in (None, "unrecognized_stage"):
            with self.subTest(stage=stage):
                self.sid = self.service.create_session(self.actor, "Unclassified failure")["session_id"]
                start = len(self.runtime.contexts)

                def fail(*args, diagnostics, **kwargs):
                    if stage is not None:
                        diagnostics["failure_stage"] = stage
                    raise AgentError("llm_contract_invalid", "Synthetic unclassified failure", http_status=502)

                with patch("agent.research_model.parse_research_action", side_effect=fail):
                    run, message = self.submit()
                self.assertEqual(run["status"], "failed", message)
                self.assertEqual(message["metadata"]["error"]["code"], "llm_contract_invalid")
                self.assertEqual(len(self.runtime.contexts) - start, 1)

    def test_review_format_failure_does_not_receive_a_separate_repair(self):
        self.runtime.callback = lambda context, index: {"status": "invalid", "feedback": []} if "answer_action" in context else answer(context)
        run, message = self.submit("请解释这份报告")
        self.assertEqual(run["status"], "failed", message)
        self.assertEqual(message["metadata"]["error"]["code"], "llm_contract_invalid")
        self.assertEqual(message["metadata"]["model_calls"], 2)
        self.assertEqual(message["metadata"]["research_diagnostics"]["calls"][-1]["failure_stage"], "review_shape")
        self.assertEqual(message["metadata"]["citations"], [])


if __name__ == "__main__":
    unittest.main()
