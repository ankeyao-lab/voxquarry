import copy
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agent.contracts import AgentError
from agent.harness_findings import documents, evidence_packet
from agent.review_topic_counts import prepare, statistics, parse_review
from agent.tests.test_harness_research import HarnessFixture, DATASET_ID
from agent.tests.test_research_report_export import ProductReportLifecycleTests


def fixture():
    original = {"r1": {"record_id": "r1", "review_text": "Quiet but hard to clean.", "rating": 5},
                "r2": {"record_id": "r2", "review_text": "Hard to clean in the kitchen.", "rating": 2},
                "r3": {"record_id": "r3", "review_text": "Great!", "rating": 5},
                "r4": {"record_id": "r4", "review_text": "", "rating": 1}}
    topics = [{"topic_id": "T1", "section": "pros", "title": "安静", "definition": "认可声音小"},
              {"topic_id": "T2", "section": "cons", "title": "清洁困难", "definition": "抱怨无法方便清洁"}]
    reviews = [{"record_id": "r1", "assignments": [{"topic_id": "T1", "quote": "Quiet", "context": ""},
               {"topic_id": "T2", "quote": "hard to clean", "context": ""}]},
              {"record_id": "r2", "assignments": [{"topic_id": "T2", "quote": "Hard to clean in the kitchen.", "context": "厨房"}]},
              {"record_id": "r3", "assignments": [], "no_match_reason": "一般好评，没有具体体验。"}]
    return {"topics": topics, "reviews": reviews}, original


class TopicCountsTests(unittest.TestCase):
    def test_topic_identity_does_not_require_an_arbitrary_letter_prefix(self):
        value, original = fixture()
        value["topics"][0]["topic_id"] = "quiet_positive"
        value["reviews"][0]["assignments"][0]["topic_id"] = "quiet_positive"
        topics, rows = prepare(value, original)
        self.assertEqual(statistics(topics, rows, DATASET_ID, "version", 4)["counts"][0]["count"], 1)

    def test_counts_use_all_review_members_not_rating_or_number_of_examples(self):
        value, original = fixture()
        topics, rows = prepare(value, original)
        result = statistics(topics, rows, DATASET_ID, "version", 4)
        self.assertEqual(result["sample"], {"total_entities": 4, "written_entities": 3})
        self.assertEqual([x["count"] for x in result["counts"]], [1, 2])
        self.assertEqual([x["percent"] for x in result["counts"]], [33.33, 66.67])
        self.assertEqual({x["record_id"] for x in result["counts"][1]["members"]}, {"r1", "r2"})
        self.assertEqual(rows[0]["original"]["review_text"], original["r1"]["review_text"])

    def test_missing_duplicate_foreign_and_invented_evidence_never_count(self):
        value, original = fixture()
        for change in (lambda x: x["reviews"].pop(),
                       lambda x: x["reviews"].append(x["reviews"][0]),
                       lambda x: x["reviews"][0].update(record_id="foreign"),
                       lambda x: x["reviews"][0]["assignments"][0].update(quote="motor burned"),
                       lambda x: x["reviews"][0]["assignments"].append(x["reviews"][0]["assignments"][0])):
            bad = copy.deepcopy(value); change(bad)
            with self.assertRaises(AgentError): prepare(bad, original)

    def test_audit_cannot_skip_unassigned_comments_or_return_truncated_success(self):
        value, original = fixture(); topics, rows = prepare(value, original)
        payload = {"reviews": [{"record_id": "r1", "supported": True, "feedback": ""}], "topic_issues": []}
        for reason in ("stop", "length"):
            with self.assertRaises(AgentError):
                parse_review(json.dumps({"choices": [{"finish_reason": reason, "message": {"content": json.dumps(payload)}}]}), rows, topics)

    def test_findings_bind_counts_to_the_same_topic_and_cannot_forge_counts(self):
        value, original = fixture(); topics, rows = prepare(value, original)
        result = statistics(topics, rows, DATASET_ID, "version", 4)
        source = {"source_hash": "hash", "source": {"source_type": "analysis", "content": result,
                  "provenance": {"operation": "rank_review_topics"}}}
        finding = {"finding_id": "F1", "section": "cons", "title": "清洁困难", "statement": "两条评论抱怨不易清洁。",
                   "frequency": {"source_id": "src_counts", "topic_id": "T2"},
                   "evidence": [{"source_id": "src_counts", "pointer": "/counts/1"}]}
        prepared, _ = evidence_packet([finding], {"src_counts": source})
        self.assertEqual(prepared[0]["frequency_count"], 2)
        self.assertIn("2条评论", documents(prepared)[1])
        finding["section"] = "pros"
        with self.assertRaises(AgentError): evidence_packet([finding], {"src_counts": source})
        finding["section"] = "cons"; source["source"]["provenance"] = {}
        with self.assertRaises(AgentError): evidence_packet([finding], {"src_counts": source})


