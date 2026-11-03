from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .audit import event_hash
from .contracts import format_utc, utc_now
from .filesystem import (
    absolute_path,
    create_secure_file,
    path_lexists,
    secure_directory,
    secure_file,
    secure_sqlite_family,
)
from .passwords import PasswordCredential


PACKAGE_ROOT = Path(__file__).resolve().parent
MIGRATIONS_ROOT = PACKAGE_ROOT / "migrations"


class StoreError(RuntimeError):
    pass


class _SecureSQLiteConnection(sqlite3.Connection):
    _security_path: Path | None = None

    def _secure_runtime_files(self) -> None:
        if self._security_path is not None:
            secure_sqlite_family(self._security_path)

    def execute(self, *args, **kwargs):
        try:
            return super().execute(*args, **kwargs)
        finally:
            self._secure_runtime_files()

    def executemany(self, *args, **kwargs):
        try:
            return super().executemany(*args, **kwargs)
        finally:
            self._secure_runtime_files()

    def executescript(self, *args, **kwargs):
        try:
            return super().executescript(*args, **kwargs)
        finally:
            self._secure_runtime_files()

    def commit(self) -> None:
        try:
            super().commit()
        finally:
            self._secure_runtime_files()

    def rollback(self) -> None:
        try:
            super().rollback()
        finally:
            self._secure_runtime_files()

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._secure_runtime_files()


