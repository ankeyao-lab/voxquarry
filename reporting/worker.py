from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from reporting.job_runtime import JobSupervisor, RuntimePolicy, read_worker_status


class Heartbeat:
    def __init__(self, callback: object, seconds: int) -> None:
        self.callback = callback
        self.seconds = max(2, int(seconds))
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="report-worker-heartbeat", daemon=True
        )

    def _run(self) -> None:
        while not self.stop_event.wait(self.seconds):
            self.callback()  # type: ignore[operator]

    def __enter__(self) -> "Heartbeat":
        self.callback()  # type: ignore[operator]
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=self.seconds + 1)


def run_one(
    *,
    kind: str,
    job_id: str,
    snapshot_root: Path,
    var_root: Path,
    heartbeat_seconds: int,
) -> int:
    # Import after the process boundary so the API process never loads or runs
    # the CPU-heavy analysis call stack for queued work.
    from reporting.app import ReportingService

    service = ReportingService(
        snapshot_root,
        var_root,
        execution_mode="worker",
    )
    if kind == "report":
        job = service.store.get_job(job_id)
        if job is None:
            return 2
        callback = lambda: service.store.touch_worker(job_id, worker_pid=os.getpid())
        with Heartbeat(callback, heartbeat_seconds):
            service._run_job(job_id)
        final = service.store.get_job(job_id) or {}
    else:
        job = service.brand_personas.get_job(job_id)
        if job is None:
            return 2
        callback = lambda: service.brand_personas.touch_worker(
            job_id, worker_pid=os.getpid()
        )
        with Heartbeat(callback, heartbeat_seconds):
            service._run_brand_persona_job(job_id)
        final = service.brand_personas.get_job(job_id) or {}
    return 0 if final.get("status") == "completed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="VoxQuarry reporting worker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--kind", choices=("report", "brand-persona"), required=True)
    run_parser.add_argument("--job-id", required=True)
    run_parser.add_argument("--snapshot-root", type=Path, required=True)
    run_parser.add_argument("--var-root", type=Path, required=True)
    run_parser.add_argument("--heartbeat-seconds", type=int, default=5)

    supervise_parser = subparsers.add_parser("supervise")
    supervise_parser.add_argument("--snapshot-root", type=Path, required=True)
    supervise_parser.add_argument("--var-root", type=Path, required=True)
    supervise_parser.add_argument("--concurrency", type=int, default=2)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--var-root", type=Path, required=True)

    arguments = parser.parse_args()
    if arguments.command == "run":
        return run_one(
            kind=arguments.kind,
            job_id=arguments.job_id,
            snapshot_root=arguments.snapshot_root,
            var_root=arguments.var_root,
            heartbeat_seconds=arguments.heartbeat_seconds,
        )
    if arguments.command == "status":
        status = read_worker_status(arguments.var_root)
        if status is None:
            print(json.dumps({"status": "unavailable"}))
            return 1
        try:
            updated_at = datetime.fromisoformat(
                str(status.get("updated_at") or "").replace("Z", "+00:00")
            )
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
        except (TypeError, ValueError):
            age = 9999
        print(json.dumps(status, ensure_ascii=False, sort_keys=True))
        expected_instance = os.environ.get("HOSTNAME") or "local"
        return 0 if (
            status.get("leader") is True
            and age <= 30
            and status.get("worker_instance_id") == expected_instance
        ) else 1

    environment = dict(os.environ)
    environment["VOXQUARRY_REPORT_EXECUTION_MODE"] = "process"
    environment["VOXQUARRY_REPORT_WORKER_CONCURRENCY"] = str(
        max(1, min(4, arguments.concurrency))
    )
    policy = RuntimePolicy.from_environment(environment)
    supervisor = JobSupervisor(
        snapshot_root=arguments.snapshot_root,
        var_root=arguments.var_root,
        policy=policy,
    )

    def stop(_signum: int, _frame: object) -> None:
        supervisor.close(terminate=True)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    supervisor.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
