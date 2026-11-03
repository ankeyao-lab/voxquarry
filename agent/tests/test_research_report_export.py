"""Report export and URL-to-download lifecycle; no real provider or collection."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.contracts import AgentError
from agent.research_report_export import portable_report
from agent.tests.test_agent_trace import actor
from agent.tests.test_harness_research import DATASET_ID, REVIEW_TEXT, HarnessFixture, SyntheticData


class PortableReportTests(unittest.TestCase):
    def test_analysis_reference_contains_dataset_and_actual_statistics(self):
        source = {"source_type": "analysis", "title": "单产品网评分析", "dataset_id": "ds_example",
                  "content": {"schema_version": "wenquan-dataset-analysis/v1", "manifest_sha256": "sha256:version",
                              "sample": {"review_entities": 7, "written_entities": 5, "rating_n": 7},
                              "aspect_experience": [{"label": "噪音", "review_entities": 4, "explicit_positive_entities": 1, "explicit_negative_entities": 3}]}}
        result = portable_report("简报", "有噪音反馈。[1]", [{"source_id": "src_stats", "label": "1"}], {"src_stats": {"source": source}})
        self.assertIn("ds\\_example", result)
        self.assertIn("评论总数：7", result)
        self.assertIn("有正文评论：5", result)
        self.assertIn("| 噪音 | 4 | 1 | 3 |", result)
        self.assertIn("sha256:version", result)

    def test_report_has_one_document_title_even_when_model_title_differs(self):
        for body in ("# 模型写的另一版本标题\n\n## 优点\n\n正文", "模型自带标题\n===\n\n## 优点\n正文"):
            result = portable_report("正式标题", body, [], {})
            self.assertEqual(result.count("# 正式标题"), 1)
            self.assertNotIn("模型", result)
            self.assertIn("## 优点", result)
            self.assertIn("正文", result)

    def test_review_original_is_literal_and_citation_number_matches(self):
        text = "Dry grass only.\n```\n<img src='https://example.com/image'>\nWet grass was not tested."
        sources = {"src_known": {"source": {"source_type": "review", "title": "Original [title]",
            "content": {"rating": 4, "review_text": text, "product_url": "https://example.com/mower"}}}}
        result = portable_report("割草机研究", "仅验证了干草。[1]", [{"source_id": "src_known", "label": "1"}], sources)
        self.assertIn("仅验证了干草。[1]", result)
        self.assertIn("### 来源 1", result)
        self.assertIn("原始评分：4 星", result)
        self.assertIn("````text\n" + text + "\n````", result)
        self.assertNotIn("src_known", result)
        self.assertIn("<https://example.com/mower>", result)

    def test_unavailable_private_link_is_not_made_into_portable_public_link(self):
        source = {"src_a": {"source": {"source_type": "report", "title": "已有报告", "url": "http://127.0.0.1/private"}}}
        result = portable_report("研究", "参考报告。[1]", [{"source_id": "src_a", "label": "1"}], source)
        self.assertIn("已有报告", result)
        self.assertNotIn("127.0.0.1", result)

    def test_shared_reference_export_keeps_exact_chapter_and_original_identity(self):
        text = "工具正文 118,353 条。\n```\n<script>这是资料原文</script>\n信息不足保留未知。"
        source = {"src_a": {"source": {"source_type": "reference", "title": "客群一览", "content": text,
            "locator": {"section_id": "personas", "original_filename": "EGO-Persona-研究报告.html"},
            "provenance": {"sha256": "a" * 64}}}}
        result = portable_report("概览", "依据报告。[1]", [{"source_id": "src_a", "label": "1"}], source)
        self.assertIn("原件：EGO-Persona-研究报告\\.html", result)
        self.assertIn("章节：personas", result)
        self.assertIn("原件 SHA-256：" + "a" * 64, result)
        self.assertIn("````text\n" + text + "\n````", result)
        self.assertNotIn("src_a", result)

    def test_excessive_originals_are_rejected_without_truncation(self):
        source = {"src_a": {"source": {"source_type": "review", "content": {"review_text": "文" * 100000}}}}
        with self.assertRaises(AgentError) as caught:
            portable_report("研究", "发现。[1]", [{"source_id": "src_a", "label": "1"}], source)
        self.assertEqual(caught.exception.code, "report_export_too_large")


class ProductReportLifecycleTests(HarnessFixture, unittest.TestCase):
    def test_unresolved_citation_placeholder_cannot_be_published(self):
        run, cap = self.live_cap()
        known = self.research._source(cap, {"source_type": "review", "content": {"review_text": "Light in dry snow."}})
        for placeholder in ("src_?", "src_待补", "src_"):
            output = self.research.tool(cap, "publish_report", {"title": "简报", "filename": "简报",
                "markdown": "已有原文。[" + known + "] 另一结论。[" + placeholder + "]"})
            self.assertEqual(output["error"]["code"], "citation_unknown")
            self.assertIn(placeholder, output["error"]["message"])
        self.assertEqual(self.store.get_run(self.user, run["run_id"])["artifacts"], [])

    def test_unknown_citation_feedback_locates_errors_without_accepting_or_publishing(self):
        run, cap = self.live_cap()
        known = self.research._source(cap, {"source_type": "review", "title": "原文", "content": {"review_text": "Dry grass only."}})
        unknown = ["src_missing_" + str(i) for i in range(12)]
        text = "有支持的内容。[" + known + "] " + " ".join("[" + sid + "]" for sid in unknown)
        output = self.research.tool(cap, "publish_report", {"title": "简报", "filename": "简报", "markdown": text})
        self.assertEqual(output["error"]["code"], "citation_unknown")
        message = output["error"]["message"]
        self.assertIn("12个", message)
        self.assertIn("src_missing_0", message)
        self.assertIn("src_missing_7", message)
        self.assertNotIn("src_missing_8", message)
        self.assertNotIn(known, message)
        self.assertEqual(self.store.get_run(self.user, run["run_id"])["artifacts"], [])

    def test_large_originals_split_and_two_formats_reuse_complete_appendices(self):
        self.install_renderer()
        run, cap = self.live_cap()
        original = '长引用原文' * 26000
        sid = self.research._source(cap, {'source_type': 'reference', 'title': '大来源', 'content': original,
            'provenance': {'sha256': 'original-version'}})
        result = self.research.tool(cap, 'submit_answer', {'answer': '已整理资料。[' + sid + ']',
            'report': {'title': '报告', 'filename': '报告', 'formats': ['md', 'pdf']}})
        self.assertTrue(result['accepted'])
        checkpoint = self.state.get(self.user, run['run_id'])['checkpoint']
        files = checkpoint['report_delivery']['files']
        self.assertEqual(len(files), len({f['filename'] for f in files}))
        appendices = [f for f in files if '原文附录' in f['filename']]
        self.assertEqual(len(appendices), 2)
        contents = [self.research.artifact(self.user, self.sid, f['artifact_id'])[1].read_text() for f in appendices]
        self.assertEqual(sum(text.count('长引用原文') for text in contents), 26000)
        self.assertTrue(all(len(text) <= 100000 and 'SHA-256' in text for text in contents))
        main = next(f for f in files if f['filename'] == '报告.md')
        main_text = self.research.artifact(self.user, self.sid, main['artifact_id'])[1].read_text()
        self.assertTrue(all(f['download_url'] in main_text for f in appendices))
        self.assertEqual(checkpoint['report_delivery']['failures'], [])

    def install_renderer(self):
        self.rendered = {}
        def render(attempt, name, args):
            self.assertEqual(name, "render_report")
            self.render_arguments = args
            files = []
            for form in args["formats"]:
                filename = args["filename"] + "." + form
                path = "/workspace/" + filename
                self.rendered[path] = args["markdown"].encode() if form == "md" else b"%PDF-synthetic-transport-only"
                files.append({"path": path, "filename": filename})
            return {"files": files}
        self.manager.tool = Mock(side_effect=render)
        self.manager.read_output = Mock(side_effect=lambda attempt, path: self.rendered[path])

    def live_cap(self, **request):
        run = self.accept(**request)
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question=run["request"]["question"])
        return run, self.research.broker.issue(self.user, run, trace["trace_id"])[1]

    def test_report_generates_owned_md_pdf_with_exact_original_and_no_fabricated_citation(self):
        self.install_renderer()
        run, cap = self.live_cap()
        review = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID})["items"][0]
        output = self.research.tool(cap, "publish_report", {"title": "单产品研究简报", "filename": "简报",
            "markdown": "| 条件 | 结果 |\n| --- | --- |\n| 小草坪、中档 | 25分钟。[" + review["source_id"] + "] |",
            "formats": ["md", "pdf"]})
        self.assertEqual(output["status"], "available")
        self.assertEqual({f["filename"] for f in output["files"]}, {"简报.md", "简报.pdf"})
        self.assertIn(REVIEW_TEXT, self.render_arguments["markdown"])
        self.assertIn("25分钟。[1]", self.render_arguments["markdown"])
        self.assertNotIn(review["source_id"], self.render_arguments["markdown"])
        for file in output["files"]:
            artifact, path = self.research.artifact(self.user, self.sid, file["artifact_id"])
            self.assertEqual(artifact["run_id"], run["run_id"])
            self.assertTrue(path.is_file())
            with self.assertRaises(AgentError):
                self.research.artifact(actor("other-user", roles=("admin",)), self.sid, file["artifact_id"])
        unknown = self.research.tool(cap, "publish_report", {"title": "错误报告", "filename": "错误", "markdown": "虚构。[src_unknown]"})
        self.assertEqual(unknown["error"]["code"], "citation_unknown")
        self.assertEqual(self.manager.tool.call_count, 1)

    def test_default_md_and_empty_format_or_unfinished_render_never_publish_false_files(self):
        self.install_renderer()
        run, cap = self.live_cap()
        args = {"title": "资料情况", "filename": "资料", "markdown": "尚没有评论正文。"}
        output = self.research.tool(cap, "publish_report", args)
        self.assertEqual([f["filename"] for f in output["files"]], ["资料.md"])
        empty = self.research.tool(cap, "publish_report", {**args, "formats": []})
        self.assertEqual(empty["error"]["code"], "report_format_invalid")
        self.manager.tool.side_effect = None
        self.manager.tool.return_value = {"files": []}
        failed = self.research.tool(cap, "publish_report", {**args, "markdown": "旧文件仍在。[" + output["files"][0]["source_id"] + "]", "formats": ["pdf"]})
        self.assertEqual(failed["error"]["code"], "report_render_failed")
        self.assertEqual(len(self.store.get_run(self.user, run["run_id"])["artifacts"]), 1)

    def test_followup_report_reuses_same_session_originals_without_rereading_dataset(self):
        self.install_renderer()
        first, first_cap = self.live_cap()
        review = self.research.tool(first_cap, "query_dataset", {"dataset_id": DATASET_ID})["items"][0]
        self.research.cancel(self.user, self.sid, first["run_id"])
        _, cap = self.live_cap(request_id="report-followup")
        output = self.research.tool(cap, "publish_report", {"title": "修订报告", "filename": "修订报告",
            "markdown": "仅修订措辞，保留原文。[" + review["source_id"] + "]", "formats": ["md", "pdf"]})
        self.assertEqual(output["status"], "available")
        self.assertIn(REVIEW_TEXT, self.render_arguments["markdown"])
        self.assertEqual(sum(op == "query" for _, op, _ in self.data.calls), 1)
        self.research.cancel(self.user, self.sid, cap.run_id)
        self.sid = self.store.create_session(self.user, "另一个会话")["session_id"]
        _, other_cap = self.live_cap(request_id="other-report")
        denied = self.research.tool(other_cap, "publish_report", {"title": "错误", "filename": "错误",
            "markdown": "不能跨会话引用。[" + review["source_id"] + "]"})
        self.assertEqual(denied["error"]["code"], "citation_unknown")
        self.assertEqual(self.manager.tool.call_count, 1)

    def test_display_filename_extensions_are_not_duplicated(self):
        self.install_renderer()
        _, cap = self.live_cap()
        with patch("agent.harness_research.mimetypes.guess_type", return_value=(None, None)):
            output = self.research.tool(cap, "publish_report", {"title": "单产品研究简报", "filename": "简报.MD.pdf",
                "markdown": "尚没有评论正文。", "formats": ["md", "pdf"]})
        self.assertEqual({file["filename"] for file in output["files"]}, {"简报.md", "简报.pdf"})
        markdown = next(file for file in output["files"] if file["filename"].endswith(".md"))
        self.assertEqual(markdown["media_type"], "text/markdown")
        saved = self.research.tool(cap, "read_saved_source", {"source_id": markdown["source_id"]})
        self.assertIn("尚没有评论正文。", saved["content"])
        invalid = self.research.tool(cap, "publish_report", {"title": "研究简报", "filename": ".md",
            "markdown": "尚没有评论正文。"})
        self.assertEqual(invalid["error"]["code"], "artifact_name_invalid")
        self.assertEqual(self.manager.tool.call_count, 1)

    def test_restored_analysis_keeps_registered_field_definitions_without_changing_source(self):
        _, cap = self.live_cap()
        content = {"schema_version": "wenquan-dataset-analysis/v1", "version": "wenquan-dataset-analysis/1.0.0",
            "aspect_experience": [{"aspect_id": "runtime", "review_entities": 10, "explicit_positive_entities": 2,
                "explicit_negative_entities": 1, "positive_rating_entities": 8, "negative_rating_entities": 0}]}
        source_id = self.research._source(cap, {"source_type": "analysis", "title": "规则分析", "content": content})
        before = self.state.source(self.user, self.sid, source_id)
        restored = self.research.tool(cap, "read_saved_source", {"source_id": source_id})
        self.assertIn("report_field_guide", restored)
        self.assertEqual(self.state.source(self.user, self.sid, source_id), before)
        content["version"] = "wenquan-dataset-analysis/2.0.0"
        future = self.research._source(cap, {"source_type": "analysis", "title": "新版本", "content": content})
        self.assertNotIn("report_field_guide", self.research.tool(cap, "read_saved_source", {"source_id": future}))

    def test_link_wait_resume_then_small_sample_analysis_and_report_in_same_conversation(self):
        self.install_renderer()
        url = "https://www.lowes.com/pd/synthetic/12345"
        run = self.accept(question="采集这个产品的网评并给一份可下载报告：" + url, web_enabled=False)
        self.assertEqual(run["request"]["product_urls"], [url])
        def collect(payload):
            cap = self.cap(payload)
            operation = self.research.tool(cap, "collect_product", {"product_url": url, "channel": "lowes"})
            self.research.tool(cap, "wait_for_collection", {"operation_ids": [operation["operation_id"]], "research_note": "采集完成后生成单产品报告。"})
        self.manager.on_start = collect
        self.research.execute(self.user, run)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "waiting_collection")
        self.assertIn(run["attempt_id"], self.manager.stops)
        self.collection.ready = True
        with patch.object(self.research, "_stopped", SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
            self.research._monitor_waiting()
        original_call = self.data.call
        def data_call(user, operation, args):
            if operation == "definition":
                return {"schema_version": "wenquan-research-definition/v1", "definition_id": "rdef_" + "c" * 64,
                    "category_id": "walk_behind_mower", "chapters": [{"id": "pros_cons", "title": "优缺点"}]}
            if operation == "analyze":
                self.assertEqual(args["dataset_id"], DATASET_ID)
                return {"schema_version": "wenquan-dataset-analysis/v1", "status": "ready", "dataset_id": DATASET_ID,
                    "sample": {"written_entities": 1}, "readiness": {"formal_sample_threshold_met": False},
                    "aspect_experience": [{"aspect_id": "runtime", "review_entities": 1, "explicit_positive_entities": 1}]}
            return original_call(user, operation, args)
        self.data.call = Mock(side_effect=data_call)
        def analyze(payload):
            cap = self.cap(payload)
            # The archived v1 product-analysis flow still accepts a single
            # frozen dataset; v2 package/native-method behavior has its own tests.
            cap.runtime_contract = 'wenquan-dsh/v1'
            dataset = self.research.tool(cap, "open_dataset", {"scope": {"job_ids": ["sample-job"]}})
            definition = self.research.tool(cap, "get_product_review_definition", {"category_id": "walk_behind_mower"})
            outcome = self.research.tool(cap, "analyze_product_reviews", {"dataset_id": dataset["dataset_id"], "definition_id": definition["definition_id"]})
            self.assertEqual(outcome["sample"]["written_entities"], 1)
            self.assertFalse(outcome["readiness"]["formal_sample_threshold_met"])
            review = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID})["items"][0]
            text = "仅有一条评论，适合形成研究简报。用户以中档刀速割小草坪，耗时25分钟。[" + review["source_id"] + "]"
            report = self.research.tool(cap, "publish_report", {"title": "单产品研究简报", "filename": "单产品简报", "markdown": text})
            self.assertEqual(report["status"], "available")
            self.research.tool(cap, "submit_answer", {"answer": text})
        self.manager.on_start = analyze
        function, args, kwargs = self.pool.calls.pop()
        function(*args, **kwargs)
        final = self.state.get(self.user, run["run_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["request"]["question"], run["request"]["question"])
        self.assertEqual(len(self.store.get_run(self.user, run["run_id"])["artifacts"]), 1)
        self.assertIsNone(self.service.executor)  # No shared formal report was created.


if __name__ == "__main__":
    unittest.main()