class AccountAdminStore:
    def __init__(self, data_dir: Path):
        self.data_dir = absolute_path(Path(data_dir))
        self.auth_path = self.data_dir / "auth.sqlite3"
        self.audit_path = self.data_dir / "audit.sqlite3"

    def initialize(self) -> None:
        secure_directory(self.data_dir, create=True)
        self._apply_migrations(self.auth_path, MIGRATIONS_ROOT / "auth")
        self._apply_migrations(self.audit_path, MIGRATIONS_ROOT / "audit")

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        path = absolute_path(path)
        secure_directory(path.parent, create=True)
        if path_lexists(path):
            secure_file(path)
        else:
            create_secure_file(path)
        secure_sqlite_family(path)
        connection = sqlite3.connect(path, timeout=10, factory=_SecureSQLiteConnection)
        connection._security_path = path
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            secure_sqlite_family(path)
            return connection
        except Exception:
            connection.close()
            raise

    def auth_connection(self) -> sqlite3.Connection:
        return self._connect(self.auth_path)

    def audit_connection(self) -> sqlite3.Connection:
        return self._connect(self.audit_path)

    @staticmethod
    def _apply_migrations(path: Path, migration_dir: Path) -> None:
        connection = AccountAdminStore._connect(path)
        try:
            for migration in sorted(migration_dir.glob("*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.name, format_utc(utc_now())),
                )
                connection.commit()
        finally:
            try:
                connection.close()
            finally:
                secure_sqlite_family(path)

    @contextmanager
    def auth_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.auth_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            try:
                connection.close()
            finally:
                secure_sqlite_family(self.auth_path)

    def seed_principal(self, fixture: Mapping[str, Any]) -> None:
        now = format_utc(utc_now())
        roles = sorted(set(str(role) for role in fixture.get("roles") or []))
        if not roles or any(role not in {"admin", "user"} for role in roles):
            raise StoreError("fixture contains unsupported role")
        with self.auth_transaction() as connection:
            connection.execute(
                """
                INSERT INTO principals(
                    principal_id, fixture_id, auth_source, external_subject, username,
                    employee_id, display_name,
                    status, non_production, created_at, updated_at
                ) VALUES (?, ?, 'local_fixture', NULL, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(principal_id) DO UPDATE SET
                    fixture_id=excluded.fixture_id,
                    username=excluded.username,
                    employee_id=excluded.employee_id,
                    display_name=excluded.display_name,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    fixture["principal_id"],
                    fixture["fixture_id"],
                    fixture["fixture_id"],
                    fixture["employee_id"],
                    fixture["display_name"],
                    fixture["status"],
                    now,
                    now,
                ),
            )
            connection.execute("DELETE FROM role_bindings WHERE principal_id=?", (fixture["principal_id"],))
            connection.executemany(
                "INSERT INTO role_bindings(principal_id, role, created_at) VALUES (?, ?, ?)",
                [(fixture["principal_id"], role, now) for role in roles],
            )

    def create_local_principal(
        self,
        principal: Mapping[str, Any],
        credential: PasswordCredential,
        *,
        role: str,
        event: Mapping[str, Any],
    ) -> None:
        if role not in {"admin", "user"}:
            raise StoreError("unsupported role")
        with self.auth_transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO principals(
                        principal_id, fixture_id, auth_source, external_subject, username,
                        employee_id, display_name, status, non_production, created_at, updated_at
                    ) VALUES (?, NULL, 'local_account', NULL, ?, ?, ?, 'active', 1, ?, ?)
                    """,
                    (
                        principal["principal_id"], principal["username"], principal["employee_id"],
                        principal["display_name"], principal["created_at"], principal["created_at"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO local_credentials(
                        principal_id, algorithm, salt_b64, password_hash_b64, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        principal["principal_id"], credential.algorithm, credential.salt_b64,
                        credential.password_hash_b64, principal["created_at"], principal["created_at"],
                    ),
                )
                connection.execute(
                    "INSERT INTO role_bindings(principal_id, role, created_at) VALUES (?, ?, ?)",
                    (principal["principal_id"], role, principal["created_at"]),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("username or employee ID already exists") from exc
            self._enqueue(connection, event)

    @staticmethod
    def _principal_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        roles = [
            item["role"]
            for item in connection.execute(
                "SELECT role FROM role_bindings WHERE principal_id=? ORDER BY role",
                (row["principal_id"],),
            ).fetchall()
        ]
        value = dict(row)
        value["roles"] = roles
        value["non_production"] = bool(value["non_production"])
        return value

    def get_principal_by_fixture(self, fixture_id: str) -> dict[str, Any] | None:
        with closing(self.auth_connection()) as connection:
            row = connection.execute("SELECT * FROM principals WHERE fixture_id=?", (fixture_id,)).fetchone()
            return self._principal_from_row(connection, row) if row else None

    def get_principal(self, principal_id: str) -> dict[str, Any] | None:
        with closing(self.auth_connection()) as connection:
            row = connection.execute("SELECT * FROM principals WHERE principal_id=?", (principal_id,)).fetchone()
            return self._principal_from_row(connection, row) if row else None

    def get_principal_by_username(self, username: str) -> dict[str, Any] | None:
        with closing(self.auth_connection()) as connection:
            row = connection.execute(
                "SELECT * FROM principals WHERE username=? COLLATE NOCASE",
                (username,),
            ).fetchone()
            return self._principal_from_row(connection, row) if row else None

    def get_local_credential(self, principal_id: str) -> PasswordCredential | None:
        with closing(self.auth_connection()) as connection:
            row = connection.execute(
                "SELECT algorithm, salt_b64, password_hash_b64 FROM local_credentials WHERE principal_id=?",
                (principal_id,),
            ).fetchone()
        return PasswordCredential(**dict(row)) if row else None

    def list_principals(self) -> list[dict[str, Any]]:
        with closing(self.auth_connection()) as connection:
            rows = connection.execute("SELECT * FROM principals ORDER BY employee_id").fetchall()
            return [self._principal_from_row(connection, row) for row in rows]

    def set_principal_status(
        self,
        principal_id: str,
        status: str,
        *,
        updated_at: str,
        event: Mapping[str, Any],
    ) -> bool:
        if status not in {"active", "disabled"}:
            raise StoreError("unsupported principal status")
        with self.auth_transaction() as connection:
            changed = connection.execute(
                "UPDATE principals SET status=?, updated_at=? WHERE principal_id=? AND status<>?",
                (status, updated_at, principal_id, status),
            ).rowcount
            if changed and status == "disabled":
                connection.execute(
                    """
                    UPDATE sessions SET status='revoked', revoked_at=?, revoke_reason='principal_disabled'
                    WHERE principal_id=? AND status='active'
                    """,
                    (updated_at, principal_id),
                )
            if changed:
                self._enqueue(connection, event)
            return bool(changed)

    def reset_local_password(
        self,
        principal_id: str,
        credential: PasswordCredential,
        *,
        updated_at: str,
        event: Mapping[str, Any],
    ) -> bool:
        with self.auth_transaction() as connection:
            changed = connection.execute(
                """
                UPDATE local_credentials
                SET algorithm=?, salt_b64=?, password_hash_b64=?, updated_at=?
                WHERE principal_id=?
                """,
                (
                    credential.algorithm, credential.salt_b64, credential.password_hash_b64,
                    updated_at, principal_id,
                ),
            ).rowcount
            if changed:
                connection.execute(
                    """
                    UPDATE sessions SET status='revoked', revoked_at=?, revoke_reason='password_reset'
                    WHERE principal_id=? AND status='active'
                    """,
                    (updated_at, principal_id),
                )
                self._enqueue(connection, event)
            return bool(changed)

    @staticmethod
    def _enqueue(connection: sqlite3.Connection, event: Mapping[str, Any]) -> None:
        connection.execute(
            "INSERT INTO audit_outbox(event_id, payload_json, created_at) VALUES (?, ?, ?)",
            (
                event["event_id"],
                json.dumps(dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                event["occurred_at"],
            ),
        )

    def enqueue_audit_event(self, event: Mapping[str, Any]) -> None:
        with self.auth_transaction() as connection:
            self._enqueue(connection, event)

    def create_session(self, session: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        with self.auth_transaction() as connection:
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, principal_id, token_hash, csrf_hash, status,
                    created_at, expires_at, last_seen_at, remember_me, auth_verified_at,
                    rotated_from_session_id
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["session_id"],
                    session["principal_id"],
                    session["token_hash"],
                    session["csrf_hash"],
                    session["created_at"],
                    session["expires_at"],
                    session["created_at"],
                    int(bool(session["remember_me"])),
                    session.get("auth_verified_at", session["created_at"]),
                    session.get("rotated_from_session_id"),
                ),
            )
            self._enqueue(connection, event)

    def get_active_session(self, token_hash: str, now_iso: str) -> dict[str, Any] | None:
        with self.auth_transaction() as connection:
            row = connection.execute(
                """
                SELECT s.*, p.fixture_id, p.auth_source, p.external_subject, p.username,
                       p.employee_id, p.display_name,
                       p.status AS principal_status, p.non_production
                FROM sessions s JOIN principals p ON p.principal_id=s.principal_id
                WHERE s.token_hash=?
                """,
                (token_hash,),
            ).fetchone()
            if not row or row["status"] != "active" or row["principal_status"] != "active":
                return None
            if row["expires_at"] <= now_iso:
                connection.execute(
                    "UPDATE sessions SET status='expired', revoked_at=?, revoke_reason='expired' WHERE session_id=?",
                    (now_iso, row["session_id"]),
                )
                return None
            connection.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?", (now_iso, row["session_id"]))
            roles = [
                item["role"]
                for item in connection.execute(
                    "SELECT role FROM role_bindings WHERE principal_id=? ORDER BY role",
                    (row["principal_id"],),
                ).fetchall()
            ]
            result = dict(row)
            result["roles"] = roles
            result["non_production"] = bool(result["non_production"])
            return result

    def rotate_session(
        self,
        *,
        old_session_id: str,
        new_session: Mapping[str, Any],
        revoke_event: Mapping[str, Any],
        create_event: Mapping[str, Any],
    ) -> None:
        with self.auth_transaction() as connection:
            changed = connection.execute(
                """
                UPDATE sessions SET status='revoked', revoked_at=?, revoke_reason='rotated'
                WHERE session_id=? AND status='active'
                """,
                (new_session["created_at"], old_session_id),
            ).rowcount
            if changed != 1:
                raise StoreError("session is not active")
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, principal_id, token_hash, csrf_hash, status,
                    created_at, expires_at, last_seen_at, remember_me, auth_verified_at,
                    rotated_from_session_id
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_session["session_id"],
                    new_session["principal_id"],
                    new_session["token_hash"],
                    new_session["csrf_hash"],
                    new_session["created_at"],
                    new_session["expires_at"],
                    new_session["created_at"],
                    int(bool(new_session["remember_me"])),
                    new_session.get("auth_verified_at", new_session["created_at"]),
                    old_session_id,
                ),
            )
            self._enqueue(connection, revoke_event)
            self._enqueue(connection, create_event)

    def revoke_session(
        self,
        session_id: str,
        *,
        revoked_at: str,
        reason: str,
        event: Mapping[str, Any],
    ) -> bool:
        with self.auth_transaction() as connection:
            changed = connection.execute(
                """
                UPDATE sessions SET status='revoked', revoked_at=?, revoke_reason=?
                WHERE session_id=? AND status='active'
                """,
                (revoked_at, reason, session_id),
            ).rowcount
            if changed:
                self._enqueue(connection, event)
            return bool(changed)

    def pending_outbox_count(self) -> int:
        with closing(self.auth_connection()) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM audit_outbox WHERE delivered_at IS NULL").fetchone()[0])

    def dispatch_audit_outbox(self, limit: int = 100) -> int:
        with closing(self.auth_connection()) as auth:
            rows = auth.execute(
                "SELECT event_id, payload_json FROM audit_outbox WHERE delivered_at IS NULL ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        delivered: list[str] = []
        with closing(self.audit_connection()) as audit:
            for row in rows:
                event = json.loads(row["payload_json"])
                audit.execute(
                    """
                    INSERT OR IGNORE INTO audit_events(
                        event_id, schema_version, occurred_at, actor_principal_id,
                        actor_employee_id, actor_display_name, actor_roles_json,
                        session_id, request_id, correlation_id, module, action,
                        resource_type, resource_id, result, reason_code, client_ip,
                        user_agent, policy_version, changes_json, metadata_json,
                        event_hash, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"], event["schema_version"], event["occurred_at"],
                        event["actor_principal_id"], event["actor_employee_id"],
                        event["actor_display_name"], json.dumps(event["actor_roles"], ensure_ascii=False),
                        event["session_id"], event["request_id"], event["correlation_id"],
                        event["module"], event["action"], event["resource_type"],
                        event["resource_id"], event["result"], event["reason_code"],
                        event["client_ip"], event["user_agent"], event["policy_version"],
                        json.dumps(event["changes"], ensure_ascii=False, sort_keys=True),
                        json.dumps(event["metadata"], ensure_ascii=False, sort_keys=True),
                        event_hash(event), format_utc(utc_now()),
                    ),
                )
                delivered.append(row["event_id"])
            audit.commit()
        if delivered:
            delivered_at = format_utc(utc_now())
            with self.auth_transaction() as auth:
                auth.executemany(
                    "UPDATE audit_outbox SET delivered_at=? WHERE event_id=? AND delivered_at IS NULL",
                    [(delivered_at, event_id) for event_id in delivered],
                )
        return len(delivered)

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        with closing(self.audit_connection()) as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY occurred_at DESC, rowid DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["actor_roles"] = json.loads(event.pop("actor_roles_json"))
            event["changes"] = json.loads(event.pop("changes_json"))
            event["metadata"] = json.loads(event.pop("metadata_json"))
            result.append(event)
        return result

    def integrity(self) -> dict[str, str]:
        values = {}
        for name, opener in (("auth", self.auth_connection), ("audit", self.audit_connection)):
            with closing(opener()) as connection:
                values[name] = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        return values