class TopicLifecycleTests(HarnessFixture, unittest.TestCase):
    live_cap = ProductReportLifecycleTests.live_cap

    def configure(self, cap):
        value, original = fixture()
        raw = b"\n".join(json.dumps(row).encode() for row in original.values())
        manifest = {"dataset_id": DATASET_ID, "manifest_sha256": "sha256:" + "a" * 64,
                    "shards": [{"path": "a.jsonl", "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}]}
        self.research.data = SimpleNamespace(call=Mock(return_value=manifest), export=Mock(return_value=raw))
        self.manager.read_output = Mock(side_effect=lambda *_: json.dumps(value).encode())
        self.research._checkpoint(cap, exported_datasets=[DATASET_ID])
        calls = []
        def model(_cap, payload, _tools):
            packet = json.loads(payload["messages"][1]["content"]); calls.append(packet)
            verdict = {"topic_issues": [], "reviews": [{"record_id": row["record_id"], "supported": True, "feedback": ""} for row in packet["reviews"]]}
            return [json.dumps({"choices": [{"message": {"content": json.dumps(verdict)}}]}).encode()]
        self.research.broker.model_stream = Mock(side_effect=model)
        return value, manifest, calls

    def test_tool_persists_complete_originals_and_reuses_unchanged_review(self):
        _, cap = self.live_cap(); value, manifest, calls = self.configure(cap)
        args = {"dataset_id": DATASET_ID, "path": "/workspace/labels.json"}
        result = self.research.tool(cap, "rank_review_topics", args)
        self.assertEqual(result["status"], "ready")
        self.assertEqual([row["record_id"] for row in calls[0]["reviews"]], ["r1", "r2", "r3"])
        saved = self.state.source(self.user, self.sid, result["source_id"])["source"]["content"]
        self.assertEqual(saved["counts"][1]["count"], 2)
        self.research.tool(cap, "rank_review_topics", args)
        self.research.broker.model_stream.assert_called_once()
        # A changed original hash cannot be hidden behind a still valid file.
        manifest["shards"][0]["sha256"] = "sha256:bad"
        bad = self.research.tool(cap, "rank_review_topics", args)
        self.assertEqual(bad["error"]["code"], "dataset_changed")

    def test_failed_classification_cannot_become_an_accepted_source(self):
        _, cap = self.live_cap(); self.configure(cap)
        body = {"topic_issues": [], "reviews": [{"record_id": key, "supported": key != "r1", "feedback": "否定理解反了" if key == "r1" else ""} for key in ("r1", "r2", "r3")]}
        self.research.broker.model_stream = Mock(return_value=[json.dumps({"choices": [{"message": {"content": json.dumps(body)}}]}).encode()])
        result = self.research.tool(cap, "rank_review_topics", {"dataset_id": DATASET_ID, "path": "/workspace/labels.json"})
        self.assertEqual(result["status"], "needs_revision")
        self.assertNotIn("source_id", result)
        self.assertEqual(result["checked_reviews"], 2)

    def test_large_classification_continues_across_calls_without_rechecking(self):
        _, cap = self.live_cap(); value, manifest, calls = self.configure(cap)
        original = {}
        value["reviews"] = []
        for i in range(53):
            key = "review-" + str(i)
            original[key] = {"record_id": key, "review_text": "Quiet."}
            value["reviews"].append({"record_id": key, "assignments": [{"topic_id": "T1", "quote": "Quiet.", "context": ""}]})
        raw = b"\n".join(json.dumps(row).encode() for row in original.values())
        self.research.data.export.return_value = raw
        manifest["shards"][0]["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        args = {"dataset_id": DATASET_ID, "path": "/workspace/labels.json"}
        first = self.research.tool(cap, "rank_review_topics", args)
        self.assertEqual(first["status"], "reviewing")
        self.assertEqual(first["checked_reviews"], 50)
        self.assertNotIn("source_id", first)
        second = self.research.tool(cap, "rank_review_topics", args)
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["counts"][0]["count"], 53)
        self.assertEqual([len(packet["reviews"]) for packet in calls], [50, 3])

    def test_model_failure_preserves_draft_and_recovery_needs_no_previous_sandbox_file(self):
        run, cap = self.live_cap(); self.configure(cap)
        review = self.research.broker.model_stream.side_effect
        self.research.broker.model_stream = Mock(side_effect=AgentError("model_unavailable", "synthetic interruption"))
        result = self.research.tool(cap, "rank_review_topics", {"dataset_id": DATASET_ID, "path": "/workspace/labels.json"})
        self.assertEqual(result["error"]["code"], "model_unavailable")
        context = self.research._resume_context(self.user, run["run_id"])
        summary = context["saved_research"]["topic_classification"]
        self.assertEqual(summary["total_reviews"], 3)
        self.assertNotIn("Quiet", json.dumps(summary))
        self.assertNotIn("content", summary)
        self.manager.read_output = Mock(side_effect=AssertionError("old sandbox is gone"))
        self.research.broker.model_stream = Mock(side_effect=review)
        recovered = self.research.tool(cap, "rank_review_topics", {"dataset_id": DATASET_ID})
        self.assertEqual(recovered["status"], "ready")
        self.manager.read_output.assert_not_called()
