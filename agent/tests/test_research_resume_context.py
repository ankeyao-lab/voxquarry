"""Resume compact source indexes and published reports without new model calls."""
import copy
import json
import unittest
from unittest.mock import patch

from agent.contracts import AgentError
from agent.tests.test_agent_trace import actor
from agent.tests.test_harness_research import HarnessFixture, REVIEW_TEXT
from agent.tests import test_research_report_export as report_fixtures


class ResumeContextTests(HarnessFixture, unittest.TestCase):
    install_renderer = report_fixtures.ProductReportLifecycleTests.install_renderer
    live_cap = report_fixtures.ProductReportLifecycleTests.live_cap

    def test_completed_report_resume_keeps_result_but_never_forwards_job_credential(self):
        run = self.accept()
        outcome = {"job": {"job_id": "report-job", "status": "completed", "report_id": "rpt_result"},
                   "job_token": "private-report-job-credential", "ready": True}
        self.state.checkpoint(self.user, run["run_id"], {**run["checkpoint"],
            "collection_completed": True, "collection_outcomes": [outcome]}, status="running", attempt_id=run["attempt_id"])
        # Exercise the actual prompt boundary, including previously persisted
        # checkpoints written before credentials were stripped by the monitor.
        self.research.execute(self.user, run)
        prompt = self.manager.starts[0]["prompt"]
        self.assertNotIn(outcome["job_token"], prompt)
        self.assertNotIn("job_token", prompt)
        context = json.loads(prompt)["saved_research"]
        self.assertEqual(context["collection_outcomes"][0]["job"], outcome["job"])
        self.assertTrue(context["collection_completed"])

    def test_compact_index_keeps_every_source_and_full_original_remains_readable(self):
        run, cap = self.live_cap()
        ids = []
        for number in range(12):
            ids.append(self.research._source(cap, {"source_type":"review", "title":"使用条件",
                "report_id":"rpt_"+"a"*24, "dataset_id":"ds_"+"b"*64, "locator":"review-"+str(number),
                "content":{"review_text":REVIEW_TEXT,"rating":5,"original_fields":{"unique_condition":"dry grass"}},
                "provenance":{"report":{"description":"repeated metadata "*300},"snapshot":{"path":"private-storage"}}}))
        before = self.state.list_sources(self.user, run["run_id"])
        context = self.research._resume_context(self.user, run["run_id"])
        index = context["saved_sources"]
        self.assertEqual({row["source_id"] for row in index}, set(ids))
        self.assertLess(len(json.dumps(index)),len(json.dumps(before))/5)
        for row in index:
            original = next(item for item in before if item["source_id"]==row["source_id"])
            self.assertEqual(row["source_sha256"],original["source_hash"])
            self.assertEqual(row["locator"],original["source"]["locator"])
            self.assertEqual(row["report_id"],original["source"]["report_id"])
            self.assertEqual(row["dataset_id"],original["source"]["dataset_id"])
            self.assertNotIn("provenance", row)
            self.assertNotIn("content", row)
        restored = self.research.tool(cap,"read_saved_source",{"source_id":ids[0]})
        self.assertEqual(restored["content"],before[0]["source"]["content"])
        self.assertEqual(restored["provenance"],before[0]["source"]["provenance"])
        self.assertEqual(self.state.list_sources(self.user,run["run_id"]),before)
        with self.assertRaises(AgentError):
            self.research._resume_context(actor("another-user",roles=("admin",)),run["run_id"])

    def test_publication_checkpoint_survives_repeated_resume_without_republishing_or_auto_answer(self):
        self.install_renderer()
        run = self.accept()
        output = {}
        def publish(payload):
            output.update(self.research.tool(self.cap(payload),"publish_report",{
                "title":"资料简报","filename":"简报.MD","markdown":"暂无足够评论，不能形成优缺点判断。","formats":["md","pdf"]}))
        self.manager.on_start = publish
        self.manager.outcome = {"status":"failed","events":[],"error":{"code":"synthetic_failure"}}
        self.research.execute(self.user,run)
        original = self.state.get(self.user,run["run_id"])
        checkpoint = original["checkpoint"]["published_report"]
        self.assertEqual(checkpoint["status"],"files_available")
        self.assertEqual(checkpoint["attempt_id"],run["attempt_id"])
        self.assertEqual(checkpoint["files"],output["files"])
        md = next(file for file in output["files"] if file["media_type"]=="text/markdown")
        self.assertEqual({file["filename"] for file in checkpoint["files"]},{"简报.md","简报.pdf"})
        self.assertNotIn("answer_draft",original["checkpoint"])
        restored_publications = []
        def read_existing(payload):
            context = json.loads(payload["prompt"])
            self.assertEqual(context["published_report"]["files"],output["files"])
            self.assertNotIn("published_report",context["saved_research"])
            self.assertIn("read_saved_source",context["continuation_instruction"])
            self.assertIn("不代表",context["continuation_instruction"])
            restored = self.research.tool(self.cap(payload),"read_saved_source",{"source_id":md["source_id"]})
            self.assertIn("不能形成优缺点判断",restored["content"])
            restored_publications.append(restored)
        self.manager.on_start = read_existing
        for number in range(2):
            resumed = self.state.resume(self.user,run["run_id"],request_id="resume-"+str(number))["run"]
            self.assertNotEqual(resumed["attempt_id"],run["attempt_id"])
            self.research.execute(self.user,resumed)
            current = self.state.get(self.user,run["run_id"])
            self.assertEqual(current["status"],"blocked")
            self.assertEqual(current["checkpoint"]["published_report"],checkpoint)
            self.assertNotIn("answer_draft",current["checkpoint"])
        self.assertEqual(self.manager.tool.call_count,1)
        self.assertEqual(len(restored_publications),2)
        self.assertEqual(len(self.store.get_run(self.user,run["run_id"])["artifacts"]),2)
        other = actor("another-user",roles=("admin",))
        with self.assertRaises(AgentError):self.research._resume_context(other,run["run_id"])
        with self.assertRaises(AgentError):self.research.artifact(other,self.sid,md["artifact_id"])

    def test_partial_format_failure_never_records_report_completed_checkpoint(self):
        self.install_renderer()
        run, cap = self.live_cap()
        original = self.research.publish_artifact
        def fail_second(cap,args):
            if args["filename"].endswith(".pdf"):
                raise AgentError("artifact_temporarily_unavailable","暂时不能保存",retryable=True)
            return original(cap,args)
        with patch.object(self.research,"publish_artifact",side_effect=fail_second):
            result = self.research.tool(cap,"publish_report",{"title":"资料","filename":"简报","markdown":"暂缺正文。","formats":["md","pdf"]})
        self.assertIn("error",result)
        self.assertNotIn("published_report",self.state.get(self.user,run["run_id"])["checkpoint"])
        self.assertEqual(len(self.store.get_run(self.user,run["run_id"])["artifacts"]),1)

    def test_mismatched_saved_artifact_identity_is_not_a_resume_publication(self):
        self.install_renderer()
        run, cap = self.live_cap()
        self.research.tool(cap,"publish_report",{"title":"资料","filename":"简报","markdown":"暂缺正文。"})
        original = self.state.list_sources(self.user,run["run_id"])
        self.assertIn("published_report",self.research._resume_context(self.user,run["run_id"]))
        for field in ("source_type","sha256"):
            changed = copy.deepcopy(original)
            source = next(row["source"] for row in changed if row["source"].get("artifact_id"))
            if field == "source_type":
                source[field] = "review"
            else:
                source["provenance"][field] = "sha256:" + "0" * 64
            with self.subTest(field=field), patch.object(self.state,"list_sources",return_value=changed):
                context = self.research._resume_context(self.user,run["run_id"])
                self.assertNotIn("published_report",context)
                self.assertNotIn("continuation_instruction",context)

    def test_progress_exposes_cumulative_token_remaining_without_changing_limits(self):
        run, cap = self.live_cap()
        with self.research.broker._budget_connection() as db:
            db.execute("UPDATE research_budget SET total_tokens=199999999 WHERE run_id=?",(run["run_id"],))
        output = self.research.tool(cap,"list_datasets",{})
        self.assertEqual(output["research_progress"]["token_limit"],200000000)
        self.assertEqual(output["research_progress"]["remaining_tokens"],1)
        self.assertNotIn("instruction", output["research_progress"])
        with self.research.broker._budget_connection() as db:
            db.execute("UPDATE research_budget SET total_tokens=200000000 WHERE run_id=?",(run["run_id"],))
        with self.assertRaises(AgentError) as caught:
            self.research.tool(cap,"list_datasets",{})
        self.assertEqual(caught.exception.code,"research_budget_reached")
        self.assertEqual(self.research.broker.max_tokens,200000000)


if __name__ == "__main__":unittest.main()
