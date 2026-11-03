from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent.contracts import AgentError
from agent.research_attachments import AttachmentStore, IsolatedAttachmentParser
from agent.store import WenquanStore
from agent.tests.test_agent_trace import actor


PDF = b"%PDF-1.7\nSynthetic fixture: parser must still inspect.\n%%EOF\n"


def office(extension, extra=None, compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml" if extension == ".docx" else "xl/workbook.xml", "<document/>")
        for name, body in (extra or {}).items():
            archive.writestr(name, body)
    return buffer.getvalue()


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wenquan = WenquanStore(self.root / "wenquan.sqlite3")
        self.wenquan.initialize()
        self.user = actor()
        self.session = self.wenquan.create_session(self.user, "附件研究")["session_id"]
        self.attachments = self.make_store()

    def tearDown(self):
        self.temp.cleanup()

    def make_store(self, **kwargs):
        value = AttachmentStore(self.wenquan, self.root / "attachments", **kwargs)
        value.initialize()
        return value

    def create(self, filename="研究.txt", media_type="text/plain", content=b"A bounded synthetic input"):
        return self.attachments.create(self.user, self.session, filename, media_type, content)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(AgentError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_text_roundtrip_preserves_original_and_paginated_content_with_hash(self):
        raw = "第一行\n第二行\nhttps://example.test/no-network".encode()
        item = self.create(content=raw)
        self.assertEqual(item["status"], "ready")
        self.assertRegex(item["attachment_id"], r"^att_[a-f0-9]{24}$")
        page = self.attachments.read_text(self.user, self.session, item["attachment_id"], limit=4)
        self.assertEqual(page["text"], "第一行\n")
        self.assertEqual(page["next_offset"], 4)
        self.assertEqual(page["start_line"], 1)
        remainder = self.attachments.read_text(self.user, self.session, item["attachment_id"], offset=4)
        self.assertEqual((page["text"] + remainder["text"]).encode(), raw)
        self.assertEqual(self.attachments.download_path(self.user, self.session, item["attachment_id"]).read_bytes(), raw)
        reopened = self.make_store()
        self.assertEqual(reopened.get(self.user, self.session, item["attachment_id"]), item)

    def test_all_text_formats_validate_without_executing_content(self):
        cases = [("review.csv", "text/csv", b'name,value\nunit,"=1+1"\n'),
                 ("review.json", "application/json", b'{"reviews":[{"text":"sample"}]}'),
                 ("review.jsonl", "application/x-ndjson", b'{"a":1}\n{"a":2}\n'),
                 ("review.md", "text/markdown", b"# Research\n```python\nraise Exception('not run')\n```"),
                 ("bom.txt", "text/plain; charset=utf-8", b"\xef\xbb\xbfUnicode text")]
        for filename, media_type, content in cases:
            with self.subTest(filename=filename):
                item = self.create(filename, media_type, content)
                read = self.attachments.read_text(self.user, self.session, item["attachment_id"])
                self.assertEqual(read["text"], content.decode("utf-8-sig"))
                self.assertEqual(item["parser_version"], "wenquan-text-input/v1")

    def test_foreign_owner_admin_and_different_session_cannot_read_files(self):
        item = self.create()
        other = actor("user-b", roles=("admin",))
        methods = [(self.attachments.get, (item["attachment_id"],)), (self.attachments.read_text, (item["attachment_id"],)),
                   (self.attachments.download_path, (item["attachment_id"],)), (self.attachments.list, ()),
                   (self.attachments.parse_pending, (item["attachment_id"],))]
        for method, tail in methods:
            self.assert_code("session_not_found", method, other, self.session, *tail)
        other_session = self.wenquan.create_session(self.user, "另一会话")["session_id"]
        self.assert_code("attachment_not_found", self.attachments.get, self.user, other_session, item["attachment_id"])
        self.assert_code("session_not_found", self.attachments.create, other, self.session, "a.txt", "text/plain", b"abc")

    def test_filename_and_declared_type_do_not_override_actual_file_type(self):
        for name in ("../file.txt", "dir/file.txt", "dir\\file.txt", "evil\r\n.txt", "file\x00.txt", "C:file.txt"):
            self.assert_code("attachment_filename_invalid", self.create, name)
        self.assert_code("attachment_type_unsupported", self.create, "file.exe", "application/octet-stream", b"MZbinary")
        self.assert_code("attachment_type_mismatch", self.create, "file.pdf", "application/pdf", b"This is not a PDF")
        self.assert_code("attachment_type_mismatch", self.create, "file.json", "image/png", b"{}")
        self.assert_code("attachment_type_mismatch", self.create, "file.txt", "text/plain", b"binary\x00bytes")
        self.assertEqual(self.attachments.list(self.user, self.session)["items"], [])

    def test_invalid_json_csv_and_encoding_are_not_marked_ready(self):
        for filename, media_type, content in (("file.json", "application/json", b'{"x":1,"x":2}'),
                    ("file.json", "application/json", b'{"x":NaN}'),
                    ("file.jsonl", "application/x-ndjson", b'{"a":1}\nbad'),
                    ("file.csv", "text/csv", b"a,b\n1,2,3")):
            self.assert_code("attachment_text_invalid", self.create, filename, media_type, content)
        self.assert_code("attachment_encoding_invalid", self.create, "file.txt", "text/plain", b"\xff\xfe\xfd")

    def test_single_file_and_total_byte_limits_reject_before_writing(self):
        self.attachments = self.make_store(max_file_bytes=10, max_total_bytes=15)
        self.create(content=b"1234567890")
        self.assert_code("attachment_size_invalid", self.create, content=b"x" * 11)
        self.assert_code("attachment_quota_exceeded", self.create, content=b"123456")
        self.assertEqual(self.attachments.list(self.user, self.session)["total_bytes"], 10)
        self.assertEqual(len(list(self.attachments.root.iterdir())), 2)

    def test_concurrent_uploads_cannot_bypass_count_limit(self):
        self.attachments = self.make_store(max_files=2)
        second = self.make_store(max_files=2)
        def upload(index):
            store = self.attachments if index % 2 else second
            try:
                return store.create(self.user, self.session, str(index) + ".txt", "text/plain", b"sample")
            except AgentError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(upload, range(5)))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 2)
        self.assertEqual(results.count("attachment_quota_exceeded"), 3)

    def test_pdf_office_and_images_stay_pending_without_isolated_parser(self):
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\0" * 17 + b"\x00\x00\x00\x00IEND" + b"\0" * 4
        jpeg = b"\xff\xd8\xff\xe0synthetic\xff\xd9"
        webp = b"RIFF" + (16).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 8
        for filename, media_type, content in (("file.pdf", "application/pdf", PDF),
                 ("file.docx", "application/octet-stream", office(".docx")),
                 ("file.xlsx", "application/octet-stream", office(".xlsx")),
                 ("file.png", "image/png", png), ("file.jpg", "image/jpeg", jpeg), ("file.webp", "image/webp", webp)):
            with self.subTest(filename=filename):
                item = self.create(filename, media_type, content)
                self.assertEqual(item["status"], "pending")
                self.assertIsNone(item["parser_version"])
                self.assert_code("attachment_not_ready", self.attachments.read_text, self.user, self.session, item["attachment_id"])
                self.assertEqual(self.attachments.parse_pending(self.user, self.session, item["attachment_id"])["status"], "pending")
                self.assertEqual(self.attachments.download_path(self.user, self.session, item["attachment_id"]).read_bytes(), content)

    def test_office_macros_path_traversal_bombs_and_wrong_container_are_rejected(self):
        for extra in ({"word/vbaProject.bin": "macro"}, {"../outside.xml": "bad"}, {"word/repeated.xml": "x" * 100000}):
            self.assert_code("attachment_container_invalid", self.create, "file.docx", "application/octet-stream", office(".docx", extra, zipfile.ZIP_DEFLATED))
        self.assert_code("attachment_container_invalid", self.create, "file.docx", "application/octet-stream", office(".xlsx"))

    def test_binary_parser_must_be_explicit_isolated_adapter_and_output_is_bounded(self):
        self.assert_code("attachment_parser_config_invalid", self.make_store, parser=lambda _request: {"text": "unsafe direct parsing"})
        requests = []
        def dispatch(request):
            requests.append(request)
            return {"text": "一页提取文字", "parser_version": "fixture-worker/v1", "locators": [{"start": 0, "end": 6, "page": 1}], "source_sha256": request["source_sha256"]}
        self.attachments = self.make_store(parser=IsolatedAttachmentParser(dispatch, "isolated-fixture"))
        item = self.create("file.pdf", "application/pdf", PDF)
        self.assertEqual(requests, [])  # Upload never starts binary parsing in HTTP.
        parsed = self.attachments.parse_pending(self.user, self.session, item["attachment_id"])
        self.assertEqual(parsed["status"], "ready")
        request = requests[0]
        self.assertFalse(request["limits"]["network"])
        self.assertFalse(request["limits"]["macros"])
        self.assertFalse(request["limits"]["evaluate_formulas"])
        self.assertEqual(request["source_sha256"], item["sha256"])
        read = self.attachments.read_text(self.user, self.session, item["attachment_id"])
        self.assertEqual(read["text"], "一页提取文字")
        self.assertEqual(read["locators"][0]["page"], 1)
        self.attachments.parse_pending(self.user, self.session, item["attachment_id"])
        self.assertEqual(len(requests), 1)

    def test_parse_failure_retains_original_and_does_not_leak_exception_or_silently_retry(self):
        count = []
        def dispatch(_request):
            count.append(1)
            raise RuntimeError("a private input or fixture-secret from the worker")
        self.attachments = self.make_store(parser=IsolatedAttachmentParser(dispatch, "isolated-fixture"))
        item = self.create("file.pdf", "application/pdf", PDF)
        failed = self.attachments.parse_pending(self.user, self.session, item["attachment_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("fixture-secret", json.dumps(failed))
        self.attachments.parse_pending(self.user, self.session, item["attachment_id"])
        self.assertEqual(len(count), 1)
        self.attachments.parse_pending(self.user, self.session, item["attachment_id"], retry=True)
        self.assertEqual(len(count), 2)
        self.assertEqual(self.attachments.download_path(self.user, self.session, item["attachment_id"]).read_bytes(), PDF)

    def test_process_exit_during_parse_recovers_original_for_explicit_retry(self):
        item = self.create("crash.pdf", "application/pdf", PDF)
        queued = self.create("queued.pdf", "application/pdf", PDF)
        ready = self.create()
        script = '''
import os, sys
from pathlib import Path
from agent.research_attachments import AttachmentStore, IsolatedAttachmentParser
from agent.store import WenquanStore
from agent.tests.test_agent_trace import actor
def crash(request):
    output=Path(request["input_path"]).with_suffix(".text")
    output.write_text("uncommitted parser output")
    output.chmod(0o600)
    os._exit(23)
store=AttachmentStore(WenquanStore(Path(sys.argv[1])), Path(sys.argv[2]), parser=IsolatedAttachmentParser(crash,"fixture-crash"))
store.initialize()
store.parse_pending(actor(),sys.argv[3],sys.argv[4])
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.wenquan.database_path), str(self.attachments.root), self.session, item["attachment_id"]],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 23, result.stderr.decode())
        self.assertEqual(self.attachments.get(self.user, self.session, item["attachment_id"])["status"], "parsing")
        calls = []
        def dispatch(request):
            calls.append(request["attachment_id"])
            return {"text": "Recovered text", "source_sha256": request["source_sha256"], "parser_version": "fixture/v1"}
        reopened = self.make_store(parser=IsolatedAttachmentParser(dispatch, "isolated-fixture"))
        self.assertEqual(reopened.recover_interrupted(), 2)
        self.assertEqual(reopened.recover_interrupted(), 0)
        for saved in (item, queued):
            current = reopened.get(self.user, self.session, saved["attachment_id"])
            self.assertEqual((current["status"], current["error_code"]), ("failed", "attachment_parse_interrupted"))
            self.assertEqual(reopened.download_path(self.user, self.session, saved["attachment_id"]).read_bytes(), PDF)
        self.assertFalse(reopened._path(item["attachment_id"], extracted=True).exists())
        self.assertEqual(reopened.get(self.user, self.session, ready["attachment_id"])["status"], "ready")
        reopened.parse_pending(self.user, self.session, item["attachment_id"])
        self.assertEqual(calls, [])
        first = reopened.request_retry(self.user, self.session, item["attachment_id"])
        duplicate = reopened.request_retry(self.user, self.session, item["attachment_id"])
        self.assertTrue(first["retry_requested"])
        self.assertFalse(duplicate["retry_requested"])
        self.assertEqual(first["status"], "pending")
        parsed = reopened.parse_pending(self.user, self.session, item["attachment_id"], retry=True)
        self.assertEqual(parsed["status"], "ready")
        self.assertEqual(reopened.read_text(self.user, self.session, item["attachment_id"])["text"], "Recovered text")
        self.assertEqual(calls, [item["attachment_id"]])

    def test_other_store_cannot_recover_or_duplicate_a_live_parser(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def dispatch(request):
            calls.append(request["attachment_id"]); entered.set()
            if not release.wait(5):
                raise RuntimeError("test worker was not released")
            return {"text": "One parsed result", "source_sha256": request["source_sha256"], "parser_version": "fixture/v1"}
        self.attachments = self.make_store(parser=IsolatedAttachmentParser(dispatch, "isolated-fixture"))
        other = self.make_store(parser=IsolatedAttachmentParser(dispatch, "isolated-fixture"))
        item = self.create("concurrent.pdf", "application/pdf", PDF)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.attachments.parse_pending, self.user, self.session, item["attachment_id"])
            try:
                self.assertTrue(entered.wait(3))
                second = pool.submit(other.parse_pending, self.user, self.session, item["attachment_id"], retry=True)
                self.assertEqual(second.result(timeout=2)["status"], "parsing")
                self.assertEqual(other.recover_interrupted(), 0)
                retry = other.request_retry(self.user, self.session, item["attachment_id"])
                self.assertEqual(retry["status"], "parsing")
                self.assertFalse(retry["retry_requested"])
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3)["status"], "ready")
        self.assertEqual(calls, [item["attachment_id"]])
        self.assert_code("session_not_found", other.request_retry, actor("other-owner", roles=("admin",)), self.session, item["attachment_id"])

    def test_changed_source_and_symlink_are_rejected(self):
        item = self.create()
        path = self.attachments.download_path(self.user, self.session, item["attachment_id"])
        path.write_bytes(b"changed")
        self.assert_code("attachment_changed", self.attachments.download_path, self.user, self.session, item["attachment_id"])
        path.unlink()
        outside = self.root / "outside"
        outside.write_bytes(b"other")
        path.symlink_to(outside)
        self.assert_code("attachment_storage_invalid", self.attachments.download_path, self.user, self.session, item["attachment_id"])

    def test_wrong_source_or_unbounded_worker_output_does_not_become_ready(self):
        for result in ({"text": "wrong source", "source_sha256": "sha256:" + "0" * 64, "parser_version": "fixture/v1"},
                       {"text": "x" * 101, "source_sha256": "", "parser_version": "fixture/v1"}):
            def dispatch(request):
                return {**result, "source_sha256": result["source_sha256"] or request["source_sha256"]}
            self.attachments = self.make_store(parser=IsolatedAttachmentParser(dispatch, "isolated-fixture"), max_text_chars=100)
            item = self.create("file.pdf", "application/pdf", PDF)
            parsed = self.attachments.parse_pending(self.user, self.session, item["attachment_id"])
            self.assertEqual(parsed["status"], "failed")

    def test_input_and_extracted_files_have_private_permissions(self):
        self.create()
        self.assertEqual(stat.S_IMODE(self.attachments.root.stat().st_mode), 0o700)
        for path in self.attachments.root.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
