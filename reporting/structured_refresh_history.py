"""Fail-closed storage and repair primitives for structured refresh history.

The structured refresh scheduler already serializes callers with a host lock.
This module protects the JSONL itself: every append rewrites a verified copy in
the same directory and atomically replaces the live file only after fsync.
ENOSPC can therefore leave an incomplete temporary file, never a partial live
record.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


EVENT_SCHEMA = "voxquarry.reporting-structured-refresh-event/v1"
REPAIR_PLAN_SCHEMA = "voxquarry.structured-refresh-history-repair-plan/v1"
REPAIR_RECEIPT_SCHEMA = "voxquarry.structured-refresh-history-repair-receipt/v1"
MULTI_REPAIR_PLAN_SCHEMA = "voxquarry.structured-refresh-history-repair-plan/v2"
MULTI_REPAIR_RECEIPT_SCHEMA = "voxquarry.structured-refresh-history-repair-receipt/v2"
REPAIR_AUTHORITY_SCHEMA = "voxquarry.structured-refresh-history-repair-authority/v1"
AUTHORITATIVE_REGENERATION_POLICY = (
    "voxquarry.authoritative-failed-history-regeneration/v1"
)
AUTHORITATIVE_QUARANTINE_POLICY = (
    "voxquarry.authoritative-failed-history-quarantine/v1"
)
QUARANTINE_DISPOSITION_SCHEMA = (
    "voxquarry.structured-refresh-history-quarantine/v1"
)
QUARANTINE_REASONS = {
    "authoritative_event_unavailable",
    "authoritative_event_not_unique",
}
QUARANTINE_AUTHORITY_DISPOSITIONS = {
    "authoritative_event_unavailable": "authoritative_event_unavailable",
    "authoritative_event_not_unique": "not_uniquely_reconstructable",
}
AUTHORITY_SOURCE_SCHEMAS = {
    "readiness": "voxquarry.acquisition-batch-readiness/v1",
    "task": "voxquarry.structured-refresh-task-authority/v1",
    "event": "voxquarry.structured-refresh-authoritative-event/v1",
    "incident": "voxquarry.structured-refresh-incident/v1",
}
DIAGNOSTICS_SCHEMA = "voxquarry.structured-refresh-diagnostics/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_TRIGGERS = {"event_poll", "daily_fallback", "manual", "repair"}
COMMITTED_FRAGMENT_MARKERS = (
    b'"lower_activation_committed"',
    b'"processed_records"',
    b'"refresh_commit_status"',
    b'"refresh_committed_with_state_failure"',
)


class HistoryIntegrityError(ValueError):
    """The live history or a proposed repair is not machine-verifiable."""


class HistoryPostCommitAuditError(OSError):
    """The atomic replacement committed, but a durability/readback audit failed."""

    def __init__(self, outcome: "AtomicReplaceOutcome") -> None:
        self.outcome = outcome
        super().__init__(
            "structured refresh history committed with post-commit audit failure: "
            + "; ".join(outcome.audit_failures)
        )


@dataclass(frozen=True)
class HistoryRecord:
    line_number: int
    raw: bytes
    payload: dict[str, Any]


@dataclass(frozen=True)
class HistoryScan:
    records: tuple[HistoryRecord, ...]
    content_sha256: str
    byte_count: int


@dataclass(frozen=True)
class RepairResult:
    repaired_bytes: bytes
    bad_line_raw: bytes
    fragment_raw: bytes
    suffix_raw: bytes
    marker: dict[str, Any]
    suffix_event: dict[str, Any]


@dataclass(frozen=True)
class MultiRepairItem:
    plan: dict[str, Any]
    bad_line_raw: bytes
    fragment_raw: bytes
    suffix_raw: bytes
    replacement_event: dict[str, Any]
    suffix_event: dict[str, Any]
    disposition: str


@dataclass(frozen=True)
class MultiRepairResult:
    repaired_bytes: bytes
    repairs: tuple[MultiRepairItem, ...]
    original_history_sha256: str


@dataclass(frozen=True)
class AtomicReplaceOutcome:
    """Outcome after the one-way ``os.replace`` commit boundary."""

    committed: bool
    content_sha256: str
    byte_count: int
    directory_fsynced: bool
    readback_verified: bool
    audit_failures: tuple[str, ...]
    mode: int
    uid: int
    gid: int


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_json(raw: bytes, *, context: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HistoryIntegrityError(f"{context} is not UTF-8") from exc
    try:
        return json.loads(
            text,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite number: {token}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise HistoryIntegrityError(f"{context} is not valid JSON") from exc


def _parse_timestamp(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoryIntegrityError(f"{context} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoryIntegrityError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise HistoryIntegrityError(f"{context} must include a timezone")
    return value


def validate_history_event(payload: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HistoryIntegrityError(f"{context} must be an object")
    if payload.get("schema_version") != EVENT_SCHEMA:
        raise HistoryIntegrityError(f"{context} has an unsupported schema_version")
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise HistoryIntegrityError(f"{context}.status must be a non-empty string")
    trigger = payload.get("trigger")
    if trigger not in ALLOWED_TRIGGERS:
        raise HistoryIntegrityError(f"{context}.trigger is invalid")
    _parse_timestamp(payload.get("at"), context=f"{context}.at")
    return dict(payload)


def scan_history_bytes(raw: bytes, *, context: str = "history") -> HistoryScan:
    if not raw:
        return HistoryScan(records=(), content_sha256=sha256_bytes(raw), byte_count=0)
    if not raw.endswith(b"\n"):
        raise HistoryIntegrityError(f"{context} has a partial final record")
    records: list[HistoryRecord] = []
    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n"):
            raise HistoryIntegrityError(f"{context} line {line_number} is partial")
        body = line[:-1]
        if not body:
            raise HistoryIntegrityError(f"{context} line {line_number} is empty")
        payload = validate_history_event(
            _strict_json(body, context=f"{context} line {line_number}"),
            context=f"{context} line {line_number}",
        )
        records.append(HistoryRecord(line_number, line, payload))
    return HistoryScan(
        records=tuple(records),
        content_sha256=sha256_bytes(raw),
        byte_count=len(raw),
    )


def scan_history(path: Path) -> HistoryScan:
    if not path.exists():
        return scan_history_bytes(b"", context=str(path))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HistoryIntegrityError(f"history is unreadable: {path}") from exc
    return scan_history_bytes(raw, context=str(path))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("short write while persisting structured refresh history")
        written += count


def _read_committed_bytes(path: Path) -> bytes:
    """Separate post-commit read boundary for deterministic fault injection."""

    return path.read_bytes()


def atomic_replace_bytes(
    path: Path,
    content: bytes,
    *,
    writer: Callable[[int, bytes], None] = _write_all,
    mode: int | None = None,
    validator: Callable[[bytes], Any] | None = None,
) -> AtomicReplaceOutcome:
    """Persist content with ``os.replace`` as the explicit commit boundary.

    Failures before ``os.replace`` raise and leave the live path byte-exact.
    Failures after it are returned as audit failures because the replacement
    is already committed and must never be reported as an ordinary no-op.
    Existing regular-file mode, uid and gid are preserved exactly.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_metadata: os.stat_result | None = None
    try:
        existing_metadata = path.lstat()
    except FileNotFoundError:
        pass
    if existing_metadata is not None and not stat.S_ISREG(existing_metadata.st_mode):
        raise HistoryIntegrityError(
            f"atomic replacement target is not a regular file: {path}"
        )
    target_mode = (
        stat.S_IMODE(existing_metadata.st_mode)
        if existing_metadata is not None
        else (mode if mode is not None else 0o600)
    )
    target_uid = (
        existing_metadata.st_uid if existing_metadata is not None else os.geteuid()
    )
    target_gid = (
        existing_metadata.st_gid if existing_metadata is not None else os.getegid()
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    committed = False
    try:
        if existing_metadata is not None:
            os.fchown(descriptor, target_uid, target_gid)
        os.fchmod(descriptor, target_mode)
        writer(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if validator is not None:
            validator(temporary.read_bytes())
        os.replace(temporary, path)
        committed = True
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise

    audit_failures: list[str] = []
    directory_fsynced = False
    try:
        _fsync_directory(path.parent)
        directory_fsynced = True
    except BaseException as exc:  # the replace already committed
        audit_failures.append(f"directory_fsync_failed:{type(exc).__name__}:{exc}")

    readback_verified = False
    try:
        observed = _read_committed_bytes(path)
        if observed != content:
            raise HistoryIntegrityError(
                "committed content does not match intended bytes"
            )
        if validator is not None:
            validator(observed)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise HistoryIntegrityError("committed target is not a regular file")
        if stat.S_IMODE(metadata.st_mode) != target_mode:
            raise HistoryIntegrityError("committed target mode changed")
        if metadata.st_uid != target_uid or metadata.st_gid != target_gid:
            raise HistoryIntegrityError("committed target ownership changed")
        readback_verified = True
    except BaseException as exc:
        audit_failures.append(
            f"readback_verification_failed:{type(exc).__name__}:{exc}"
        )

    return AtomicReplaceOutcome(
        committed=committed,
        content_sha256=sha256_bytes(content),
        byte_count=len(content),
        directory_fsynced=directory_fsynced,
        readback_verified=readback_verified,
        audit_failures=tuple(audit_failures),
        mode=target_mode,
        uid=target_uid,
        gid=target_gid,
    )


def atomic_append_history(
    path: Path,
    payload: dict[str, Any],
    *,
    writer: Callable[[int, bytes], None] = _write_all,
) -> HistoryScan:
    """Append one event transactionally after validating every existing line."""

    event = validate_history_event(payload, context="new history event")
    existing = path.read_bytes() if path.exists() else b""
    scan_history_bytes(existing, context=str(path))
    record = canonical_json(event).encode("utf-8") + b"\n"
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    outcome = atomic_replace_bytes(
        path,
        existing + record,
        writer=writer,
        mode=mode,
        validator=lambda raw: scan_history_bytes(raw, context="new history file"),
    )
    if outcome.audit_failures:
        raise HistoryPostCommitAuditError(outcome)
    return scan_history(path)


def validate_repair_plan(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HistoryIntegrityError("repair plan must be an object")
    allowed = {
        "schema_version",
        "history_sha256",
        "damaged_line_number",
        "damaged_line_sha256",
        "fragment_sha256",
        "suffix_event_sha256",
        "fragment_classification",
        "incident_evidence_sha256",
        "observed_at",
        "trigger",
    }
    if set(payload) - allowed:
        raise HistoryIntegrityError("repair plan contains unknown fields")
    if payload.get("schema_version") != REPAIR_PLAN_SCHEMA:
        raise HistoryIntegrityError("unsupported repair plan schema_version")
    for field in (
        "history_sha256",
        "damaged_line_sha256",
        "fragment_sha256",
        "suffix_event_sha256",
        "incident_evidence_sha256",
    ):
        value = payload.get(field)
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise HistoryIntegrityError(f"repair plan {field} is invalid")
    line_number = payload.get("damaged_line_number")
    if (
        isinstance(line_number, bool)
        or not isinstance(line_number, int)
        or line_number < 1
    ):
        raise HistoryIntegrityError("repair plan damaged_line_number is invalid")
    if payload.get("fragment_classification") != "failed_event_fragment":
        raise HistoryIntegrityError(
            "only an evidence-bound failed event fragment can be repaired"
        )
    _parse_timestamp(payload.get("observed_at"), context="repair plan observed_at")
    if payload.get("trigger") not in ALLOWED_TRIGGERS - {"repair"}:
        raise HistoryIntegrityError("repair plan trigger is invalid")
    return dict(payload)


def _require_sha256(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise HistoryIntegrityError(f"{context} is invalid")
    return value


def _require_sorted_unique_strings(
    value: Any,
    *,
    context: str,
    pattern: re.Pattern[str],
) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(item, str) or not pattern.fullmatch(item) for item in value
        )
        or value != sorted(set(value))
    ):
        raise HistoryIntegrityError(f"{context} must be a sorted unique list")
    return list(value)


def _validate_repair_authority(payload: Any, *, context: str) -> dict[str, Any]:
    required = {
        "schema_version",
        "reconstruction_policy",
        "batch_ids",
        "job_ids",
        "source_content_fingerprint",
        "readiness_record_sha256",
        "task_record_sha256",
        "event_record_sha256",
        "incident_record_sha256",
        "source_records",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise HistoryIntegrityError(f"{context} fields are invalid")
    if payload.get("schema_version") != REPAIR_AUTHORITY_SCHEMA:
        raise HistoryIntegrityError(f"{context} schema_version is invalid")
    if payload.get("reconstruction_policy") not in {
        AUTHORITATIVE_REGENERATION_POLICY,
        AUTHORITATIVE_QUARANTINE_POLICY,
    }:
        raise HistoryIntegrityError(f"{context} reconstruction_policy is invalid")
    result = dict(payload)
    result["batch_ids"] = _require_sorted_unique_strings(
        payload.get("batch_ids"),
        context=f"{context}.batch_ids",
        pattern=re.compile(r"^acq_[0-9a-f]{24}$"),
    )
    result["job_ids"] = _require_sorted_unique_strings(
        payload.get("job_ids"),
        context=f"{context}.job_ids",
        pattern=re.compile(r"^[0-9a-f]{12}$"),
    )
    for field in (
        "source_content_fingerprint",
        "readiness_record_sha256",
        "task_record_sha256",
        "event_record_sha256",
        "incident_record_sha256",
    ):
        result[field] = _require_sha256(
            payload.get(field), context=f"{context}.{field}"
        )
    source_records = payload.get("source_records")
    if not isinstance(source_records, list) or len(source_records) != 4:
        raise HistoryIntegrityError(f"{context}.source_records are invalid")
    observed_kinds: list[str] = []
    normalized_records: list[dict[str, Any]] = []
    for index, source in enumerate(source_records):
        source_context = f"{context}.source_records[{index}]"
        if not isinstance(source, dict) or set(source) != {
            "kind",
            "schema_version",
            "sha256",
            "payload",
        }:
            raise HistoryIntegrityError(f"{source_context} fields are invalid")
        kind = source.get("kind")
        if kind not in AUTHORITY_SOURCE_SCHEMAS:
            raise HistoryIntegrityError(f"{source_context}.kind is invalid")
        if source.get("schema_version") != AUTHORITY_SOURCE_SCHEMAS[kind]:
            raise HistoryIntegrityError(f"{source_context}.schema_version is invalid")
        source_payload = source.get("payload")
        if not isinstance(source_payload, dict):
            raise HistoryIntegrityError(f"{source_context}.payload is invalid")
        content_sha256 = sha256_bytes(canonical_json(source_payload).encode("utf-8"))
        if source.get("sha256") != content_sha256:
            raise HistoryIntegrityError(f"{source_context}.sha256 is invalid")
        observed_kinds.append(kind)
        normalized_records.append(dict(source))
    if observed_kinds != ["readiness", "task", "event", "incident"]:
        raise HistoryIntegrityError(
            f"{context}.source_records must use the canonical authority order"
        )
    records_by_kind = {item["kind"]: item for item in normalized_records}
    for kind in AUTHORITY_SOURCE_SCHEMAS:
        hash_field = f"{kind}_record_sha256"
        if payload[hash_field] != records_by_kind[kind]["sha256"]:
            raise HistoryIntegrityError(f"{context}.{hash_field} conflicts with source")
    readiness = records_by_kind["readiness"]["payload"]
    if (
        readiness.get("batch_ids") != result["batch_ids"]
        or readiness.get("job_ids") != result["job_ids"]
        or readiness.get("source_content_fingerprint")
        != result["source_content_fingerprint"]
    ):
        raise HistoryIntegrityError(f"{context} readiness source conflicts")
    if records_by_kind["task"]["payload"].get("job_ids") != result["job_ids"]:
        raise HistoryIntegrityError(f"{context} task source conflicts")
    result["source_records"] = normalized_records
    return result


def validate_multi_repair_plan(payload: Any) -> dict[str, Any]:
    """Validate the all-or-zero v2 plan without trusting asserted identities."""

    required = {"schema_version", "history_sha256", "observed_at", "repairs"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise HistoryIntegrityError("multi-repair plan fields are invalid")
    if payload.get("schema_version") != MULTI_REPAIR_PLAN_SCHEMA:
        raise HistoryIntegrityError("unsupported multi-repair plan schema_version")
    _require_sha256(
        payload.get("history_sha256"), context="multi-repair history_sha256"
    )
    _parse_timestamp(payload.get("observed_at"), context="multi-repair observed_at")
    repairs = payload.get("repairs")
    if not isinstance(repairs, list) or not repairs:
        raise HistoryIntegrityError("multi-repair repairs must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    line_numbers: list[int] = []
    common_repair_fields = {
        "action",
        "damaged_line_number",
        "damaged_line_sha256",
        "fragment_sha256",
        "suffix_event_sha256",
        "previous_line_sha256",
        "following_line_sha256",
        "incident_evidence_sha256",
        "authority",
    }
    regeneration_fields = {
        "fragment_classification",
        "original_trigger",
        "regenerated_event",
        "regenerated_event_sha256",
    }
    quarantine_fields = {"quarantine_reason"}
    for index, repair in enumerate(repairs):
        context = f"multi-repair repairs[{index}]"
        if not isinstance(repair, dict):
            raise HistoryIntegrityError(f"{context} fields are invalid")
        action = repair.get("action")
        expected_fields = (
            common_repair_fields | regeneration_fields
            if action == "regenerate_failed_event"
            else common_repair_fields | quarantine_fields
            if action == "quarantine_failed_fragment"
            else set()
        )
        if not expected_fields or set(repair) != expected_fields:
            raise HistoryIntegrityError(f"{context} action is invalid")
        line_number = repair.get("damaged_line_number")
        if (
            isinstance(line_number, bool)
            or not isinstance(line_number, int)
            or line_number < 1
        ):
            raise HistoryIntegrityError(f"{context} damaged_line_number is invalid")
        line_numbers.append(line_number)
        item = dict(repair)
        for field in (
            "damaged_line_sha256",
            "fragment_sha256",
            "suffix_event_sha256",
            "incident_evidence_sha256",
        ):
            item[field] = _require_sha256(
                repair.get(field), context=f"{context}.{field}"
            )
        for field in ("previous_line_sha256", "following_line_sha256"):
            value = repair.get(field)
            if value is not None:
                item[field] = _require_sha256(value, context=f"{context}.{field}")
        item["authority"] = _validate_repair_authority(
            repair.get("authority"), context=f"{context}.authority"
        )
        expected_policy = (
            AUTHORITATIVE_REGENERATION_POLICY
            if action == "regenerate_failed_event"
            else AUTHORITATIVE_QUARANTINE_POLICY
        )
        if item["authority"]["reconstruction_policy"] != expected_policy:
            raise HistoryIntegrityError(f"{context} authority policy conflicts")
        if action == "regenerate_failed_event":
            if repair.get("fragment_classification") != "failed_event_fragment":
                raise HistoryIntegrityError(
                    f"{context} fragment_classification is invalid"
                )
            if repair.get("original_trigger") not in ALLOWED_TRIGGERS - {"repair"}:
                raise HistoryIntegrityError(f"{context} original_trigger is invalid")
            item["regenerated_event_sha256"] = _require_sha256(
                repair.get("regenerated_event_sha256"),
                context=f"{context}.regenerated_event_sha256",
            )
            item["regenerated_event"] = validate_history_event(
                repair.get("regenerated_event"),
                context=f"{context}.regenerated_event",
            )
        else:
            if repair.get("quarantine_reason") not in QUARANTINE_REASONS:
                raise HistoryIntegrityError(f"{context} quarantine_reason is invalid")
        normalized.append(item)
    if line_numbers != sorted(set(line_numbers)):
        raise HistoryIntegrityError(
            "multi-repair line numbers must be sorted and unique"
        )
    result = dict(payload)
    result["repairs"] = normalized
    return result


def _candidate_suffixes(body: bytes) -> list[tuple[int, bytes, dict[str, Any]]]:
    candidates: list[tuple[int, bytes, dict[str, Any]]] = []
    for index, value in enumerate(body):
        if index == 0 or value != ord("{"):
            continue
        suffix = body[index:]
        try:
            parsed = _strict_json(suffix, context="repair suffix")
            event = validate_history_event(parsed, context="repair suffix")
        except HistoryIntegrityError:
            continue
        if canonical_json(event).encode("utf-8") != suffix:
            continue
        candidates.append((index, suffix, event))
    return candidates


def _prove_failed_fragment(fragment: bytes, plan: dict[str, Any]) -> None:
    """Prove the truncated prefix identifies a failed event, not a receipt.

    The repair plan binds exact bytes and incident evidence, but classification
    is not accepted on assertion alone.  The raw prefix itself must contain the
    complete canonical schema, failed status and original trigger scalars.
    """

    if any(marker in fragment for marker in COMMITTED_FRAGMENT_MARKERS):
        raise HistoryIntegrityError(
            "committed or state-failure receipt fragments cannot be repaired"
        )
    required = (
        f'"schema_version":"{EVENT_SCHEMA}"'.encode("utf-8"),
        b'"status":"failed"',
        f'"trigger":"{plan["trigger"]}"'.encode("utf-8"),
    )
    if any(token not in fragment for token in required):
        raise HistoryIntegrityError(
            "damaged prefix does not prove a failed structured refresh event"
        )


def _build_quarantine_event(
    *,
    plan: dict[str, Any],
    original_history_sha256: str,
    observed_at: str,
) -> dict[str, Any]:
    authority = plan["authority"]
    records_by_kind = {record["kind"]: record for record in authority["source_records"]}
    event_source = records_by_kind["event"]["payload"]
    reason = plan["quarantine_reason"]
    if event_source.get("disposition") != QUARANTINE_AUTHORITY_DISPOSITIONS[reason]:
        raise HistoryIntegrityError(
            "quarantine authority does not prove the planned disposition"
        )
    candidates = event_source.get("candidate_event_sha256s")
    if (
        not isinstance(candidates, list)
        or any(
            not isinstance(item, str) or not SHA256_RE.fullmatch(item)
            for item in candidates
        )
        or candidates != sorted(set(candidates))
    ):
        raise HistoryIntegrityError("quarantine authority candidates are invalid")
    if reason == "authoritative_event_not_unique" and len(candidates) < 2:
        raise HistoryIntegrityError("quarantine authority is not demonstrably non-unique")
    if reason == "authoritative_event_unavailable" and candidates:
        raise HistoryIntegrityError("unavailable quarantine cannot list event candidates")
    if "regenerated_event" in event_source:
        raise HistoryIntegrityError(
            "quarantine authority cannot contain a unique regenerated event"
        )
    return validate_history_event(
        {
            "schema_version": EVENT_SCHEMA,
            "status": "history_fragment_quarantined",
            "trigger": "repair",
            "at": observed_at,
            "disposition_schema_version": QUARANTINE_DISPOSITION_SCHEMA,
            "quarantine_reason": reason,
            "original_history_sha256": original_history_sha256,
            "damaged_line_number": plan["damaged_line_number"],
            "damaged_line_sha256": plan["damaged_line_sha256"],
            "fragment_sha256": plan["fragment_sha256"],
            "suffix_event_sha256": plan["suffix_event_sha256"],
            "incident_evidence_sha256": plan["incident_evidence_sha256"],
            "authority_schema_version": authority["schema_version"],
            "authority_policy": authority["reconstruction_policy"],
            "authority_source_record_sha256s": {
                kind: records_by_kind[kind]["sha256"]
                for kind in ("readiness", "task", "event", "incident")
            },
            "affected_batch_ids": authority["batch_ids"],
            "affected_job_ids": authority["job_ids"],
            "source_content_fingerprint": authority[
                "source_content_fingerprint"
            ],
            "limitation": (
                "original malformed history fragment was excluded because its "
                "authoritative event could not be uniquely reconstructed"
            ),
        },
        context="quarantine tombstone",
    )


def _build_multi_repair_item(
    *,
    lines: list[bytes],
    plan: dict[str, Any],
    original_history_sha256: str,
    observed_at: str,
) -> MultiRepairItem:
    line_number = plan["damaged_line_number"]
    target_index = line_number - 1
    if target_index >= len(lines):
        raise HistoryIntegrityError("multi-repair damaged line is outside history")
    bad_line = lines[target_index]
    if sha256_bytes(bad_line) != plan["damaged_line_sha256"]:
        raise HistoryIntegrityError("multi-repair damaged line SHA-256 mismatch")
    if not bad_line.endswith(b"\n"):
        raise HistoryIntegrityError("multi-repair damaged line must end with newline")

    previous_line = lines[target_index - 1] if target_index else None
    following_line = lines[target_index + 1] if target_index + 1 < len(lines) else None
    expected_previous = (
        sha256_bytes(previous_line) if previous_line is not None else None
    )
    expected_following = (
        sha256_bytes(following_line) if following_line is not None else None
    )
    if plan["previous_line_sha256"] != expected_previous:
        raise HistoryIntegrityError("multi-repair previous line SHA-256 mismatch")
    if plan["following_line_sha256"] != expected_following:
        raise HistoryIntegrityError("multi-repair following line SHA-256 mismatch")

    body = bad_line[:-1]
    candidates = _candidate_suffixes(body)
    if len(candidates) != 1:
        raise HistoryIntegrityError(
            "multi-repair damaged line has no unique complete event suffix"
        )
    split_at, suffix, suffix_event = candidates[0]
    fragment = body[:split_at]
    if not fragment.startswith(b"{"):
        raise HistoryIntegrityError("multi-repair fragment is not a JSON object prefix")
    try:
        _strict_json(fragment, context="multi-repair fragment")
    except HistoryIntegrityError:
        pass
    else:
        raise HistoryIntegrityError(
            "multi-repair fragment is unexpectedly complete JSON"
        )
    # Unlike v1, an ENOSPC fragment may stop before canonical-key order reaches
    # schema/status/trigger.  V2 therefore proves the event from the
    # content-addressed authority record and the exact byte-prefix relation
    # below.  It still refuses any fragment that could be a committed receipt.
    if any(marker in fragment for marker in COMMITTED_FRAGMENT_MARKERS):
        raise HistoryIntegrityError(
            "committed or state-failure receipt fragments cannot be repaired"
        )
    if sha256_bytes(fragment) != plan["fragment_sha256"]:
        raise HistoryIntegrityError("multi-repair fragment SHA-256 mismatch")
    if sha256_bytes(suffix) != plan["suffix_event_sha256"]:
        raise HistoryIntegrityError("multi-repair suffix event SHA-256 mismatch")

    authority = plan["authority"]
    records_by_kind = {record["kind"]: record for record in authority["source_records"]}
    incident = records_by_kind["incident"]["payload"]
    if (
        incident.get("damaged_line_number") != line_number
        or incident.get("damaged_line_sha256") != plan["damaged_line_sha256"]
        or plan["incident_evidence_sha256"] != authority["incident_record_sha256"]
    ):
        raise HistoryIntegrityError(
            "repair incident source conflicts with damaged line"
        )
    if plan["action"] == "regenerate_failed_event":
        replacement = plan["regenerated_event"]
        if replacement.get("status") != "failed":
            raise HistoryIntegrityError("regenerated event must retain failed status")
        if replacement.get("trigger") != plan["original_trigger"]:
            raise HistoryIntegrityError(
                "regenerated event trigger conflicts with fragment"
            )
        replacement_raw = canonical_json(replacement).encode("utf-8")
        if any(marker in replacement_raw for marker in COMMITTED_FRAGMENT_MARKERS):
            raise HistoryIntegrityError("regenerated event cannot claim committed state")
        if not replacement_raw.startswith(fragment) or replacement_raw == fragment:
            raise HistoryIntegrityError(
                "regenerated event does not preserve the exact prefix bytes"
            )
        if sha256_bytes(replacement_raw) != plan["regenerated_event_sha256"]:
            raise HistoryIntegrityError("regenerated event SHA-256 mismatch")
        if (
            records_by_kind["event"]["payload"].get("regenerated_event")
            != replacement
        ):
            raise HistoryIntegrityError(
                "regenerated event conflicts with authoritative event source"
            )
        if replacement.get("affected_batch_ids") != authority["batch_ids"]:
            raise HistoryIntegrityError(
                "regenerated event affected_batch_ids conflict with authority"
            )
        if replacement.get("affected_job_ids") != authority["job_ids"]:
            raise HistoryIntegrityError(
                "regenerated event affected_job_ids conflict with authority"
            )
        if (
            replacement.get("source_content_fingerprint")
            != authority["source_content_fingerprint"]
        ):
            raise HistoryIntegrityError(
                "regenerated event source fingerprint conflicts with authority"
            )
        disposition = "regenerated"
    else:
        replacement = _build_quarantine_event(
            plan=plan,
            original_history_sha256=original_history_sha256,
            observed_at=observed_at,
        )
        disposition = "quarantined"
    return MultiRepairItem(
        plan=plan,
        bad_line_raw=bad_line,
        fragment_raw=fragment,
        suffix_raw=suffix,
        replacement_event=replacement,
        suffix_event=suffix_event,
        disposition=disposition,
    )


def build_multi_repair(raw: bytes, plan_payload: Any) -> MultiRepairResult:
    """Regenerate every damaged line in one content-addressed transaction."""

    plan = validate_multi_repair_plan(plan_payload)
    original_sha256 = sha256_bytes(raw)
    if original_sha256 != plan["history_sha256"]:
        raise HistoryIntegrityError("multi-repair history SHA-256 mismatch")
    if not raw.endswith(b"\n"):
        raise HistoryIntegrityError("multi-repair history has a partial final record")
    lines = raw.splitlines(keepends=True)
    invalid_lines: list[int] = []
    for index, raw_line in enumerate(lines):
        try:
            scan_history_bytes(raw_line, context=f"history line {index + 1}")
        except HistoryIntegrityError:
            invalid_lines.append(index + 1)
    planned_lines = [item["damaged_line_number"] for item in plan["repairs"]]
    if invalid_lines != planned_lines:
        raise HistoryIntegrityError(
            "multi-repair plan must bind every and only malformed history line"
        )

    repairs = tuple(
        _build_multi_repair_item(
            lines=lines,
            plan=item,
            original_history_sha256=original_sha256,
            observed_at=plan["observed_at"],
        )
        for item in plan["repairs"]
    )
    repairs_by_line = {item.plan["damaged_line_number"]: item for item in repairs}
    repaired_parts: list[bytes] = []
    for line_number, raw_line in enumerate(lines, start=1):
        item = repairs_by_line.get(line_number)
        if item is None:
            repaired_parts.append(raw_line)
            continue
        repaired_parts.append(
            canonical_json(item.replacement_event).encode("utf-8") + b"\n"
        )
        repaired_parts.append(item.suffix_raw + b"\n")
    repaired = b"".join(repaired_parts)
    scan_history_bytes(repaired, context="multi-repaired history")
    return MultiRepairResult(
        repaired_bytes=repaired,
        repairs=repairs,
        original_history_sha256=original_sha256,
    )


def build_repair(raw: bytes, plan_payload: Any) -> RepairResult:
    """Create repaired bytes for exactly one evidence-bound concatenated line."""

    plan = validate_repair_plan(plan_payload)
    if sha256_bytes(raw) != plan["history_sha256"]:
        raise HistoryIntegrityError("history SHA-256 does not match repair plan")
    lines = raw.splitlines(keepends=True)
    target_index = plan["damaged_line_number"] - 1
    if target_index >= len(lines):
        raise HistoryIntegrityError("damaged line is outside history")
    invalid_lines: list[int] = []
    for index, line in enumerate(lines):
        try:
            scan_history_bytes(line, context=f"history line {index + 1}")
        except HistoryIntegrityError:
            invalid_lines.append(index)
    if invalid_lines != [target_index]:
        raise HistoryIntegrityError("history must contain exactly the planned bad line")
    bad_line = lines[target_index]
    if sha256_bytes(bad_line) != plan["damaged_line_sha256"]:
        raise HistoryIntegrityError("damaged line SHA-256 does not match repair plan")
    if not bad_line.endswith(b"\n"):
        raise HistoryIntegrityError("concatenated damaged line must end with newline")
    body = bad_line[:-1]
    candidates = _candidate_suffixes(body)
    if len(candidates) != 1:
        raise HistoryIntegrityError("damaged line has no unique complete event suffix")
    split_at, suffix, suffix_event = candidates[0]
    fragment = body[:split_at]
    if not fragment.startswith(b"{"):
        raise HistoryIntegrityError("damaged prefix is not a JSON object fragment")
    try:
        _strict_json(fragment, context="damaged fragment")
    except HistoryIntegrityError:
        pass
    else:
        raise HistoryIntegrityError("damaged prefix is unexpectedly complete JSON")
    _prove_failed_fragment(fragment, plan)
    if sha256_bytes(fragment) != plan["fragment_sha256"]:
        raise HistoryIntegrityError("fragment SHA-256 does not match repair plan")
    if sha256_bytes(suffix) != plan["suffix_event_sha256"]:
        raise HistoryIntegrityError("suffix event SHA-256 does not match repair plan")
    marker = {
        "schema_version": EVENT_SCHEMA,
        "status": "failed_event_fragment_repaired",
        "trigger": "repair",
        "at": plan["observed_at"],
        "original_trigger": plan["trigger"],
        "original_history_sha256": plan["history_sha256"],
        "damaged_line_number": plan["damaged_line_number"],
        "damaged_line_sha256": plan["damaged_line_sha256"],
        "fragment_sha256": plan["fragment_sha256"],
        "fragment_bytes": len(fragment),
        "fragment_classification": "failed_event_fragment",
        "incident_evidence_sha256": plan["incident_evidence_sha256"],
        "limitation": "incomplete failed event payload was not reconstructed",
    }
    replacement = canonical_json(marker).encode("utf-8") + b"\n" + suffix + b"\n"
    repaired = (
        b"".join(lines[:target_index])
        + replacement
        + b"".join(lines[target_index + 1 :])
    )
    scan_history_bytes(repaired, context="repaired history")
    return RepairResult(repaired, bad_line, fragment, suffix, marker, suffix_event)


def repair_receipt(
    *,
    original: bytes,
    repaired: bytes,
    result: RepairResult,
    archive_path: Path | None,
    evidence_path: Path | None,
    status: str,
    audit_failures: tuple[str, ...] | list[str] = (),
    live_history_verified: bool | None = None,
    live_history_sha256: str | None = None,
    live_metadata: dict[str, int] | None = None,
) -> dict[str, Any]:
    if status not in {
        "dry_run",
        "prepared",
        "applied",
        "applied_with_audit_failure",
    }:
        raise ValueError("repair receipt status is invalid")
    payload = {
        "schema_version": REPAIR_RECEIPT_SCHEMA,
        "status": status,
        "original_history_sha256": sha256_bytes(original),
        "repaired_history_sha256": sha256_bytes(repaired),
        "original_bytes": len(original),
        "repaired_bytes": len(repaired),
        "damaged_line_sha256": sha256_bytes(result.bad_line_raw),
        "fragment_sha256": sha256_bytes(result.fragment_raw),
        "suffix_event_sha256": sha256_bytes(result.suffix_raw),
        "archive_path": str(archive_path) if archive_path else None,
        "evidence_path": str(evidence_path) if evidence_path else None,
        "live_history_verified": live_history_verified,
        "live_history_sha256": live_history_sha256,
        "live_metadata": live_metadata,
        "audit_failures": list(audit_failures),
        "marker": result.marker,
    }
    return validate_repair_receipt(payload)


def validate_repair_receipt(payload: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "status",
        "original_history_sha256",
        "repaired_history_sha256",
        "original_bytes",
        "repaired_bytes",
        "damaged_line_sha256",
        "fragment_sha256",
        "suffix_event_sha256",
        "archive_path",
        "evidence_path",
        "live_history_verified",
        "live_history_sha256",
        "live_metadata",
        "audit_failures",
        "marker",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise HistoryIntegrityError("repair receipt fields are invalid")
    if payload.get("schema_version") != REPAIR_RECEIPT_SCHEMA:
        raise HistoryIntegrityError("repair receipt schema_version is invalid")
    status = payload.get("status")
    if status not in {
        "dry_run",
        "prepared",
        "applied",
        "applied_with_audit_failure",
    }:
        raise HistoryIntegrityError("repair receipt status is invalid")
    for field in (
        "original_history_sha256",
        "repaired_history_sha256",
        "damaged_line_sha256",
        "fragment_sha256",
        "suffix_event_sha256",
    ):
        value = payload.get(field)
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise HistoryIntegrityError(f"repair receipt {field} is invalid")
    for field in ("original_bytes", "repaired_bytes"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HistoryIntegrityError(f"repair receipt {field} is invalid")
    failures = payload.get("audit_failures")
    if not isinstance(failures, list) or any(
        not isinstance(item, str) or not item for item in failures
    ):
        raise HistoryIntegrityError("repair receipt audit_failures are invalid")
    verified = payload.get("live_history_verified")
    live_sha = payload.get("live_history_sha256")
    metadata = payload.get("live_metadata")
    if status in {"dry_run", "prepared"}:
        if (
            verified is not None
            or live_sha is not None
            or metadata is not None
            or failures
        ):
            raise HistoryIntegrityError(
                "unapplied repair receipt has live outcome fields"
            )
    else:
        if not isinstance(verified, bool):
            raise HistoryIntegrityError(
                "applied repair receipt lacks verification state"
            )
        if verified:
            if live_sha != payload["repaired_history_sha256"]:
                raise HistoryIntegrityError("verified live repair SHA is inconsistent")
        elif live_sha is not None:
            raise HistoryIntegrityError("unverified live repair cannot claim a SHA")
        if not isinstance(metadata, dict) or set(metadata) != {"mode", "uid", "gid"}:
            raise HistoryIntegrityError("applied repair receipt metadata is invalid")
        for field in ("mode", "uid", "gid"):
            value = metadata.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HistoryIntegrityError(
                    "applied repair receipt metadata is invalid"
                )
    if status == "applied" and (not verified or failures):
        raise HistoryIntegrityError(
            "applied repair receipt has unresolved audit failure"
        )
    if status == "applied_with_audit_failure" and not failures:
        raise HistoryIntegrityError("audit-failure repair receipt lacks diagnostics")
    if not isinstance(payload.get("marker"), dict):
        raise HistoryIntegrityError("repair receipt marker is invalid")
    return dict(payload)


def raw_evidence_payload(result: RepairResult) -> dict[str, Any]:
    return {
        "damaged_line_base64": base64.b64encode(result.bad_line_raw).decode("ascii"),
        "damaged_line_sha256": sha256_bytes(result.bad_line_raw),
        "fragment_base64": base64.b64encode(result.fragment_raw).decode("ascii"),
        "fragment_sha256": sha256_bytes(result.fragment_raw),
        "suffix_event_base64": base64.b64encode(result.suffix_raw).decode("ascii"),
        "suffix_event_sha256": sha256_bytes(result.suffix_raw),
    }


def multi_raw_evidence_payload(result: MultiRepairResult) -> dict[str, Any]:
    return {
        "schema_version": "voxquarry.structured-refresh-history-raw-evidence/v2",
        "original_history_sha256": result.original_history_sha256,
        "repairs": [
            {
                "damaged_line_number": item.plan["damaged_line_number"],
                "damaged_line_base64": base64.b64encode(item.bad_line_raw).decode(
                    "ascii"
                ),
                "damaged_line_sha256": sha256_bytes(item.bad_line_raw),
                "fragment_base64": base64.b64encode(item.fragment_raw).decode("ascii"),
                "fragment_sha256": sha256_bytes(item.fragment_raw),
                "suffix_event_base64": base64.b64encode(item.suffix_raw).decode(
                    "ascii"
                ),
                "suffix_event_sha256": sha256_bytes(item.suffix_raw),
                "disposition": item.disposition,
                "replacement_event_sha256": sha256_bytes(
                    canonical_json(item.replacement_event).encode("utf-8")
                ),
                "regenerated_event_sha256": item.plan.get(
                    "regenerated_event_sha256"
                ),
                "quarantine_record_sha256": (
                    sha256_bytes(canonical_json(item.replacement_event).encode("utf-8"))
                    if item.disposition == "quarantined"
                    else None
                ),
                "quarantine_reason": item.plan.get("quarantine_reason"),
                "authority": item.plan["authority"],
            }
            for item in result.repairs
        ],
    }


def _multi_repair_audits(
    result: MultiRepairResult,
    *,
    quarantine_evidence_paths: dict[int, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    quarantine_evidence_paths = quarantine_evidence_paths or {}
    return [
        {
            "action": item.plan["action"],
            "disposition": item.disposition,
            "damaged_line_number": item.plan["damaged_line_number"],
            "damaged_line_sha256": sha256_bytes(item.bad_line_raw),
            "fragment_sha256": sha256_bytes(item.fragment_raw),
            "suffix_event_sha256": sha256_bytes(item.suffix_raw),
            "previous_line_sha256": item.plan["previous_line_sha256"],
            "following_line_sha256": item.plan["following_line_sha256"],
            "incident_evidence_sha256": item.plan["incident_evidence_sha256"],
            "replacement_event_sha256": sha256_bytes(
                canonical_json(item.replacement_event).encode("utf-8")
            ),
            "regenerated_event_sha256": item.plan.get("regenerated_event_sha256"),
            "quarantine_record_sha256": (
                sha256_bytes(canonical_json(item.replacement_event).encode("utf-8"))
                if item.disposition == "quarantined"
                else None
            ),
            "quarantine_reason": item.plan.get("quarantine_reason"),
            "quarantine_evidence_path": (
                quarantine_evidence_paths.get(item.plan["damaged_line_number"], {}).get(
                    "raw"
                )
            ),
            "quarantine_record_path": (
                quarantine_evidence_paths.get(item.plan["damaged_line_number"], {}).get(
                    "record"
                )
            ),
            "authority": item.plan["authority"],
        }
        for item in result.repairs
    ]


def multi_repair_receipt(
    *,
    original: bytes,
    result: MultiRepairResult,
    plan: dict[str, Any],
    archive_path: Path | None,
    evidence_path: Path | None,
    status: str,
    audit_failures: tuple[str, ...] | list[str] = (),
    live_history_verified: bool | None = None,
    live_history_sha256: str | None = None,
    live_metadata: dict[str, int] | None = None,
    quarantine_evidence_paths: dict[int, dict[str, str]] | None = None,
) -> dict[str, Any]:
    if status not in {
        "dry_run",
        "prepared",
        "applied",
        "applied_with_audit_failure",
    }:
        raise ValueError("multi-repair receipt status is invalid")
    payload = {
        "schema_version": MULTI_REPAIR_RECEIPT_SCHEMA,
        "status": status,
        "plan_sha256": sha256_bytes(canonical_json(plan).encode("utf-8")),
        "original_history_sha256": sha256_bytes(original),
        "repaired_history_sha256": sha256_bytes(result.repaired_bytes),
        "original_bytes": len(original),
        "repaired_bytes": len(result.repaired_bytes),
        "repair_count": len(result.repairs),
        "repairs": _multi_repair_audits(
            result, quarantine_evidence_paths=quarantine_evidence_paths
        ),
        "archive_path": str(archive_path) if archive_path else None,
        "evidence_path": str(evidence_path) if evidence_path else None,
        "live_history_verified": live_history_verified,
        "live_history_sha256": live_history_sha256,
        "live_metadata": live_metadata,
        "audit_failures": list(audit_failures),
    }
    return validate_multi_repair_receipt(payload)


def validate_multi_repair_receipt(payload: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "status",
        "plan_sha256",
        "original_history_sha256",
        "repaired_history_sha256",
        "original_bytes",
        "repaired_bytes",
        "repair_count",
        "repairs",
        "archive_path",
        "evidence_path",
        "live_history_verified",
        "live_history_sha256",
        "live_metadata",
        "audit_failures",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise HistoryIntegrityError("multi-repair receipt fields are invalid")
    if payload.get("schema_version") != MULTI_REPAIR_RECEIPT_SCHEMA:
        raise HistoryIntegrityError("multi-repair receipt schema_version is invalid")
    status = payload.get("status")
    if status not in {
        "dry_run",
        "prepared",
        "applied",
        "applied_with_audit_failure",
    }:
        raise HistoryIntegrityError("multi-repair receipt status is invalid")
    for field in (
        "plan_sha256",
        "original_history_sha256",
        "repaired_history_sha256",
    ):
        _require_sha256(payload.get(field), context=f"multi-repair receipt {field}")
    for field in ("original_bytes", "repaired_bytes", "repair_count"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HistoryIntegrityError(f"multi-repair receipt {field} is invalid")
    repairs = payload.get("repairs")
    if (
        not isinstance(repairs, list)
        or len(repairs) != payload["repair_count"]
        or not repairs
    ):
        raise HistoryIntegrityError("multi-repair receipt repairs are invalid")
    line_numbers: list[int] = []
    repair_fields = {
        "action",
        "disposition",
        "damaged_line_number",
        "damaged_line_sha256",
        "fragment_sha256",
        "suffix_event_sha256",
        "previous_line_sha256",
        "following_line_sha256",
        "incident_evidence_sha256",
        "replacement_event_sha256",
        "regenerated_event_sha256",
        "quarantine_record_sha256",
        "quarantine_reason",
        "quarantine_evidence_path",
        "quarantine_record_path",
        "authority",
    }
    for index, repair in enumerate(repairs):
        if not isinstance(repair, dict) or set(repair) != repair_fields:
            raise HistoryIntegrityError(
                f"multi-repair receipt repairs[{index}] fields are invalid"
            )
        action = repair.get("action")
        disposition = repair.get("disposition")
        if (action, disposition) not in {
            ("regenerate_failed_event", "regenerated"),
            ("quarantine_failed_fragment", "quarantined"),
        }:
            raise HistoryIntegrityError("multi-repair receipt disposition is invalid")
        line_number = repair.get("damaged_line_number")
        if isinstance(line_number, bool) or not isinstance(line_number, int):
            raise HistoryIntegrityError("multi-repair receipt line number is invalid")
        line_numbers.append(line_number)
        for field in (
            "damaged_line_sha256",
            "fragment_sha256",
            "suffix_event_sha256",
            "incident_evidence_sha256",
            "replacement_event_sha256",
        ):
            _require_sha256(repair.get(field), context=f"multi-repair receipt {field}")
        if disposition == "regenerated":
            _require_sha256(
                repair.get("regenerated_event_sha256"),
                context="multi-repair receipt regenerated_event_sha256",
            )
            if any(
                repair.get(field) is not None
                for field in (
                    "quarantine_record_sha256",
                    "quarantine_reason",
                    "quarantine_evidence_path",
                    "quarantine_record_path",
                )
            ):
                raise HistoryIntegrityError(
                    "regenerated receipt cannot contain quarantine fields"
                )
        else:
            if repair.get("regenerated_event_sha256") is not None:
                raise HistoryIntegrityError(
                    "quarantined receipt cannot claim a regenerated event"
                )
            _require_sha256(
                repair.get("quarantine_record_sha256"),
                context="multi-repair receipt quarantine_record_sha256",
            )
            if repair.get("quarantine_record_sha256") != repair.get(
                "replacement_event_sha256"
            ):
                raise HistoryIntegrityError(
                    "quarantine record SHA conflicts with replacement event"
                )
            if repair.get("quarantine_reason") not in QUARANTINE_REASONS:
                raise HistoryIntegrityError(
                    "multi-repair receipt quarantine_reason is invalid"
                )
            evidence_path = repair.get("quarantine_evidence_path")
            record_path = repair.get("quarantine_record_path")
            if status == "dry_run":
                if evidence_path is not None or record_path is not None:
                    raise HistoryIntegrityError(
                        "dry-run quarantine cannot claim persisted evidence"
                    )
            elif (
                not isinstance(evidence_path, str)
                or not evidence_path
                or not isinstance(record_path, str)
                or not record_path
            ):
                raise HistoryIntegrityError(
                    "persisted quarantine receipt lacks evidence path"
                )
        for field in ("previous_line_sha256", "following_line_sha256"):
            if repair.get(field) is not None:
                _require_sha256(repair[field], context=f"multi-repair receipt {field}")
        _validate_repair_authority(
            repair.get("authority"), context="multi-repair receipt authority"
        )
    if line_numbers != sorted(set(line_numbers)):
        raise HistoryIntegrityError("multi-repair receipt lines are not sorted unique")

    failures = payload.get("audit_failures")
    if not isinstance(failures, list) or any(
        not isinstance(item, str) or not item for item in failures
    ):
        raise HistoryIntegrityError("multi-repair receipt audit_failures are invalid")
    verified = payload.get("live_history_verified")
    live_sha = payload.get("live_history_sha256")
    metadata = payload.get("live_metadata")
    if status in {"dry_run", "prepared"}:
        if (
            verified is not None
            or live_sha is not None
            or metadata is not None
            or failures
        ):
            raise HistoryIntegrityError(
                "unapplied multi-repair receipt has live outcome fields"
            )
    else:
        if not isinstance(verified, bool):
            raise HistoryIntegrityError(
                "applied multi-repair receipt lacks verification state"
            )
        if verified:
            if live_sha != payload["repaired_history_sha256"]:
                raise HistoryIntegrityError("verified multi-repair SHA is inconsistent")
        elif live_sha is not None:
            raise HistoryIntegrityError("unverified multi-repair cannot claim a SHA")
        if not isinstance(metadata, dict) or set(metadata) != {"mode", "uid", "gid"}:
            raise HistoryIntegrityError("applied multi-repair metadata is invalid")
        for field in ("mode", "uid", "gid"):
            value = metadata.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HistoryIntegrityError("applied multi-repair metadata is invalid")
    if status == "applied" and (not verified or failures):
        raise HistoryIntegrityError("applied multi-repair has unresolved audit failure")
    if status == "applied_with_audit_failure" and not failures:
        raise HistoryIntegrityError("multi-repair audit failure lacks diagnostics")
    return dict(payload)


def validate_diagnostics_payload(payload: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "generated_at",
        "history_parse_status",
        "history_error",
        "history_sha256",
        "history_record_count",
        "last_history_event_at",
        "state_parse_status",
        "state_error",
        "last_successful_refresh_at",
        "pending_ready_batch_count",
        "oldest_pending_ready_at",
        "pending_ready_delay_seconds",
        "scheduler_error",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise HistoryIntegrityError("diagnostics payload fields are invalid")
    if payload.get("schema_version") != DIAGNOSTICS_SCHEMA:
        raise HistoryIntegrityError("diagnostics schema_version is invalid")
    _parse_timestamp(payload.get("generated_at"), context="diagnostics generated_at")
    for field in ("history_parse_status", "state_parse_status"):
        if payload.get(field) not in {"ok", "invalid"}:
            raise HistoryIntegrityError(f"diagnostics {field} is invalid")
    for field in ("history_error", "state_error", "scheduler_error"):
        if payload.get(field) is not None and not isinstance(payload.get(field), str):
            raise HistoryIntegrityError(f"diagnostics {field} is invalid")
    history_sha = payload.get("history_sha256")
    if history_sha is not None and (
        not isinstance(history_sha, str) or not SHA256_RE.fullmatch(history_sha)
    ):
        raise HistoryIntegrityError("diagnostics history_sha256 is invalid")
    for field in (
        "history_record_count",
        "pending_ready_batch_count",
        "pending_ready_delay_seconds",
    ):
        value = payload.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise HistoryIntegrityError(f"diagnostics {field} is invalid")
    if payload["pending_ready_batch_count"] is None:
        raise HistoryIntegrityError("pending_ready_batch_count cannot be null")
    for field in (
        "last_history_event_at",
        "last_successful_refresh_at",
        "oldest_pending_ready_at",
    ):
        if payload.get(field) is not None:
            _parse_timestamp(payload[field], context=f"diagnostics {field}")
    if payload["pending_ready_batch_count"] == 0:
        if (
            payload["oldest_pending_ready_at"] is not None
            or payload["pending_ready_delay_seconds"] is not None
        ):
            raise HistoryIntegrityError(
                "zero pending-ready diagnostics are inconsistent"
            )
    elif (
        payload["oldest_pending_ready_at"] is None
        or payload["pending_ready_delay_seconds"] is None
    ):
        raise HistoryIntegrityError("pending-ready diagnostics are incomplete")
    return dict(payload)


def read_diagnostics_sidecar(var_root: Path) -> dict[str, Any]:
    """Read additive scheduler health without trusting it as state."""

    path = var_root / "structured-refresh-diagnostics.json"
    unavailable = {
        "schema_version": DIAGNOSTICS_SCHEMA,
        "status": "unavailable",
        "freshness_status": "unknown",
    }
    invalid = {
        "schema_version": DIAGNOSTICS_SCHEMA,
        "status": "invalid",
        "freshness_status": "unknown",
    }
    if not path.is_file():
        return unavailable
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return invalid
    try:
        payload = validate_diagnostics_payload(payload)
    except HistoryIntegrityError:
        return invalid
    result = dict(payload)
    result["status"] = (
        "ok"
        if payload.get("history_parse_status") == "ok"
        and payload.get("state_parse_status") == "ok"
        else "degraded"
    )
    # Parse health and data freshness answer different questions. Derive this
    # field here so existing host-side v1 sidecars remain compatible.
    if payload.get("scheduler_error") is not None:
        result["freshness_status"] = "error"
    elif result["status"] != "ok":
        result["freshness_status"] = "unknown"
    elif payload["pending_ready_batch_count"]:
        result["freshness_status"] = "pending"
    else:
        result["freshness_status"] = "current"
    return result
