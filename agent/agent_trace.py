"""Owner-private Agent diagnostics, separate from identity and account audit logs.

Callers must bind session/run IDs to the authenticated session before invoking this
module. This store independently enforces owner_user_id on every content access;
an administrator role is deliberately not an alternative permission.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .contracts import AgentError, IdentityContext, canonical_json, new_id, sha256_value


TRACE_CONTRACT = "wenquan-agent-trace/v1"
EVENT_CONTRACT = "wenquan-agent-event/v1"
REDACTED = "[redacted]"
EVENT_FAMILIES = frozenset({"model", "tool", "data", "source", "answer", "feedback", "error", "lifecycle", "research", "eval", "custom", "budget"})
FINAL_STATUSES = frozenset({"completed", "partial", "blocked", "failed", "cancelled"})
_CONTENT_KEYS = frozenset({"question", "answer", "messages", "message", "prompt", "completion", "content", "body", "text", "raw", "rawresponse", "responsebody", "fulltext", "reviewtext", "comment"})
_NEVER_KEYS = frozenset({"reasoning", "reasoningcontent", "thinking", "thinkingcontent", "analysis", "chainofthought", "headers", "requestheaders", "responseheaders"})
_USAGE_KEYS = frozenset({"prompttokens", "completiontokens", "totaltokens", "inputtokens", "outputtokens", "cachedtokens", "reasoningtokens", "maxtokens", "tokencount", "tokens"})
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_SECRET_TEXT = re.compile(r"(?i)(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|password|passwd|client[ _-]?secret|authorization|cookie|set-cookie)[\"']?\s*[=:][^\r\n]*")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_KEY_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b")
_PEM = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)")


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _sensitive_key(value: str) -> bool:
    key = _key(value)
    return key not in _USAGE_KEYS and (key in {"key", "token", "pwd", "secret"} or any(
        part in key for part in ("password", "passwd", "apikey", "authorization", "cookie", "credential", "privatekey", "signingkey", "clientsecret", "accesstoken", "refreshtoken", "idtoken", "bearertoken")
    ) or key.endswith(("secret", "token")))


def _never_content(value: str) -> bool:
    key = _key(value)
    return (key in _NEVER_KEYS or "chainofthought" in key
            or ("reasoning" in key and key not in {"reasoningeffort", "reasoningtokens", "reasoningtokenbudget"})
            or ("thinking" in key and key not in {"enablethinking", "thinkingmode"}))


def _clean_text(value: str) -> str:
    def clean_url(match):
        try:
            parts = urlsplit(match.group())
            host = parts.hostname or ""
            if ":" in host:
                host = "[" + host + "]"
            if parts.port:
                host += ":" + str(parts.port)
            # Query values and fragments can contain unlabelled credentials.
            return urlunsplit((parts.scheme, host, parts.path, "[redacted]" if parts.query else "", ""))
        except ValueError:
            return "[redacted_url]"
    value = _PEM.sub(REDACTED, value)
    value = _URL.sub(clean_url, value)
    value = _BEARER.sub(REDACTED, value)
    value = _SECRET_TEXT.sub(REDACTED, value)
    return _KEY_TOKEN.sub(REDACTED, value)


def text_summary(value: str) -> dict[str, Any]:
    """No excerpt by default: even a short question can contain private text."""
    clean = _clean_text(value)
    return {"chars": len(clean), "sha256": "sha256:" + hashlib.sha256(clean.encode()).hexdigest(), "content": "omitted"}


def sanitize_log_value(value: Any, *, include_content: bool = False, max_text: int = 512,
                       max_items: int = 100, max_depth: int = 10) -> Any:
    """Bounded recursive JSON projection. Credentials/reasoning are never kept.

    This cannot identify an arbitrary unlabelled secret. Callers must not send
    credential-bearing objects; recognizable credential fields/text are removed
    as a second boundary, including for explicitly enabled payload artifacts.
    """
    active: set[int] = set()
    remaining = [max_items * max_depth]

    def visit(item, depth):
        remaining[0] -= 1
        if depth > max_depth or remaining[0] < 0:
            return "[omitted: structure limit]"
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else "[invalid number]"
        if isinstance(item, str):
            clean = _clean_text(item)
            return clean if len(clean) <= max_text else clean[:max_text] + "[truncated]"
        if isinstance(item, (dict, list, tuple)):
            if id(item) in active:
                return "[omitted: cycle]"
            active.add(id(item))
            try:
                if isinstance(item, dict):
                    block_type = item.get("type", item.get("kind", ""))
                    if isinstance(block_type, str) and any(part in block_type.casefold() for part in ("reasoning", "thinking", "chain_of_thought")):
                        return {"type": "private_model_block", "content": "omitted"}
                    result = {}
                    for index, (raw_key, child) in enumerate(item.items()):
                        if index >= max_items:
                            result["_omitted_fields"] = len(item) - index
                            break
                        if not isinstance(raw_key, str):
                            continue
                        name = _clean_text(raw_key)[:120]
                        if _sensitive_key(raw_key) or _never_content(raw_key):
                            result[name] = REDACTED
                        elif _key(raw_key) in _CONTENT_KEYS and not include_content:
                            result[name] = text_summary(child) if isinstance(child, str) else {"content": "omitted"}
                        else:
                            result[name] = visit(child, depth + 1)
                    return result
                result = [visit(child, depth + 1) for child in item[:max_items]]
                if len(item) > max_items:
                    result.append({"_omitted_items": len(item) - max_items})
                return result
            finally:
                active.remove(id(item))
        return "[omitted: unsupported type]"
    return visit(value, 0)


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}", value) or _clean_text(value) != value:
        raise AgentError("invalid_request", f"{name} 无效")
    return value


class AgentTraceStore:
    def __init__(self, database_path: Path | str, *, content_enabled: bool = False,
                 trace_retention_seconds: int = 30 * 86400, payload_retention_seconds: int = 7 * 86400,
                 max_payload_bytes: int = 262144, max_event_bytes: int = 32768,
                 clock: Callable[[], float] = time.time):
        if (type(content_enabled) is not bool or min(trace_retention_seconds, payload_retention_seconds,
                                                    max_payload_bytes, max_event_bytes) <= 0):
            raise AgentError("trace_config_invalid", "AI 研究日志配置无效")
        self.database_path = Path(os.path.abspath(Path(database_path).expanduser()))
        self.payload_dir = self.database_path.parent / (self.database_path.name + ".payloads")
        self.content_enabled = content_enabled
        self.trace_retention_seconds = trace_retention_seconds
        self.payload_retention_seconds = min(payload_retention_seconds, trace_retention_seconds)
        self.max_payload_bytes = max_payload_bytes
        self.max_event_bytes = max_event_bytes
        self.clock = clock
        self._lock = threading.RLock()

    @staticmethod
    def _private(path: Path, *, directory: bool = False, create: bool = False):
        if create:
            if directory:
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif not path.exists():
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                except FileExistsError:
                    pass  # Another initializing store, or a rejected symlink below.
                else:
                    os.close(fd)
        info = path.lstat()
        wanted = stat.S_ISDIR if directory else stat.S_ISREG
        # Reject a shared parent instead of chmod-ing an unrelated /tmp or /data.
        if not wanted(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
            raise AgentError("trace_storage_invalid", "AI 研究日志需要独立的私有目录和文件", http_status=503)
        return info

    @contextmanager
    def _connect(self):
        with self._lock:
            self._private(self.database_path.parent, directory=True)
            self._private(self.database_path)
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = Path(str(self.database_path) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    self._private(sidecar)
            connection = sqlite3.connect(self.database_path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        with self._lock:
            self._private(self.database_path.parent, directory=True, create=True)
            self._private(self.database_path, create=True)
            self._private(self.payload_dir, directory=True, create=True)
            with self._connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS agent_traces (
                        trace_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                        session_id TEXT NOT NULL, run_id TEXT NOT NULL, created_at REAL NOT NULL,
                        expires_at REAL NOT NULL, status TEXT NOT NULL, cancelled_at REAL,
                        finished_at REAL, latest_attempt_id TEXT, question TEXT NOT NULL,
                        answer TEXT, model_config TEXT NOT NULL, data_manifest TEXT NOT NULL,
                        start_hash TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(owner_user_id, session_id, run_id));
                    CREATE TABLE IF NOT EXISTS agent_trace_events (
                        trace_id TEXT NOT NULL REFERENCES agent_traces(trace_id) ON DELETE CASCADE,
                        seq INTEGER NOT NULL, event_id TEXT NOT NULL, event_type TEXT NOT NULL,
                        attempt_id TEXT, received_at REAL NOT NULL, disposition TEXT NOT NULL,
                        data TEXT NOT NULL, payload_ref TEXT, payload_status TEXT NOT NULL,
                        event_hash TEXT NOT NULL, PRIMARY KEY(trace_id, seq), UNIQUE(trace_id, event_id));
                    CREATE TABLE IF NOT EXISTS agent_trace_payloads (
                        payload_id TEXT PRIMARY KEY,
                        trace_id TEXT NOT NULL REFERENCES agent_traces(trace_id) ON DELETE CASCADE,
                        sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, expires_at REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS agent_trace_feedback (
                        trace_id TEXT NOT NULL REFERENCES agent_traces(trace_id) ON DELETE CASCADE,
                        feedback_id TEXT NOT NULL, actor_id TEXT NOT NULL, received_at REAL NOT NULL,
                        data TEXT NOT NULL, PRIMARY KEY(trace_id, feedback_id));
                    CREATE INDEX IF NOT EXISTS agent_trace_owner ON agent_traces(owner_user_id, created_at);
                """)

    def _owned(self, db, actor: IdentityContext, trace_id: str):
        if not isinstance(actor, IdentityContext):
            raise AgentError("identity_invalid", "需要已验证的用户身份", http_status=401)
        row = db.execute("SELECT * FROM agent_traces WHERE trace_id=? AND owner_user_id=? AND expires_at>?",
                         (trace_id, actor.owner_user_id, self.clock())).fetchone()
        if row is None:
            raise AgentError("trace_not_found", "没有找到这次AI 研究日志", http_status=404)
        return row

    @staticmethod
    def _trace(row):
        result = dict(row)
        result.pop("start_hash", None)
        for key in ("question", "answer", "model_config", "data_manifest"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        result.update(contract=TRACE_CONTRACT, final_status=result["status"] if result["status"] in FINAL_STATUSES else None)
        return result

    @staticmethod
    def _event(row):
        result = dict(row)
        result.pop("event_hash", None)
        result["data"] = json.loads(result["data"])
        result.update(contract=EVENT_CONTRACT, late_after_cancel=result["disposition"] == "late_after_cancel")
        return result

    def _payload(self, db, trace_id, value):
        if value is None:
            return None, "not_requested"
        if not self.content_enabled:
            return None, "disabled"
        try:
            original_size = len(canonical_json(value).encode())
        except (TypeError, ValueError, RecursionError):
            return None, "omitted_non_json"
        if original_size > self.max_payload_bytes:
            return None, "omitted_size_limit"
        clean = sanitize_log_value(value, include_content=True, max_text=self.max_payload_bytes,
                                   max_items=self.max_payload_bytes, max_depth=20)
        encoded = canonical_json(clean).encode()
        if len(encoded) > self.max_payload_bytes:
            return None, "omitted_size_limit"
        self._private(self.payload_dir, directory=True)
        payload_id = new_id("pay")
        path = self.payload_dir / (payload_id + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            db.execute("INSERT INTO agent_trace_payloads VALUES(?,?,?,?,?)", (payload_id, trace_id,
                       "sha256:" + hashlib.sha256(encoded).hexdigest(), len(encoded), self.clock() + self.payload_retention_seconds))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return payload_id, "available"

    def _append(self, db, actor, trace_id, *, event_id, event_type, attempt_id=None, data=None, payload=None):
        row = self._owned(db, actor, trace_id)
        _identifier(event_id, "event_id")
        if not isinstance(event_type, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", event_type) or event_type.split(".")[0] not in EVENT_FAMILIES:
            raise AgentError("invalid_request", "日志事件类型无效")
        if attempt_id is not None:
            _identifier(attempt_id, "attempt_id")
        if data is not None and not isinstance(data, dict):
            raise AgentError("invalid_request", "日志事件 data 必须是对象")
        clean = sanitize_log_value(data or {})
        if len(canonical_json(clean).encode()) > self.max_event_bytes:
            clean = {"summary": "事件摘要超过大小限制", "data_sha256": sha256_value(clean), "omitted": True}
        # Compare redacted content, not raw secrets, when checking idempotency.
        payload_hash = sha256_value(sanitize_log_value(payload, include_content=True, max_text=self.max_payload_bytes,
                                                     max_items=self.max_payload_bytes, max_depth=20)) if payload is not None else None
        fingerprint = sha256_value({"type": event_type, "attempt": attempt_id, "data": clean, "payload": payload_hash})
        old = db.execute("SELECT * FROM agent_trace_events WHERE trace_id=? AND event_id=?", (trace_id, event_id)).fetchone()
        if old:
            if old["event_hash"] != fingerprint:
                raise AgentError("trace_event_conflict", "同一日志事件标识对应不同内容", http_status=409)
            return self._event(old)
        stale_attempt = attempt_id is not None and row["latest_attempt_id"] is not None and attempt_id != row["latest_attempt_id"]
        disposition = ("late_after_cancel" if row["status"] == "cancelled" else "late_attempt" if stale_attempt
                       else "late_after_finish" if row["status"] in FINAL_STATUSES else "active")
        if event_type.startswith("feedback."):
            disposition = "feedback"
        payload_ref, payload_status = self._payload(db, trace_id, payload)
        seq = row["last_seq"] + 1
        db.execute("INSERT INTO agent_trace_events VALUES(?,?,?,?,?,?,?,?,?,?,?)", (trace_id, seq, event_id, event_type,
                   attempt_id, self.clock(), disposition, canonical_json(clean), payload_ref, payload_status, fingerprint))
        db.execute("UPDATE agent_traces SET last_seq=?, latest_attempt_id=COALESCE(?,latest_attempt_id) WHERE trace_id=?",
                   (seq, attempt_id if disposition == "active" else None, trace_id))
        return self._event(db.execute("SELECT * FROM agent_trace_events WHERE trace_id=? AND seq=?", (trace_id, seq)).fetchone())

    def start_trace(self, actor: IdentityContext, *, session_id: str, run_id: str, question: str,
                    model_config=None, data_manifest=None, trace_id: str | None = None) -> dict:
        _identifier(session_id, "session_id"); _identifier(run_id, "run_id")
        if not isinstance(actor, IdentityContext) or not isinstance(question, str):
            raise AgentError("invalid_request", "日志需要用户身份和问题")
        config = sanitize_log_value(model_config or {})
        manifest = sanitize_log_value(data_manifest or [])
        summary = text_summary(question)
        fingerprint = sha256_value({"question": summary, "model_config": config, "data_manifest": manifest})
        with self._connect() as db:
            old = db.execute("SELECT * FROM agent_traces WHERE owner_user_id=? AND session_id=? AND run_id=?", (actor.owner_user_id, session_id, run_id)).fetchone()
            if old:
                if old["start_hash"] != fingerprint or old["expires_at"] <= self.clock():
                    raise AgentError("trace_conflict", "运行日志已存在或已经过期", http_status=409)
                return self._trace(old)
            trace_id = _identifier(trace_id or new_id("trace"), "trace_id")
            if db.execute("SELECT 1 FROM agent_traces WHERE trace_id=?", (trace_id,)).fetchone():
                raise AgentError("trace_conflict", "运行日志标识不可复用", http_status=409)
            now = self.clock()
            db.execute("INSERT INTO agent_traces(trace_id,owner_user_id,actor_id,session_id,run_id,created_at,expires_at,status,question,model_config,data_manifest,start_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (trace_id, actor.owner_user_id, actor.actor_id, session_id, run_id, now, now + self.trace_retention_seconds,
                        "running", canonical_json(summary), canonical_json(config), canonical_json(manifest), fingerprint))
            event = self._append(db, actor, trace_id, event_id="start", event_type="lifecycle.started", data={"model_config": config, "data_manifest": manifest}, payload={"question": question})
            summary.update(payload_ref=event["payload_ref"], payload_status=event["payload_status"])
            db.execute("UPDATE agent_traces SET question=? WHERE trace_id=?", (canonical_json(summary), trace_id))
            return self._trace(self._owned(db, actor, trace_id))

    def append_event(self, actor: IdentityContext, trace_id: str, *, event_id: str, event_type: str,
                     attempt_id: str | None = None, data=None, payload=None) -> dict:
        with self._connect() as db:
            return self._append(db, actor, trace_id, event_id=event_id, event_type=event_type, attempt_id=attempt_id, data=data, payload=payload)

    def finish_trace(self, actor: IdentityContext, trace_id: str, *, status: str, answer: str = "",
                     error=None, event_id: str | None = None, attempt_id: str | None = None) -> dict:
        if status not in FINAL_STATUSES or not isinstance(answer, str):
            raise AgentError("invalid_request", "日志结束状态或回答无效")
        with self._connect() as db:
            row = self._owned(db, actor, trace_id)
            attempt_id = attempt_id or row["latest_attempt_id"]
            event_id = event_id or "finish:" + (attempt_id or "initial")
            event = self._append(db, actor, trace_id, event_id=event_id, event_type="lifecycle.finished", attempt_id=attempt_id,
                                 data={"status": status, "answer": answer, "error": error}, payload={"answer": answer})
            current_attempt = row["latest_attempt_id"] is None or row["latest_attempt_id"] == attempt_id
            if row["status"] not in FINAL_STATUSES and event["disposition"] == "active" and current_attempt:
                summary = text_summary(answer)
                summary.update(payload_ref=event["payload_ref"], payload_status=event["payload_status"], attempt_id=attempt_id)
                db.execute("UPDATE agent_traces SET status=?,answer=?,finished_at=?,cancelled_at=? WHERE trace_id=?",
                           (status, canonical_json(summary), self.clock(), self.clock() if status == "cancelled" else None, trace_id))
            return self._trace(self._owned(db, actor, trace_id))

    def cancel_trace(self, actor: IdentityContext, trace_id: str, *, event_id: str | None = None, attempt_id: str | None = None) -> dict:
        return self.finish_trace(actor, trace_id, status="cancelled", event_id=event_id, attempt_id=attempt_id)

    def resume_trace(self, actor: IdentityContext, trace_id: str, *, attempt_id: str, event_id: str | None = None) -> dict:
        """Called only after the research state accepts a new attempt."""
        _identifier(attempt_id, "attempt_id")
        event_id = event_id or "resume:" + attempt_id
        with self._connect() as db:
            row = self._owned(db, actor, trace_id)
            previous = db.execute("SELECT attempt_id FROM agent_trace_events WHERE trace_id=? AND event_id=?", (trace_id, event_id)).fetchone()
            if previous:
                if previous["attempt_id"] != attempt_id:
                    raise AgentError("trace_event_conflict", "日志恢复标识已用于不同执行", http_status=409)
                return self._trace(row)
            if row["status"] == "completed" or row["latest_attempt_id"] == attempt_id:
                raise AgentError("trace_resume_invalid", "研究日志需要新的可恢复执行", http_status=409)
            db.execute("UPDATE agent_traces SET status='running',cancelled_at=NULL,finished_at=NULL,latest_attempt_id=? WHERE trace_id=?", (attempt_id, trace_id))
            self._append(db, actor, trace_id, event_id=event_id, event_type="lifecycle.resumed", attempt_id=attempt_id,
                         data={"previous_attempt_id": row["latest_attempt_id"]})
            return self._trace(self._owned(db, actor, trace_id))

    def record_feedback(self, actor: IdentityContext, trace_id: str, *, feedback_id: str, rating: str,
                        labels: list[str] | None = None, comment: str = "") -> dict:
        _identifier(feedback_id, "feedback_id")
        if rating not in {"helpful", "unhelpful", "neutral"} or not isinstance(comment, str) or not isinstance(labels or [], list) or any(not isinstance(label, str) or len(label) > 100 for label in labels or []):
            raise AgentError("invalid_request", "人工反馈格式无效")
        clean = {"provenance": "user_submitted", "rating": rating, "labels": sanitize_log_value(labels or []), "comment": text_summary(comment)}
        with self._connect() as db:
            self._owned(db, actor, trace_id)
            event = self._append(db, actor, trace_id, event_id="feedback:" + feedback_id, event_type="feedback.user_submitted",
                                 data=clean, payload={"comment": comment})
            clean["comment"].update(payload_ref=event["payload_ref"], payload_status=event["payload_status"])
            db.execute("INSERT OR IGNORE INTO agent_trace_feedback VALUES(?,?,?,?,?)", (trace_id, feedback_id, actor.actor_id, event["received_at"], canonical_json(clean)))
            return {"feedback_id": feedback_id, "actor_id": actor.actor_id, "received_at": event["received_at"], **clean}

    def get_trace(self, actor: IdentityContext, trace_id: str) -> dict:
        with self._connect() as db:
            result = self._trace(self._owned(db, actor, trace_id))
            result["human_feedback"] = [{"feedback_id": row["feedback_id"], "actor_id": row["actor_id"], "received_at": row["received_at"], **json.loads(row["data"])}
                                        for row in db.execute("SELECT * FROM agent_trace_feedback WHERE trace_id=? ORDER BY received_at,feedback_id", (trace_id,))]
            return result

    def list_traces(self, actor: IdentityContext, *, session_id: str | None = None, run_id: str | None = None,
                    limit: int = 50, offset: int = 0) -> dict:
        if not isinstance(actor, IdentityContext):
            raise AgentError("identity_invalid", "需要已验证的用户身份", http_status=401)
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise AgentError("invalid_request", "日志分页参数无效")
        query = "owner_user_id=? AND expires_at>?"
        params: list[Any] = [actor.owner_user_id, self.clock()]
        for name, value in (("session_id", session_id), ("run_id", run_id)):
            if value is not None:
                query += " AND " + name + "=?"
                params.append(value)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM agent_traces WHERE " + query + " ORDER BY created_at DESC,trace_id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            total = db.execute("SELECT count(*) FROM agent_traces WHERE " + query, params).fetchone()[0]
            return {"contract": TRACE_CONTRACT, "items": [self._trace(row) for row in rows], "total": total, "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def list_events(self, actor: IdentityContext, trace_id: str, *, after_seq: int = 0, limit: int = 200) -> dict:
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise AgentError("invalid_request", "日志事件分页参数无效")
        with self._connect() as db:
            trace = self._owned(db, actor, trace_id)
            rows = db.execute("SELECT * FROM agent_trace_events WHERE trace_id=? AND seq>? ORDER BY seq LIMIT ?", (trace_id, after_seq, limit)).fetchall()
            return {"contract": EVENT_CONTRACT, "trace_id": trace_id, "items": [self._event(row) for row in rows],
                    "last_seq": trace["last_seq"], "next_after_seq": rows[-1]["seq"] if rows and rows[-1]["seq"] < trace["last_seq"] else None}

    def read_payload(self, actor: IdentityContext, trace_id: str, payload_id: str, *, include_content: bool = False) -> dict:
        with self._connect() as db:
            self._owned(db, actor, trace_id)
            if include_content is not True:
                raise AgentError("trace_content_opt_in_required", "读取完整日志内容需要明确选择", http_status=403)
            row = db.execute("SELECT * FROM agent_trace_payloads WHERE trace_id=? AND payload_id=? AND expires_at>?", (trace_id, payload_id, self.clock())).fetchone()
            if row is None:
                raise AgentError("trace_payload_unavailable", "完整日志内容未保存或已过期", http_status=404)
            self._private(self.payload_dir, directory=True)
            path = self.payload_dir / (row["payload_id"] + ".json")
            try:
                info = self._private(path)
            except FileNotFoundError:
                raise AgentError("trace_payload_unavailable", "完整日志内容未保存或已过期", http_status=404) from None
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                    raise AgentError("trace_storage_invalid", "日志内容文件发生变化", http_status=503)
                raw = stream.read(self.max_payload_bytes + 1)
            if len(raw) != row["byte_size"] or len(raw) > self.max_payload_bytes or "sha256:" + hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise AgentError("trace_payload_changed", "日志内容校验失败", http_status=409)
            return {"payload_id": payload_id, "sha256": row["sha256"], "expires_at": row["expires_at"], "data": json.loads(raw)}

    def purge_expired(self) -> dict:
        """Server maintenance only; returns counts, never another owner's data."""
        with self._connect() as db:
            now = self.clock()
            rows = db.execute("SELECT payload_id FROM agent_trace_payloads WHERE expires_at<=? OR trace_id IN (SELECT trace_id FROM agent_traces WHERE expires_at<=?)", (now, now)).fetchall()
            self._private(self.payload_dir, directory=True)
            for row in rows:
                (self.payload_dir / (row["payload_id"] + ".json")).unlink(missing_ok=True)
            payload_count = db.execute("DELETE FROM agent_trace_payloads WHERE expires_at<=? OR trace_id IN (SELECT trace_id FROM agent_traces WHERE expires_at<=?)", (now, now)).rowcount
            trace_count = db.execute("DELETE FROM agent_traces WHERE expires_at<=?", (now,)).rowcount
            # A process interruption between file creation and DB commit can
            # leave an orphan; remove only this store's generated filenames.
            known = {row[0] + ".json" for row in db.execute("SELECT payload_id FROM agent_trace_payloads")}
            orphan_count = 0
            for path in self.payload_dir.iterdir():
                if re.fullmatch(r"pay_[a-f0-9]{24}\.json", path.name) and path.name not in known:
                    path.unlink(); orphan_count += 1
            return {"traces_deleted": trace_count, "payloads_deleted": payload_count, "orphan_payloads_deleted": orphan_count}
