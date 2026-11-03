from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from reporting.brand_persona_store import BrandPersonaStore
from reporting.storage import ReportStore, atomic_json, utc_now


WORKER_STATUS_SCHEMA_VERSION = "report-worker-status/v1"
DRAIN_SCHEMA_VERSION = "voxquarry.reporting-drain/v1"
DRAIN_LOCK_FILENAME = ".reporting-dispatch.lock"
DRAIN_REQUEST_FILENAME = ".reporting-drain-request.json"


def open_dispatch_lock(var_root: Path) -> int:
    """Open the permanent dispatch lock without requiring write access to it.

    The host refresh process and container Worker may have different owners.
    Both can flock a read-only descriptor; neither may unlink the lock, which
    would let different processes protect different inodes at the same path.
    O_NONBLOCK lets us reject a FIFO without blocking while opening it.
    """
    descriptor = os.open(
        var_root / DRAIN_LOCK_FILENAME,
        os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o644,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("reporting dispatch lock is not a regular file")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_drain_request(var_root: Path) -> dict[str, Any]:
    descriptor = os.open(
        var_root / DRAIN_REQUEST_FILENAME,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
    )
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("reporting drain request is not a regular file")
        content = handle.read(4097)
    if len(content) > 4096:
        raise ValueError("reporting drain request exceeds size limit")
    request = json.loads(content)
    if not isinstance(request, dict) or request.get("schema_version") != DRAIN_SCHEMA_VERSION:
        raise ValueError("reporting drain request schema is invalid")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("reporting drain request_id must be a canonical UUID")
    requested_at = request.get("requested_at")
    if not isinstance(requested_at, str):
        raise ValueError("reporting drain requested_at must be an ISO timestamp")
    parsed = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("reporting drain requested_at requires a timezone")
    return request


def _bounded_int(
    source: Mapping[str, str], key: str, default: int, minimum: int, maximum: int
) -> int:
    raw = str(source.get(key, default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class RuntimePolicy:
    execution_mode: str
    concurrency: int
    brand_concurrency: int
    heartbeat_seconds: int
    heartbeat_timeout_seconds: int
    report_timeout_seconds: int
    brand_timeout_seconds: int

    @classmethod
    def from_environment(cls, source: Mapping[str, str]) -> "RuntimePolicy":
        mode = str(source.get("VOXQUARRY_REPORT_EXECUTION_MODE", "process")).strip()
        if mode not in {"thread", "process", "external", "worker"}:
            mode = "process"
        return cls(
            execution_mode=mode,
            concurrency=_bounded_int(
                source, "VOXQUARRY_REPORT_WORKER_CONCURRENCY", 2, 1, 4
            ),
            brand_concurrency=_bounded_int(
                source, "VOXQUARRY_BRAND_WORKER_CONCURRENCY", 1, 1, 2
            ),
            heartbeat_seconds=_bounded_int(
                source, "VOXQUARRY_REPORT_HEARTBEAT_SECONDS", 5, 2, 30
            ),
            heartbeat_timeout_seconds=_bounded_int(
                source, "VOXQUARRY_REPORT_HEARTBEAT_TIMEOUT_SECONDS", 90, 60, 600
            ),
            report_timeout_seconds=_bounded_int(
                source, "VOXQUARRY_REPORT_JOB_TIMEOUT_SECONDS", 3600, 300, 21600
            ),
            brand_timeout_seconds=_bounded_int(
                source, "VOXQUARRY_BRAND_JOB_TIMEOUT_SECONDS", 1800, 600, 21600
            ),
        )

    def status_payload(
        self,
        *,
        leader: bool,
        active: list[dict[str, Any]],
        queued_reports: int,
        queued_personas: int,
        drain: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": WORKER_STATUS_SCHEMA_VERSION,
            "updated_at": utc_now(),
            "worker_instance_id": os.environ.get("HOSTNAME") or "local",
            "execution_mode": self.execution_mode,
            "leader": bool(leader),
            "concurrency": self.concurrency,
            "brand_concurrency": self.brand_concurrency,
            "active": active,
            "queued_report_jobs": int(queued_reports),
            "queued_brand_persona_jobs": int(queued_personas),
            "drain": dict(drain) if drain is not None else {
                "schema_version": DRAIN_SCHEMA_VERSION,
                "request_id": None,
                "state": "running",
            },
        }


@dataclass(frozen=True)
class WorkItem:
    kind: str
    job_id: str
    priority: int
    created_at: str


def queued_work_items(
    reports: ReportStore,
    personas: BrandPersonaStore,
    *,
    active_ids: set[str],
) -> list[WorkItem]:
    items: list[WorkItem] = []
    for job in reports.list_jobs():
        job_id = str(job.get("job_id") or "")
        if job.get("status") == "queued" and job_id not in active_ids:
            items.append(
                WorkItem(
                    kind="report",
                    job_id=job_id,
                    priority=0,
                    created_at=str(job.get("created_at") or ""),
                )
            )
    for job in personas.list_jobs():
        job_id = str(job.get("job_id") or "")
        if job.get("status") != "queued" or job_id in active_ids:
            continue
        purpose = str(job.get("purpose") or "custom")
        trigger = str(job.get("trigger") or "user")
        priority = 1 if purpose == "custom" else 2 if trigger == "user" else 3
        items.append(
            WorkItem(
                kind="brand-persona",
                job_id=job_id,
                priority=priority,
                created_at=str(job.get("created_at") or ""),
            )
        )
    items.sort(key=lambda item: (item.priority, item.created_at, item.job_id))
    return items


def can_start_item(
    item: WorkItem, active_items: list[WorkItem], policy: RuntimePolicy
) -> bool:
    if len(active_items) >= policy.concurrency:
        return False
    if item.kind == "brand-persona":
        return sum(row.kind == "brand-persona" for row in active_items) < policy.brand_concurrency
    return True


def read_worker_status(var_root: Path) -> dict[str, Any] | None:
    path = var_root / "scheduler" / "status.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _age_seconds(value: Any) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


@dataclass
class RunningWorker:
    item: WorkItem
    process: subprocess.Popen[Any]
    started_monotonic: float
    log_handle: Any


class JobSupervisor:
    """Bounded process supervisor shared by reports and brand personas.

    It may run inside the API process for backward-compatible local use, but
    production runs it in a separate container.  A filesystem lock ensures a
    blue/green candidate stays read-only until the previous supervisor exits.
    """

    def __init__(
        self,
        *,
        snapshot_root: Path,
        var_root: Path,
        policy: RuntimePolicy | None = None,
    ) -> None:
        self.snapshot_root = snapshot_root.resolve()
        self.var_root = var_root.resolve()
        self.policy = policy or RuntimePolicy.from_environment(os.environ)
        self.reports = ReportStore(self.var_root)
        self.personas = BrandPersonaStore(self.var_root)
        self.scheduler_root = self.var_root / "scheduler"
        self.logs_root = self.scheduler_root / "logs"
        self.scheduler_root.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self._active: dict[str, RunningWorker] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock_handle: Any = None
        self._leader = False
        self._recovered = False
        self._drain_request: dict[str, Any] | None = None
        self._drain_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run_forever,
            name="report-job-supervisor",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def close(self, *, terminate: bool = False) -> None:
        self._stop.set()
        self._wake.set()
        if terminate:
            for worker in list(self._active.values()):
                self._stop_process(worker.process)
                worker.log_handle.close()
                self._clear_heartbeat(worker.item)
                self._active.pop(worker.item.job_id, None)
            # A planned worker-container replacement is not an analysis
            # failure. Preserve the same stable job and put it back in queue;
            # the next elected supervisor resumes it from a clean process.
            self.reports.requeue_interrupted_jobs()
            self.personas.requeue_interrupted_jobs()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self._release_leader()

    def _acquire_leader(self) -> bool:
        if self._leader:
            return True
        if self._lock_handle is None:
            self._lock_handle = (self.scheduler_root / "leader.lock").open("a+")
        try:
            fcntl.flock(
                self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError:
            return False
        self._leader = True
        return True

    def _release_leader(self) -> None:
        if self._lock_handle is None:
            return
        if self._leader:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        self._lock_handle.close()
        self._lock_handle = None
        self._leader = False

    def _recover(self) -> None:
        if self._recovered:
            return
        self.reports.requeue_interrupted_jobs()
        self.personas.requeue_interrupted_jobs()
        self._recovered = True

    def _job(self, item: WorkItem) -> dict[str, Any] | None:
        return (
            self.reports.get_job(item.job_id)
            if item.kind == "report"
            else self.personas.get_job(item.job_id)
        )

    def _heartbeat(self, item: WorkItem) -> dict[str, Any] | None:
        return (
            self.reports.get_worker_heartbeat(item.job_id)
            if item.kind == "report"
            else self.personas.get_worker_heartbeat(item.job_id)
        )

    def _clear_heartbeat(self, item: WorkItem) -> None:
        if item.kind == "report":
            self.reports.clear_worker_heartbeat(item.job_id)
        else:
            self.personas.clear_worker_heartbeat(item.job_id)

    def _start_item(self, item: WorkItem) -> None:
        job = self._job(item) or {}
        if item.kind == "report" and job.get("cancel_requested"):
            self.reports.update_job(
                item.job_id,
                "cancelled",
                "已取消",
                int(job.get("progress") or 0),
            )
            return
        log_path = self.logs_root / f"{item.kind}-{item.job_id}.log"
        log_handle = log_path.open("ab", buffering=0)
        command = [
            sys.executable,
            "-m",
            "reporting.worker",
            "run",
            "--kind",
            item.kind,
            "--job-id",
            item.job_id,
            "--snapshot-root",
            str(self.snapshot_root),
            "--var-root",
            str(self.var_root),
            "--heartbeat-seconds",
            str(self.policy.heartbeat_seconds),
        ]
        environment = dict(os.environ)
        environment["VOXQUARRY_REPORT_EXECUTION_MODE"] = "worker"
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            raise
        self._active[item.job_id] = RunningWorker(
            item=item,
            process=process,
            started_monotonic=time.monotonic(),
            log_handle=log_handle,
        )

    def _fail_job(self, item: WorkItem, *, code: str, message: str) -> None:
        job = self._job(item) or {}
        progress = int(job.get("progress") or 0)
        if item.kind == "report":
            if job.get("cancel_requested"):
                self.reports.update_job(
                    item.job_id, "cancelled", "已取消", progress
                )
            elif job.get("status") not in {"completed", "failed", "cancelled"}:
                self.reports.update_job(
                    item.job_id,
                    "failed",
                    "分析进程已停止",
                    progress,
                    error={"code": code, "message": message, "retryable": True},
                )
        elif job.get("status") not in {"completed", "failed"}:
            self.personas.update_job(
                item.job_id,
                status="failed",
                stage="分析进程已停止",
                progress=progress,
                error={"code": code, "message": message, "retryable": True},
            )

    @staticmethod
    def _stop_process(process: subprocess.Popen[Any]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _terminate(self, worker: RunningWorker, *, code: str) -> None:
        self._stop_process(worker.process)
        self._fail_job(
            worker.item,
            code=code,
            message="独立分析进程超过心跳或墙钟门槛，已被隔离停止；API 与其他任务未中断。",
        )

    def _monitor(self) -> None:
        for job_id, worker in list(self._active.items()):
            job = self._job(worker.item) or {}
            exit_code = worker.process.poll()
            if exit_code is not None:
                worker.log_handle.close()
                self._active.pop(job_id, None)
                self._clear_heartbeat(worker.item)
                if job.get("status") not in {"completed", "failed", "cancelled"}:
                    self._fail_job(
                        worker.item,
                        code="REPORT_WORKER_EXITED",
                        message=f"独立分析进程提前退出（exit={exit_code}），可安全重试。",
                    )
                continue
            if worker.item.kind == "report" and job.get("cancel_requested"):
                self._terminate(worker, code="REPORT_WORKER_CANCELLED")
                worker.log_handle.close()
                self._active.pop(job_id, None)
                continue
            elapsed = time.monotonic() - worker.started_monotonic
            timeout = (
                self.policy.report_timeout_seconds
                if worker.item.kind == "report"
                else self.policy.brand_timeout_seconds
            )
            heartbeat = self._heartbeat(worker.item) or {}
            heartbeat_age = _age_seconds(heartbeat.get("heartbeat_at"))
            if elapsed > timeout:
                self._terminate(worker, code="REPORT_WORKER_WALL_TIMEOUT")
            elif elapsed > self.policy.heartbeat_timeout_seconds and (
                heartbeat_age is None
                or heartbeat_age > self.policy.heartbeat_timeout_seconds
            ):
                self._terminate(worker, code="REPORT_WORKER_HEARTBEAT_TIMEOUT")
            else:
                continue
            worker.log_handle.close()
            self._active.pop(job_id, None)
            self._clear_heartbeat(worker.item)

    def _dispatch_queued(self) -> None:
        """Claim work only while no refresh owns the exclusive dispatch lock.

        Monitor and status publication stay outside this lock so already
        running work can finish and acknowledge a drain. A stale request has
        no authority without the host's live flock: a crashed refresh therefore
        cannot leave the queue permanently paused.
        """
        self._drain_request = None
        self._drain_error = None
        try:
            descriptor = open_dispatch_lock(self.var_root)
        except (OSError, RuntimeError) as exc:
            self._drain_error = str(exc)
            return
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    self._drain_request = _read_drain_request(self.var_root)
                except (OSError, ValueError) as exc:
                    self._drain_error = str(exc)
                return
            except OSError as exc:
                self._drain_error = str(exc)
                return
            try:
                for item in queued_work_items(
                    self.reports, self.personas, active_ids=set(self._active)
                ):
                    active_items = [worker.item for worker in self._active.values()]
                    if len(active_items) >= self.policy.concurrency:
                        break
                    if not can_start_item(item, active_items, self.policy):
                        continue
                    self._start_item(item)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _write_status(self) -> None:
        report_jobs = self.reports.list_jobs()
        persona_jobs = self.personas.list_jobs()
        active = []
        for worker in self._active.values():
            job = self._job(worker.item) or {}
            heartbeat = self._heartbeat(worker.item) or {}
            active.append(
                {
                    "kind": worker.item.kind,
                    "job_id": worker.item.job_id,
                    "pid": worker.process.pid,
                    "status": job.get("status"),
                    "stage": job.get("stage_label") or job.get("stage"),
                    "progress": int(job.get("progress") or 0),
                    "started_at": datetime.fromtimestamp(
                        time.time() - (time.monotonic() - worker.started_monotonic),
                        tz=timezone.utc,
                    ).replace(microsecond=0).isoformat(),
                    "heartbeat_at": heartbeat.get("heartbeat_at"),
                }
            )
        drain: dict[str, Any] = {
            "schema_version": DRAIN_SCHEMA_VERSION,
            "request_id": None,
            "state": "running",
        }
        if self._drain_error is not None:
            drain.update(state="blocked", error=self._drain_error)
        elif self._drain_request is not None:
            drain.update(
                request_id=self._drain_request["request_id"],
                state="draining" if active else "drained",
            )
        payload = self.policy.status_payload(
            leader=self._leader,
            active=active,
            queued_reports=sum(job.get("status") == "queued" for job in report_jobs),
            queued_personas=sum(job.get("status") == "queued" for job in persona_jobs),
            drain=drain,
        )
        atomic_json(self.scheduler_root / "status.json", payload)

    def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                if not self._acquire_leader():
                    self._wake.wait(timeout=1)
                    self._wake.clear()
                    continue
                self._recover()
                self._monitor()
                self._dispatch_queued()
                self._write_status()
                self._wake.wait(timeout=1)
                self._wake.clear()
        finally:
            if self._leader:
                self._write_status()
            self._release_leader()
