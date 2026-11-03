import json
import unittest
from unittest.mock import Mock
from jsonschema import Draft202012Validator

from agent.contracts import AgentError
from agent.harness_findings import documents, evidence_packet, parse_audit, validate_frequency_claim
from agent.harness_tools import sdk_research_tools
from agent.tests.test_harness_research import HarnessFixture
from agent.tests import test_research_report_export as report_tests


class FindingDeliveryTests(HarnessFixture, unittest.TestCase):
    install_renderer = report_tests.ProductReportLifecycleTests.install_renderer
    live_cap = report_tests.ProductReportLifecycleTests.live_cap

    def finding(self, cap, *, text="I have big hands and wear gloves. I accidentally activate the drive while turning."):
        sid = self.research._source(cap, {"source_type": "review", "title": "Controls", "content": {
            "review_id": "review-one", "review_text": text, "rating": 2, "rating_valid": True}})
        return {"finding_id": "F1", "section": "cons", "title": "转弯时误触自走按钮",
            "statement": "一位大手且戴手套的用户表示，转弯时会误触自走按钮。",
            "evidence": [{"source_id": sid, "quote": text}]}

    def audit_response(self, supported=True, feedback=""):
        body = {"items": [{"finding_id": "F1", "supported": supported, "feedback": feedback}]}
        self.research.broker.model_stream = Mock(return_value=[json.dumps({"choices": [{"message": {"content": json.dumps(body)}}]}).encode()])

    def test_free_markdown_report_preserves_user_selected_structure_without_review(self):
        self.install_renderer(); run, cap = self.live_cap()
        finding = self.finding(cap)
        sid = finding["evidence"][0]["source_id"]
        markdown = "## 转弯体验\n\n戴手套的大手用户可能误触。[" + sid + "]\n\n## 设计验证建议\n测试转弯操作。"
        self.audit_response()
        result = self.research.tool(cap, "submit_answer", {"answer": "建议验证戴手套转弯的操作体验。[" + sid + "]",
            "report": {"title": "转弯研究", "filename": "turning", "markdown": markdown, "formats": ["md"]}})
        self.assertTrue(result["accepted"], result)
        self.assertIn("## 设计验证建议", self.render_arguments["markdown"])
        self.assertNotIn("主要优点", self.render_arguments["markdown"])
        self.research.broker.model_stream.assert_not_called()
        self.assertIn("turning.md", self.state.get(self.user, run["run_id"])["checkpoint"]["answer_draft"]["answer"])

    def test_free_report_rejects_unknown_sources_before_rendering(self):
        self.install_renderer(); _, cap = self.live_cap()
        result = self.research.tool(cap, "submit_answer", {"answer": "研究完成", "report": {
            "title": "研究", "filename": "report", "markdown": "虚构依据[src_unknown]", "formats": ["md"]}})
        self.assertEqual(result["error"]["code"], "citation_unknown")
        self.manager.tool.assert_not_called()

    def test_sdk_report_can_reuse_answer_without_generating_a_second_body(self):
        self.install_renderer(); run, cap = self.live_cap()
        sid = self.finding(cap)["evidence"][0]["source_id"]
        args = {"answer": "## 操作体验\n\n转弯时可能误触。[" + sid + "]",
            "report": {"title": "操作体验", "filename": "controls", "formats": ["md"]}}
        schema = next(tool["parameters"] for tool in sdk_research_tools('wenquan-dsh/v1') if tool["name"] == "submit_answer")
        Draft202012Validator(schema).validate(args)
        result = self.research.tool(cap, "submit_answer", args)
        self.assertTrue(result["accepted"], result)
        self.assertIn("## 操作体验", self.render_arguments["markdown"])
        self.assertIn("转弯时可能误触。", self.render_arguments["markdown"])
        draft = self.state.get(self.user, run["run_id"])["checkpoint"]["answer_draft"]
        self.assertEqual(draft["citations"][0]["source_id"], sid)
        self.assertIn("controls.md", draft["answer"])
        self.research.tool(cap, "submit_answer", args)
        self.assertEqual(self.manager.tool.call_count, 1)

    def test_reused_report_body_still_requires_real_citations_before_rendering(self):
        self.install_renderer(); _, cap = self.live_cap()
        result = self.research.tool(cap, "submit_answer", {"answer": "虚构依据[src_unknown]",
            "report": {"title": "研究", "filename": "report"}})
        self.assertEqual(result["error"]["code"], "citation_unknown")
        self.manager.tool.assert_not_called()

    def test_followup_citations_can_reuse_same_session_but_not_other_session(self):
        _, cap = self.live_cap()
        finding = self.finding(cap); sid = finding["evidence"][0]["source_id"]
        self.state.cancel(self.user, cap.run_id)
        run, followup = self.live_cap(question="补充解释", request_id="followup")
        answer, citations = self.research.validate_answer(self.user, run["run_id"], "戴手套转弯。[" + sid + "]")
        self.assertEqual(citations[0]["source_id"], sid)
        self.state.cancel(self.user, followup.run_id)
        self.sid = self.store.create_session(self.user, "另一会话")["session_id"]
        other, _ = self.live_cap(question="另一问题", request_id="other")
        with self.assertRaises(AgentError) as error:
            self.research.validate_answer(self.user, other["run_id"], "引用[" + sid + "]")
        self.assertEqual(error.exception.code, "citation_unknown")

    def test_followup_report_cannot_drop_all_answer_citations_before_publication(self):
        self.install_renderer(); _, cap = self.live_cap()
        sid = self.finding(cap)["evidence"][0]["source_id"]
        self.state.cancel(self.user, cap.run_id)
        run, cap = self.live_cap(question="生成追问报告", request_id="report-followup")
        answer = "## 操作结论\n\n戴手套转弯时可能误触。[" + sid + "]\n\n## 下一步\n验证控制位置。"
        args = {"answer": answer, "report": {"title": "操作研究", "filename": "followup",
            "markdown": "## 操作结论\n\n以下内容将标注", "formats": ["md", "pdf"]}}
        rejected = self.research.tool(cap, "submit_answer", args)
        self.assertEqual(rejected["error"]["code"], "report_citations_missing")
        self.assertNotIn("answer_draft", self.state.get(self.user, run["run_id"])["checkpoint"])
        self.assertEqual(self.store.get_run(self.user, run["run_id"])["artifacts"], [])
        self.manager.tool.assert_not_called()
        del args["report"]["markdown"]
        repaired = self.research.tool(cap, "submit_answer", args)
        self.assertEqual(repaired["format_failures"], [])
        self.assertEqual({f["filename"] for f in repaired["files"]}, {"followup.md", "followup.pdf"})
        rendered = [call.args[2]["markdown"] for call in self.manager.tool.call_args_list]
        self.assertEqual(rendered[0], rendered[1])
        self.assertIn("## 下一步", rendered[0])
        self.assertIn("戴手套转弯时可能误触。[1]", rendered[0])

    def test_source_free_report_formats_share_validation_before_artifact_sources_exist(self):
        self.install_renderer(); run, cap = self.live_cap()
        result = self.research.tool(cap, "submit_answer", {"answer": "## 说明\n\n目前没有可用资料。",
            "report": {"title": "资料说明", "filename": "availability", "formats": ["md", "pdf"]}})
        self.assertEqual(result["format_failures"], [])
        self.assertEqual(len(result["files"]), 2)
        self.assertEqual(len(self.store.get_run(self.user, run["run_id"])["artifacts"]), 2)
        rendered = [call.args[2]["markdown"] for call in self.manager.tool.call_args_list]
        self.assertEqual(rendered[0], rendered[1])

    def test_oversized_references_export_complete_appendices_without_repair_loop(self):
        self.install_renderer(); run, cap = self.live_cap()
        sid = self.research._source(cap, {"source_type": "reference", "title": "合成大章节", "content": "原始材料" * 30000})
        result = self.research.tool(cap, "submit_answer", {"answer": "结论。[" + sid + "]",
            "report": {"title": "研究", "filename": "study", "formats": ["md", "pdf"]}})
        self.assertTrue(result['accepted'])
        self.assertEqual(result['format_failures'], [])
        self.assertIn('answer_draft', self.state.get(self.user, run['run_id'])['checkpoint'])
        self.assertEqual({f['filename'] for f in result['files']}, {'study.md', 'study.pdf', 'study-原文附录-1.md', 'study-原文附录-2.md'})
        parts = [call.args[2]['markdown'] for call in self.manager.tool.call_args_list if '原文附录' in call.args[2]['filename']]
        self.assertEqual(sum(part.count('原始材料') for part in parts), 30000)

    def test_full_original_reaches_independent_review_and_same_findings_make_chat_and_files(self):
        self.install_renderer(); run, cap = self.live_cap()
        finding = self.finding(cap)
        self.audit_response()
        checked = self.research.tool(cap, "review_findings", {"findings": [finding]})
        request = self.research.broker.model_stream.call_args.args[1]
        context = json.loads(request["messages"][1]["content"])
        self.assertEqual(next(iter(context["original_sources"].values()))["content"]["review_text"], finding["evidence"][0]["quote"])
        self.assertIn("大手", context["findings"][0]["statement"])
        args = {"finding_set_id": checked["finding_set_id"], "report": {"title": "控制体验", "filename": "控制体验", "formats": ["md", "pdf"]}}
        result = self.research.tool(cap, "submit_answer", args)
        self.assertTrue(result["accepted"])
        draft = self.state.get(self.user, run["run_id"])["checkpoint"]["answer_draft"]
        self.assertIn(finding["statement"], draft["answer"])
        self.assertIn(finding["statement"], self.render_arguments["markdown"])
        self.assertEqual(len(result["files"]), 2)
        self.research.tool(cap, "submit_answer", args)
        self.assertEqual(self.manager.tool.call_count, 2)  # One render per format, never replayed.
        self.assertEqual(self.research.broker.model_stream.call_count, 1)  # No model after publication.

    def test_semantic_revision_cannot_be_submitted_as_a_passed_finding(self):
        run, cap = self.live_cap(); finding = self.finding(cap)
        finding["statement"] = "小手用户转弯容易误触。"
        self.audit_response(False, "原文是大手且戴手套，不能写成小手用户。")
        result = self.research.tool(cap, "review_findings", {"findings": [finding]})
        self.assertIsNone(result["finding_set_id"])
        self.assertIn("大手", result["needs_revision"][0]["feedback"])
        denied = self.research.tool(cap, "submit_answer", {"finding_set_id": "findings_fake"})
        self.assertEqual(denied["error"]["code"], "finding_set_invalid")
        self.assertNotIn("answer_draft", self.state.get(self.user, run["run_id"])["checkpoint"])

    def test_invented_quote_rejected_before_model_call(self):
        _, cap = self.live_cap(); finding = self.finding(cap)
        finding["evidence"][0]["quote"] = "Invented quote"
        self.audit_response()
        result = self.research.tool(cap, "review_findings", {"findings": [finding]})
        self.assertEqual(result["error"]["code"], "finding_quote_invalid")
        self.research.broker.model_stream.assert_not_called()

    def test_final_report_cannot_substitute_unreviewed_prose(self):
        run, cap = self.live_cap(); finding = self.finding(cap); self.audit_response()
        checked = self.research.tool(cap, "review_findings", {"findings": [finding]})
        result = self.research.tool(cap, "submit_answer", {"finding_set_id": checked["finding_set_id"], "answer": "小手容易误触"})
        self.assertEqual(result["error"]["code"], "finding_answer_conflict")
        self.assertNotIn("answer_draft", self.state.get(self.user, run["run_id"])["checkpoint"])

    def test_pdf_failure_preserves_checked_answer_and_md(self):
        self.install_renderer(); run, cap = self.live_cap(); finding = self.finding(cap); self.audit_response()
        checked = self.research.tool(cap, "review_findings", {"findings": [finding]})
        render = self.manager.tool.side_effect
        def fail_pdf(attempt, name, args):
            if args["formats"] == ["pdf"]:
                raise AgentError("report_render_failed", "synthetic PDF failure")
            return render(attempt, name, args)
        self.manager.tool.side_effect = fail_pdf
        result = self.research.tool(cap, "submit_answer", {"finding_set_id": checked["finding_set_id"],
            "report": {"title": "简报", "filename": "简报", "formats": ["pdf", "md"]}})
        self.assertTrue(result["accepted"])
        self.assertEqual(result["format_failures"], [{"format": "pdf", "code": "report_render_failed"}])
        self.assertEqual([f["filename"] for f in result["files"]], ["简报.md"])
        self.assertIn("PDF 文件暂未生成", self.state.get(self.user, run["run_id"])["checkpoint"]["answer_draft"]["answer"])

    def test_review_result_cache_avoids_rechecking_identical_sources(self):
        _, cap = self.live_cap(); finding = self.finding(cap); self.audit_response()
        one = self.research.tool(cap, "review_findings", {"findings": [finding]})
        two = self.research.tool(cap, "review_findings", {"findings": [finding]})
        self.assertEqual(one["finding_set_id"], two["finding_set_id"])
        self.research.broker.model_stream.assert_called_once()

    def test_repairing_one_finding_preserves_previously_passed_findings(self):
        _, cap = self.live_cap(); first = self.finding(cap); self.audit_response()
        self.research.tool(cap, "review_findings", {"findings": [first]})
        second = {**first, "finding_id": "F2", "title": "另一条已核对发现"}
        self.research.broker.model_stream.return_value = [json.dumps({"choices": [{"message": {"content": json.dumps({"items": [
            {"finding_id": "F2", "supported": True, "feedback": ""}]})}}]}).encode()]
        reviewed = self.research.tool(cap, "review_findings", {"findings": [second]})
        self.assertEqual(reviewed["accepted_finding_ids"], ["F1", "F2"])

    def test_full_resubmission_reviews_only_changed_findings(self):
        _, cap = self.live_cap(); first = self.finding(cap); self.audit_response()
        self.research.tool(cap, "review_findings", {"findings": [first]})
        second = {**first, "finding_id": "F2", "title": "另一发现"}
        self.research.broker.model_stream.return_value = [json.dumps({"choices": [{"message": {"content": json.dumps({"items": [
            {"finding_id": "F2", "supported": True, "feedback": ""}]})}}]}).encode()]
        result = self.research.tool(cap, "review_findings", {"findings": [first, second]})
        payload = json.loads(self.research.broker.model_stream.call_args.args[1]["messages"][1]["content"])
        self.assertEqual([item["finding_id"] for item in payload["findings"]], ["F2"])
        self.assertEqual(result["accepted_finding_ids"], ["F1", "F2"])

    def test_source_from_other_run_is_not_eligible_for_findings(self):
        _, cap = self.live_cap(); finding = self.finding(cap)
        self.sid = self.store.create_session(self.user, "另一会话")["session_id"]
        _, other = self.live_cap(question="另一个问题", request_id="other")
        self.audit_response()
        result = self.research.tool(other, "review_findings", {"findings": [finding]})
        self.assertEqual(result["error"]["code"], "citation_unknown")
        self.research.broker.model_stream.assert_not_called()


