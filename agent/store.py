from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import (
    AgentError,
    IdentityContext,
    canonical_json,
    new_id,
    normalize_text,
    reject_sensitive_text,
    sha256_value,
    utc_now,
)


SESSION_STATUSES = {
    "active",
    "waiting_confirmation",
    "running",
    "completed",
    "failed",
    "archived",
}
RUN_STATUSES = {
    "planned",
    "waiting_confirmation",
    "running",
    "waiting",
    "completed",
    "blocked",
    "failed",
    "cancelled",
}
STEP_STATUSES = {
    "queued",
    "waiting_confirmation",
    "running",
    "waiting",
    "completed",
    "blocked",
    "failed",
    "skipped",
}
ARTIFACT_STATUSES = {"pending", "available", "failed", "expired"}
DATA_DIRECTORY_MODE = 0o700
RUNTIME_FILE_MODE = 0o600


class WenquanStore:
    def __init__(self, database_path: Path | str) -> None:
        expanded = Path(database_path).expanduser()
        self.database_path = Path(os.path.abspath(expanded))

    @staticmethod
    def _mode(path: Path) -> int:
        return stat.S_IMODE(os.lstat(path).st_mode)

    @staticmethod
    def _storage_error(message: str) -> AgentError:
        return AgentError("storage_boundary_invalid", message, http_status=503)

    def _secure_data_directory(self, *, create: bool) -> None:
        directory = self.database_path.parent
        try:
            metadata = os.lstat(directory)
        except FileNotFoundError:
            if not create:
                raise self._storage_error("AI 研究数据目录不存在")
            try:
                directory.mkdir(mode=DATA_DIRECTORY_MODE, parents=True, exist_ok=False)
            except FileExistsError:
                pass
            except OSError as exc:
                raise self._storage_error("AI 研究数据目录无法安全创建") from exc
            metadata = os.lstat(directory)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise self._storage_error("AI 研究数据目录必须是非符号链接目录")
        if metadata.st_uid != os.getuid():
            raise self._storage_error("AI 研究数据目录所有者不是当前运行用户")
        if stat.S_IMODE(metadata.st_mode) != DATA_DIRECTORY_MODE:
            try:
                os.chmod(directory, DATA_DIRECTORY_MODE)
            except OSError as exc:
                raise self._storage_error("AI 研究数据目录权限无法收紧为 0700") from exc
        verified = os.lstat(directory)
        if (
            stat.S_ISLNK(verified.st_mode)
            or not stat.S_ISDIR(verified.st_mode)
            or verified.st_uid != os.getuid()
            or stat.S_IMODE(verified.st_mode) != DATA_DIRECTORY_MODE
        ):
            raise self._storage_error("AI 研究数据目录 owner/type/mode 校验失败")

    def _secure_regular_file(
        self, path: Path, *, create: bool, transient: bool = False
    ) -> None:
        # SQLite may remove/recreate only these sidecars on connection close.
        # Do not open/close an extra descriptor to a live database or SHM file:
        # POSIX close can release SQLite's process-wide advisory locks.
        transient = transient and path in {
            Path(str(self.database_path) + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        }
        for _attempt in range(3):
            try:
                metadata = os.lstat(path)
            except FileNotFoundError as exc:
                if not create:
                    if transient:
                        return
                    raise self._storage_error(f"AI 研究运行文件不存在：{path.name}") from exc
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(path, flags, RUNTIME_FILE_MODE)
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise self._storage_error(f"AI 研究运行文件无法安全创建：{path.name}") from exc
                try:
                    os.fchmod(descriptor, RUNTIME_FILE_MODE)
                    metadata = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != os.getuid()
                        or stat.S_IMODE(metadata.st_mode) != RUNTIME_FILE_MODE
                    ):
                        raise self._storage_error(f"AI 研究运行文件 owner/type/mode 校验失败：{path.name}")
                finally:
                    os.close(descriptor)
                return
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise self._storage_error(f"AI 研究运行文件必须是普通文件：{path.name}")
            if metadata.st_uid != os.getuid():
                raise self._storage_error(f"AI 研究运行文件所有者无效：{path.name}")
            try:
                if stat.S_IMODE(metadata.st_mode) != RUNTIME_FILE_MODE:
                    os.chmod(path, RUNTIME_FILE_MODE)
                verified = os.lstat(path)
            except FileNotFoundError as exc:
                if transient:
                    # Recheck the name: chmod can also report ENOENT after a
                    # replacement with a dangling symlink, which must fail.
                    continue
                raise self._storage_error(f"AI 研究运行文件在校验期间消失：{path.name}") from exc
            except OSError as exc:
                raise self._storage_error(f"AI 研究运行文件无法安全校验：{path.name}") from exc
            if (
                stat.S_ISLNK(verified.st_mode)
                or not stat.S_ISREG(verified.st_mode)
                or verified.st_uid != os.getuid()
            ):
                raise self._storage_error(f"AI 研究运行文件 owner/type/mode 校验失败：{path.name}")
            if (metadata.st_dev, metadata.st_ino) != (verified.st_dev, verified.st_ino):
                if transient:
                    continue
                raise self._storage_error(f"AI 研究运行文件身份发生变化：{path.name}")
            if stat.S_IMODE(verified.st_mode) != RUNTIME_FILE_MODE:
                raise self._storage_error(f"AI 研究运行文件 owner/type/mode 校验失败：{path.name}")
            return
        raise self._storage_error(f"AI 研究运行文件持续变化，无法完成安全校验：{path.name}")

    def _is_managed_runtime_file(self, path: Path) -> bool:
        if path.name == self.database_path.name:
            return True
        suffix = path.name.removeprefix(self.database_path.name)
        return suffix in {"-wal", "-shm", "-journal"} or suffix.startswith(
            (".backup", ".restore", ".tmp")
        )

    def _secure_runtime_files(self, *, create_database: bool) -> None:
        self._secure_regular_file(self.database_path, create=create_database)
        try:
            children = list(self.database_path.parent.iterdir())
        except OSError as exc:
            raise self._storage_error("AI 研究数据目录无法审计") from exc
        for path in children:
            if self._is_managed_runtime_file(path):
                self._secure_regular_file(
                    path, create=False,
                    transient=path.name in {self.database_path.name + suffix for suffix in ("-wal", "-shm", "-journal")},
                )

    def _secure_storage_boundary(self, *, create: bool) -> None:
        self._secure_data_directory(create=create)
        self._secure_runtime_files(create_database=create)

    def initialize(self) -> None:
        self._secure_storage_boundary(create=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_meta(singleton, schema_version)
                VALUES (1, 'voxquarry-wenquan-sqlite/v1');

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_owner_scope_activity
                ON sessions(owner_user_id, scope_id, archived, updated_at DESC);

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    answer_status TEXT,
                    run_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(owner_user_id, scope_id, session_id, created_at);

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_session
                ON runs(owner_user_id, scope_id, session_id, created_at);

                CREATE TABLE IF NOT EXISTS steps (
                    step_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    session_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    tool_name TEXT,
                    status TEXT NOT NULL,
                    input_summary_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, sequence_no)
                );
                CREATE INDEX IF NOT EXISTS idx_steps_run
                ON steps(owner_user_id, scope_id, run_id, sequence_no);

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    session_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    upstream_id TEXT NOT NULL,
                    uri TEXT,
                    contract_version TEXT NOT NULL,
                    content_hash TEXT,
                    status TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_run
                ON artifacts(owner_user_id, scope_id, run_id, created_at);

                CREATE TABLE IF NOT EXISTS confirmations (
                    confirmation_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    session_id TEXT REFERENCES sessions(session_id),
                    run_id TEXT REFERENCES runs(run_id),
                    action TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    required_count INTEGER NOT NULL CHECK (required_count IN (1, 2)),
                    confirmed_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'consumed', 'expired')),
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_confirmations_owner
                ON confirmations(owner_user_id, scope_id, status, created_at);

                CREATE TABLE IF NOT EXISTS memory_settings (
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_user_id, scope_id)
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    preference_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    preference_key TEXT NOT NULL,
                    value_json TEXT,
                    revision INTEGER NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
                    source_confirmation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_user_id, scope_id, preference_key, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_preferences_latest
                ON preferences(owner_user_id, scope_id, preference_key, revision DESC);

                CREATE TABLE IF NOT EXISTS feedback (
                    feedback_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    message_id TEXT NOT NULL REFERENCES messages(message_id),
                    run_id TEXT,
                    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
                    reason_codes_json TEXT NOT NULL,
                    comment TEXT,
                    context_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_owner
                ON feedback(owner_user_id, scope_id, session_id, created_at);

                CREATE TABLE IF NOT EXISTS audit_events (
                    audit_event_id TEXT PRIMARY KEY,
                    audit_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    request_id TEXT,
                    session_id TEXT,
                    run_id TEXT,
                    step_id TEXT,
                    tool_name TEXT,
                    status TEXT NOT NULL,
                    input_hash TEXT,
                    output_hash TEXT,
                    error_code TEXT,
                    upstream_ids_json TEXT NOT NULL DEFAULT '[]',
                    duration_ms INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_owner
                ON audit_events(owner_user_id, scope_id, audit_id, created_at);
                CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                BEFORE DELETE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END;
                """
            )
        self._secure_storage_boundary(create=False)

    def _connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        self._secure_storage_boundary(create=False)
        connection = sqlite3.connect(self.database_path, timeout=10, check_same_thread=check_same_thread)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            self._secure_runtime_files(create_database=False)
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _json(value: Any) -> str:
        return canonical_json(value)

    def health(self) -> dict[str, Any]:
        self._secure_storage_boundary(create=False)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT schema_version FROM schema_meta WHERE singleton=1"
                ).fetchone()
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as exc:
            raise AgentError(
                "storage_unavailable", "AI 研究存储不可用", http_status=503, retryable=True
            ) from exc
        return {
            "status": "ready" if row and integrity == "ok" else "unavailable",
            "schema_version": row[0] if row else None,
            "integrity": integrity,
            "storage_security": {
                "owner_uid": os.getuid(),
                "data_directory_mode": f"{self._mode(self.database_path.parent):04o}",
                "database_mode": f"{self._mode(self.database_path):04o}",
                "symlink_allowed": False,
            },
        }

    def create_session(self, identity: IdentityContext, title: str) -> dict[str, Any]:
        normalized = normalize_text(title, field="title", maximum=160)
        now = utc_now()
        item = {
            "session_id": new_id("ses"),
            "owner_user_id": identity.owner_user_id,
            "scope_id": identity.scope_id,
            "title": normalized,
            "status": "active",
            "archived": 0,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO sessions
                (session_id, owner_user_id, scope_id, title, status, archived, created_at, updated_at)
                VALUES (:session_id, :owner_user_id, :scope_id, :title, :status, :archived, :created_at, :updated_at)""",
                item,
            )
        item["archived"] = False
        return item

    def list_sessions(
        self, identity: IdentityContext, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        query = """SELECT * FROM sessions
            WHERE owner_user_id=? AND scope_id=?"""
        params: list[Any] = [identity.owner_user_id, identity.scope_id]
        if not include_archived:
            query += " AND archived=0"
        query += " ORDER BY updated_at DESC, session_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [{**dict(row), "archived": bool(row["archived"])} for row in rows]

    def get_session(self, identity: IdentityContext, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM sessions WHERE session_id=?
                AND owner_user_id=? AND scope_id=?""",
                (session_id, identity.owner_user_id, identity.scope_id),
            ).fetchone()
        if row is None:
            raise AgentError("session_not_found", "Session 不存在", http_status=404)
        return {**dict(row), "archived": bool(row["archived"])}

    def update_session(
        self,
        identity: IdentityContext,
        session_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        current = self.get_session(identity, session_id)
        next_title = current["title"] if title is None else normalize_text(title, field="title", maximum=160)
        next_archived = current["archived"] if archived is None else bool(archived)
        next_status = "archived" if next_archived else (
            "active" if current["status"] == "archived" else current["status"]
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """UPDATE sessions SET title=?, archived=?, status=?, updated_at=?
                WHERE session_id=? AND owner_user_id=? AND scope_id=?""",
                (
                    next_title,
                    int(next_archived),
                    next_status,
                    now,
                    session_id,
                    identity.owner_user_id,
                    identity.scope_id,
                ),
            )
        return self.get_session(identity, session_id)

    def add_message(
        self,
        identity: IdentityContext,
        session_id: str,
        *,
        role: str,
        content: str,
        answer_status: str | None = None,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.get_session(identity, session_id)
        if role not in {"user", "assistant"}:
            raise AgentError("invalid_request", "消息 role 只能是 user 或 assistant")
        text = normalize_text(content, field="content", maximum=20000)
        now = utc_now()
        item = {
            "message_id": new_id("msg"),
            "session_id": session_id,
            "owner_user_id": identity.owner_user_id,
            "scope_id": identity.scope_id,
            "role": role,
            "content": text,
            "answer_status": answer_status,
            "run_id": run_id,
            "metadata_json": self._json(metadata or {}),
            "created_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO messages
                (message_id, session_id, owner_user_id, scope_id, role, content,
                 answer_status, run_id, metadata_json, created_at)
                VALUES (:message_id, :session_id, :owner_user_id, :scope_id, :role, :content,
                 :answer_status, :run_id, :metadata_json, :created_at)""",
                item,
            )
            connection.execute(
                """UPDATE sessions SET updated_at=? WHERE session_id=?
                AND owner_user_id=? AND scope_id=?""",
                (now, session_id, identity.owner_user_id, identity.scope_id),
            )
        return self._decode_json_columns(item, "metadata_json")

    def list_messages(self, identity: IdentityContext, session_id: str) -> list[dict[str, Any]]:
        self.get_session(identity, session_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM messages WHERE session_id=?
                AND owner_user_id=? AND scope_id=? ORDER BY created_at, message_id""",
                (session_id, identity.owner_user_id, identity.scope_id),
            ).fetchall()
        return [self._decode_json_columns(dict(row), "metadata_json") for row in rows]

    @staticmethod
    def _decode_json_columns(item: dict[str, Any], *columns: str) -> dict[str, Any]:
        for column in columns:
            raw = item.pop(column, "{}")
            item[column.removesuffix("_json")] = json.loads(raw)
        return item

    def create_run(
        self,
        identity: IdentityContext,
        session_id: str,
        *,
        intent: str,
        plan: list[dict[str, Any]],
        status: str = "planned",
    ) -> dict[str, Any]:
        self.get_session(identity, session_id)
        if status not in RUN_STATUSES:
            raise AgentError("invalid_request", "Run status 无效")
        normalized_intent = normalize_text(intent, field="intent", maximum=160)
        now = utc_now()
        run = {
            "run_id": new_id("run"),
            "session_id": session_id,
            "owner_user_id": identity.owner_user_id,
            "scope_id": identity.scope_id,
            "intent": normalized_intent,
            "plan_hash": sha256_value(plan),
            "status": status,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO runs
                (run_id, session_id, owner_user_id, scope_id, intent, plan_hash, status, created_at, updated_at)
                VALUES (:run_id, :session_id, :owner_user_id, :scope_id, :intent, :plan_hash, :status, :created_at, :updated_at)""",
                run,
            )
            for index, raw in enumerate(plan, start=1):
                title = normalize_text(raw.get("title"), field="step.title", maximum=160)
                tool_name = raw.get("tool_name")
                if tool_name is not None:
                    tool_name = normalize_text(tool_name, field="step.tool_name", maximum=180)
                step_status = str(raw.get("status") or "queued")
                if step_status not in STEP_STATUSES:
                    raise AgentError("invalid_request", "Step status 无效")
                connection.execute(
                    """INSERT INTO steps
                    (step_id, run_id, session_id, owner_user_id, scope_id, sequence_no,
                     title, tool_name, status, input_summary_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("stp"),
                        run["run_id"],
                        session_id,
                        identity.owner_user_id,
                        identity.scope_id,
                        index,
                        title,
                        tool_name,
                        step_status,
                        self._json(raw.get("input_summary") or {}),
                        now,
                    ),
                )
        return self.get_run(identity, run["run_id"])

    def get_run(self, identity: IdentityContext, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM runs WHERE run_id=?
                AND owner_user_id=? AND scope_id=?""",
                (run_id, identity.owner_user_id, identity.scope_id),
            ).fetchone()
            steps = connection.execute(
                """SELECT * FROM steps WHERE run_id=?
                AND owner_user_id=? AND scope_id=? ORDER BY sequence_no""",
                (run_id, identity.owner_user_id, identity.scope_id),
            ).fetchall()
            artifacts = connection.execute(
                """SELECT * FROM artifacts WHERE run_id=?
                AND owner_user_id=? AND scope_id=? ORDER BY created_at""",
                (run_id, identity.owner_user_id, identity.scope_id),
            ).fetchall()
        if row is None:
            raise AgentError("run_not_found", "Run 不存在", http_status=404)
        return {
            **dict(row),
            "steps": [self._decode_json_columns(dict(item), "input_summary_json") for item in steps],
            "artifacts": [self._decode_json_columns(dict(item), "metadata_json") for item in artifacts],
        }

    def list_runs(self, identity: IdentityContext, session_id: str) -> list[dict[str, Any]]:
        self.get_session(identity, session_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT run_id FROM runs WHERE session_id=?
                AND owner_user_id=? AND scope_id=? ORDER BY created_at""",
                (session_id, identity.owner_user_id, identity.scope_id),
            ).fetchall()
        return [self.get_run(identity, row["run_id"]) for row in rows]

    def update_run_status(self, identity: IdentityContext, run_id: str, status: str) -> dict[str, Any]:
        if status not in RUN_STATUSES:
            raise AgentError("invalid_request", "Run status 无效")
        self.get_run(identity, run_id)
        with self._connect() as connection:
            connection.execute(
                """UPDATE runs SET status=?, updated_at=? WHERE run_id=?
                AND owner_user_id=? AND scope_id=?""",
                (status, utc_now(), run_id, identity.owner_user_id, identity.scope_id),
            )
        return self.get_run(identity, run_id)

    def update_step_status(
        self,
        identity: IdentityContext,
        step_id: str,
        status: str,
        *,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in STEP_STATUSES:
            raise AgentError("invalid_request", "Step status 无效")
        now = utc_now()
        started_at = now if status == "running" else None
        finished_at = now if status in {"completed", "blocked", "failed", "skipped"} else None
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE steps SET status=?, error_code=?,
                started_at=COALESCE(started_at, ?), finished_at=COALESCE(?, finished_at)
                WHERE step_id=? AND owner_user_id=? AND scope_id=?""",
                (
                    status,
                    error_code,
                    started_at,
                    finished_at,
                    step_id,
                    identity.owner_user_id,
                    identity.scope_id,
                ),
            )
            if cursor.rowcount != 1:
                raise AgentError("step_not_found", "Step 不存在", http_status=404)
            row = connection.execute("SELECT * FROM steps WHERE step_id=?", (step_id,)).fetchone()
        return self._decode_json_columns(dict(row), "input_summary_json")

    def add_artifact(
        self,
        identity: IdentityContext,
        *,
        run_id: str,
        artifact_type: str,
        upstream_id: str,
        contract_version: str,
        status: str,
        display_name: str,
        uri: str | None = None,
        content_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(identity, run_id)
        if status not in ARTIFACT_STATUSES:
            raise AgentError("invalid_request", "Artifact status 无效")
        item = {
            "artifact_id": new_id("art"),
            "run_id": run_id,
            "session_id": run["session_id"],
            "owner_user_id": identity.owner_user_id,
            "scope_id": identity.scope_id,
            "artifact_type": normalize_text(artifact_type, field="artifact_type", maximum=80),
            "upstream_id": normalize_text(upstream_id, field="upstream_id", maximum=200),
            "uri": uri,
            "contract_version": normalize_text(contract_version, field="contract_version", maximum=160),
            "content_hash": content_hash,
            "status": status,
            "display_name": normalize_text(display_name, field="display_name", maximum=200),
            "metadata_json": self._json(metadata or {}),
            "created_at": utc_now(),
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO artifacts
                (artifact_id, run_id, session_id, owner_user_id, scope_id, artifact_type,
                 upstream_id, uri, contract_version, content_hash, status, display_name,
                 metadata_json, created_at)
                VALUES (:artifact_id, :run_id, :session_id, :owner_user_id, :scope_id,
                 :artifact_type, :upstream_id, :uri, :contract_version, :content_hash,
                 :status, :display_name, :metadata_json, :created_at)""",
                item,
            )
        return self._decode_json_columns(item, "metadata_json")

    def create_confirmation(
        self,
        identity: IdentityContext,
        *,
        action: str,
        preview: Mapping[str, Any],
        expires_at: str,
        session_id: str | None = None,
        run_id: str | None = None,
        required_count: int = 1,
    ) -> dict[str, Any]:
        if required_count not in {1, 2}:
            raise AgentError("invalid_request", "确认次数必须是 1 或 2")
        if session_id is not None:
            self.get_session(identity, session_id)
        if run_id is not None:
            self.get_run(identity, run_id)
        now = utc_now()
        normalized_action = normalize_text(action, field="action", maximum=160)
        payload_hash = sha256_value(preview)
        item = {
            "confirmation_id": new_id("cfm"),
            "owner_user_id": identity.owner_user_id,
            "scope_id": identity.scope_id,
            "session_id": session_id,
            "run_id": run_id,
            "action": normalized_action,
            "payload_hash": payload_hash,
            "preview_json": self._json(preview),
            "required_count": required_count,
            "confirmed_count": 0,
            "status": "pending",
            "expires_at": expires_at,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as connection:
            # Serialize identical active previews so concurrent duplicate turns cannot
            # create two confirmation objects before either caller observes the other.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM confirmations
                WHERE owner_user_id=? AND scope_id=?
                AND session_id IS ? AND run_id IS ?
                AND action=? AND payload_hash=? AND required_count=?
                AND status IN ('pending', 'confirmed') AND expires_at>?
                ORDER BY created_at DESC LIMIT 1""",
                (
                    identity.owner_user_id,
                    identity.scope_id,
                    session_id,
                    run_id,
                    normalized_action,
                    payload_hash,
                    required_count,
                    now,
                ),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO confirmations
                    (confirmation_id, owner_user_id, scope_id, session_id, run_id, action,
                     payload_hash, preview_json, required_count, confirmed_count, status,
                     expires_at, created_at, updated_at)
                    VALUES (:confirmation_id, :owner_user_id, :scope_id, :session_id, :run_id,
                     :action, :payload_hash, :preview_json, :required_count, :confirmed_count,
                     :status, :expires_at, :created_at, :updated_at)""",
                    item,
                )
        if existing is not None:
            reused = self._decode_json_columns(dict(existing), "preview_json")
            self.append_audit_event(
                identity,
                audit_id=new_id("aud"),
                event_type="confirmation.reused",
                status=reused["status"],
                session_id=session_id,
                run_id=run_id,
                input_hash=payload_hash,
                upstream_ids=(reused["confirmation_id"],),
            )
            return reused
        self.append_audit_event(
            identity,
            audit_id=new_id("aud"),
            event_type="confirmation.created",
            status="pending",
            session_id=session_id,
            run_id=run_id,
            input_hash=item["payload_hash"],
            upstream_ids=(item["confirmation_id"],),
        )
        return self._decode_json_columns(item, "preview_json")

    def get_confirmation(self, identity: IdentityContext, confirmation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM confirmations WHERE confirmation_id=?
                AND owner_user_id=? AND scope_id=?""",
                (confirmation_id, identity.owner_user_id, identity.scope_id),
            ).fetchone()
        if row is None:
            raise AgentError("confirmation_not_found", "确认对象不存在", http_status=404)
        return self._decode_json_columns(dict(row), "preview_json")

    def confirm(
        self, identity: IdentityContext, confirmation_id: str, payload_hash: str
    ) -> dict[str, Any]:
        item = self.get_confirmation(identity, confirmation_id)
        if item["status"] != "pending":
            raise AgentError("confirmation_invalid", "确认对象不再等待确认", http_status=409)
        if item["payload_hash"] != payload_hash:
            raise AgentError("confirmation_invalid", "确认内容已变化", http_status=409)
        if item["expires_at"] <= utc_now():
            with self._connect() as connection:
                connection.execute(
                    """UPDATE confirmations SET status='expired', updated_at=?
                    WHERE confirmation_id=? AND owner_user_id=? AND scope_id=?""",
                    (utc_now(), confirmation_id, identity.owner_user_id, identity.scope_id),
                )
            raise AgentError("confirmation_expired", "确认已过期", http_status=409)
        next_count = item["confirmed_count"] + 1
        next_status = "confirmed" if next_count >= item["required_count"] else "pending"
        with self._connect() as connection:
            connection.execute(
                """UPDATE confirmations SET confirmed_count=?, status=?, updated_at=?
                WHERE confirmation_id=? AND owner_user_id=? AND scope_id=?""",
                (
                    next_count,
                    next_status,
                    utc_now(),
                    confirmation_id,
                    identity.owner_user_id,
                    identity.scope_id,
                ),
            )
        self.append_audit_event(
            identity,
            audit_id=new_id("aud"),
            event_type="confirmation.recorded",
            status=next_status,
            session_id=item.get("session_id"),
            run_id=item.get("run_id"),
            input_hash=item["payload_hash"],
            upstream_ids=(confirmation_id,),
        )
        return self.get_confirmation(identity, confirmation_id)

    def _consume_confirmation(
        self,
        identity: IdentityContext,
        confirmation_id: str,
        *,
        action: str,
        payload: Mapping[str, Any],
    ) -> None:
        item = self.get_confirmation(identity, confirmation_id)
        if item["status"] != "confirmed" or item["action"] != action:
            raise AgentError("confirmation_required", "需要有效确认", http_status=409)
        if item["payload_hash"] != sha256_value(payload):
            raise AgentError("confirmation_invalid", "确认内容与写入内容不一致", http_status=409)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE confirmations SET status='consumed', updated_at=?
                WHERE confirmation_id=? AND owner_user_id=? AND scope_id=? AND status='confirmed'""",
                (utc_now(), confirmation_id, identity.owner_user_id, identity.scope_id),
            )
            if cursor.rowcount != 1:
                raise AgentError("confirmation_invalid", "确认已被使用", http_status=409)

    def memory_status(self, identity: IdentityContext) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT enabled, updated_at FROM memory_settings
                WHERE owner_user_id=? AND scope_id=?""",
                (identity.owner_user_id, identity.scope_id),
            ).fetchone()
        return {
            "enabled": True if row is None else bool(row["enabled"]),
            "updated_at": None if row is None else row["updated_at"],
        }

    def set_memory_enabled(self, identity: IdentityContext, enabled: bool) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO memory_settings(owner_user_id, scope_id, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_user_id, scope_id)
                DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (identity.owner_user_id, identity.scope_id, int(enabled), now),
            )
        self.append_audit_event(
            identity,
            audit_id=new_id("aud"),
            event_type="memory.setting_changed",
            status="enabled" if enabled else "disabled",
            input_hash=sha256_value({"enabled": bool(enabled)}),
        )
        return {"enabled": bool(enabled), "updated_at": now}

    def list_preferences(self, identity: IdentityContext) -> list[dict[str, Any]]:
        if not self.memory_status(identity)["enabled"]:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT p.* FROM preferences p
                JOIN (
                  SELECT preference_key, MAX(revision) AS revision
                  FROM preferences WHERE owner_user_id=? AND scope_id=?
                  GROUP BY preference_key
                ) latest ON latest.preference_key=p.preference_key AND latest.revision=p.revision
                WHERE p.owner_user_id=? AND p.scope_id=? AND p.deleted=0
                ORDER BY p.preference_key""",
                (
                    identity.owner_user_id,
                    identity.scope_id,
                    identity.owner_user_id,
                    identity.scope_id,
                ),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            item["deleted"] = bool(item["deleted"])
            items.append(item)
        return items

    def write_preference(
        self,
        identity: IdentityContext,
        *,
        preference_key: str,
        value: Any,
        confirmation_id: str,
    ) -> dict[str, Any]:
        if not self.memory_status(identity)["enabled"]:
            raise AgentError("memory_disabled", "偏好记忆已关闭", http_status=409)
        key = normalize_text(preference_key, field="preference_key", maximum=80)
        serialized = self._json(value)
        if len(serialized) > 4000:
            raise AgentError("invalid_request", "偏好值过大")
        reject_sensitive_text(serialized, field="preference")
        payload = {"preference_key": key, "value": value}
        self._consume_confirmation(
            identity,
            confirmation_id,
            action="memory.preference",
            payload=payload,
        )
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COALESCE(MAX(revision), 0) FROM preferences
                WHERE owner_user_id=? AND scope_id=? AND preference_key=?""",
                (identity.owner_user_id, identity.scope_id, key),
            ).fetchone()
            revision = int(row[0]) + 1
            item = {
                "preference_id": new_id("prf"),
                "owner_user_id": identity.owner_user_id,
                "scope_id": identity.scope_id,
                "preference_key": key,
                "value_json": serialized,
                "revision": revision,
                "deleted": 0,
                "source_confirmation_id": confirmation_id,
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(
                """INSERT INTO preferences
                (preference_id, owner_user_id, scope_id, preference_key, value_json,
                 revision, deleted, source_confirmation_id, created_at, updated_at)
                VALUES (:preference_id, :owner_user_id, :scope_id, :preference_key,
                 :value_json, :revision, :deleted, :source_confirmation_id, :created_at, :updated_at)""",
                item,
            )
        self.append_audit_event(
            identity,
            audit_id=new_id("aud"),
            event_type="memory.preference_written",
            status="ok",
            input_hash=sha256_value(
                {"preference_key": key, "value": value, "revision": revision}
            ),
            upstream_ids=(item["preference_id"], confirmation_id),
        )
        item.pop("value_json")
        return {**item, "value": value, "deleted": False}

    def delete_preference(
        self,
        identity: IdentityContext,
        *,
        preference_key: str,
        confirmation_id: str,
    ) -> dict[str, Any]:
        key = normalize_text(preference_key, field="preference_key", maximum=80)
        payload = {"preference_key": key, "delete": True}
        self._consume_confirmation(
            identity,
            confirmation_id,
            action="memory.preference.delete",
            payload=payload,
        )
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COALESCE(MAX(revision), 0) FROM preferences
                WHERE owner_user_id=? AND scope_id=? AND preference_key=?""",
                (identity.owner_user_id, identity.scope_id, key),
            ).fetchone()
            if int(row[0]) == 0:
                raise AgentError("preference_not_found", "偏好不存在", http_status=404)
            revision = int(row[0]) + 1
            preference_id = new_id("prf")
            connection.execute(
                """INSERT INTO preferences
                (preference_id, owner_user_id, scope_id, preference_key, value_json,
                 revision, deleted, source_confirmation_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, ?, 1, ?, ?, ?)""",
                (
                    preference_id,
                    identity.owner_user_id,
                    identity.scope_id,
                    key,
                    revision,
                    confirmation_id,
                    now,
                    now,
                ),
            )
        self.append_audit_event(
            identity,
            audit_id=new_id("aud"),
            event_type="memory.preference_deleted",
            status="ok",
            input_hash=sha256_value({"preference_key": key, "revision": revision}),
            upstream_ids=(preference_id, confirmation_id),
        )
        return {"preference_key": key, "deleted": True, "revision": revision}

    def add_feedback(
        self,
        identity: IdentityContext,
        *,
        session_id: str,
        message_id: str,
        rating: str,
        reason_codes: list[str],
        comment: str | None,
        context: Mapping[str, Any],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self.get_session(identity, session_id)
        if rating not in {"up", "down"}:
            raise AgentError("invalid_request", "feedback rating 无效")
        if not isinstance(reason_codes, list) or len(reason_codes) > 8:
            raise AgentError("invalid_request", "feedback reasons 无效")
        safe_comment = None
        if comment is not None:
            safe_comment = normalize_text(comment, field="comment", maximum=2000)
            reject_sensitive_text(safe_comment, field="comment")
        with self._connect() as connection:
            message = connection.execute(
                """SELECT message_id FROM messages WHERE message_id=? AND session_id=?
                AND owner_user_id=? AND scope_id=?""",
                (message_id, session_id, identity.owner_user_id, identity.scope_id),
            ).fetchone()
        if message is None:
            raise AgentError("message_not_found", "消息不存在", http_status=404)
        item = {
            "feedback_id": new_id("fdb"),
            "owner_user_id": identity.owner_user_id,
            "scope_id": identity.scope_id,
            "session_id": session_id,
            "message_id": message_id,
            "run_id": run_id,
            "rating": rating,
            "reason_codes_json": self._json(reason_codes),
            "comment": safe_comment,
            "context_json": self._json(context),
            "created_at": utc_now(),
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO feedback
                (feedback_id, owner_user_id, scope_id, session_id, message_id, run_id,
                 rating, reason_codes_json, comment, context_json, created_at)
                VALUES (:feedback_id, :owner_user_id, :scope_id, :session_id, :message_id,
                 :run_id, :rating, :reason_codes_json, :comment, :context_json, :created_at)""",
                item,
            )
        self.append_audit_event(
            identity,
            audit_id=new_id("aud"),
            event_type="feedback.recorded",
            status="ok",
            session_id=session_id,
            run_id=run_id,
            input_hash=sha256_value(
                {
                    "message_id": message_id,
                    "rating": rating,
                    "reason_codes": reason_codes,
                    "comment_present": safe_comment is not None,
                }
            ),
            upstream_ids=(item["feedback_id"], message_id),
        )
        return self._decode_json_columns(item, "reason_codes_json", "context_json")

    def append_audit_event(
        self,
        identity: IdentityContext,
        *,
        audit_id: str,
        event_type: str,
        status: str,
        request_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        tool_name: str | None = None,
        input_hash: str | None = None,
        output_hash: str | None = None,
        error_code: str | None = None,
        upstream_ids: Iterable[str] = (),
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        item = {
            "audit_event_id": new_id("aev"),
            "audit_id": audit_id,
            "owner_user_id": identity.owner_user_id,
            "scope_id": identity.scope_id,
            "actor_id": identity.actor_id,
            "event_type": normalize_text(event_type, field="event_type", maximum=160),
            "request_id": request_id,
            "session_id": session_id,
            "run_id": run_id,
            "step_id": step_id,
            "tool_name": tool_name,
            "status": normalize_text(status, field="status", maximum=80),
            "input_hash": input_hash,
            "output_hash": output_hash,
            "error_code": error_code,
            "upstream_ids_json": self._json(sorted(set(upstream_ids))),
            "duration_ms": duration_ms,
            "created_at": utc_now(),
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO audit_events
                (audit_event_id, audit_id, owner_user_id, scope_id, actor_id, event_type,
                 request_id, session_id, run_id, step_id, tool_name, status, input_hash,
                 output_hash, error_code, upstream_ids_json, duration_ms, created_at)
                VALUES (:audit_event_id, :audit_id, :owner_user_id, :scope_id, :actor_id,
                 :event_type, :request_id, :session_id, :run_id, :step_id, :tool_name,
                 :status, :input_hash, :output_hash, :error_code, :upstream_ids_json,
                 :duration_ms, :created_at)""",
                item,
            )
        return self._decode_json_columns(item, "upstream_ids_json")

    def list_audit_events(
        self, identity: IdentityContext, *, audit_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = """SELECT * FROM audit_events
            WHERE owner_user_id=? AND scope_id=?"""
        params: list[Any] = [identity.owner_user_id, identity.scope_id]
        if audit_id is not None:
            query += " AND audit_id=?"
            params.append(audit_id)
        # audit_event_id is random and millisecond timestamps can tie. SQLite rowid is
        # the physical append order for this no-update/no-delete ledger, so use it as
        # the deterministic tie-breaker instead of reordering simultaneous calls.
        query += " ORDER BY created_at, rowid"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode_json_columns(dict(row), "upstream_ids_json") for row in rows]

    def session_bundle(self, identity: IdentityContext, session_id: str) -> dict[str, Any]:
        # The UI stops polling a terminal run. Separate connections can read
        # messages before finish() commits and runs afterwards, losing the answer
        # from that response. Keep every part of the bundle in one read snapshot.
        params = (session_id, identity.owner_user_id, identity.scope_id)
        with self._connect() as connection:
            connection.execute("BEGIN")
            session = connection.execute(
                """SELECT * FROM sessions WHERE session_id=?
                AND owner_user_id=? AND scope_id=?""", params,
            ).fetchone()
            if session is None:
                raise AgentError("session_not_found", "Session 不存在", http_status=404)
            messages = connection.execute(
                """SELECT * FROM messages WHERE session_id=?
                AND owner_user_id=? AND scope_id=? ORDER BY created_at, message_id""", params,
            ).fetchall()
            rows = connection.execute(
                """SELECT * FROM runs WHERE session_id=?
                AND owner_user_id=? AND scope_id=? ORDER BY created_at""", params,
            ).fetchall()
            runs = []
            for row in rows:
                run_params = (row["run_id"], identity.owner_user_id, identity.scope_id)
                steps = connection.execute(
                    """SELECT * FROM steps WHERE run_id=?
                    AND owner_user_id=? AND scope_id=? ORDER BY sequence_no""", run_params,
                ).fetchall()
                artifacts = connection.execute(
                    """SELECT * FROM artifacts WHERE run_id=?
                    AND owner_user_id=? AND scope_id=? ORDER BY created_at""", run_params,
                ).fetchall()
                runs.append({
                    **dict(row),
                    "steps": [self._decode_json_columns(dict(item), "input_summary_json") for item in steps],
                    "artifacts": [self._decode_json_columns(dict(item), "metadata_json") for item in artifacts],
                })
        return {"session": {**dict(session), "archived": bool(session["archived"])},
                "messages": [self._decode_json_columns(dict(row), "metadata_json") for row in messages],
                "runs": runs}
