"""Private, immutable research inputs; binary parsing belongs to an isolated worker.

The HTTP adapter decodes X-Filename once, caps the request body before buffering,
and serves downloads with Content-Disposition: attachment and nosniff. No content
in this module is evaluated, imported into shared data, or fetched as a URL.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .contracts import AgentError, IdentityContext, canonical_json, new_id, utc_now
from .store import WenquanStore


ATTACHMENT_CONTRACT = "wenquan-research-attachment/v1"
TEXT_PARSER_VERSION = "wenquan-text-input/v1"
MIB = 1024 * 1024
_FORMATS = {
    ".txt": ("text/plain", {"text/plain"}),
    ".md": ("text/markdown", {"text/markdown", "text/plain", "text/x-markdown"}),
    ".csv": ("text/csv", {"text/csv", "application/csv", "application/vnd.ms-excel"}),
    ".json": ("application/json", {"application/json", "text/json"}),
    ".jsonl": ("application/x-ndjson", {"application/x-ndjson", "application/jsonl", "text/plain", "application/json"}),
    ".pdf": ("application/pdf", {"application/pdf"}),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    ".png": ("image/png", {"image/png"}),
    ".jpg": ("image/jpeg", {"image/jpeg"}),
    ".jpeg": ("image/jpeg", {"image/jpeg"}),
    ".webp": ("image/webp", {"image/webp"}),
}
_TEXT_FORMATS = frozenset({".txt", ".md", ".csv", ".json", ".jsonl"})
# Database transactions serialize claims; this process-wide guard also keeps a
# second store instance from recovering an attachment whose parser is alive.
_PARSE_LOCK = threading.RLock()
_ACTIVE_PARSES: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class IsolatedAttachmentParser:
    """Trusted adapter to a separate process/VM, not an in-process document parser.

    The application integrator supplies dispatch, which must enforce the supplied
    limits in its worker (read-only input, no network/macros, bounded CPU/memory).
    This wrapper declares that boundary; it does not turn a Python callable into
    a sandbox. No PDF/Office/image parser is imported by this service.
    """
    dispatch: Callable[[dict[str, Any]], dict[str, Any]]
    backend_id: str

    def __post_init__(self):
        if not callable(self.dispatch) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", self.backend_id):
            raise AgentError("attachment_parser_config_invalid", "需要明确的隔离解析后端")


def _filename(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 240
            or value in {".", ".."} or value != value.strip()
            or any(character in value for character in ("/", "\\", ":"))
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise AgentError("attachment_filename_invalid", "文件名无效，请使用不包含路径的文件名")
    return value


def _sha(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_loads(value: str):
    def unique(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = child
        return result
    def invalid(_value):
        raise ValueError("non-finite JSON value")
    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid)


def _text_input(content: bytes, extension: str, max_text_chars: int) -> tuple[str, dict]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AgentError("attachment_encoding_invalid", "文本文件需要使用 UTF-8 编码") from None
    if len(text) > max_text_chars:
        raise AgentError("attachment_text_too_large", "文字过多，请拆分文件或使用数据集入口")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise AgentError("attachment_type_mismatch", "文件内容不是支持的文本格式")
    details: dict[str, Any] = {"line_count": len(text.splitlines()), "encoding": "utf-8"}
    try:
        if extension == ".json":
            value = _json_loads(text)
            details["json_root"] = "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "scalar"
            details["record_count"] = len(value) if isinstance(value, (dict, list)) else 1
        elif extension == ".jsonl":
            count = 0
            for line in text.splitlines():
                if line.strip():
                    _json_loads(line)
                    count += 1
            if not count:
                raise ValueError("empty JSONL")
            details["record_count"] = count
        elif extension == ".csv":
            row_count = width = 0
            for row in csv.reader(io.StringIO(text, newline=""), strict=True):
                if not row:
                    continue
                if row_count == 0:
                    width = len(row)
                if len(row) != width or width > 1000 or row_count >= 100000:
                    raise ValueError("CSV shape or limit")
                row_count += 1
            if not row_count:
                raise ValueError("empty CSV")
            details.update(row_count=row_count, column_count=width, formulas_evaluated=False)
    except (ValueError, RecursionError, csv.Error):
        raise AgentError("attachment_text_invalid", "文件内容不符合所选文本格式，请检查后重新上传") from None
    return text, details


def _office_container(content: bytes, extension: str) -> None:
    """Only inspect ZIP directory metadata; decompression/parsing runs elsewhere."""
    try:
        # Bound directory allocation before ZipFile builds its in-memory entry
        # list. A small compressed upload must not allocate millions of entries.
        end = content.rfind(b"PK\x05\x06", max(0, len(content) - 65557))
        if end < 0 or end + 22 > len(content):
            raise ValueError("missing ZIP directory")
        footer = content[end:end + 22]
        entries_on_disk = int.from_bytes(footer[8:10], "little")
        entry_count = int.from_bytes(footer[10:12], "little")
        directory_size = int.from_bytes(footer[12:16], "little")
        directory_offset = int.from_bytes(footer[16:20], "little")
        if (footer[4:8] != b"\0" * 4 or entries_on_disk != entry_count or entry_count > 1000
                or directory_size > 2 * MIB or directory_offset + directory_size > end
                or end + 22 + int.from_bytes(footer[20:22], "little") != len(content)):
            raise ValueError("ZIP directory limit")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000:
                raise ValueError("entry limit")
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)):
                raise ValueError("duplicate entry")
            required = {"[Content_Types].xml", "word/document.xml" if extension == ".docx" else "xl/workbook.xml"}
            if not required.issubset(names) or (extension == ".docx" and "xl/workbook.xml" in names) or (extension == ".xlsx" and "word/document.xml" in names):
                raise ValueError("wrong office type")
            if sum(entry.file_size for entry in entries) > 100 * MIB:
                raise ValueError("expanded size limit")
            for entry in entries:
                name = entry.filename.casefold()
                mode = entry.external_attr >> 16
                if (name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/")
                        or re.match(r"^[a-z]:", name) or stat.S_ISLNK(mode) or entry.flag_bits & 1
                        or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or "vbaproject" in name or "macros" in name or name.endswith((".exe", ".dll", ".js", ".vbs"))
                        or entry.file_size > max(1, entry.compress_size) * 200):
                    raise ValueError("unsafe archive entry")
    except (ValueError, zipfile.BadZipFile, NotImplementedError, OSError):
        raise AgentError("attachment_container_invalid", "Office 文件类型或内部结构不受支持，请另存为无宏文件") from None


def _inspect(content: bytes, extension: str, media_type: str, max_text_chars: int):
    canonical, allowed = _FORMATS[extension]
    declared = media_type.split(";", 1)[0].strip().casefold()
    if declared and declared != "application/octet-stream" and declared not in allowed:
        raise AgentError("attachment_type_mismatch", "文件扩展名与上传类型不一致")
    if extension in _TEXT_FORMATS:
        text, details = _text_input(content, extension, max_text_chars)
        return canonical, text, details
    good = False
    if extension == ".pdf":
        good = bool(re.match(rb"%PDF-(?:1\.[0-7]|2\.0)[\r\n ]", content[:12])) and b"%%EOF" in content[-1024:]
    elif extension in {".docx", ".xlsx"}:
        if not content.startswith(b"PK\x03\x04"):
            raise AgentError("attachment_type_mismatch", "文件内容与 Office 类型不一致")
        _office_container(content, extension)
        good = True
    elif extension == ".png":
        good = (content.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") and len(content) >= 45
                and content[-12:-4] == b"\x00\x00\x00\x00IEND")
    elif extension in {".jpg", ".jpeg"}:
        good = content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9") and len(content) > 10
    elif extension == ".webp":
        good = (len(content) >= 20 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
                and int.from_bytes(content[4:8], "little") + 8 == len(content)
                and content[12:16] in {b"VP8 ", b"VP8L", b"VP8X"})
    if not good:
        raise AgentError("attachment_type_mismatch", "文件内容与扩展名不一致，或文件不完整")
    # Magic/container recognition is deliberately not claimed as successful
    # document decoding, OCR, validity verification, or malware scanning.
    return canonical, None, {"validation": "signature_and_container_only", "needs_isolated_parser": True}


class AttachmentStore:
    def __init__(self, store: WenquanStore, root: Path | str, *, parser: IsolatedAttachmentParser | None = None,
                 max_file_bytes: int = 20 * MIB, max_files: int = 10, max_total_bytes: int = 100 * MIB,
                 max_text_chars: int = 8 * MIB):
        if parser is not None and not isinstance(parser, IsolatedAttachmentParser):
            raise AgentError("attachment_parser_config_invalid", "二进制附件必须通过隔离解析适配器处理")
        if min(max_file_bytes, max_files, max_total_bytes, max_text_chars) <= 0:
            raise AgentError("attachment_config_invalid", "附件限额无效")
        self.store, self.root, self.parser = store, Path(os.path.abspath(Path(root).expanduser())), parser
        self.max_file_bytes, self.max_files, self.max_total_bytes = max_file_bytes, max_files, max_total_bytes
        self.max_text_chars = max_text_chars
        self._lock = threading.RLock()

    def _directory(self, *, create=False):
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise AgentError("attachment_storage_invalid", "附件需要独立的私有目录", http_status=503)

    @contextmanager
    def _connect(self):
        with self._lock:
            self._directory()
            db = self.store._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def initialize(self):
        self._directory(create=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS research_attachments (
                attachment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
                owner_user_id TEXT NOT NULL, scope_id TEXT NOT NULL, filename TEXT NOT NULL,
                media_type TEXT NOT NULL, extension TEXT NOT NULL, byte_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                text_sha256 TEXT, text_chars INTEGER, parser_version TEXT, parse_details TEXT NOT NULL,
                error_code TEXT, parse_attempt_id TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS research_attachments_session ON research_attachments(owner_user_id,scope_id,session_id)")

    def recover_interrupted(self) -> int:
        """At service startup, make abandoned parses explicitly retryable."""
        recovered = 0
        with _PARSE_LOCK, self._connect() as db:
            rows = db.execute("SELECT attachment_id,parse_attempt_id FROM research_attachments WHERE status IN ('pending','parsing')").fetchall()
            for row in rows:
                if (str(self.root.resolve()), row["parse_attempt_id"]) in _ACTIVE_PARSES:
                    continue
                # A crash after writing extracted text but before its DB commit
                # can leave this uncommitted output behind. Original bytes stay.
                self._path(row["attachment_id"], extracted=True).unlink(missing_ok=True)
                db.execute("UPDATE research_attachments SET status='failed',error_code='attachment_parse_interrupted',parse_attempt_id=NULL WHERE attachment_id=?", (row["attachment_id"],))
                recovered += 1
        return recovered

    def request_retry(self, actor, session_id, attachment_id) -> dict:
        """Reserve the existing file for a queued retry without uploading again."""
        self._authorize(actor, session_id)
        with _PARSE_LOCK, self._connect() as db:
            row = self._owned(db, actor, session_id, attachment_id)
            requested = row["status"] == "failed"
            if requested:
                self._read(row)
                db.execute("UPDATE research_attachments SET status='pending',error_code=NULL WHERE attachment_id=?", (attachment_id,))
            return {**self._metadata(self._owned(db, actor, session_id, attachment_id)), "retry_requested": requested}

    def _authorize(self, actor, session_id):
        if not isinstance(actor, IdentityContext):
            raise AgentError("identity_invalid", "附件需要已验证的用户身份", http_status=401)
        return self.store.get_session(actor, session_id)

    def _owned(self, db, actor, session_id, attachment_id):
        row = db.execute("SELECT * FROM research_attachments WHERE attachment_id=? AND session_id=? AND owner_user_id=? AND scope_id=?",
                         (attachment_id, session_id, actor.owner_user_id, actor.scope_id)).fetchone()
        if row is None:
            raise AgentError("attachment_not_found", "没有找到当前会话的附件", http_status=404)
        return row

    @staticmethod
    def _metadata(row):
        result = dict(row)
        result.pop("parse_attempt_id", None)
        result["parse_details"] = json.loads(result["parse_details"])
        result["parse_details"].pop("image_analysis", None)
        result.update(contract=ATTACHMENT_CONTRACT, ready=result["status"] == "ready", source_version=result["sha256"])
        return result

    def _path(self, attachment_id, *, extracted=False):
        if not re.fullmatch(r"att_[a-f0-9]{24}", attachment_id):
            raise AgentError("attachment_not_found", "没有找到当前会话的附件", http_status=404)
        return self.root / (attachment_id + (".text" if extracted else ".input"))

    @staticmethod
    def _write(path, content):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def _read(self, row, *, extracted=False):
        self._directory()
        path = self._path(row["attachment_id"], extracted=extracted)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise AgentError("attachment_storage_invalid", "附件文件类型或权限无效", http_status=503)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise AgentError("attachment_content_unavailable", "附件文件暂时不可用", http_status=404) from None
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                raise AgentError("attachment_changed", "附件文件发生变化", http_status=409)
            content = stream.read((self.max_text_chars * 4 if extracted else self.max_file_bytes) + 1)
        expected = row["text_sha256"] if extracted else row["sha256"]
        if _sha(content) != expected or (not extracted and len(content) != row["byte_size"]):
            raise AgentError("attachment_changed", "附件内容与上传版本不一致", http_status=409)
        return content

    def create(self, actor: IdentityContext, session_id: str, filename: str, media_type: str, content: bytes) -> dict:
        self._authorize(actor, session_id)
        filename = _filename(filename)
        if not isinstance(media_type, str) or len(media_type) > 200 or "\n" in media_type or "\r" in media_type:
            raise AgentError("attachment_type_mismatch", "上传类型无效")
        extension = Path(filename).suffix.casefold()
        if extension not in _FORMATS:
            raise AgentError("attachment_type_unsupported", "暂不支持这个文件类型")
        if not isinstance(content, bytes) or not content or len(content) > self.max_file_bytes:
            raise AgentError("attachment_size_invalid", "文件为空或超过单文件大小限制", http_status=413)
        canonical, text, details = _inspect(content, extension, media_type, self.max_text_chars)
        attachment_id = new_id("att")
        original_path = self._path(attachment_id)
        text_path = self._path(attachment_id, extracted=True)
        created_paths = []
        try:
            with self._connect() as db:
                count, total = db.execute("SELECT count(*),COALESCE(sum(byte_size),0) FROM research_attachments WHERE owner_user_id=? AND scope_id=? AND session_id=?",
                                          (actor.owner_user_id, actor.scope_id, session_id)).fetchone()
                if count >= self.max_files or total + len(content) > self.max_total_bytes:
                    raise AgentError("attachment_quota_exceeded", "当前会话的附件数量或总大小超过限制", http_status=409)
                self._write(original_path, content); created_paths.append(original_path)
                encoded = text.encode() if text is not None else None
                if encoded is not None:
                    self._write(text_path, encoded); created_paths.append(text_path)
                db.execute("INSERT INTO research_attachments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (attachment_id, session_id, actor.owner_user_id, actor.scope_id, filename, canonical, extension,
                            len(content), _sha(content), "ready" if text is not None else "pending", utc_now(),
                            _sha(encoded) if encoded is not None else None, len(text) if text is not None else None,
                            TEXT_PARSER_VERSION if text is not None else None, canonical_json(details), None, None))
                result = self._metadata(self._owned(db, actor, session_id, attachment_id))
        except BaseException:
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise
        return result

    def list(self, actor: IdentityContext, session_id: str) -> dict:
        self._authorize(actor, session_id)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM research_attachments WHERE owner_user_id=? AND scope_id=? AND session_id=? ORDER BY created_at,attachment_id",
                              (actor.owner_user_id, actor.scope_id, session_id)).fetchall()
            return {"contract": ATTACHMENT_CONTRACT, "items": [self._metadata(row) for row in rows],
                    "limits": {"max_file_bytes": self.max_file_bytes, "max_files": self.max_files, "max_total_bytes": self.max_total_bytes},
                    "total_bytes": sum(row["byte_size"] for row in rows)}

    def get(self, actor: IdentityContext, session_id: str, attachment_id: str) -> dict:
        self._authorize(actor, session_id)
        with self._connect() as db:
            return self._metadata(self._owned(db, actor, session_id, attachment_id))

    def read_text(self, actor: IdentityContext, session_id: str, attachment_id: str, *, offset: int = 0, limit: int = 20000) -> dict:
        self._authorize(actor, session_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100000:
            raise AgentError("invalid_request", "附件文字分页参数无效")
        with self._connect() as db:
            row = self._owned(db, actor, session_id, attachment_id)
            if row["status"] != "ready":
                raise AgentError("attachment_not_ready", "附件尚未完成解析，暂时不能读取内容", http_status=409, retryable=row["status"] in {"pending", "parsing"})
            text = self._read(row, extracted=True).decode("utf-8")
            if len(text) != row["text_chars"] or offset > len(text):
                raise AgentError("invalid_request", "附件文字位置超出范围")
            end = min(len(text), offset + limit)
            details = json.loads(row["parse_details"])
            locators = [locator for locator in details.get("locators", []) if locator["start"] < end and locator["end"] > offset]
            return {"contract": ATTACHMENT_CONTRACT, "attachment": self._metadata(row), "text": text[offset:end],
                    "offset": offset, "next_offset": end if end < len(text) else None, "total_chars": len(text),
                    "start_line": text.count("\n", 0, offset) + 1, "end_line": text.count("\n", 0, end) + 1,
                    "locators": locators, "text_sha256": row["text_sha256"]}

    def download_path(self, actor: IdentityContext, session_id: str, attachment_id: str) -> Path:
        """Trusted route only: keep the filename from get(), never path.name, in UI."""
        self._authorize(actor, session_id)
        with self._connect() as db:
            row = self._owned(db, actor, session_id, attachment_id)
            self._read(row)
            return self._path(attachment_id)

    def image_analysis(self, actor, session_id, attachment_id, cache_key, *, value=None):
        """Reuse a trusted reader result on this immutable, owned attachment.

        Kept in existing parser metadata, excluded from public attachment lists.
        Source snapshots retain the exact interpretation cited by each answer.
        """
        self._authorize(actor, session_id)
        with self._connect() as db:
            row = self._owned(db, actor, session_id, attachment_id)
            if row["status"] != "ready" or row["media_type"] not in {"image/png", "image/jpeg", "image/webp"}:
                raise AgentError("attachment_not_ready", "图片尚未完成校验", http_status=409)
            self._read(row)
            details = json.loads(row["parse_details"])
            if value is not None:
                if value.get("source_sha256") != row["sha256"] or value.get("cache_key") != cache_key:
                    raise AgentError("attachment_changed", "图片版本与识别结果不一致", http_status=409)
                details["image_analysis"] = value
                db.execute("UPDATE research_attachments SET parse_details=? WHERE attachment_id=?", (canonical_json(details), attachment_id))
            cached = details.get("image_analysis") or {}
            return cached if cached.get("cache_key") == cache_key and cached.get("source_sha256") == row["sha256"] else None

    def parse_pending(self, actor: IdentityContext, session_id: str, attachment_id: str, *, retry: bool = False) -> dict:
        """Dispatch binary extraction to the injected isolated worker adapter.

        The caller schedules this outside the HTTP request worker. Failed parses
        preserve the original attachment and require an explicit retry.
        """
        self._authorize(actor, session_id)
        with _PARSE_LOCK:
            with self._connect() as db:
                row = self._owned(db, actor, session_id, attachment_id)
                if row["status"] in {"ready", "parsing"} or (row["status"] == "failed" and not retry) or self.parser is None:
                    return self._metadata(row)
                self._read(row)
                attempt_id = new_id("parse")
                db.execute("UPDATE research_attachments SET status='parsing',error_code=NULL,parse_attempt_id=? WHERE attachment_id=?", (attempt_id, attachment_id))
                request = {"contract": "wenquan-isolated-attachment-parse/v1", "attachment_id": attachment_id,
                           "input_path": str(self._path(attachment_id)), "source_sha256": row["sha256"],
                           "filename": row["filename"], "media_type": row["media_type"],
                           "limits": {"max_output_chars": self.max_text_chars, "max_output_bytes": self.max_text_chars * 4,
                                      "timeout_seconds": 30, "memory_bytes": 512 * MIB, "max_archive_entries": 1000,
                                      "max_expanded_bytes": 100 * MIB, "network": False, "macros": False, "evaluate_formulas": False}}
            active = (str(self.root.resolve()), attempt_id)
            _ACTIVE_PARSES.add(active)
        text_path = self._path(attachment_id, extracted=True)
        try:
            parsed = self.parser.dispatch(request)
            if (not isinstance(parsed, dict) or set(parsed) - {"text", "parser_version", "locators", "source_sha256"}
                    or not isinstance(parsed.get("text"), str) or parsed.get("source_sha256") != request["source_sha256"]):
                raise ValueError("invalid isolated parse result")
            text = parsed["text"]
            version = parsed.get("parser_version")
            locators = parsed.get("locators", [])
            if (len(text) > self.max_text_chars or "\x00" in text or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", version)
                    or not isinstance(locators, list) or len(locators) > 10000):
                raise ValueError("invalid isolated parse content")
            for location in locators:
                if (not isinstance(location, dict) or set(location) - {"start", "end", "page", "sheet", "row", "column"}
                        or type(location.get("start")) is not int or type(location.get("end")) is not int
                        or not 0 <= location["start"] < location["end"] <= len(text)
                        or any(type(location[key]) is not int or location[key] < 1 for key in ("page", "row", "column") if key in location)
                        or ("sheet" in location and (not isinstance(location["sheet"], str) or len(location["sheet"]) > 100))):
                    raise ValueError("invalid isolated parse locator")
            with self._connect() as db:
                current = self._owned(db, actor, session_id, attachment_id)
                if current["parse_attempt_id"] != attempt_id or current["status"] != "parsing":
                    return self._metadata(current)
                self._read(current)
                encoded = text.encode()
                self._write(text_path, encoded)
                details = {"locators": locators, "line_count": len(text.splitlines()), "isolated_backend": self.parser.backend_id}
                db.execute("UPDATE research_attachments SET status='ready',text_sha256=?,text_chars=?,parser_version=?,parse_details=?,error_code=NULL WHERE attachment_id=?",
                           (_sha(encoded), len(text), version, canonical_json(details), attachment_id))
                return self._metadata(self._owned(db, actor, session_id, attachment_id))
        except Exception:
            # Parser exceptions may contain source text or environment secrets.
            # The API gets a stable code, never their repr/traceback.
            with self._connect() as db:
                current = self._owned(db, actor, session_id, attachment_id)
                if current["parse_attempt_id"] == attempt_id and current["status"] == "parsing":
                    text_path.unlink(missing_ok=True)
                    db.execute("UPDATE research_attachments SET status='failed',error_code='attachment_parse_failed' WHERE attachment_id=?", (attachment_id,))
                return self._metadata(self._owned(db, actor, session_id, attachment_id))
        finally:
            with _PARSE_LOCK:
                _ACTIVE_PARSES.discard(active)
