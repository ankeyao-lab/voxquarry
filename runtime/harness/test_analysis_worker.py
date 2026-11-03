import base64
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("wenquan_analysis_worker", Path(__file__).with_name("analysis_worker.py"))
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class AnalysisWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inputs, self.work = self.root / "inputs", self.root / "workspace"
        self.inputs.mkdir(); self.work.mkdir()
        self.patches = [patch.object(worker, "INPUT_ROOT", str(self.inputs)), patch.object(worker, "WORK_ROOT", str(self.work))]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def test_file_roundtrip_and_paging_keep_exact_original_bytes(self):
        path = str(self.work / "nested" / "result.txt")
        result = worker.dispatch("write_file", {"path": path, "content": "abc中文def"})
        self.assertEqual(result["bytes"], 12)
        page = worker.dispatch("read_file", {"path": path, "offset": 0, "limit": 3})
        self.assertEqual(page["content"], "abc")
        self.assertEqual(page["next_offset"], 3)
        self.assertTrue(page["has_more"])
        result = worker.dispatch("_read_output", {"path": path})
        self.assertEqual(base64.b64decode(result["base64"]), "abc中文def".encode())

    def test_symlink_and_parent_escape_never_read_or_truncate_other_files(self):
        secret = self.root / "secret"; secret.write_text("unchanged")
        (self.work / "link").symlink_to(secret)
        (self.work / "directory").symlink_to(self.root, target_is_directory=True)
        for path in [str(self.work / "link"), str(self.work / "directory" / "secret"), str(self.work) + "/../secret", str(secret)]:
            with self.assertRaises(worker.WorkerError):
                worker.dispatch("read_file", {"path": path})
            with self.assertRaises(worker.WorkerError):
                worker.dispatch("write_file", {"path": path, "content": "overwrite"})
        self.assertEqual(secret.read_text(), "unchanged")

    def test_inputs_are_readable_but_never_writable_or_exported_as_computed_output(self):
        path = self.inputs / "reviews.jsonl"; path.write_text('{"rating":5}')
        self.assertEqual(worker.dispatch("read_file", {"path": str(path)})["content"], '{"rating":5}')
        with self.assertRaises(worker.WorkerError):
            worker.dispatch("write_file", {"path": str(path), "content": "changed"})
        with self.assertRaises(worker.WorkerError):
            worker.dispatch("_read_output", {"path": str(path)})

    def test_invalid_tool_fields_and_bounds_fail_closed(self):
        for name, arguments in [("docker", {}), ("run_python", {"code": "pass", "network": True}),
                                ("run_shell", {"command": "pwd", "timeout_seconds": 999}),
                                ("read_file", {"path": str(self.work / "x"), "limit": True})]:
            with self.assertRaises(worker.WorkerError):
                worker.dispatch(name, arguments)

    def test_process_output_and_timeout_are_explicit_not_silent_success(self):
        result = worker.dispatch("run_python", {"code": "print(15/20)"})
        self.assertEqual(result["stdout"], "0.75\n"); self.assertEqual(result["exit_code"], 0)
        result = worker.dispatch("run_python", {"code": "import time; time.sleep(5)", "timeout_seconds": 1})
        self.assertTrue(result["timed_out"]); self.assertNotEqual(result["exit_code"], 0)
        with patch.object(worker, "MAX_OUTPUT", 100):
            result = worker.dispatch("run_python", {"code": "print('x'*5000)"})
        self.assertTrue(result["output_truncated"])
        self.assertEqual(result["error_code"], "analysis_output_limit")

    def test_whole_output_read_refuses_to_misrepresent_an_oversized_file(self):
        path = self.work / "too-big.bin"
        with path.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        with self.assertRaises(worker.WorkerError) as raised:
            worker.dispatch("_read_output", {"path": str(path)})
        self.assertEqual(raised.exception.code, "analysis_file_too_large")

    def test_parser_checks_frozen_input_hash_before_loading_a_document_library(self):
        (self.inputs / "document.pdf").write_bytes(b"test")
        with self.assertRaises(worker.WorkerError) as raised:
            worker.dispatch("parse_attachment", {"filename": "document.pdf", "source_sha256": "sha256:wrong"})
        self.assertEqual(raised.exception.code, "attachment_changed")

    def test_valid_photo_without_ocr_or_with_ocr_timeout_remains_usable_for_vision(self):
        import hashlib
        import io
        from types import SimpleNamespace
        from unittest.mock import Mock
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("isolated image decoder dependency")
        data = io.BytesIO(); Image.new("RGB", (48, 48), "blue").save(data, format="PNG")
        content = data.getvalue(); (self.inputs / "photo.png").write_bytes(content)
        for ocr in (Mock(return_value=""), Mock(side_effect=RuntimeError("OCR timeout"))):
            with patch.dict("sys.modules", {"pytesseract": SimpleNamespace(image_to_string=ocr)}):
                result = worker.dispatch("parse_attachment", {"filename": "photo.png", "source_sha256": "sha256:" + hashlib.sha256(content).hexdigest()})
            self.assertEqual(result["text"], "")
            self.assertEqual(result["locators"], [])


if __name__ == "__main__":
    unittest.main()
