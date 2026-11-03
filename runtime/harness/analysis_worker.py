"""One request per disposable, networkless container. No broker capability here."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import stat
import subprocess
import sys
import time

INPUT_ROOT = "/inputs"
WORK_ROOT = "/workspace"
MAX_OUTPUT = 512 * 1024
MAX_TEXT = 500_000
PARSER_VERSION = "wenquan-isolated-parser/v1"


class WorkerError(Exception):
    def __init__(self, code):
        self.code = code


def secure_file(path, *, write=False, workspace_only=False):
    if not isinstance(path, str) or len(path) > 500 or "\x00" in path:
        raise WorkerError("analysis_path_invalid")
    root = WORK_ROOT if path.startswith(WORK_ROOT + "/") else INPUT_ROOT if path.startswith(INPUT_ROOT + "/") else None
    if root is None or (write or workspace_only) and root != WORK_ROOT:
        raise WorkerError("analysis_path_invalid")
    parts = path[len(root) + 1:].split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkerError("analysis_path_invalid")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if write:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = following
        flags = (os.O_WRONLY | os.O_CREAT if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(parts[-1], flags, 0o600, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd); raise WorkerError("analysis_path_invalid")
        if write:
            os.ftruncate(fd, 0)
        return fd
    except OSError:
        raise WorkerError("analysis_file_unavailable") from None
    finally:
        os.close(directory)


def read_bytes(path, *, maximum, workspace_only=False):
    with os.fdopen(secure_file(path, workspace_only=workspace_only), "rb") as stream:
        if os.fstat(stream.fileno()).st_size > maximum:
            raise WorkerError("analysis_file_too_large")
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise WorkerError("analysis_file_too_large")
        return data


def execute(command, timeout):
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise WorkerError("analysis_timeout_invalid")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": WORK_ROOT,
           "LANG": "C.UTF-8", "MPLCONFIGDIR": WORK_ROOT + "/.matplotlib",
           "OPENBLAS_NUM_THREADS": "2", "OMP_NUM_THREADS": "2"}
    process = subprocess.Popen(command, cwd=WORK_ROOT, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout
    reason = None
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                reason = "analysis_timeout"; break
            for key, _mask in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj); continue
                if sum(map(len, output.values())) + len(chunk) > MAX_OUTPUT:
                    reason = "analysis_output_limit"; break
                output[key.data].extend(chunk)
            if reason:
                break
        if reason:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=2)
    finally:
        selector.close()
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdout.close(); process.stderr.close()
    return {"exit_code": process.returncode, **{key: value.decode("utf-8", "replace") for key, value in output.items()},
            "timed_out": reason == "analysis_timeout", "output_truncated": reason == "analysis_output_limit", "error_code": reason}


def parse_attachment(arguments):
    filename = arguments.get("filename")
    if not isinstance(filename, str) or PurePosixPath(filename).name != filename or filename.startswith("."):
        raise WorkerError("attachment_filename_invalid")
    data = read_bytes(INPUT_ROOT + "/" + filename, maximum=20 * 1024 * 1024)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if arguments.get("source_sha256") not in {None, digest}:
        raise WorkerError("attachment_changed")
    extension = filename.rsplit(".", 1)[-1].lower()
    texts, locators, size = [], [], 0

    def add(text, locator):
        nonlocal size
        if not text:
            return
        if size + len(text) + 1 > MAX_TEXT:
            raise WorkerError("attachment_text_limit")
        if len(locators) >= 10000:
            raise WorkerError("attachment_locator_limit")
        start = size; texts.append(text + "\n"); size += len(text) + 1
        locators.append({**locator, "start": start, "end": size})

    if extension == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or len(reader.pages) > 500:
            raise WorkerError("attachment_pdf_unsupported")
        for index, page in enumerate(reader.pages):
            value = page.extract_text() or ""
            if not value.strip() and list(page.images):
                raise WorkerError("attachment_pdf_ocr_required")
            add(value, {"page": index + 1})
    elif extension == "docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = Document(io.BytesIO(data))
        for index, block in enumerate(document.iter_inner_content()):
            if isinstance(block, Paragraph):
                add(block.text, {"row": index + 1})
            elif isinstance(block, Table):
                for row_index, row in enumerate(block.rows):
                    add("\t".join(cell.text for cell in row.cells), {"sheet": f"Document table {index + 1}", "row": row_index + 1})
    elif extension == "xlsx":
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
        cells = 0
        try:
            for sheet in book:
                if sheet.max_row > 100_000 or sheet.max_column > 1000:
                    raise WorkerError("attachment_sheet_limit")
                for row_index, row in enumerate(sheet.iter_rows(values_only=True)):
                    cells += len(row)
                    if cells > 2_000_000:
                        raise WorkerError("attachment_sheet_limit")
                    add("\t".join("" if value is None else str(value) for value in row), {"sheet": sheet.title, "row": row_index + 1})
        finally:
            book.close()
    elif extension in {"png", "jpg", "jpeg", "webp"}:
        from PIL import Image
        import pytesseract
        Image.MAX_IMAGE_PIXELS = 25_000_000
        picture = Image.open(io.BytesIO(data)); picture.load()
        if picture.width * picture.height > 25_000_000:
            raise WorkerError("attachment_image_limit")
        try:
            add(pytesseract.image_to_string(picture, lang="eng+chi_sim", timeout=25), {"page": 1})
        except RuntimeError:
            pass  # A validated photo need not have OCR text; the vision reader can interpret it.
    else:
        raise WorkerError("attachment_type_unsupported")
    text = "".join(texts)
    if not text.strip() and extension not in {"png", "jpg", "jpeg", "webp"}:
        raise WorkerError("attachment_text_empty")
    return {"text": text, "parser_version": PARSER_VERSION, "locators": locators, "source_sha256": digest}


def dispatch(name, args):
    if not isinstance(args, dict):
        raise WorkerError("analysis_arguments_invalid")
    allowed = {
        "run_python": {"code", "timeout_seconds"}, "run_shell": {"command", "timeout_seconds"},
        "read_file": {"path", "offset", "limit"}, "write_file": {"path", "content"},
        "parse_attachment": {"filename", "media_type", "source_sha256", "timeout_seconds"},
        "render_report": {"markdown", "title", "filename", "formats"},
        "_read_output": {"path"},
    }
    if name not in allowed or set(args) - allowed[name]:
        raise WorkerError("analysis_tool_rejected")
    if name == "render_report":
        spec = importlib.util.spec_from_file_location("wenquan_report_render", Path(__file__).with_name("report_render.py"))
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        try:
            return renderer.render_report(args, open_output=secure_file, work_root=WORK_ROOT)
        except renderer.RenderError as error:
            raise WorkerError(error.code) from None
    if name in {"run_python", "run_shell"}:
        value = args.get("code" if name == "run_python" else "command")
        if not isinstance(value, str) or not value or len(value) > (60000 if name == "run_python" else 20000):
            raise WorkerError("analysis_arguments_invalid")
        command = [sys.executable, "-I", "-c", value] if name == "run_python" else ["/bin/bash", "--noprofile", "--norc", "-c", value]
        return execute(command, args.get("timeout_seconds", 60))
    if name == "write_file":
        value = args.get("content")
        if not isinstance(value, str) or len(value) > 100000:
            raise WorkerError("analysis_arguments_invalid")
        with os.fdopen(secure_file(args.get("path"), write=True), "wb") as stream:
            encoded = value.encode(); stream.write(encoded)
        return {"path": args["path"], "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    if name == "read_file":
        offset, limit = args.get("offset", 0), args.get("limit", 60000)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 60000:
            raise WorkerError("analysis_arguments_invalid")
        with os.fdopen(secure_file(args.get("path")), "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if offset > size:
                raise WorkerError("analysis_offset_invalid")
            stream.seek(offset); data = stream.read(limit)
        return {"path": args["path"], "content": data.decode("utf-8", "replace"), "offset": offset,
                "offset_unit": "bytes", "next_offset": offset + len(data) if offset + len(data) < size else None,
                "total_bytes": size, "has_more": offset + len(data) < size}
    if name == "_read_output":
        data = read_bytes(args.get("path"), maximum=16 * 1024 * 1024, workspace_only=True)
        return {"base64": base64.b64encode(data).decode(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return parse_attachment(args)


def main():
    os.umask(0o077)
    try:
        raw = sys.stdin.buffer.read(500001)
        if len(raw) > 500000:
            raise WorkerError("analysis_request_limit")
        request = json.loads(raw)
        if set(request) != {"name", "arguments"}:
            raise WorkerError("analysis_request_invalid")
        result = dispatch(request["name"], request["arguments"])
        print(json.dumps({"result": result}, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"error": {"code": exc.code if isinstance(exc, WorkerError) else "analysis_failed", "type": type(exc).__name__}}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