class FindingDocumentTests(unittest.TestCase):
    def test_more_selected_examples_do_not_raise_a_topic_in_population_ranking(self):
        source = {"source_hash": "hash", "source": {"source_type": "review", "content": {"review_id": "one", "review_text": "Quiet and useful."}}}
        findings = [{"finding_id": "F1", "section": "pros", "title": "安静", "statement": "一条评价认为安静。", "evidence": [
            {"source_id": "src_a", "quote": "Quiet"}, {"source_id": "src_b", "quote": "useful"}]}]
        prepared, _ = evidence_packet(findings, {"src_a": source, "src_b": source})
        self.assertEqual(prepared[0]["supporting_review_count"], 1)
        second = {**prepared[0], "finding_id": "F2", "title": "其他优点", "supporting_review_count": 2}
        answer, report = documents([prepared[0], second], ["F1"])
        self.assertLess(report.index("安静"), report.index("其他优点"))
        self.assertIn("尚未形成全量", report)
        self.assertNotIn("其他优点", answer)

    def test_audit_requires_each_item_and_boolean_result(self):
        for value in ({"items": []}, {"items": [{"finding_id": "F1", "supported": "true", "feedback": ""}]}):
            raw = json.dumps({"choices": [{"message": {"content": json.dumps(value)}}]})
            with self.assertRaises(AgentError):
                parse_audit(raw, [{"finding_id": "F1"}])


