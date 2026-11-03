from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.agent_trace import AgentTraceStore
from agent.contracts import AgentError, sha256_value
from agent.eval_export import EVAL_EXPORT_CONTRACT, EvalExporter
from agent.tests.test_agent_trace import actor


class EvalExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = AgentTraceStore(Path(self.temp.name) / "logs" / "traces.sqlite3", content_enabled=True,
                                     payload_retention_seconds=10, clock=lambda: self.now)
        self.store.initialize()
        self.user = actor()
        self.exporter = EvalExporter(self.store)
        self.trace = self.store.start_trace(self.user, session_id="session1", run_id="run1", question="用户真正的问题",
            model_config={"model": "requested-alias", "effort": "high", "prompt_version": "research/v2"},
            data_manifest=[{"dataset_id": "dataset1", "snapshot_id": "snapshot1", "sha256": "sha256:" + "a" * 64}])
        self.trace_id = self.trace["trace_id"]
        self.store.append_event(self.user, self.trace_id, event_id="model1", event_type="model.finished", attempt_id="attempt1",
            data={"actual_model": "fixed-model-id", "usage": {"prompt_tokens": 30}, "elapsed_ms": 400, "review_result": "pass"},
            payload={"messages": [{"content": "完整评论原文"}], "reasoning_content": "never-export-reasoning", "api_key": "never-export-key"})
        self.store.append_event(self.user, self.trace_id, event_id="tool1", event_type="tool.finished", attempt_id="attempt1",
            data={"tool_name": "query_reviews", "args": {"limit": 24}, "result_summary": {"records": 24}, "source_sha256": "sha256:" + "b" * 64})
        self.store.finish_trace(self.user, self.trace_id, status="partial", answer="有原文支持的答案")

    def tearDown(self):
        self.temp.cleanup()

    def test_default_export_omits_content_and_keeps_reproduction_identity(self):
        result = self.exporter.export(self.user, [self.trace_id])
        self.assertEqual(result["contract"], EVAL_EXPORT_CONTRACT)
        encoded = json.dumps(result, ensure_ascii=False)
        for content in ("用户真正的问题", "有原文支持的答案", "完整评论原文", "never-export-key", "never-export-reasoning"):
            self.assertNotIn(content, encoded)
        case = result["cases"][0]
        self.assertEqual(case["question"]["content"], "omitted")
        self.assertEqual(case["model_config"]["prompt_version"], "research/v2")
        self.assertEqual(case["model_events"][0]["data"]["actual_model"], "fixed-model-id")
        self.assertEqual(case["data_manifest"][0]["snapshot_id"], "snapshot1")
        self.assertEqual(case["tool_events"][0]["data"]["args"], {"limit": 24})
        self.assertFalse(case["reproduction"]["has_question_content"])
        self.assertFalse(case["reproduction"]["deterministic_replay_guaranteed"])
        expected = result.pop("export_sha256")
        self.assertEqual(expected, sha256_value(result))

    def test_explicit_content_export_includes_same_owner_artifacts_still_redacted(self):
        result = self.exporter.export(self.user, [self.trace_id], include_content=True)
        case = result["cases"][0]
        self.assertEqual(case["question"]["value"], "用户真正的问题")
        self.assertEqual(case["answer"]["value"], "有原文支持的答案")
        self.assertTrue(case["reproduction"]["has_question_content"])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertIn("完整评论原文", encoded)
        self.assertNotIn("never-export-key", encoded)
        self.assertNotIn("never-export-reasoning", encoded)

    def test_other_owner_admin_cannot_export_or_mix_traces(self):
        other = actor("user-b", roles=("admin",))
        other_trace = self.store.start_trace(other, session_id="session2", run_id="run2", question="another user")
        for user, ids in ((other, [self.trace_id]), (self.user, [self.trace_id, other_trace["trace_id"]])):
            with self.assertRaises(AgentError) as caught:
                self.exporter.export(user, ids, include_content=True)
            self.assertEqual(caught.exception.code, "trace_not_found")

    def test_only_explicit_feedback_is_human_label(self):
        self.store.append_event(self.user, self.trace_id, event_id="fake-feedback", event_type="feedback.model_guess", data={"rating": "helpful", "provenance": "user_submitted"})
        self.assertEqual(self.exporter.export(self.user, [self.trace_id])["cases"][0]["human_feedback"], [])
        self.store.record_feedback(self.user, self.trace_id, feedback_id="f1", rating="unhelpful", labels=["wrong_condition"], comment="漏掉了草种条件")
        case = self.exporter.export(self.user, [self.trace_id], include_content=True)["cases"][0]
        self.assertEqual(len(case["human_feedback"]), 1)
        self.assertEqual(case["human_feedback"][0]["labels"], ["wrong_condition"])
        self.assertEqual(case["human_feedback"][0]["comment"]["value"], "漏掉了草种条件")
        self.assertEqual(case["model_events"][0]["data"]["review_result"], "pass")

    def test_expired_content_export_is_explicitly_unavailable_not_fabricated(self):
        self.now += 11
        result = self.exporter.export(self.user, [self.trace_id], include_content=True)
        case = result["cases"][0]
        self.assertEqual(case["question"]["content"], "unavailable")
        self.assertNotIn("value", case["question"])
        self.assertFalse(case["reproduction"]["has_question_content"])

    def test_exports_are_stable_until_trace_revision_changes(self):
        first = self.exporter.export(self.user, [self.trace_id])
        self.now += 1
        self.assertEqual(first, self.exporter.export(self.user, [self.trace_id]))
        self.store.append_event(self.user, self.trace_id, event_id="late", event_type="tool.finished", data={"records": 1})
        second = self.exporter.export(self.user, [self.trace_id])
        self.assertNotEqual(first["export_sha256"], second["export_sha256"])
        self.assertEqual(second["cases"][0]["through_seq"], first["cases"][0]["through_seq"] + 1)
        self.assertEqual(second["cases"][0]["answer"], first["cases"][0]["answer"])


if __name__ == "__main__":
    unittest.main()
