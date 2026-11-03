from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import fcntl
import unicodedata
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reporting.source import canonical_json, sha256_text


TERMINAL_STATES = {"completed", "failed", "cancelled"}
NON_RETRYABLE_ERROR_CODES = {"REPORT_SCOPE_NOT_READY"}
NON_RETRYABLE_MESSAGE_MARKERS = (
    "低于 pilot 门槛",
    "below pilot threshold",
)
REPORT_ID_PATTERN = re.compile(r"^rpt_[0-9a-f]{24}$")
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DISPLAY_NAME_EVENT_SCHEMA = "report-display-name-event/v1"
DISPLAY_NAME_METADATA_SCHEMA = "report-display-name-metadata/v1"
DISPLAY_NAME_FAILURE_SCHEMA = "report-display-name-failure/v1"
DISPLAY_NAME_MAX_LENGTH = 100
DISPLAY_NAME_EVENT_FILE_PATTERN = re.compile(
    r"^rpt_[0-9a-f]{24}-[0-9a-f]{64}\.json$"
)
DISPLAY_NAME_FAILURE_FILE_PATTERN = re.compile(r"^rdnf_[0-9a-f]{64}\.json$")


class ReportDisplayNameConflict(ValueError):
    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("report display name changed")
        self.current = current


class ReportDisplayNameLedgerError(RuntimeError):
    pass


def report_error_retryable(error: Any) -> bool:
    """Normalize legacy readiness failures to the current retry contract."""
    if not isinstance(error, dict):
        return True
    if error.get("retryable") is False:
        return False
    if str(error.get("code") or "") in NON_RETRYABLE_ERROR_CODES:
        return False
    message = str(error.get("message") or "")
    return not any(marker in message for marker in NON_RETRYABLE_MESSAGE_MARKERS)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