class FrequencyClaimTests(unittest.TestCase):
    def test_unreviewed_analysis_cannot_bypass_frequency_through_title_or_statement(self):
        for section in ['pros', 'cons']:
            for title, statement in [('风量大：96条评论', '送风足'), ('风量大', '96 条评论明确称赞风量'), ('风量大', '96 reviews praise airflow')]:
                with self.assertRaises(AgentError) as error:
                    validate_frequency_claim({'finding_id': 'F1', 'section': section, 'title': title, 'statement': statement})
                self.assertEqual(error.exception.code, 'finding_frequency_required')
        validate_frequency_claim({'finding_id': 'F1', 'section': 'pros', 'title': '风量大', 'statement': '有用户用它为门口送风。'})
        validate_frequency_claim({'finding_id': 'F1', 'section': 'pros', 'title': '节能', 'statement': '低速功耗下降20%。'})

    def test_manual_counts_must_match_verified_frequency(self):
        finding = {'finding_id': 'F1', 'section': 'pros', 'title': '风量大', 'statement': '在428条评论中，96条评论认可风量。', 'frequency': {'source_id': 'src_1', 'topic_id': 'T1'}, 'frequency_count': 96, 'frequency_denominator': 428}
        validate_frequency_claim(finding)
        with self.assertRaises(AgentError) as error:
            validate_frequency_claim({**finding, 'statement': '120条评论认可风量。'})
        self.assertEqual(error.exception.code, 'finding_frequency_mismatch')
