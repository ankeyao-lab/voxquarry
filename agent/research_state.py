"""Owner-private durable state for Harness research, alongside legacy Wenquan runs.

The caller validates research tools/inputs and freezes a server ModelRegistry
selection before accept(). Every worker mutation must carry its attempt_id.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from typing import Any

from .agent_trace import sanitize_log_value
from .contracts import AgentError, IdentityContext, canonical_json, new_id, normalize_text, sha256_value, utc_now
from .model_registry import ModelRegistry
from .research_limits import ResearchCapacity
from .store import STEP_STATUSES, WenquanStore
from .review_citation_resolution import entity_ids, resolve_entities


STATE_CONTRACT = "wenquan-harness-research/v1"
_WAITING = frozenset({"waiting_collection", "waiting_attachments", "waiting_data"})
_FINAL = frozenset({"completed", "partial", "blocked", "failed", "cancelled"})
_RESUMABLE = (_FINAL - {"completed"}) | _WAITING
ANSWER_REPAIR_CODES = frozenset({"citation_unknown", "citation_missing", "citation_requires_original_source", "answer_numeric_relation_conflict"})


def recovery_checkpoint(checkpoint, attempt_id, code):
    """Bounded continuation of one run, never an unbounded replay of a tool."""
    known = {"model_stream_interrupted", "model_connection_failed", "model_rate_limited",
             "model_timeout", "harness_unavailable", "research_interrupted"}
    generic = {"harness_execution_failed", "research_failed"}
    previous = checkpoint.get("auto_recovery", {})
    codes = previous.get("codes", [])
    if (checkpoint.get("resume_disabled") or code not in known | generic | ANSWER_REPAIR_CODES
            or len(codes) >= 2 or code in generic and any(item in generic for item in codes)
            or code in ANSWER_REPAIR_CODES and any(item in ANSWER_REPAIR_CODES for item in codes)):
        return None
    return {**checkpoint, "auto_recovery": {"attempt_id": attempt_id, "pending": True, "codes": [*codes, code]}}


def _json_value(value, *, maximum=2 * 1024 * 1024):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > maximum:
            raise ValueError("size limit")
        return json.loads(encoded)
    except (ValueError, TypeError, RecursionError):
        raise AgentError("invalid_request", "研究记录不是受支持大小的 JSON") from None


def _id(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}", value):
        raise AgentError("invalid_request", f"{label} 无效")
    return value


class ResearchState:
    def __init__(self, store: WenquanStore, registry: ModelRegistry, *, capacity=None):
        self.store, self.registry = store, registry
        self.capacity = capacity or ResearchCapacity.from_environment()
        self._lock = threading.RLock()

    @contextmanager
    def _connect(self):
        with self._lock:
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
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS research_session_settings (
                session_id TEXT PRIMARY KEY REFERENCES sessions(session_id), owner_user_id TEXT NOT NULL,
                scope_id TEXT NOT NULL, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS research_runs (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id), session_id TEXT NOT NULL REFERENCES sessions(session_id),
                owner_user_id TEXT NOT NULL, scope_id TEXT NOT NULL, request_id TEXT NOT NULL,
                request_hash TEXT NOT NULL, request_json TEXT NOT NULL, settings_json TEXT NOT NULL,
                attempt_id TEXT NOT NULL, attempt_number INTEGER NOT NULL, checkpoint_json TEXT NOT NULL,
                trace_id TEXT, actor_snapshot_json TEXT NOT NULL,
                UNIQUE(owner_user_id,scope_id,session_id,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS research_attempts (
                attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES research_runs(run_id),
                attempt_number INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, finish_hash TEXT, UNIQUE(run_id,attempt_number))""")
            db.execute("""CREATE TABLE IF NOT EXISTS research_events (
                run_id TEXT NOT NULL REFERENCES research_runs(run_id), attempt_id TEXT NOT NULL,
                event_id TEXT NOT NULL, step_id TEXT NOT NULL REFERENCES steps(step_id),
                PRIMARY KEY(run_id,attempt_id,event_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS research_resumes (
                run_id TEXT NOT NULL REFERENCES research_runs(run_id), request_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL REFERENCES research_attempts(attempt_id), created_at TEXT NOT NULL,
                PRIMARY KEY(run_id,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS research_sources (
                source_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES research_runs(run_id),
                session_id TEXT NOT NULL, owner_user_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                source_hash TEXT NOT NULL, source_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(run_id,source_hash))""")

    def _session(self, actor, session_id):
        if not isinstance(actor, IdentityContext):
            raise AgentError("identity_invalid", "研究需要已验证的用户身份", http_status=401)
        return self.store.get_session(actor, session_id)

    @staticmethod
    def _owned(db, actor, run_id):
        if not isinstance(actor, IdentityContext):
            raise AgentError("identity_invalid", "研究需要已验证的用户身份", http_status=401)
        row = db.execute("""SELECT r.*,h.request_id,h.request_hash,h.request_json,h.settings_json,h.attempt_id,
                     h.attempt_number,h.checkpoint_json,h.trace_id FROM runs r
                     JOIN research_runs h ON r.run_id=h.run_id
                     JOIN sessions s ON r.session_id=s.session_id
                     WHERE r.run_id=? AND r.owner_user_id=? AND r.scope_id=?
                     AND h.owner_user_id=r.owner_user_id AND h.scope_id=r.scope_id AND h.session_id=r.session_id
                     AND s.owner_user_id=r.owner_user_id AND s.scope_id=r.scope_id""",
                         (run_id, actor.owner_user_id, actor.scope_id)).fetchone()
        if row is None:
            raise AgentError("run_not_found", "没有找到当前用户的研究", http_status=404)
        return row

    @staticmethod
    def _worker(row, attempt_id, *, allow_finished=False):
        if not attempt_id:
            raise AgentError("research_attempt_required", "研究写入需要对应的执行标识", http_status=409)
        if row["attempt_id"] != attempt_id:
            raise AgentError("research_attempt_stale", "这次执行已经被后续研究替代", http_status=409)
        if row["status"] == "cancelled":
            raise AgentError("research_cancelled", "研究已停止", http_status=409)
        if not allow_finished and row["status"] != "running":
            raise AgentError("research_not_running", "这次研究当前未在执行", http_status=409)

    @staticmethod
    def _actions(status, checkpoint=None):
        return [{"action": "continue_available", "label": "继续用现有资料分析"}] if status == "waiting_collection" and not (checkpoint or {}).get("resume_disabled") else []

    @staticmethod
    def _controls(status, checkpoint=None):
        return {"can_cancel": status == "running" or status in _WAITING,
                "can_resume": status in _RESUMABLE and not (checkpoint or {}).get("resume_disabled")
                and not (status == "waiting_data" and (checkpoint or {}).get("auto_recovery", {}).get("pending")), "can_feedback": status in _FINAL}

    def _admit(self, db, actor, session_id, *, exclude_run=None):
        session = db.execute("SELECT archived FROM sessions WHERE session_id=? AND owner_user_id=? AND scope_id=?",
                             (session_id, actor.owner_user_id, actor.scope_id)).fetchone()
        if session is None:
            raise AgentError("session_not_found", "没有找到当前用户的会话", http_status=404)
        if session["archived"]:
            raise AgentError("session_archived", "请先恢复这个会话再开始研究", http_status=409)
        rows = db.execute("SELECT run_id,session_id,owner_user_id FROM runs WHERE status='running' AND run_id<>?", (exclude_run or "",)).fetchall()
        if any(row["session_id"] == session_id for row in rows):
            raise AgentError("research_busy", "当前会话已有研究在执行，请先等待或停止", http_status=409)
        if len(rows) >= self.capacity.researches + self.capacity.queued:
            raise AgentError("research_capacity_exceeded", f"当前研究队列已满（{self.capacity.researches}项执行、最多{self.capacity.queued}项排队），请稍后再试", http_status=409, retryable=True)

    @staticmethod
    def _queue_snapshot(db):
        return [dict(r) for r in db.execute("""SELECT a.attempt_id,a.rowid AS ordinal,a.status,a.started_at
            FROM research_attempts a JOIN research_runs h ON h.attempt_id=a.attempt_id
            JOIN runs r ON r.run_id=h.run_id WHERE r.status='running' ORDER BY a.rowid""")]

    @staticmethod
    def _queue_info(attempt_id, snapshot):
        current = next((a for a in snapshot if a['attempt_id'] == attempt_id), None)
        if not current or current['status'] != 'queued':
            return {'queue_position': 0, 'queue_ahead': 0}
        waiting = sum(a['status'] == 'queued' and a['ordinal'] < current['ordinal'] for a in snapshot)
        active = sum(a['status'] == 'running' for a in snapshot)
        return {'queue_position': waiting + 1, 'queue_ahead': waiting + active,
                'queue_running_ahead': active, 'queue_waiting_ahead': waiting}

    def session_activity(self, actor, session_ids):
        """Batch owner-scoped projection; no contents or other users' task details."""
        if not session_ids:
            return {}
        result = {sid: {'execution_status': 'idle', 'activity_version': '', 'queue_ahead': 0} for sid in session_ids}
        with self.store._connect() as db:
            db.execute('BEGIN')
            queue = self._queue_snapshot(db)
            params = (actor.owner_user_id, actor.scope_id)
            rows = db.execute("""SELECT r.*,h.attempt_id,h.checkpoint_json FROM runs r
                LEFT JOIN research_runs h ON r.run_id=h.run_id
                JOIN sessions s ON s.session_id=r.session_id AND s.owner_user_id=r.owner_user_id AND s.scope_id=r.scope_id
                WHERE r.owner_user_id=? AND r.scope_id=? ORDER BY r.created_at,r.rowid""", params).fetchall()
            latest = {}
            for row in rows:
                if row['session_id'] in result:
                    prior = latest.get(row['session_id'])
                    if not prior or prior['status'] != 'running' or row['status'] == 'running':
                        latest[row['session_id']] = row
            messages = {r['session_id']: str(r['version']) for r in db.execute(
                "SELECT session_id,COUNT(*) AS version FROM messages WHERE owner_user_id=? AND scope_id=? AND role='assistant' GROUP BY session_id", params)}
            artifacts = {r['session_id']: str(r['version']) for r in db.execute(
                "SELECT session_id,COUNT(*) AS version FROM artifacts WHERE owner_user_id=? AND scope_id=? AND artifact_type='research_output' GROUP BY session_id", params)}
            steps = {}
            for r in db.execute("""SELECT run_id,sequence_no,status FROM steps WHERE owner_user_id=? AND scope_id=?
                    AND tool_name IS NOT NULL AND tool_name!='model' ORDER BY run_id,sequence_no""", params):
                steps[r['run_id']] = f"{r['sequence_no']}:{r['status']}"
            for sid, item in result.items():
                row = latest.get(sid)
                stamp = [messages.get(sid, '0'), artifacts.get(sid, '0')]
                if row:
                    queued = next((a for a in queue if a['attempt_id'] == row['attempt_id'] and a['status'] == 'queued'), None)
                    recovery = json.loads(row['checkpoint_json'] or '{}').get('auto_recovery', {})
                    status = 'queued' if queued else 'recovering' if row['status'] == 'waiting_data' and recovery.get('pending') else row['status']
                    item.update(execution_status=status, run_id=row['run_id'], **self._queue_info(row['attempt_id'], queue))
                    stamp += [row['run_id'], status, steps.get(row['run_id'], '')]
                item['activity_version'] = '|'.join(stamp) if row or stamp != ['0', '0'] else ''
        return result

    @staticmethod
    def _execution(db, row):
        attempt = db.execute("SELECT status,started_at,rowid FROM research_attempts WHERE attempt_id=?", (row["attempt_id"],)).fetchone()
        queued = row["status"] == "running" and attempt["status"] == "queued"
        recovery = json.loads(row["checkpoint_json"]).get("auto_recovery", {})
        recovering = row["status"] == "waiting_data" and recovery.get("pending")
        result = {"execution_status": "queued" if queued else "recovering" if recovering else row["status"],
                  **ResearchState._queue_info(row['attempt_id'], ResearchState._queue_snapshot(db))}
        if queued:
            result['queued_at'] = json.loads(row['checkpoint_json']).get('queued_at') or attempt['started_at']
        # Use the existing execution windows, which exclude queue and data waits.
        # State-only/legacy databases need not have the Harness budget tables.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='research_active_windows'").fetchone():
            now = datetime.now().astimezone().timestamp()
            active = 0
            for window in db.execute("""SELECT w.*,a.status,a.finished_at FROM research_active_windows w
                    JOIN research_attempts a ON a.attempt_id=w.attempt_id WHERE w.run_id=?""", (row["run_id"],)):
                end = window["stopped_at"]
                if end is None and window["status"] != "running":
                    stamp = row["updated_at"] if window["attempt_id"] == row["attempt_id"] else window["finished_at"]
                    end = datetime.fromisoformat(stamp).timestamp() if stamp else now
                active += max(0, (end if end is not None else now) - window["started_at"])
            measured = now if row["status"] == "running" and not queued else datetime.fromisoformat(row["updated_at"]).timestamp()
            result["timing"] = {"active_seconds": round(active, 3), "measured_at": measured}
        return result

    def enqueue(self, actor, run_id, *, attempt_id):
        with self._connect() as db:
            row = self._owned(db, actor, run_id)
            self._worker(row, attempt_id)
            db.execute("UPDATE research_attempts SET status='queued' WHERE attempt_id=?", (attempt_id,))
            checkpoint = json.loads(row['checkpoint_json'])
            checkpoint['queued_at'] = utc_now()
            db.execute('UPDATE research_runs SET checkpoint_json=? WHERE run_id=?', (json.dumps(checkpoint), run_id))
            return self._run(db, self._owned(db, actor, run_id))

    def claim(self, actor, run_id, *, attempt_id):
        with self._connect() as db:
            row = self._owned(db, actor, run_id)
            self._worker(row, attempt_id)
            db.execute("UPDATE research_attempts SET status='running' WHERE attempt_id=?", (attempt_id,))

    def _run(self, db, row):
        result = dict(row)
        result['capacity'] = {'researches': self.capacity.researches, 'queued': self.capacity.queued}
        result.update(self._execution(db, row))
        for key in ("request", "settings", "checkpoint"):
            result[key] = json.loads(result.pop(key + "_json"))
        steps = db.execute("SELECT * FROM steps WHERE run_id=? ORDER BY sequence_no", (row["run_id"],)).fetchall()
        result["steps"] = [{**{key: value for key, value in dict(step).items() if key != "input_summary_json"},
                            "input_summary": json.loads(step["input_summary_json"])} for step in steps]
        result.update(contract=STATE_CONTRACT, allowed_actions=self._actions(row["status"], result["checkpoint"]), **self._controls(row["status"], result["checkpoint"]))
        return result

    def settings(self, actor: IdentityContext, session_id: str) -> dict:
        self._session(actor, session_id)
        with self._connect() as db:
            row = db.execute("SELECT settings_json FROM research_session_settings WHERE session_id=? AND owner_user_id=? AND scope_id=?",
                             (session_id, actor.owner_user_id, actor.scope_id)).fetchone()
            return self.registry.saved_preferences(json.loads(row["settings_json"])) if row else self.registry.selection()

    def update_settings(self, actor: IdentityContext, session_id: str, payload: dict) -> dict:
        self._session(actor, session_id)
        if not isinstance(payload, dict):
            raise AgentError("invalid_request", "研究设置必须是对象")
        with self._connect() as db:
            row = db.execute("SELECT settings_json FROM research_session_settings WHERE session_id=? AND owner_user_id=? AND scope_id=?",
                             (session_id, actor.owner_user_id, actor.scope_id)).fetchone()
            current = self.registry.saved_preferences(json.loads(row["settings_json"])) if row else self.registry.selection()
            merged = {**current, **payload}
            if payload.get("model_key", current["model_key"]) != current["model_key"] and "effort" not in payload:
                merged["effort"] = current.get("effort_by_model", {}).get(payload["model_key"], "default")
            selected = self.registry.selection(merged)
            db.execute("INSERT INTO research_session_settings VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET settings_json=excluded.settings_json,updated_at=excluded.updated_at",
                       (session_id, actor.owner_user_id, actor.scope_id, canonical_json(selected), utc_now()))
            return selected

    def accept(self, actor: IdentityContext, session_id: str, request: dict, frozen: dict) -> dict:
        self._session(actor, session_id)
        if not isinstance(request, dict) or not isinstance(frozen, dict):
            raise AgentError("invalid_request", "研究请求或模型配置无效")
        request = _json_value(request, maximum=256 * 1024)
        frozen = _json_value(frozen, maximum=32768)
        request_id = _id(request.get("request_id"), "request_id")
        question = request.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 20000:
            raise AgentError("invalid_request", "研究问题长度无效")
        # Only the server's frozen selection is accepted, never runtime secrets.
        selected = self.registry.selection({key: frozen[key] for key in ("model_key", "effort", "web_enabled", "thinking_enabled", "effort_by_model") if key in frozen})
        if any(key in frozen for key in ("api_key", "base_url", "headers", "token", "owner_user_id", "actor_id", "roles")):
            raise AgentError("invalid_request", "研究配置包含不应保存的权限或凭据字段")
        if any(frozen.get(key) != selected[key] for key in ("model_key", "effort", "web_enabled", "thinking_enabled")):
            raise AgentError("invalid_request", "需要完整固定的研究模型设置")
        fingerprint = sha256_value({"request": request, "settings": frozen})
        with self._connect() as db:
            old = db.execute("SELECT run_id,request_hash FROM research_runs WHERE session_id=? AND owner_user_id=? AND scope_id=? AND request_id=?",
                             (session_id, actor.owner_user_id, actor.scope_id, request_id)).fetchone()
            if old:
                if old["request_hash"] != fingerprint:
                    raise AgentError("request_id_conflict", "同一请求标识对应了不同研究内容或设置", http_status=409)
                run = self._run(db, self._owned(db, actor, old["run_id"]))
                return {"run": run, "created": False, "attempt_id": run["attempt_id"]}
            self._admit(db, actor, session_id)
            run_id, attempt_id, now = new_id("run"), new_id("attempt"), utc_now()
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)", (run_id, session_id, actor.owner_user_id, actor.scope_id, "harness_research", fingerprint, "running", now, now))
            snapshot = {"actor_id": actor.actor_id, "owner_user_id": actor.owner_user_id, "scope_id": actor.scope_id,
                        "authority_id": actor.authority_id, "roles": list(actor.roles), "mode": actor.mode,
                        "allowed_report_ids": sorted(actor.allowed_report_ids) if actor.allowed_report_ids is not None else None,
                        "allow_archived_reports": actor.allow_archived_reports, "captured_at": now}
            db.execute("INSERT INTO research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, session_id, actor.owner_user_id, actor.scope_id, request_id,
                       fingerprint, canonical_json(request), canonical_json(frozen), attempt_id, 1, "{}", None, canonical_json(snapshot)))
            db.execute("INSERT INTO research_attempts VALUES(?,?,?,?,?,?,?)", (attempt_id, run_id, 1, now, None, "running", None))
            self._message(db, actor, session_id, run_id, "user", question, None, {"request_id": request_id, "settings": frozen, "research_request_hash": fingerprint})
            db.execute("UPDATE sessions SET status='running',updated_at=? WHERE session_id=?", (now, session_id))
            return {"run": self._run(db, self._owned(db, actor, run_id)), "created": True, "attempt_id": attempt_id}

    @staticmethod
    def _message(db, actor, session_id, run_id, role, content, answer_status, metadata):
        now = utc_now()
        prior = db.execute("SELECT max(created_at) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]
        if prior and now <= prior:
            now = (datetime.fromisoformat(prior) + timedelta(milliseconds=1)).isoformat(timespec="milliseconds")
        db.execute("INSERT INTO messages(message_id,session_id,owner_user_id,scope_id,role,content,answer_status,run_id,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (new_id("msg"), session_id, actor.owner_user_id, actor.scope_id, role, content, answer_status, run_id, canonical_json(metadata), now))

    def get(self, actor: IdentityContext, run_id: str) -> dict:
        with self._connect() as db:
            return self._run(db, self._owned(db, actor, run_id))

    def active_identity(self, actor: IdentityContext, run_id: str) -> dict:
        """Stream guard without loading accumulated sources, steps or outputs."""
        with self.identity_reader() as read:
            return read(actor, run_id)

    @contextmanager
    def identity_reader(self):
        """One stream-local connection, fresh committed identity on every read.

        Do not begin a transaction: retaining a read snapshot would hide a
        concurrent cancellation or attempt replacement. query_only also makes
        this hot path incapable of acquiring a write transaction. The store's
        connection factory retains its storage-boundary checks. A synchronous
        response generator can resume on a different ASGI worker thread; its
        reads are sequential, never concurrent, so thread affinity is disabled
        for this connection alone.
        """
        db = self.store._connect(check_same_thread=False)
        try:
            db.execute("PRAGMA query_only=ON")
            yield lambda actor, run_id: self._active_identity(db, actor, run_id)
        finally:
            db.close()

    @staticmethod
    def _active_identity(db, actor, run_id):
        if not isinstance(actor, IdentityContext):
            raise AgentError("identity_invalid", "研究需要已验证的用户身份", http_status=401)
        row = db.execute("""SELECT r.status,h.attempt_id,h.settings_json FROM runs r
                JOIN research_runs h ON h.run_id=r.run_id JOIN sessions s ON s.session_id=r.session_id
                WHERE r.run_id=? AND r.owner_user_id=? AND r.scope_id=?
                AND h.owner_user_id=r.owner_user_id AND h.scope_id=r.scope_id AND h.session_id=r.session_id
                AND s.owner_user_id=r.owner_user_id AND s.scope_id=r.scope_id""",
                (run_id, actor.owner_user_id, actor.scope_id)).fetchone()
        if row is None:
            raise AgentError("run_not_found", "没有找到当前用户的研究", http_status=404)
        return {"status": row["status"], "attempt_id": row["attempt_id"], "settings": json.loads(row["settings_json"])}

    def enrich_bundle(self, actor: IdentityContext, bundle: dict) -> dict:
        result = copy.deepcopy(bundle)
        session_id = result.get("session", {}).get("session_id")
        self._session(actor, session_id)
        result["session"]["settings"] = self.settings(actor, session_id)
        with self._connect() as db:
            for run in result.get("runs", []):
                if run.get("session_id") != session_id:
                    raise AgentError("run_not_found", "研究不属于当前会话", http_status=404)
                if run.get("intent") == "harness_research":
                    row = self._owned(db, actor, run["run_id"])
                    # Preserve the existing bundle's atomic messages/run snapshot.
                    checkpoint = json.loads(row["checkpoint_json"])
                    request = json.loads(row["request_json"])
                    run["request"] = {key: request.get(key, []) for key in ("attachment_ids", "report_ids", "dataset_ids", "product_urls")}
                    run.update(settings=json.loads(row["settings_json"]), trace_id=row["trace_id"],
                               **self._execution(db, row),
                               allowed_actions=self._actions(run["status"], checkpoint), **self._controls(run["status"], checkpoint))
        result['messages'] = self.project_message_citations(actor, session_id, result.get('messages', []))
        return result

    def _review_citation_sources(self, actor, session_id, run_id, wanted, before):
        """Owned current sources plus originals already saved before this run began."""
        if not isinstance(actor, IdentityContext):
            raise AgentError('identity_invalid', '需要已验证的用户身份', http_status=401)
        with closing(sqlite3.connect(self.store.database_path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            deadline = time.monotonic() + 5
            db.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
            run = self._owned(db, actor, run_id)
            if run['session_id'] != session_id:
                raise AgentError('run_not_found', '研究不属于当前会话', http_status=404)
            for timestamp in (run['created_at'], before):
                if datetime.fromisoformat(timestamp).utcoffset() is None:
                    raise ValueError('citation boundary has no timezone')
            if len(wanted) > 900:
                raise AgentError('citation_limit', '已有原评来源超出本次读取容量')
            placeholders = ','.join('?' for _ in wanted)
            # Malformed rows make the result incomplete instead of silently
            # selecting another source and claiming it is the unique version.
            rows = db.execute('''SELECT s.* FROM research_sources s JOIN runs r ON r.run_id=s.run_id
                JOIN research_runs h ON h.run_id=r.run_id
                WHERE s.owner_user_id=? AND s.scope_id=? AND s.session_id=?
                AND r.owner_user_id=s.owner_user_id AND r.scope_id=s.scope_id AND r.session_id=s.session_id
                AND h.owner_user_id=r.owner_user_id AND h.scope_id=r.scope_id AND h.session_id=r.session_id
                AND (julianday(s.created_at) IS NULL OR julianday(s.created_at)<=julianday(?))
                AND (s.run_id=? OR (r.rowid<(SELECT rowid FROM runs WHERE run_id=?)
                    AND julianday(r.created_at)<=julianday(?)
                    AND (julianday(s.created_at) IS NULL OR julianday(s.created_at)<=julianday(?))))
                AND (NOT json_valid(s.source_json) OR CASE WHEN json_valid(s.source_json)
                    THEN json_extract(s.source_json,'$.content.review_entity_id') END IN (''' + placeholders + '))',
                (actor.owner_user_id, actor.scope_id, session_id, before, run_id, run_id,
                 run['created_at'], run['created_at'], *wanted))
            result, size = [], 0
            for row in rows:
                size += len(row['source_json'].encode('utf-8'))
                if size > 64 * 1024**2 or len(result) >= 1000:
                    raise AgentError('citation_limit', '已有原评来源超出本次读取容量')
                if datetime.fromisoformat(row['created_at']).utcoffset() is None:
                    raise ValueError('source timestamp has no timezone')
                result.append(self._source(row))
            return result

    def project_message_citations(self, actor: IdentityContext, session_id: str, messages: list[dict]) -> list[dict]:
        """Recover legacy rendering on copies; original message/trace data stays intact."""
        if not isinstance(actor, IdentityContext):
            raise AgentError('identity_invalid', '需要已验证的用户身份', http_status=401)
        result = list(messages)
        groups = {}
        for index, message in enumerate(messages):
            if message.get('role') != 'assistant' or not message.get('run_id'):
                continue
            wanted = entity_ids(message.get('content') or '')
            if not wanted:
                continue
            if (message.get('owner_user_id') != actor.owner_user_id or message.get('scope_id') != actor.scope_id
                    or message.get('session_id') != session_id):
                raise AgentError('session_not_found', '没有找到当前用户的会话', http_status=404)
            groups.setdefault(message['run_id'], []).append((index, message, wanted))
        for run_id, entries in groups.items():
            wanted = list(dict.fromkeys(value for _, _, values in entries for value in values))
            sources, unavailable = [], False
            # Avoid unbounded parameter counts from unusually long histories.
            if len(wanted) > 900:
                unavailable = True
            else:
                try:
                    latest = max(datetime.fromisoformat(message['created_at']) for _, message, _ in entries)
                    sources = self._review_citation_sources(actor, session_id, run_id, wanted,
                        latest.isoformat())
                except AgentError as error:
                    if error.code not in {'run_not_found', 'citation_limit'}:
                        raise
                    unavailable = True
                except (sqlite3.Error, ValueError, TypeError, KeyError):
                    unavailable = True
            for index, message, _ in entries:
                projected = copy.deepcopy(message)
                metadata = projected.setdefault('metadata', {})
                citations = metadata.get('citations', []) if isinstance(metadata, dict) else None
                readable = not unavailable and isinstance(citations, list)
                prior = []
                if readable:
                    try:
                        before = datetime.fromisoformat(message['created_at'])
                        if before.utcoffset() is None:
                            raise ValueError('message timestamp has no timezone')
                        prior = [source for source in sources if datetime.fromisoformat(source['created_at']) <= before]
                    except (ValueError, TypeError, KeyError):
                        readable = False
                projected['content'], resolved = resolve_entities(message['content'], prior,
                    citations=citations if isinstance(citations, list) else [], strict=False, unavailable=not readable, run_id=run_id)
                if isinstance(citations, list):
                    metadata['citations'] = resolved
                result[index] = projected
        return result

    def event(self, actor: IdentityContext, run_id: str, title: str, status: str, tool_name: str | None = None,
              event_id: str | None = None, detail=None, *, attempt_id: str) -> dict:
        title = normalize_text(title, field="step.title", maximum=160)
        if status not in STEP_STATUSES:
            raise AgentError("invalid_request", "研究步骤状态无效")
        if tool_name is not None:
            tool_name = normalize_text(tool_name, field="step.tool_name", maximum=180)
        event_id = _id(event_id or new_id("event"), "event_id")
        safe_detail = sanitize_log_value(detail or {})
        description = safe_detail.get("summary", "") if isinstance(safe_detail, dict) else safe_detail
        summary = {"live_event": True, "event_id": event_id, "attempt_id": attempt_id,
                   "detail": description if isinstance(description, str) else "", "data": safe_detail}
        with self._connect() as db:
            row = self._owned(db, actor, run_id)
            self._worker(row, attempt_id)
            existing = db.execute("SELECT step_id FROM research_events WHERE run_id=? AND attempt_id=? AND event_id=?", (run_id, attempt_id, event_id)).fetchone()
            now = utc_now()
            finished = now if status in {"completed", "blocked", "failed", "skipped"} else None
            if existing:
                step_id = existing["step_id"]
                old = db.execute("SELECT * FROM steps WHERE step_id=?", (step_id,)).fetchone()
                if old["status"] in {"completed", "blocked", "failed", "skipped"}:
                    return {**dict(old), "input_summary": json.loads(old["input_summary_json"]), "event_id": event_id}
                db.execute("UPDATE steps SET title=?,tool_name=?,status=?,input_summary_json=?,started_at=COALESCE(started_at,?),finished_at=? WHERE step_id=?",
                           (title, tool_name, status, canonical_json(summary), now if status == "running" else None, finished, step_id))
            else:
                step_id = new_id("stp")
                sequence = db.execute("SELECT COALESCE(max(sequence_no),0)+1 FROM steps WHERE run_id=?", (run_id,)).fetchone()[0]
                db.execute("INSERT INTO steps(step_id,run_id,session_id,owner_user_id,scope_id,sequence_no,title,tool_name,status,input_summary_json,started_at,finished_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (step_id, run_id, row["session_id"], actor.owner_user_id, actor.scope_id, sequence, title, tool_name, status, canonical_json(summary), now if status == "running" else None, finished, now))
                db.execute("INSERT INTO research_events VALUES(?,?,?,?)", (run_id, attempt_id, event_id, step_id))
            code = safe_detail.get("code") if isinstance(safe_detail, dict) else None
            if status == "failed" and isinstance(code, str):
                db.execute("UPDATE steps SET error_code=? WHERE step_id=?", (code[:180], step_id))
            step = dict(db.execute("SELECT * FROM steps WHERE step_id=?", (step_id,)).fetchone())
            step["input_summary"] = json.loads(step.pop("input_summary_json"))
            step["event_id"] = event_id
            return step

    def finish(self, actor: IdentityContext, run_id: str, answer: str, status: str = "completed", metadata=None, *, attempt_id: str, allow_waiting: bool = False) -> dict:
        if (status not in _FINAL - {"cancelled"} or not isinstance(answer, str) or len(answer) > 200000
                or type(allow_waiting) is not bool or (allow_waiting and status != "blocked")):
            raise AgentError("invalid_request", "研究回答或结束状态无效")
        metadata = _json_value(metadata or {})
        if not isinstance(metadata, dict):
            raise AgentError("invalid_request", "回答元数据必须是对象")
        fingerprint = sha256_value({"answer": answer, "status": status, "metadata": metadata})
        with self._connect() as db:
            row = self._owned(db, actor, run_id)
            self._worker(row, attempt_id, allow_finished=True)
            attempt = db.execute("SELECT * FROM research_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt["finish_hash"]:
                if attempt["finish_hash"] != fingerprint:
                    raise AgentError("research_finish_conflict", "这次执行已经保存了不同结果", http_status=409)
                return self._run(db, row)
            if row["status"] != "running" and not (allow_waiting and row["status"] in _WAITING):
                raise AgentError("research_not_running", "这次研究当前未在执行", http_status=409)
            if status in {"blocked", "partial"} and metadata.get("resume_disabled") is True:
                checkpoint = json.loads(row["checkpoint_json"])
                error = metadata.get("error")
                checkpoint.update(resume_disabled=True, resume_disabled_reason=error.get("code", "resume_unavailable") if isinstance(error, dict) else "resume_unavailable")
                db.execute("UPDATE research_runs SET checkpoint_json=? WHERE run_id=?", (canonical_json(checkpoint), run_id))
            now = utc_now()
            self._message(db, actor, row["session_id"], run_id, "assistant", answer, status,
                          {**metadata, "settings": json.loads(row["settings_json"]), "attempt_id": attempt_id, "trace_id": row["trace_id"]})
            db.execute("UPDATE runs SET status=?,updated_at=? WHERE run_id=?", (status, now, run_id))
            db.execute("UPDATE research_attempts SET status=?,finished_at=?,finish_hash=? WHERE attempt_id=?", (status, now, fingerprint, attempt_id))
            self._settle_steps(db, run_id, attempt_id, "completed" if status == "completed" else "blocked", now)
            self._session_status(db, row["session_id"], now)
            return self._run(db, self._owned(db, actor, run_id))

    @staticmethod
    def _settle_steps(db, run_id, attempt_id, status, now):
        db.execute("UPDATE steps SET status=?,finished_at=? WHERE step_id IN (SELECT step_id FROM research_events WHERE run_id=? AND attempt_id=?) AND status IN ('queued','running','waiting')",
                   (status, now, run_id, attempt_id))

    @staticmethod
    def _session_status(db, session_id, now):
        active = db.execute("SELECT 1 FROM runs WHERE session_id=? AND status IN ('running','waiting_collection','waiting_attachments','waiting_data') LIMIT 1", (session_id,)).fetchone()
        db.execute("UPDATE sessions SET status=CASE WHEN archived=1 THEN 'archived' ELSE ? END,updated_at=? WHERE session_id=?", ("running" if active else "active", now, session_id))

    def checkpoint(self, actor: IdentityContext, run_id: str, payload: dict, status: str = "waiting_collection", *, attempt_id: str) -> dict:
        if status not in _WAITING | {"running"} or not isinstance(payload, dict):
            raise AgentError("invalid_request", "研究进度或等待状态无效")
        payload = _json_value(payload, maximum=8 * 1024 * 1024)
        with self._connect() as db:
            row = self._owned(db, actor, run_id)
            self._worker(row, attempt_id)
            db.execute("UPDATE research_runs SET checkpoint_json=? WHERE run_id=?", (canonical_json(payload), run_id))
            db.execute("UPDATE runs SET status=?,updated_at=? WHERE run_id=?", (status, utc_now(), run_id))
            db.execute("UPDATE research_attempts SET status=CASE WHEN status='queued' AND ?='running' THEN status ELSE ? END WHERE attempt_id=?", (status, status, attempt_id))
            return self._run(db, self._owned(db, actor, run_id))

    def resume(self, actor: IdentityContext, run_id: str, request_id: str | None = None, *, expected_attempt_id=None) -> dict:
        if request_id is not None:
            _id(request_id, "request_id")
        with self._connect() as db:
            row = self._owned(db, actor, run_id)
            if expected_attempt_id is not None and (row["attempt_id"] != expected_attempt_id or row["status"] not in _WAITING):
                raise AgentError("research_attempt_stale", "研究状态已经变化", http_status=409)
            if json.loads(row["checkpoint_json"]).get("resume_disabled"):
                raise AgentError("resume_unavailable", "当前问题不能直接继续，请先处理已显示的配置、权限或预算问题，再新建研究。", http_status=409)
            if request_id is not None:
                previous = db.execute("SELECT attempt_id FROM research_resumes WHERE run_id=? AND request_id=?", (run_id, request_id)).fetchone()
                if previous:
                    return {"run": self._run(db, row), "created": False, "attempt_id": previous["attempt_id"]}
            if row["status"] not in _RESUMABLE:
                raise AgentError("resume_unavailable", "当前研究不能继续执行", http_status=409)
            self._admit(db, actor, row["session_id"], exclude_run=run_id)
            checkpoint = json.loads(row["checkpoint_json"])
            if checkpoint.get("auto_recovery", {}).get("pending"):
                checkpoint["auto_recovery"]["pending"] = False
                db.execute("UPDATE research_runs SET checkpoint_json=? WHERE run_id=?", (canonical_json(checkpoint), run_id))
            attempt_id, now, number = new_id("attempt"), utc_now(), row["attempt_number"] + 1
            db.execute("UPDATE research_attempts SET finished_at=COALESCE(finished_at,?) WHERE attempt_id=?", (now, row["attempt_id"]))
            self._settle_steps(db, run_id, row["attempt_id"], "skipped", now)
            db.execute("UPDATE research_runs SET attempt_id=?,attempt_number=? WHERE run_id=?", (attempt_id, number, run_id))
            db.execute("INSERT INTO research_attempts VALUES(?,?,?,?,?,?,?)", (attempt_id, run_id, number, now, None, "running", None))
            if request_id is not None:
                db.execute("INSERT INTO research_resumes VALUES(?,?,?,?)", (run_id, request_id, attempt_id, now))
            db.execute("UPDATE runs SET status='running',updated_at=? WHERE run_id=?", (now, run_id))
            self._session_status(db, row["session_id"], now)
            return {"run": self._run(db, self._owned(db, actor, run_id)), "created": True, "attempt_id": attempt_id}

    def defer_recovery(self, actor, run_id, code, *, attempt_id):
        """Persist a recoverable wait without posting a failure as an answer."""
        with self._connect() as db:
            row = self._owned(db, actor, run_id)
            self._worker(row, attempt_id)
            checkpoint = recovery_checkpoint(json.loads(row["checkpoint_json"]), attempt_id, code)
            if checkpoint is None:
                return False
            now = utc_now()
            db.execute("UPDATE research_runs SET checkpoint_json=? WHERE run_id=?", (canonical_json(checkpoint), run_id))
            db.execute("UPDATE runs SET status='waiting_data',updated_at=? WHERE run_id=?", (now, run_id))
            db.execute("UPDATE research_attempts SET status='waiting_data',finished_at=? WHERE attempt_id=?", (now, attempt_id))
            self._settle_steps(db, run_id, attempt_id, "skipped", now)
            self._session_status(db, row["session_id"], now)
            return True

    def cancel(self, actor: IdentityContext, run_id: str) -> dict:
        with self._connect() as db:
            row = self._owned(db, actor, run_id)
            if row["status"] == "running" or row["status"] in _WAITING:
                now = utc_now()
                db.execute("UPDATE runs SET status='cancelled',updated_at=? WHERE run_id=?", (now, run_id))
                db.execute("UPDATE research_attempts SET status='cancelled',finished_at=? WHERE attempt_id=?", (now, row["attempt_id"]))
                self._settle_steps(db, run_id, row["attempt_id"], "skipped", now)
                self._session_status(db, row["session_id"], now)
            return self._run(db, self._owned(db, actor, run_id))

    def attach_trace(self, actor: IdentityContext, run_id: str, trace_id: str, *, attempt_id: str) -> dict:
        trace_id = _id(trace_id, "trace_id")
        with self._connect() as db:
            row = self._owned(db, actor, run_id)
            self._worker(row, attempt_id)
            if row["trace_id"] is not None and row["trace_id"] != trace_id:
                raise AgentError("research_trace_conflict", "研究日志标识已经固定", http_status=409)
            db.execute("UPDATE research_runs SET trace_id=? WHERE run_id=?", (trace_id, run_id))
            return self._run(db, self._owned(db, actor, run_id))

    def add_source(self, actor: IdentityContext, run_id: str, source: dict, *, attempt_id: str) -> dict:
        if not isinstance(source, dict) or set(source) & {"source_id", "run_id", "session_id", "owner_user_id", "scope_id", "source_hash"}:
            raise AgentError("invalid_request", "来源标识由服务端生成")
        value = _json_value(source)
        digest = sha256_value(value)
        with self._connect() as db:
            run = self._owned(db, actor, run_id)
            self._worker(run, attempt_id)
            row = db.execute("SELECT * FROM research_sources WHERE run_id=? AND source_hash=?", (run_id, digest)).fetchone()
            if row is None:
                source_id = new_id("src")
                db.execute("INSERT INTO research_sources VALUES(?,?,?,?,?,?,?,?)", (source_id, run_id, run["session_id"], actor.owner_user_id,
                           actor.scope_id, digest, canonical_json(value), utc_now()))
                row = db.execute("SELECT * FROM research_sources WHERE source_id=?", (source_id,)).fetchone()
            return self._source(row)

    @staticmethod
    def _source(row):
        result = dict(row)
        result["source"] = json.loads(result.pop("source_json"))
        return result

    def source(self, actor: IdentityContext, session_id: str, source_id: str) -> dict:
        self._session(actor, session_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM research_sources WHERE source_id=? AND session_id=? AND owner_user_id=? AND scope_id=?",
                             (source_id, session_id, actor.owner_user_id, actor.scope_id)).fetchone()
            if row is None:
                raise AgentError("source_not_found", "没有找到当前会话的来源", http_status=404)
            return self._source(row)

    def list_sources(self, actor: IdentityContext, run_id: str) -> list[dict]:
        with self._connect() as db:
            self._owned(db, actor, run_id)
            return [self._source(row) for row in db.execute("SELECT * FROM research_sources WHERE run_id=? ORDER BY created_at,source_id", (run_id,))]

    def recover_interrupted(self) -> dict:
        """Call once at trusted server startup, not on each object initialization."""
        with self._connect() as db:
            rows = db.execute("SELECT r.run_id,r.session_id,h.attempt_id,h.checkpoint_json FROM runs r JOIN research_runs h ON r.run_id=h.run_id JOIN research_attempts a ON a.attempt_id=h.attempt_id WHERE r.status='running' AND a.status<>'queued'").fetchall()
            now = utc_now()
            for row in rows:
                checkpoint = recovery_checkpoint(json.loads(row["checkpoint_json"]), row["attempt_id"], "research_interrupted")
                status = "waiting_data" if checkpoint else "blocked"
                if checkpoint:
                    db.execute("UPDATE research_runs SET checkpoint_json=? WHERE run_id=?", (canonical_json(checkpoint), row["run_id"]))
                db.execute("UPDATE runs SET status=?,updated_at=? WHERE run_id=?", (status, now, row["run_id"]))
                db.execute("UPDATE research_attempts SET status=?,finished_at=? WHERE attempt_id=?", (status, now, row["attempt_id"]))
                self._settle_steps(db, row["run_id"], row["attempt_id"], "blocked", now)
                self._session_status(db, row["session_id"], now)
            return {"interrupted_runs": len(rows)}

    def pending_runs(self, *, limit: int = 100) -> list[dict]:
        """Internal trusted scheduler only; never expose via an owner-facing route.

        actor_snapshot records who accepted the run. It is not current authority:
        the worker must revalidate account/permissions before resuming tools.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise AgentError("invalid_request", "研究恢复分页参数无效")
        with self._connect() as db:
            rows = db.execute("SELECT h.*,r.status,a.status AS execution_status FROM research_runs h JOIN runs r ON h.run_id=r.run_id JOIN research_attempts a ON a.attempt_id=h.attempt_id WHERE r.status IN ('running','waiting_collection','waiting_attachments','waiting_data') ORDER BY a.rowid LIMIT ?", (limit,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                for key in ("request", "settings", "checkpoint", "actor_snapshot"):
                    item[key] = json.loads(item.pop(key + "_json"))
                item["requires_identity_revalidation"] = True
                result.append(item)
            return result
