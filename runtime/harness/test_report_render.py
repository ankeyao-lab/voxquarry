"""Synthetic-only renderer checks; no business materials or live services."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


renderer = load("test_wenquan_renderer", "report_render.py")
worker = load("test_wenquan_render_worker", "analysis_worker.py")
HAS_MARKDOWN = importlib.util.find_spec("markdown_it") is not None
HAS_PDF = all(importlib.util.find_spec(name) is not None for name in ("reportlab", "pypdf"))
TEST_FONT = next((path for path in (os.environ.get("WENQUAN_TEST_REPORT_FONT", ""), renderer.FONT_PATH,
                                  "/System/Library/Fonts/Supplemental/Arial Unicode.ttf") if path and Path(path).is_file()), None)


class RenderValidationTests(unittest.TestCase):
    def arguments(self, **updates):
        return {"markdown": "# 合成测试\n\n只验证格式。", "title": "合成测试", "filename": "brief", "formats": ["md"], **updates}

    def test_rejects_invalid_paths_bounds_and_unknown_fields(self):
        for changes in ({"filename": "../outside"}, {"filename": "/tmp/out"}, {"filename": "a\\b"},
                        {"filename": ".hidden"}, {"filename": "brief\n"}, {"filename": ""},
                        {"markdown": "a" * 100001}, {"markdown": ""}, {"markdown": "\ud800"},
                        {"title": "a" * 201}, {"title": "title\n"}, {"formats": []},
                        {"formats": ["pdf", "pdf"]}, {"formats": ["html"]}, {"formats": "pdf"},
                        {"font_path": "/secret"}, {"url": "https://example.com"}):
            with self.subTest(changes=changes.keys()), self.assertRaises(renderer.RenderError):
                renderer.validate(self.arguments(**changes))

    def test_alarm_is_restored_even_after_render_error(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(renderer.RenderError):
            with renderer.bounded_render():
                signal.raise_signal(signal.SIGALRM)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_render_output_buffer_has_a_hard_limit(self):
        with patch.object(renderer, "MAX_FILE_BYTES", 3):
            output = renderer.LimitedBytesIO()
            output.write(b"123")
            with self.assertRaises(renderer.RenderError):
                output.write(b"4")


@unittest.skipUnless(HAS_MARKDOWN, "analysis-image Markdown dependency")
class MarkdownRenderTests(unittest.TestCase):
    arguments = RenderValidationTests.arguments
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "workspace"; self.work.mkdir()
        self.work_patch = patch.object(worker, "WORK_ROOT", str(self.work)); self.work_patch.start()

    def tearDown(self):
        self.work_patch.stop(); self.temp.cleanup()

    def render(self, **updates):
        return renderer.render_report(self.arguments(**updates), open_output=worker.secure_file, work_root=str(self.work),
                                      font_path=TEST_FONT or "/font-unavailable")

    def test_headings_lists_table_and_source_address_survive_without_active_resources(self):
        markdown = '''# 合成测试

## 场景

- **转弯**：控制较难。
  - 内层评论条件。

1. 先读取来源。
2. 保留不确定性。

| 项目 | 原文支持 |
| --- | --- |
| 续航 | 仅这一条评论 |

[公开来源](https://example.com/product)

<script>secret-script-marker</script>

![image-marker](https://example.com/tracker.png)

正常文字 <img src="file:///secret"> 保留。
'''
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            result = self.render(markdown=markdown)
        output = Path(result["files"][0]["path"]).read_text()
        self.assertIn("## 场景", output); self.assertIn("**转弯**", output)
        self.assertIn("    - 内层评论条件", output); self.assertIn("1. 先读取来源", output)
        self.assertIn("| 续航 |", output); self.assertIn("https://example", output)
        self.assertNotIn("secret-script-marker", output); self.assertNotIn("tracker", output)
        self.assertNotIn("file:///secret", output); self.assertIn("正常文字", output)

    def test_worker_dispatch_uses_secure_output_and_returns_file_metadata(self):
        result = worker.dispatch("render_report", self.arguments())
        item = result["files"][0]
        data = Path(item["path"]).read_bytes()
        self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(item["bytes"], len(data)); self.assertEqual(item["media_type"], "text/markdown")
        self.assertEqual(Path(item["path"]).stat().st_mode & 0o777, 0o600)

    def test_native_cli_keeps_markdown_when_optional_pdf_fails(self):
        source = self.work / 'draft.md'
        source.write_text('# 自主研究\n\n合成结论与方法。')
        real_render = renderer.render_report
        def render(arguments, **kwargs):
            if arguments['formats'] == ['pdf']:
                raise renderer.RenderError('report_font_unavailable')
            return real_render(arguments, **kwargs)
        output = io.StringIO()
        with patch.dict(sys.modules, {'analysis_worker': worker}), \
             patch.object(sys, 'argv', ['report_render.py', '--input', str(source), '--title', '自主研究',
                                       '--filename', 'published', '--formats', 'pdf', 'md']), \
             patch.object(renderer, 'render_report', side_effect=render), patch('sys.stdout', output):
            status = renderer.main()
        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(result['failures'], [{'format': 'pdf', 'code': 'report_font_unavailable'}])
        self.assertEqual(len(result['files']), 1)
        self.assertIn('合成结论与方法', Path(result['files'][0]['path']).read_text())

    def test_symlink_never_overwrites_outside_file(self):
        secret = self.root / "secret"; secret.write_text("untouched")
        (self.work / "escape.md").symlink_to(secret)
        with self.assertRaises(worker.WorkerError):
            self.render(filename="escape")
        self.assertEqual(secret.read_text(), "untouched")

    def test_active_only_input_is_not_a_successful_empty_report(self):
        with self.assertRaises(renderer.RenderError) as raised:
            self.render(markdown='<iframe src="https://example.com"></iframe>\n\n![a](https://example.com/a.png)')
        self.assertEqual(raised.exception.code, "report_render_empty")
        self.assertEqual(list(self.work.iterdir()), [])

    def test_literal_angle_brackets_stay_text_when_markdown_reopens(self):
        from markdown_it import MarkdownIt
        result = self.render(markdown="文字 ` <img src=x> ` 和 &lt;script&gt;。")
        safe = Path(result["files"][0]["path"]).read_text()
        rendered = MarkdownIt("commonmark", {"html": True}).render(safe)
        self.assertNotIn("<img", rendered); self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;img", rendered)

    def test_variable_fence_keeps_original_html_and_backticks_as_literal_text(self):
        from markdown_it import MarkdownIt
        original = '原文包含 ```text 和 <img src="https://example.com/private">\n完整结尾条件。\n'
        result = self.render(markdown="# 原文\n\n`````text\n" + original + "`````\n")
        saved = Path(result["files"][0]["path"]).read_text()
        fences = [token for token in MarkdownIt("commonmark").parse(saved) if token.type == "fence"]
        self.assertEqual(len(fences), 1); self.assertEqual(fences[0].content, original)


@unittest.skipUnless(HAS_MARKDOWN and HAS_PDF and TEST_FONT, "analysis-image PDF dependencies and embedded CJK font")
class PdfRenderTests(unittest.TestCase):
    arguments = RenderValidationTests.arguments
    setUp = MarkdownRenderTests.setUp
    tearDown = MarkdownRenderTests.tearDown
    render = MarkdownRenderTests.render
    def test_mathematical_minus_survives_pdf_and_markdown(self):
        from markdown_it import MarkdownIt
        from pypdf import PdfReader
        markdown = "# 差值 −0.042\n\n正文 −1.25，正值 +1.25。\n\n"
        markdown += "| 差值 |\n| --- |\n| −2.5 |\n\n```text\n−3.75\n```\n"
        result = self.render(markdown=markdown, title="差值 −0.042", formats=["md", "pdf"])
        text = "".join(page.extract_text() for page in PdfReader(result["files"][1]["path"]).pages)
        saved = MarkdownIt().render(Path(result["files"][0]["path"]).read_text())
        for number in ("−0.042", "−1.25", "+1.25", "−2.5", "−3.75"):
            self.assertIn(number, text)
            self.assertIn(number, saved)
        self.assertNotIn("\x00", text)

    def test_chinese_paginated_table_is_embedded_deterministic_and_complete(self):
        from pypdf import PdfReader
        markdown = "# 合成测试\n\n## 条件与发现\n\n" + "| 评论 | 场景 | 发现 |\n| --- | --- | --- |\n"
        markdown += "\n".join(f"| 第{i}条 | 坡地转弯 | 仅保留此评论的具体条件，不能推广到全部产品。 |" for i in range(1, 100))
        result = self.render(markdown=markdown, formats=["md", "pdf"])
        pdf = Path(result["files"][1]["path"]).read_bytes()
        repeat = self.render(markdown=markdown, formats=["md", "pdf"])
        self.assertEqual(result, repeat)
        reader = PdfReader(io.BytesIO(pdf))
        self.assertGreater(len(reader.pages), 2)
        text = "".join(page.extract_text() for page in reader.pages)
        self.assertIn("第99条", text); self.assertIn("坡地转弯", text)
        embedded = []
        for page in reader.pages:
            for font in page["/Resources"]["/Font"].values():
                descriptor = font.get_object().get("/FontDescriptor")
                if descriptor:
                    embedded.append("/FontFile2" in descriptor.get_object())
            self.assertFalse(page.get("/Annots"))
        self.assertTrue(embedded); self.assertTrue(all(embedded))

    def test_very_long_cell_and_wide_table_keep_last_content(self):
        from pypdf import PdfReader
        markdown = "| 条件 | 解释 |\n| --- | --- |\n| 长评论 | " + "具体中文条件。" * 400 + "最后证据。 |\n\n"
        markdown += "| " + " | ".join("维度" + str(i) for i in range(7)) + " |\n"
        markdown += "| " + " | ".join("---" for _ in range(7)) + " |\n"
        markdown += "| " + " | ".join("条件" + str(i) for i in range(7)) + " |\n"
        result = self.render(markdown=markdown, formats=["pdf"])
        reader = PdfReader(result["files"][0]["path"])
        text = "".join(page.extract_text() for page in reader.pages)
        self.assertIn("最后证据", text); self.assertIn("条件6", text)

    def test_pdf_failure_does_not_write_partial_md_delivery(self):
        with patch.object(renderer, "MAX_PAGES", 1), self.assertRaises(renderer.RenderError) as raised:
            self.render(markdown=("一段独立的合成测试。\n\n" * 150), formats=["md", "pdf"])
        self.assertEqual(raised.exception.code, "report_render_page_limit")
        self.assertEqual(list(self.work.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