class ReportStore:
    def __init__(self, root: Path):
        self.root = root
        self.jobs_dir = root / "jobs"
        self.reports_dir = root / "reports"
        self.metadata_dir = root / "metadata"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        if self.metadata_dir.is_symlink():
            raise ReportDisplayNameLedgerError(
                "reporting metadata directory must not be a symlink"
            )
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.display_name_events_dir = (
            self.metadata_dir / "report-display-name-events"
        )
        self.display_name_failures_dir = (
            self.metadata_dir / "report-display-name-failures"
        )
        self._ensure_private_directory(self.display_name_events_dir)
        self._ensure_private_directory(self.display_name_failures_dir)
        self._validate_private_audit_files(
            self.display_name_events_dir, DISPLAY_NAME_EVENT_FILE_PATTERN
        )
        self._validate_private_audit_files(
            self.display_name_failures_dir, DISPLAY_NAME_FAILURE_FILE_PATTERN
        )
        self.locks_dir = root / "locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self._card_cache_lock = threading.Lock()
        self._card_cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

    def _heartbeat_path(self, job_id: str) -> Path:
        return self.root / "scheduler" / "heartbeats" / f"report-{job_id}.json"

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    @contextmanager
    def _job_lock(self, job_id: str):
        path = self.locks_dir / f"{job_id}.lock"
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _report_path(self, report_id: str) -> Path:
        return self.reports_dir / f"{report_id}.json"

    def _archive_path(self, report_id: str) -> Path:
        return self.metadata_dir / f"archive-{report_id}.json"

    def _report_card_cache_path(self, report_id: str) -> Path:
        return self.metadata_dir / f"card-{report_id}.json"

    @contextmanager
    def _display_name_lock(self, report_id: str):
        path = self.locks_dir / f"display-name-{report_id}.lock"
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _display_name_base_version(report: dict[str, Any]) -> str:
        identity = {
            "schema_version": "report-display-name-base/v1",
            "report_id": report["report_id"],
            "original_title_sha256": "sha256:"
            + sha256_text(str(report["title"])),
        }
        return "sha256:" + sha256_text(canonical_json(identity))

    @staticmethod
    def _normalize_custom_display_name(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("display_name 必须是字符串或 null")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("display_name 不能为空；恢复原始标题请使用 null")
        if len(cleaned) > DISPLAY_NAME_MAX_LENGTH:
            raise ValueError(
                f"display_name 不能超过 {DISPLAY_NAME_MAX_LENGTH} 个字符"
            )
        if "<" in cleaned or ">" in cleaned:
            raise ValueError("display_name 不得包含 HTML")
        if any(unicodedata.category(character).startswith("C") for character in cleaned):
            raise ValueError("display_name 不得包含控制字符")
        return cleaned

    @staticmethod
    def _display_name_event_body(
        *,
        report: dict[str, Any],
        previous_version: str,
        previous_display_name: str,
        custom_display_name: str | None,
        actor: dict[str, str],
        occurred_at: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": DISPLAY_NAME_EVENT_SCHEMA,
            "report_id": report["report_id"],
            "previous_version": previous_version,
            "previous_display_name": previous_display_name,
            "original_title_sha256": "sha256:"
            + sha256_text(str(report["title"])),
            "operation": (
                "clear" if custom_display_name is None else "rename"
            ),
            "custom_display_name": custom_display_name,
            "display_name": custom_display_name or str(report["title"]),
            "actor": actor,
            "occurred_at": occurred_at,
        }

    @staticmethod
    def _display_name_event_version(body: dict[str, Any]) -> str:
        return "sha256:" + sha256_text(canonical_json(body))

    def _display_name_event_path(self, report_id: str, version: str) -> Path:
        return self.display_name_events_dir / (
            f"{report_id}-{version.removeprefix('sha256:')}.json"
        )

    @staticmethod
    def _immutable_json_bytes(payload: dict[str, Any]) -> bytes:
        return (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        details = os.lstat(path)
        if stat.S_ISLNK(details.st_mode):
            raise ReportDisplayNameLedgerError(
                f"display-name audit directory must not be a symlink: {path.name}"
            )
        if not stat.S_ISDIR(details.st_mode):
            raise ReportDisplayNameLedgerError(
                f"display-name audit path is not a directory: {path.name}"
            )
        if details.st_uid != os.geteuid():
            raise ReportDisplayNameLedgerError(
                f"display-name audit directory owner is invalid: {path.name}"
            )
        if stat.S_IMODE(details.st_mode) != 0o700:
            raise ReportDisplayNameLedgerError(
                f"display-name audit directory mode must be 0700: {path.name}"
            )

    @staticmethod
    def _validate_private_audit_file(path: Path) -> None:
        details = os.lstat(path)
        if not stat.S_ISREG(details.st_mode):
            raise ReportDisplayNameLedgerError(
                f"display-name audit file must be regular: {path.name}"
            )
        if details.st_uid != os.geteuid():
            raise ReportDisplayNameLedgerError(
                f"display-name audit file owner is invalid: {path.name}"
            )
        if stat.S_IMODE(details.st_mode) != 0o400:
            raise ReportDisplayNameLedgerError(
                f"display-name audit file mode must be 0400: {path.name}"
            )

    @staticmethod
    def _validate_private_temp_file(file_descriptor: int) -> None:
        details = os.fstat(file_descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ReportDisplayNameLedgerError(
                "display-name audit temporary file must be regular"
            )
        if details.st_uid != os.geteuid():
            raise ReportDisplayNameLedgerError(
                "display-name audit temporary file owner is invalid"
            )
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise ReportDisplayNameLedgerError(
                "display-name audit temporary file mode must be 0600"
            )

    @classmethod
    def _validate_private_audit_files(
        cls, directory: Path, filename_pattern: re.Pattern[str]
    ) -> None:
        for path in directory.iterdir():
            if not filename_pattern.fullmatch(path.name):
                raise ReportDisplayNameLedgerError(
                    f"unexpected display-name audit entry: {path.name}"
                )
            cls._validate_private_audit_file(path)

    def _write_immutable_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self._immutable_json_bytes(payload)
        if path.exists() or path.is_symlink():
            self._validate_private_audit_file(path)
            if path.read_bytes() != content:
                raise ReportDisplayNameLedgerError(
                    f"immutable display-name event conflicts: {path.name}"
                )
            return
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb", dir=path.parent, delete=False
            ) as handle:
                temp_path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                self._validate_private_temp_file(handle.fileno())
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.chmod(0o400)
            self._validate_private_audit_file(temp_path)
            os.replace(temp_path, path)
            self._validate_private_audit_file(path)
            self._fsync_directory(path.parent)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _validate_display_name_event(
        self, event: Any, *, report: dict[str, Any], path: Path
    ) -> dict[str, Any]:
        required = {
            "schema_version",
            "report_id",
            "previous_version",
            "previous_display_name",
            "original_title_sha256",
            "operation",
            "custom_display_name",
            "display_name",
            "actor",
            "occurred_at",
            "version",
        }
        if not isinstance(event, dict) or set(event) != required:
            raise ReportDisplayNameLedgerError(
                f"display-name event shape invalid: {path.name}"
            )
        if (
            event["schema_version"] != DISPLAY_NAME_EVENT_SCHEMA
            or event["report_id"] != report["report_id"]
            or not SHA256_PATTERN.fullmatch(str(event["previous_version"]))
            or not SHA256_PATTERN.fullmatch(str(event["version"]))
            or not isinstance(event["actor"], dict)
            or set(event["actor"])
            != {"authority_id", "principal_id", "request_id"}
            or not all(
                isinstance(event["actor"].get(key), str)
                and event["actor"].get(key)
                for key in ("authority_id", "principal_id", "request_id")
            )
            or not isinstance(event["occurred_at"], str)
            or not event["occurred_at"]
            or not isinstance(event["previous_display_name"], str)
            or not event["previous_display_name"]
            or not isinstance(event["display_name"], str)
            or not event["display_name"]
        ):
            raise ReportDisplayNameLedgerError(
                f"display-name event contract invalid: {path.name}"
            )
        expected_original_hash = "sha256:" + sha256_text(str(report["title"]))
        custom = event["custom_display_name"]
        if (
            event["original_title_sha256"] != expected_original_hash
            or event["operation"]
            != ("clear" if custom is None else "rename")
        ):
            raise ReportDisplayNameLedgerError(
                f"display-name event report identity invalid: {path.name}"
            )
        try:
            normalized = self._normalize_custom_display_name(custom)
        except ValueError as error:
            raise ReportDisplayNameLedgerError(
                f"display-name event value invalid: {path.name}"
            ) from error
        if normalized != custom:
            raise ReportDisplayNameLedgerError(
                f"display-name event value is not canonical: {path.name}"
            )
        body = {key: event[key] for key in required if key != "version"}
        expected_version = self._display_name_event_version(body)
        if (
            event["version"] != expected_version
            or path != self._display_name_event_path(report["report_id"], expected_version)
        ):
            raise ReportDisplayNameLedgerError(
                f"display-name event content address invalid: {path.name}"
            )
        return event

    def display_name_metadata_for_report(
        self, report: dict[str, Any]
    ) -> dict[str, Any]:
        report_id = str(report["report_id"])
        base_version = self._display_name_base_version(report)
        events: list[dict[str, Any]] = []
        for path in sorted(
            self.display_name_events_dir.glob(f"{report_id}-*.json")
        ):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ReportDisplayNameLedgerError(
                    f"display-name event unreadable: {path.name}"
                ) from error
            events.append(
                self._validate_display_name_event(raw, report=report, path=path)
            )
        children: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            children.setdefault(str(event["previous_version"]), []).append(event)
        current_version = base_version
        custom_display_name: str | None = None
        current_display_name = str(report["title"])
        updated_at: str | None = None
        visited: set[str] = set()
        while current_version in children:
            candidates = children[current_version]
            if len(candidates) != 1:
                raise ReportDisplayNameLedgerError(
                    f"display-name event chain forked for {report_id}"
                )
            event = candidates[0]
            if event["previous_display_name"] != current_display_name:
                raise ReportDisplayNameLedgerError(
                    f"display-name event old value conflicts for {report_id}"
                )
            expected_display_name = event["custom_display_name"] or str(
                report["title"]
            )
            if event["display_name"] != expected_display_name:
                raise ReportDisplayNameLedgerError(
                    f"display-name event new value conflicts for {report_id}"
                )
            version = str(event["version"])
            if version in visited:
                raise ReportDisplayNameLedgerError(
                    f"display-name event chain cycles for {report_id}"
                )
            visited.add(version)
            current_version = version
            custom_display_name = event["custom_display_name"]
            current_display_name = expected_display_name
            updated_at = event["occurred_at"]
        if len(visited) != len(events):
            raise ReportDisplayNameLedgerError(
                f"display-name event chain is disconnected for {report_id}"
            )
        original_title = str(report["title"])
        return {
            "schema_version": DISPLAY_NAME_METADATA_SCHEMA,
            "report_id": report_id,
            "display_name": custom_display_name or original_title,
            "original_title": original_title,
            "custom_display_name": custom_display_name,
            "version": current_version,
            "updated_at": updated_at,
        }

    def get_report_view(self, report_id: str) -> dict[str, Any] | None:
        report = self.get_report(report_id)
        if report is None:
            return None
        metadata = self.display_name_metadata_for_report(report)
        return {
            **report,
            "display_name": metadata["display_name"],
            "original_title": metadata["original_title"],
            "custom_display_name": metadata["custom_display_name"],
            "display_name_version": metadata["version"],
            "display_name_updated_at": metadata["updated_at"],
            "display_name_metadata": metadata,
        }

    def _write_display_name_failure(
        self,
        *,
        report: dict[str, Any],
        actor: dict[str, str],
        code: str,
        expected_version: str,
        current_version: str,
        requested_display_name: str | None,
    ) -> None:
        body = {
            "schema_version": DISPLAY_NAME_FAILURE_SCHEMA,
            "report_id": report["report_id"],
            "outcome": "rejected",
            "code": code,
            "expected_version": expected_version,
            "current_version": current_version,
            "requested_display_name_sha256": "sha256:"
            + sha256_text("" if requested_display_name is None else requested_display_name),
            "actor": actor,
            "occurred_at": utc_now(),
        }
        digest = sha256_text(canonical_json(body))
        payload = {**body, "failure_id": f"rdnf_{digest[:24]}"}
        self._write_immutable_json(
            self.display_name_failures_dir / f"rdnf_{digest}.json", payload
        )

    def set_report_display_name(
        self,
        report_id: str,
        *,
        display_name: Any,
        expected_version: Any,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        if not REPORT_ID_PATTERN.fullmatch(str(report_id or "")):
            raise ValueError("invalid report_id")
        if not isinstance(expected_version, str) or not SHA256_PATTERN.fullmatch(
            expected_version
        ):
            raise ValueError("expected_version 必须是合法 SHA-256 版本")
        if (
            not isinstance(actor, dict)
            or set(actor) != {"authority_id", "principal_id", "request_id"}
            or not all(isinstance(value, str) and value for value in actor.values())
        ):
            raise ValueError("actor identity invalid")
        report = self.get_report(report_id)
        if report is None:
            raise KeyError(report_id)
        try:
            normalized = self._normalize_custom_display_name(display_name)
        except ValueError:
            current = self.display_name_metadata_for_report(report)
            self._write_display_name_failure(
                report=report,
                actor=actor,
                code="invalid_display_name",
                expected_version=expected_version,
                current_version=str(current["version"]),
                requested_display_name=(
                    display_name if isinstance(display_name, str) else None
                ),
            )
            raise
        with self._display_name_lock(report_id):
            report = self.get_report(report_id)
            if report is None:
                raise KeyError(report_id)
            current = self.display_name_metadata_for_report(report)
            if expected_version != current["version"]:
                self._write_display_name_failure(
                    report=report,
                    actor=actor,
                    code="display_name_conflict",
                    expected_version=expected_version,
                    current_version=str(current["version"]),
                    requested_display_name=normalized,
                )
                raise ReportDisplayNameConflict(current)
            if normalized == current["custom_display_name"]:
                return {**current, "changed": False}
            body = self._display_name_event_body(
                report=report,
                previous_version=str(current["version"]),
                previous_display_name=str(current["display_name"]),
                custom_display_name=normalized,
                actor=actor,
                occurred_at=utc_now(),
            )
            version = self._display_name_event_version(body)
            event = {**body, "version": version}
            self._write_immutable_json(
                self._display_name_event_path(report_id, version), event
            )
            updated = self.display_name_metadata_for_report(report)
            return {**updated, "changed": True}

    def create_job(
        self,
        request: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        retry_of: str | None = None,
        root_job_id: str | None = None,
        attempt_no: int = 1,
    ) -> dict[str, Any]:
        request_hash = sha256_text(canonical_json(request))
        identity = idempotency_key or request_hash
        job_id = f"rjob_{sha256_text(identity)[:24]}"
        path = self._job_path(job_id)
        with self._job_lock(job_id):
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            now = utc_now()
            job = {
                "job_id": job_id,
                "status": "queued",
                "stage_label": "排队中",
                "progress": 0,
                "request": request,
                "request_sha256": f"sha256:{request_hash}",
                "job_group_id": f"rgrp_{request_hash[:24]}",
                "retry_of": retry_of,
                "root_job_id": root_job_id or job_id,
                "attempt_no": max(1, int(attempt_no)),
                "created_at": now,
                "updated_at": now,
                "heartbeat_at": None,
                "worker_pid": None,
                "report_id": None,
                "error": None,
                "cancel_requested": False,
                "events": [
                    {
                        "status": "queued",
                        "stage_label": "排队中",
                        "progress": 0,
                        "at": now,
                    }
                ],
            }
            atomic_json(path, job)
        return job

    @staticmethod
    def _job_group_id(job: dict[str, Any]) -> str:
        existing = str(job.get("job_group_id") or "").strip()
        if existing.startswith("rgrp_"):
            return existing
        request_hash = str(job.get("request_sha256") or "").removeprefix("sha256:")
        if not request_hash:
            request_hash = sha256_text(canonical_json(job.get("request") or {}))
        return f"rgrp_{request_hash[:24]}"

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        path = self._job_path(job_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def update_job(
        self,
        job_id: str,
        status: str,
        stage_label: str,
        progress: int,
        **changes: Any,
    ) -> dict[str, Any]:
        with self._job_lock(job_id):
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] in TERMINAL_STATES and status != job["status"]:
                return job
            now = utc_now()
            job.update(changes)
            job.update(
                {
                    "status": status,
                    "stage_label": stage_label,
                    "progress": progress,
                    "updated_at": now,
                }
            )
            job["events"].append(
                {
                    "status": status,
                    "stage_label": stage_label,
                    "progress": progress,
                    "at": now,
                }
            )
            atomic_json(self._job_path(job_id), job)
        return job

    def touch_worker(self, job_id: str, *, worker_pid: int | None = None) -> dict[str, Any]:
        """Persist a heartbeat sidecar without racing the authoritative job JSON."""
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.get("status") in TERMINAL_STATES:
            return job
        now = utc_now()
        heartbeat = {
            "schema_version": "report-worker-heartbeat/v1",
            "kind": "report",
            "job_id": job_id,
            "heartbeat_at": now,
            "worker_pid": int(worker_pid) if worker_pid is not None else None,
        }
        atomic_json(self._heartbeat_path(job_id), heartbeat)
        return {**job, **heartbeat}

    def get_worker_heartbeat(self, job_id: str) -> dict[str, Any] | None:
        path = self._heartbeat_path(job_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def clear_worker_heartbeat(self, job_id: str) -> None:
        self._heartbeat_path(job_id).unlink(missing_ok=True)

    def requeue_interrupted_jobs(self) -> list[str]:
        """Recover non-terminal work after its previous worker has disappeared.

        Jobs which never started remain queued as-is.  Running stages restart
        from the beginning under the same stable job ID, preserving the prior
        stage/progress in the event history instead of fabricating a 100%
        failure.
        """
        recovered: list[str] = []
        for listed_job in self.list_jobs():
            job_id = str(listed_job["job_id"])
            with self._job_lock(job_id):
                job = self.get_job(job_id) or {}
                if (
                    job.get("status") in TERMINAL_STATES
                    or job.get("status") == "queued"
                ):
                    continue
                previous_status = str(job.get("status") or "")
                previous_stage = str(job.get("stage_label") or "")
                previous_progress = int(job.get("progress") or 0)
                now = utc_now()
                job.update(
                    {
                        "status": "queued",
                        "stage_label": "服务恢复，等待重新生成",
                        "progress": 0,
                        "last_status": previous_status,
                        "last_stage_label": previous_stage,
                        "last_progress": previous_progress,
                        "worker_pid": None,
                        "heartbeat_at": None,
                        "updated_at": now,
                    }
                )
                job.setdefault("events", []).append(
                    {
                        "status": "queued",
                        "stage_label": "服务恢复，等待重新生成",
                        "progress": 0,
                        "at": now,
                        "recovered_from": {
                            "status": previous_status,
                            "stage_label": previous_stage,
                            "progress": previous_progress,
                        },
                    }
                )
                atomic_json(self._job_path(job_id), job)
                recovered.append(job_id)
        recovered.sort()
        return recovered

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        with self._job_lock(job_id):
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] in TERMINAL_STATES:
                return job
            job["cancel_requested"] = True
            job["updated_at"] = utc_now()
            atomic_json(self._job_path(job_id), job)
        return job

    def store_report(self, report: dict[str, Any]) -> Path:
        path = self._report_path(report["report_id"])
        if not path.exists():
            atomic_json(path, report)
        self._write_report_card_cache(path, self._report_card(report))
        return path

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        path = self._report_path(report_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def set_archived(
        self, report_id: str, archived: bool = True
    ) -> dict[str, Any]:
        if not REPORT_ID_PATTERN.fullmatch(str(report_id or "")):
            raise ValueError("invalid report_id")
        if self.get_report(report_id) is None:
            raise KeyError(report_id)
        marker = self._archive_path(report_id)
        if archived:
            payload = {
                "report_id": report_id,
                "archived": True,
                "archived_at": utc_now(),
            }
            atomic_json(marker, payload)
            return payload
        if marker.exists():
            marker.unlink()
        return {"report_id": report_id, "archived": False}

    def is_archived(self, report_id: str) -> bool:
        return self._archive_path(report_id).exists()

    def retry_job(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in {"failed", "cancelled"}:
            return job
        error = job.get("error") or {}
        if not report_error_retryable(error):
            return job
        return self.create_job(
            job["request"],
            idempotency_key=f"retry:{job_id}:{utc_now()}",
            retry_of=job_id,
            root_job_id=str(job.get("root_job_id") or job_id),
            attempt_no=int(job.get("attempt_no") or 1) + 1,
        )

    def delete_failed_job(self, job_id: str) -> dict[str, Any]:
        """Remove a failed task record without touching immutable reports."""
        if (
            len(job_id) != 29
            or not job_id.startswith("rjob_")
            or any(character not in "0123456789abcdef" for character in job_id[5:])
        ):
            raise KeyError(job_id)
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.get("status") != "failed":
            raise ValueError("only failed report jobs can be deleted")
        report_id = str(job.get("report_id") or "").strip()
        if report_id:
            raise ValueError(
                "failed report job links to an immutable report artifact"
            )
        self._job_path(job_id).unlink()
        return {"job_id": job_id, "deleted": True}

    def list_jobs(self) -> list[dict[str, Any]]:
        jobs = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in self.jobs_dir.glob("rjob_*.json")
        ]
        jobs.sort(key=lambda item: (item["created_at"], item["job_id"]), reverse=True)
        return jobs

    @staticmethod
    def _attempt_summary(job: dict[str, Any]) -> dict[str, Any]:
        error = job.get("error") or {}
        return {
            "job_id": job.get("job_id"),
            "status": job.get("status"),
            "stage_label": job.get("stage_label"),
            "progress": int(job.get("progress") or 0),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "attempt_no": int(job.get("attempt_no") or 1),
            "retry_of": job.get("retry_of"),
            "report_id": job.get("report_id"),
            "error": error if isinstance(error, dict) else {"message": str(error)},
        }

    def get_job_group(self, job_group_id: str) -> dict[str, Any] | None:
        if (
            len(job_group_id) != 29
            or not job_group_id.startswith("rgrp_")
            or any(character not in "0123456789abcdef" for character in job_group_id[5:])
        ):
            return None
        jobs = [
            job
            for job in self.list_jobs()
            if self._job_group_id(job) == job_group_id
        ]
        if not jobs:
            return None
        jobs.sort(
            key=lambda item: (
                int(item.get("attempt_no") or 1),
                str(item.get("created_at") or ""),
                str(item.get("job_id") or ""),
            ),
            reverse=True,
        )
        attempts = []
        for job in jobs:
            attempts.append(
                {
                    **self._attempt_summary(job),
                    "events": list(job.get("events") or []),
                }
            )
        return {
            "schema_version": "report-job-history/v1",
            "job_group_id": job_group_id,
            "request_sha256": jobs[0].get("request_sha256"),
            "request": jobs[0].get("request") or {},
            "attempt_count": len(attempts),
            "attempts": attempts,
        }

    def _report_card(self, report: dict[str, Any]) -> dict[str, Any]:
        samples = report.get("samples") or {}
        limitations = report.get("limitations") or []
        severities = {item.get("severity") for item in limitations}
        grade = (
            "C"
            if "blocking" in severities
            else "B"
            if severities & {"material", "blocking-for-metric"}
            else "A"
        )
        metadata = self.display_name_metadata_for_report(report)
        return {
            "report_id": report["report_id"],
            "title": metadata["display_name"],
            "display_name": metadata["display_name"],
            "original_title": metadata["original_title"],
            "custom_display_name": metadata["custom_display_name"],
            "display_name_version": metadata["version"],
            "display_name_updated_at": metadata["updated_at"],
            "report_type": report.get("report_type", "quantitative_core"),
            "status": report.get("status", "completed"),
            "primary_direction": (report.get("research_plan") or {}).get(
                "primary_direction"
            ),
            "analysis_modules": (report.get("research_plan") or {}).get(
                "analysis_modules", []
            ),
            "factors": (report.get("research_plan") or {}).get("factors", []),
            "scope": report.get("scope") or {},
            "samples": samples,
            "summary_text": report.get("summary_text"),
            "quality": {
                "grade": grade,
                "limitation_count": len(limitations),
                "headline": limitations[0]["message"] if limitations else "未发现主要限制",
            },
            "analysis_version": report.get("analysis_version"),
            "product_insight_availability": report.get(
                "product_insight_availability"
            ),
            "evidence_summary": (
                (report.get("product_insight") or {}).get("evidence_access")
                or {
                    "evidence_rows": len(report.get("evidence") or []),
                    "evidence_groups": 0,
                }
            ),
            "generated_at": report.get("generated_at"),
            "archived": self.is_archived(report["report_id"]),
        }

    def _write_report_card_cache(
        self, report_path: Path, card: dict[str, Any]
    ) -> None:
        stat = report_path.stat()
        atomic_json(
            self._report_card_cache_path(str(card["report_id"])),
            {
                "schema_version": "report-card-cache/v2",
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "card": {**card, "archived": False},
            },
        )

    def _cached_report_card(self, report_path: Path) -> dict[str, Any]:
        report_id = report_path.stem
        cache_path = self._report_card_cache_path(report_id)
        stat = report_path.stat()
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = {}
            card = cached.get("card") if isinstance(cached, dict) else None
            if (
                isinstance(card, dict)
                and cached.get("schema_version") == "report-card-cache/v2"
                and int(cached.get("source_size") or -1) == stat.st_size
                and int(cached.get("source_mtime_ns") or -1) == stat.st_mtime_ns
            ):
                report_stub = {
                    "report_id": report_id,
                    "title": card.get("original_title"),
                }
                metadata = self.display_name_metadata_for_report(report_stub)
                return {
                    **card,
                    "title": metadata["display_name"],
                    "display_name": metadata["display_name"],
                    "original_title": metadata["original_title"],
                    "custom_display_name": metadata["custom_display_name"],
                    "display_name_version": metadata["version"],
                    "display_name_updated_at": metadata["updated_at"],
                    "archived": self.is_archived(report_id),
                }
        report = json.loads(report_path.read_text(encoding="utf-8"))
        card = self._report_card(report)
        self._write_report_card_cache(report_path, card)
        return card

    def list_reports(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        cards = [
            self._cached_report_card(path)
            for path in self.reports_dir.glob("rpt_*.json")
        ]
        if not include_archived:
            cards = [card for card in cards if not card["archived"]]
        cards.sort(
            key=lambda item: (item.get("generated_at") or "", item["report_id"]),
            reverse=True,
        )
        return cards

    @staticmethod
    def _file_signature(paths: Any) -> tuple[tuple[Any, ...], ...]:
        """Describe authoritative files, including in-place atomic replaces.

        Directory mtimes do not change when an existing job JSON is replaced.
        The inode and nanosecond mtime make that state transition visible while
        keeping derived report-card cache files outside the signature.
        """
        rows: list[tuple[Any, ...]] = []
        for path in sorted(paths, key=lambda item: item.name):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            rows.append(
                (
                    path.name,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ino,
                )
            )
        return tuple(rows)

    def _card_source_signature(self) -> tuple[Any, ...]:
        return (
            self._file_signature(self.jobs_dir.glob("rjob_*.json")),
            self._file_signature(self.reports_dir.glob("rpt_*.json")),
            self._file_signature(
                self.metadata_dir.glob("archive-rpt_*.json")
            ),
            self._file_signature(
                self.display_name_events_dir.glob("rpt_*.json")
            ),
        )

    def _build_cards_with_jobs(
        self, *, include_archived: bool
    ) -> list[dict[str, Any]]:
        cards = self.list_reports(include_archived=include_archived)
        card_by_report_id = {str(card["report_id"]): card for card in cards}
        groups: dict[str, list[dict[str, Any]]] = {}
        for job in self.list_jobs():
            groups.setdefault(self._job_group_id(job), []).append(job)
        for group_id, jobs in groups.items():
            jobs.sort(
                key=lambda item: (
                    int(item.get("attempt_no") or 1),
                    str(item.get("created_at") or ""),
                    str(item.get("job_id") or ""),
                ),
                reverse=True,
            )
            attempts = [self._attempt_summary(job) for job in jobs]
            linked_card = next(
                (
                    card_by_report_id[str(job.get("report_id"))]
                    for job in jobs
                    if str(job.get("report_id") or "") in card_by_report_id
                ),
                None,
            )
            if linked_card is not None:
                completed_job = next(
                    (
                        job
                        for job in jobs
                        if job.get("status") == "completed"
                        and job.get("report_id")
                        == linked_card.get("report_id")
                    ),
                    jobs[0],
                )
                linked_card.update(
                    {
                        "job_id": completed_job.get("job_id"),
                        "job_group_id": group_id,
                        "attempt_count": len(attempts),
                        "attempts": attempts,
                    }
                )
                continue
            job = jobs[0]
            # A completed job is not itself a report.  Completed cards only
            # come from an authoritative immutable Report JSON.  This also
            # prevents an archived report from being resurrected as a ghost
            # card with report_id=null when include_archived is false.
            if job.get("status") == "completed":
                continue
            request = job.get("request") or {}
            scope = request.get("scope") or {}
            error = job.get("error") or {}
            error_message = (
                error.get("message")
                if isinstance(error, dict)
                else str(error)
            )
            cards.append(
                {
                    "report_id": None,
                    "job_id": job["job_id"],
                    "job_group_id": group_id,
                    "attempt_count": len(attempts),
                    "attempts": attempts,
                    "title": request.get("report_name") or "未命名报告",
                    "status": job["status"],
                    "stage_label": job["stage_label"],
                    "progress": job["progress"],
                    "primary_direction": request.get(
                        "primary_direction", "product-opportunity"
                    ),
                    "analysis_modules": request.get("analysis_modules") or [],
                    "factors": request.get("factors") or [],
                    "scope": scope,
                    "samples": job.get("sample_estimate") or {},
                    "summary_text": error_message
                    or f"{job['stage_label']} · {job['progress']}%",
                    "error": (
                        error
                        if isinstance(error, dict)
                        else {"message": str(error)}
                    ),
                    "retryable": report_error_retryable(error),
                    "quality": {
                        "grade": "—",
                        "limitation_count": 0,
                        "headline": error_message or "报告生成中",
                    },
                    "analysis_version": None,
                    "generated_at": job["created_at"],
                    "archived": False,
                }
            )
        cards.sort(
            key=lambda item: (
                item.get("generated_at") or "",
                item.get("job_id") or "",
            ),
            reverse=True,
        )
        return cards

    def list_cards_with_jobs(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        # Retry if any authoritative file changes while cards are being built.
        # Never cache a pre-update read under a post-update signature.
        for _attempt in range(3):
            signature = self._card_source_signature()
            cache_key = (include_archived, signature)
            with self._card_cache_lock:
                cached_cards = self._card_cache.get(cache_key)
            if cached_cards is not None:
                return deepcopy(cached_cards)
            cards = self._build_cards_with_jobs(
                include_archived=include_archived
            )
            if self._card_source_signature() != signature:
                continue
            with self._card_cache_lock:
                self._card_cache = {cache_key: deepcopy(cards)}
            return cards
        # Sustained writers must not be blocked by an unbounded retry.  Return
        # a fresh, uncached fail-closed view; the next request will rebuild.
        return self._build_cards_with_jobs(include_archived=include_archived)
