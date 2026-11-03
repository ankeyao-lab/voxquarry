"""Exact, artifact-bound energy projection maintenance for completed jobs.

This module is intentionally not exposed by the HTTP API.  It verifies one
content-addressed Operations plan, one single-use Control Plane authorization,
the live job identities, and the immutable active selection/validation files
before atomically changing the governed energy projection.  Rollback is a
second authorized transaction and never deletes the apply audit events.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .product_identity import (
    PRODUCT_OBSERVATION_SCHEMA_VERSION,
    TASK_ENERGY_SCHEMA_VERSION,
    VOLTAGE_PLATFORM_SCHEMA_VERSION,
    normalize_brand_token,
    normalize_voltage,
    observation_hash,
    validate_task_energy,
)


PLAN_SCHEMA_VERSION = "voxquarry.operations-task-energy-apply-plan/v2"
AUTH_SCHEMA_VERSION = "voxquarry.completed-task-energy-authorization/v1"
RECEIPT_SCHEMA_VERSION = "voxquarry.completed-task-energy-transaction-receipt/v1"
RUNTIME_MANIFEST_SCHEMA_VERSION = "voxquarry.connector-runtime-manifest/v1"
BACKUP_RECEIPT_SCHEMA_VERSION = "voxquarry.sqlite-verified-backup-receipt/v1"
PENDING_RECEIPT_SCHEMA_VERSION = "voxquarry.committed-pending-receipt/v1"
EXECUTOR_VERSION = "completed-task-energy-apply/1.2.1"
EXCLUDED_GUARD_HASH_POLICY = "sha256-canonical-json/v1"
EXCLUDED_PROTECTED_FIELDS_CONTRACT = "voxquarry.completed-task-energy-excluded-protected-fields/v2"
PRODUCTION_RUNTIME_MANIFEST = Path("/opt/voxquarry/runtime/connector-runtime-manifest.json")
PRODUCTION_AUTHORIZATION_KEY = Path("/run/secrets/voxquarry-energy-authorization.key")
RUNTIME_FILES = (
    "app/product_identity.py",
    "app/task_energy_completed_apply.py",
    "tools/apply_completed_task_energy.py",
)

PLAN_FILES = {
    "apply-plan.json",
    "dry-run-receipt.json",
    "source-facts.json",
    "artifact-manifest.json",
    "SHA256SUMS",
}
PROJECTION_COLUMNS = (
    "energy_schema_version", "energy_type", "energy_readiness_status",
    "energy_evidence_source", "energy_canonical_voltage", "energy_unknown_reason",
    "product_observation_json", "product_observation_hash",
    "voltage_platform_schema_version", "voltage_platform_status",
    "voltage_platform_key", "voltage_platform_brand",
    "voltage_platform_voltage", "voltage_platform_label",
    "voltage_platform_evidence_source", "voltage_platform_null_reason",
)
JSON_COLUMNS = {"product_observation_json"}
PROTECTED_COLUMNS = (
    "id", "action", "channel", "status", "progress", "result_json", "error",
    "created_at", "started_at", "finished_at", "target_url", "target_key",
    "brand", "model", "source_product_id", "category",
    "source_product_identity_status", "source_product_identity_json",
    "source_product_identity_hash", "source_product_identity_evidence_source",
    "market_region", "priority", "retry_of", "superseded_by",
    "cancel_requested", "attempt_count", "active_attempt_no",
    "active_result_status", "active_run_path", "last_attempt_status",
    "acquisition_batch_id", "product_master_schema_version",
    "product_master_snapshot_id", "product_master_model_id",
    "product_master_entity_version", "product_master_record_snapshot_hash",
    "product_master_record_json", "product_enrichment_status",
    "product_quality_flags_json",
)
EXCLUDED_PROTECTED_COLUMNS = (
    "active_attempt_no", "active_result_status", "active_run_path", "attempt_count",
    "brand", "category", "channel", "energy_canonical_voltage",
    "energy_evidence_source", "energy_readiness_status", "energy_schema_version",
    "energy_type", "energy_unknown_reason", "id", "last_attempt_status",
    "market_region", "model", "product_observation_hash", "product_observation_json",
    "source_product_id", "source_product_identity_status", "status", "target_key",
    "target_url", "voltage_platform_brand", "voltage_platform_evidence_source",
    "voltage_platform_key", "voltage_platform_label", "voltage_platform_null_reason",
    "voltage_platform_schema_version", "voltage_platform_status", "voltage_platform_voltage",
)
PRECONDITION_JOB_FIELDS = (
    "active_attempt_no", "active_result_status", "active_run_path",
    "attempt_count", "brand", "category", "channel", "id",
    "last_attempt_status", "model", "source_product_id",
    "source_product_identity_status", "status", "target_key",
)
AUTH_KEYS = {
    "schema_version", "action", "work_order_id", "dispatch_id", "scope_sha256",
    "plan_artifact_id", "plan_logical_digest", "plan_file_sha256",
    "evidence_artifact_digest", "candidate_commit", "runtime_manifest_sha256",
    "authorization_issuer", "backup_device_uuid", "signature_algorithm", "signature",
    "database_prestate_sha256", "backup_receipt_sha256", "apply_receipt_sha256",
    "authorized_at_utc", "expires_at_utc", "single_use", "production_authorization",
}


@dataclass(frozen=True)
class ApplyPlanBinding:
    artifact_id: str
    logical_digest: str
    plan_sha256: str
    manifest_sha256: str
    dry_run_sha256: str
    source_facts_sha256: str
    sums_sha256: str
    evidence_digest: str
    current_facts_digest: str
    current_facts_manifest_sha256: str
    current_facts_receipt_sha256: str
    work_order_id: str = "VQ-ENERGY-MIGRATION-V1"
    dispatch_id: str = "VQ-ENERGY-DISPATCH-V1"
    scope_sha256: str = "sha256:90915440afac2dc0b5e2893f9b3c43761005d38a31bca2f21ad9125eb34262ba"
    expected_records: int = 4
    expected_excluded: int = 31
    require_distinct_backup_device: bool = True


PRODUCTION_BINDING = ApplyPlanBinding(
    artifact_id="task-energy-v4-apply-plan-6d742f386b23b1142d0f85592c9c02d19a6eeeb2f43078f558881936050f9373",
    logical_digest="sha256:6d742f386b23b1142d0f85592c9c02d19a6eeeb2f43078f558881936050f9373",
    plan_sha256="19d7af89198a8e5fd4a78705511045484c5980ec80d0a86bc80c17f3167ee3d9",
    manifest_sha256="55a86dd808417b6ce14bb37fd41116fcc78addcb8d98861a5b42f9a318a54cc2",
    dry_run_sha256="d93b513c4aba1c905576f46b58fda315bd5c0ed93f1d50ae1c6a6179f845c827",
    source_facts_sha256="5c105e611936e7f3a29904a5bb75cc1a5c02df943584f93ee73cff40274964a9",
    sums_sha256="241b45045de88cbab4be62d5622dc793869c690316cf12f8b4c64e3df82909b0",
    evidence_digest="sha256:9aebb3d99ba1877c4d6bbda5ce211055cad553e900a3b9e6367beedae8139aae",
    current_facts_digest="sha256:8c590701322d3a1f21463de71ad2335c073fe9d0fd47b46668b60694b58975e4",
    current_facts_manifest_sha256="sha256:2667d32080748abbfb0b62f002b7332d70f40ebb0d7ef7be84358dc5897e4e3c",
    current_facts_receipt_sha256="sha256:213749d05484147b553293239001eb39a69aef754cdf027718bcf6c721e5a9d0",
)


class EnergyApplyError(RuntimeError):
    """A fail-closed plan, authorization, identity, or transaction error."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_value(value):
    return "sha256:" + _sha_bytes(_canonical(value).encode("utf-8"))


def _load_json(path, label):
    path = Path(path)
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EnergyApplyError(f"duplicate JSON key in {label}: {key}")
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnergyApplyError(f"unreadable {label}") from exc


def _require_regular_private(path, *, directory=False):
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise EnergyApplyError(f"missing governed path: {path.name}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise EnergyApplyError(f"symlink is forbidden: {path.name}")
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        raise EnergyApplyError(f"unexpected governed path type: {path.name}")
    if info.st_uid != os.getuid():
        raise EnergyApplyError(f"governed path owner mismatch: {path.name}")
    if info.st_mode & 0o077:
        raise EnergyApplyError(f"governed path is not private: {path.name}")


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_time(value, label):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EnergyApplyError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise EnergyApplyError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _runtime_file_paths():
    service = Path(__file__).resolve().parents[1]
    return {relative: service / relative for relative in RUNTIME_FILES}


def _load_runtime_manifest(path):
    path = Path(path)
    _require_regular_private(path)
    value = _load_json(path, "runtime manifest")
    required = {"schema_version", "candidate_commit", "files", "manifest_content_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise EnergyApplyError("runtime manifest fields mismatch")
    if value["schema_version"] != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise EnergyApplyError("runtime manifest schema mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", str(value["candidate_commit"])):
        raise EnergyApplyError("runtime manifest candidate is invalid")
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != set(RUNTIME_FILES):
        raise EnergyApplyError("runtime manifest file scope mismatch")
    for relative, actual_path in _runtime_file_paths().items():
        expected = files.get(relative)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(expected)):
            raise EnergyApplyError("runtime manifest file digest is invalid")
        if not actual_path.is_file() or "sha256:" + _sha_file(actual_path) != expected:
            raise EnergyApplyError(f"runtime file drift: {relative}")
    content = dict(value)
    content.pop("manifest_content_sha256")
    if _sha_value(content) != value["manifest_content_sha256"]:
        raise EnergyApplyError("runtime manifest content digest mismatch")
    return value, "sha256:" + _sha_file(path)


def _load_authorization_key(path):
    path = Path(path)
    _require_regular_private(path)
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise EnergyApplyError("authorization signing key is unreadable") from exc
    if len(value) < 32:
        raise EnergyApplyError("authorization signing key is too short")
    return value


def _authorization_signature(value, key):
    signed = dict(value)
    signed.pop("signature", None)
    return "hmac-sha256:" + hmac.new(key, _canonical(signed).encode("utf-8"), hashlib.sha256).hexdigest()


def _sqlite_readonly_checks(path):
    uri = f"file:{Path(path).resolve()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise EnergyApplyError("verified backup SQLite is unreadable") from exc
    if quick != "ok" or integrity != "ok" or foreign:
        raise EnergyApplyError("verified backup SQLite checks failed")
    return {"quick_check": quick, "integrity_check": integrity, "foreign_key_violations": foreign}


def _verify_backup_receipt(path, authorization, db_path, *, require_distinct_device,
                           verified_committed_recovery=False):
    path = Path(path)
    _require_regular_private(path)
    if "sha256:" + _sha_file(path) != authorization["backup_receipt_sha256"]:
        raise EnergyApplyError("verified backup receipt SHA mismatch")
    value = _load_json(path, "backup receipt")
    required = {
        "schema_version", "status", "source_database_realpath", "source_database_sha256",
        "source_database_size_bytes", "backup_database_realpath", "backup_database_sha256",
        "backup_database_size_bytes", "physical_device_id", "physical_device_uuid",
        "available_bytes", "created_at_utc", "verified_at_utc", "expires_at_utc",
        "sqlite_checks", "restore_check",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise EnergyApplyError("backup receipt fields mismatch")
    if value["schema_version"] != BACKUP_RECEIPT_SCHEMA_VERSION or value["status"] != "VERIFIED":
        raise EnergyApplyError("backup receipt is not VERIFIED")
    source = Path(value["source_database_realpath"])
    backup = Path(value["backup_database_realpath"])
    if not source.is_absolute() or source.resolve() != Path(db_path).resolve():
        raise EnergyApplyError("backup receipt source database mismatch")
    _require_regular_private(backup)
    source_stat = Path(db_path).stat()
    backup_stat = backup.stat()
    if str(backup_stat.st_dev) != str(value["physical_device_id"]):
        raise EnergyApplyError("backup physical device identity mismatch")
    if authorization["backup_device_uuid"] != value["physical_device_uuid"]:
        raise EnergyApplyError("authorization backup device UUID mismatch")
    if require_distinct_device and source_stat.st_dev == backup_stat.st_dev:
        raise EnergyApplyError("verified backup must reside on a distinct physical device")
    if (
        value["source_database_sha256"] != authorization["database_prestate_sha256"]
        or value["backup_database_sha256"] != "sha256:" + _sha_file(backup)
        or value["backup_database_size_bytes"] != backup_stat.st_size
        or value["source_database_size_bytes"] != backup_stat.st_size
        or value["source_database_sha256"] != value["backup_database_sha256"]
    ):
        raise EnergyApplyError("backup receipt database bytes mismatch")
    if not verified_committed_recovery and (
        value["source_database_sha256"] != "sha256:" + _sha_file(db_path)
        or value["source_database_size_bytes"] != source_stat.st_size
    ):
        raise EnergyApplyError("live database no longer matches the authorized backup prestate")
    if not isinstance(value["available_bytes"], int) or value["available_bytes"] < backup_stat.st_size:
        raise EnergyApplyError("backup capacity evidence is insufficient")
    now = datetime.now(timezone.utc)
    created = _parse_time(value["created_at_utc"], "backup created_at")
    verified = _parse_time(value["verified_at_utc"], "backup verified_at")
    expires = _parse_time(value["expires_at_utc"], "backup expires_at")
    authorized = _parse_time(authorization["authorized_at_utc"], "authorization authorized_at")
    if not created <= verified <= authorized <= now <= expires or (authorized - verified).total_seconds() > 86400:
        raise EnergyApplyError("backup receipt time validity failed")
    checks = _sqlite_readonly_checks(backup)
    if value["sqlite_checks"] != checks:
        raise EnergyApplyError("backup SQLite check receipt mismatch")
    if value["restore_check"] != {
        "status": "VERIFIED", "quick_check": "ok", "integrity_check": "ok",
        "foreign_key_violations": 0,
    }:
        raise EnergyApplyError("backup restore-check is not VERIFIED")
    return value


def _verify_sums(root, binding):
    root = Path(root)
    _require_regular_private(root, directory=True)
    actual = {item.name for item in root.iterdir()}
    if actual != PLAN_FILES:
        raise EnergyApplyError(f"plan artifact closed tree mismatch: {sorted(actual)}")
    for item in root.iterdir():
        _require_regular_private(item)
    expected = {
        "apply-plan.json": binding.plan_sha256,
        "artifact-manifest.json": binding.manifest_sha256,
        "dry-run-receipt.json": binding.dry_run_sha256,
        "source-facts.json": binding.source_facts_sha256,
    }
    if _sha_file(root / "SHA256SUMS") != binding.sums_sha256:
        raise EnergyApplyError("plan SHA256SUMS binding mismatch")
    parsed = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        if not match or match.group(2) in parsed:
            raise EnergyApplyError("invalid or duplicate SHA256SUMS entry")
        parsed[match.group(2)] = match.group(1)
    if parsed != expected:
        raise EnergyApplyError("plan SHA256SUMS entries mismatch")
    for name, digest in expected.items():
        if _sha_file(root / name) != digest:
            raise EnergyApplyError(f"plan payload digest mismatch: {name}")


def _validate_new_projection(value):
    if not isinstance(value, dict) or set(value) != set(PROJECTION_COLUMNS):
        raise EnergyApplyError("governed projection fields mismatch")
    observation = value["product_observation_json"]
    if not isinstance(observation, dict):
        raise EnergyApplyError("product observation must be an object")
    if observation.get("schema_version") != PRODUCT_OBSERVATION_SCHEMA_VERSION:
        raise EnergyApplyError("product observation schema mismatch")
    if observation_hash(observation) != value["product_observation_hash"]:
        raise EnergyApplyError("product observation hash mismatch")
    energy = {
        "schema_version": value["energy_schema_version"],
        "energy_type": value["energy_type"],
        "readiness_status": value["energy_readiness_status"],
        "evidence_source": value["energy_evidence_source"],
        "canonical_voltage": value["energy_canonical_voltage"],
        "unknown_reason": value["energy_unknown_reason"],
    }
    try:
        validate_task_energy(energy)
    except ValueError as exc:
        raise EnergyApplyError(str(exc)) from exc
    if energy["energy_type"] != "battery" or energy["readiness_status"] != "ready":
        raise EnergyApplyError("exact plan must contain ready battery energy")
    voltage = normalize_voltage(value["voltage_platform_voltage"])
    brand_token = normalize_brand_token(value["voltage_platform_brand"])
    if (
        value["voltage_platform_schema_version"] != VOLTAGE_PLATFORM_SCHEMA_VERSION
        or value["voltage_platform_status"] != "known"
        or not voltage or not brand_token
        or value["voltage_platform_key"] != f"{brand_token}|{voltage}"
        or value["voltage_platform_label"] != f"{value['voltage_platform_brand']} {voltage}"
        or value["voltage_platform_null_reason"] is not None
        or value["energy_canonical_voltage"] != voltage
        or value["voltage_platform_evidence_source"] != value["energy_evidence_source"]
        or observation.get("brand") != value["voltage_platform_brand"]
        or observation.get("normalized_voltage") != voltage
        or observation.get("energy_type") != "battery"
    ):
        raise EnergyApplyError("energy, observation, and platform projections disagree")


def load_plan(artifact_dir, binding=PRODUCTION_BINDING):
    root = Path(artifact_dir)
    _verify_sums(root, binding)
    plan = _load_json(root / "apply-plan.json", "apply plan")
    manifest = _load_json(root / "artifact-manifest.json", "plan manifest")
    source = _load_json(root / "source-facts.json", "source facts")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise EnergyApplyError("unsupported plan schema")
    if plan.get("artifact_id") != binding.artifact_id:
        raise EnergyApplyError("plan artifact id mismatch")
    if plan.get("logical_digest") != binding.logical_digest:
        raise EnergyApplyError("plan logical digest mismatch")
    if manifest.get("artifact_id") != binding.artifact_id or manifest.get("logical_digest") != binding.logical_digest:
        raise EnergyApplyError("plan manifest lineage mismatch")
    if plan.get("proposal_lineage", {}).get("artifact_digest") != binding.evidence_digest.removeprefix("sha256:"):
        raise EnergyApplyError("evidence artifact lineage mismatch")
    current_lineage = plan.get("current_facts_lineage")
    if not isinstance(current_lineage, dict) or set(current_lineage) != {
        "schema_version", "artifact_id", "artifact_digest", "manifest_sha256",
        "receipt_sha256", "schema_sha256", "connector_candidate",
        "reporting_candidate", "database_canonical_sha256", "database_schema_sha256",
        "observed_at_utc",
    }:
        raise EnergyApplyError("current facts lineage fields mismatch")
    if (
        current_lineage.get("schema_version") != "voxquarry.operations-task-energy-current-facts/v4"
        or current_lineage.get("artifact_digest") != binding.current_facts_digest
        or current_lineage.get("manifest_sha256") != binding.current_facts_manifest_sha256
        or current_lineage.get("receipt_sha256") != binding.current_facts_receipt_sha256
    ):
        raise EnergyApplyError("current facts lineage mismatch")
    if plan.get("executor_binding") != {
        "executor_version": EXECUTOR_VERSION,
        "runtime_manifest_schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "runtime_files": list(RUNTIME_FILES),
        "authorization_schema_version": AUTH_SCHEMA_VERSION,
        "rollback_uses_same_plan": True,
    }:
        raise EnergyApplyError("plan executor/runtime binding mismatch")
    records = plan.get("records")
    excluded_guard = plan.get("excluded_guard")
    if not isinstance(excluded_guard, dict) or set(excluded_guard) != {
        "count", "hash_policy", "protected_fields_contract", "protected_fields", "records",
    }:
        raise EnergyApplyError("excluded guard fields mismatch")
    excluded = excluded_guard.get("records")
    if not isinstance(records, list) or len(records) != binding.expected_records:
        raise EnergyApplyError("exact write allowlist count mismatch")
    if (
        not isinstance(excluded, list)
        or len(excluded) != binding.expected_excluded
        or excluded_guard.get("count") != binding.expected_excluded
        or excluded_guard.get("hash_policy") != EXCLUDED_GUARD_HASH_POLICY
        or excluded_guard.get("protected_fields_contract") != EXCLUDED_PROTECTED_FIELDS_CONTRACT
        or excluded_guard.get("protected_fields") != list(EXCLUDED_PROTECTED_COLUMNS)
    ):
        raise EnergyApplyError("exact excluded guard count mismatch")
    ids = [item.get("job_id") for item in records]
    excluded_ids = []
    for item in excluded:
        if not isinstance(item, dict) or set(item) != {
            "job_id", "governed_projection_sha256", "protected_fields_sha256",
        }:
            raise EnergyApplyError("excluded guard record fields mismatch")
        job_id = item.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise EnergyApplyError("excluded guard job identity is invalid")
        for key in ("governed_projection_sha256", "protected_fields_sha256"):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get(key))):
                raise EnergyApplyError("excluded guard hash is invalid")
        excluded_ids.append(job_id)
    if excluded_ids != sorted(excluded_ids):
        raise EnergyApplyError("excluded guard records are not canonically ordered")
    if len(set(ids)) != len(ids) or set(ids) & set(excluded_ids) or len(set(excluded_ids)) != len(excluded_ids):
        raise EnergyApplyError("plan allowlist/excluded identity mismatch")
    if plan.get("counts") != {
        "current_production_write_count": 0,
        "excluded_unknown": binding.expected_excluded,
        "planned_append_only_audit_events": binding.expected_records,
        "planned_business_row_updates": binding.expected_records,
        "proposal_total": binding.expected_records + binding.expected_excluded,
        "write_allowlist": binding.expected_records,
    }:
        raise EnergyApplyError("plan counts mismatch")
    source_tasks = source.get("tasks") if isinstance(source, dict) else None
    if not isinstance(source_tasks, dict) or set(source_tasks) != set(ids):
        raise EnergyApplyError("source facts task scope mismatch")
    for record in records:
        if set(record.get("old", {})) != set(PROJECTION_COLUMNS) or any(record["old"].values()):
            raise EnergyApplyError("old governed projection must be exact NULL")
        if _sha_value(record["old"]) != record.get("precondition", {}).get("old_projection_sha256"):
            raise EnergyApplyError("old projection hash mismatch")
        _validate_new_projection(record.get("new"))
        if _sha_value(record["new"]) != record.get("new_projection_sha256"):
            raise EnergyApplyError("new projection hash mismatch")
        if record.get("precondition", {}).get("job_id") != record.get("job_id"):
            raise EnergyApplyError("record precondition identity mismatch")
        facts = source_tasks[record["job_id"]]
        for key in ("active_attempt_no", "active_result_status", "active_run_path", "attempt_count", "brand", "category", "channel", "last_attempt_status", "model", "source_product_id", "source_product_identity_status", "status", "target_key", "selected_product_id"):
            if facts.get(key) != record["precondition"].get(key):
                raise EnergyApplyError(f"source facts mismatch for {record['job_id']}: {key}")
        record["_source_facts"] = facts
    return plan


def verify_evidence_artifact(evidence_dir, plan, binding=PRODUCTION_BINDING):
    """Re-hash the frozen v3 evidence tree and the four authority payloads."""
    root = Path(evidence_dir)
    _require_regular_private(root, directory=True)
    allowed_root = {"authority-evidence.json", "authority-payloads", "proposal.json", "receipt.json", "SHA256SUMS"}
    if {item.name for item in root.iterdir()} != allowed_root:
        raise EnergyApplyError("evidence artifact closed tree mismatch")
    payload_root = root / "authority-payloads"
    _require_regular_private(payload_root, directory=True)
    for item in root.rglob("*"):
        _require_regular_private(item, directory=item.is_dir())
    parsed = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._/-]+)", line)
        if not match or match.group(2) in parsed or ".." in Path(match.group(2)).parts:
            raise EnergyApplyError("invalid evidence SHA256SUMS entry")
        parsed[match.group(2)] = match.group(1)
    actual_files = {
        str(item.relative_to(root)) for item in root.rglob("*")
        if item.is_file() and item.name != "SHA256SUMS"
    }
    if set(parsed) != actual_files:
        raise EnergyApplyError("evidence SHA256SUMS closed tree mismatch")
    for relative, digest in parsed.items():
        if _sha_file(root / relative) != digest:
            raise EnergyApplyError(f"evidence payload drift: {relative}")
    receipt = _load_json(root / "receipt.json", "evidence receipt")
    expected_id = "task-energy-evidence-proposal-v3-" + binding.evidence_digest.removeprefix("sha256:")
    if receipt.get("artifact_id") != expected_id:
        raise EnergyApplyError("evidence content identity mismatch")
    evidence = _load_json(root / "authority-evidence.json", "authority evidence")
    records = evidence.get("records") if isinstance(evidence, dict) else None
    if not isinstance(records, list):
        raise EnergyApplyError("authority evidence records are missing")
    proposed = {item.get("job_id"): item for item in records if item.get("decision") == "proposed"}
    if set(proposed) != {item["job_id"] for item in plan["records"]}:
        raise EnergyApplyError("authority evidence allowlist mismatch")
    for plan_record in plan["records"]:
        evidence_record = proposed[plan_record["job_id"]]
        relative = evidence_record.get("authority_payload_path")
        if not isinstance(relative, str) or Path(relative).name != relative:
            raise EnergyApplyError("authority payload path is unsafe")
        digest = "sha256:" + _sha_file(payload_root / relative)
        expected = plan_record["precondition"]["authority_payload_sha256"]
        if digest != expected or evidence_record.get("authority_payload_sha256") != expected:
            raise EnergyApplyError(f"authority payload hash drift: {plan_record['job_id']}")
    return evidence


def load_authorization(path, expected_sha256, *, action, binding=PRODUCTION_BINDING,
                       runtime_manifest_path=PRODUCTION_RUNTIME_MANIFEST,
                       authorization_key_path=PRODUCTION_AUTHORIZATION_KEY):
    path = Path(path)
    _require_regular_private(path)
    actual = _sha_file(path)
    if actual != str(expected_sha256).removeprefix("sha256:"):
        raise EnergyApplyError("authorization SHA mismatch")
    value = _load_json(path, "authorization")
    if not isinstance(value, dict) or set(value) != AUTH_KEYS:
        raise EnergyApplyError("authorization fields mismatch")
    if (
        value["schema_version"] != AUTH_SCHEMA_VERSION
        or value["action"] != action
        or value["work_order_id"] != binding.work_order_id
        or value["dispatch_id"] != binding.dispatch_id
        or value["scope_sha256"] != binding.scope_sha256
        or value["plan_artifact_id"] != binding.artifact_id
        or value["plan_logical_digest"] != binding.logical_digest
        or value["plan_file_sha256"] != "sha256:" + binding.plan_sha256
        or value["evidence_artifact_digest"] != binding.evidence_digest
        or value["single_use"] is not True
        or value["production_authorization"] is not True
    ):
        raise EnergyApplyError("authorization does not bind the exact operation")
    manifest, manifest_sha = _load_runtime_manifest(runtime_manifest_path)
    if (
        value["candidate_commit"] != manifest["candidate_commit"]
        or value["runtime_manifest_sha256"] != manifest_sha
    ):
        raise EnergyApplyError("authorization does not bind the trusted runtime candidate")
    if value["authorization_issuer"] != "voxquarry-control-plane":
        raise EnergyApplyError("authorization issuer is not trusted")
    if value["signature_algorithm"] != "hmac-sha256/v1":
        raise EnergyApplyError("authorization signature algorithm mismatch")
    key = _load_authorization_key(authorization_key_path)
    expected_signature = _authorization_signature(value, key)
    if not hmac.compare_digest(str(value["signature"]), expected_signature):
        raise EnergyApplyError("authorization signature is invalid")
    if action == "apply" and value["apply_receipt_sha256"] is not None:
        raise EnergyApplyError("apply authorization cannot bind an apply receipt")
    if action == "rollback" and not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value["apply_receipt_sha256"])):
        raise EnergyApplyError("rollback authorization requires exact apply receipt")
    for key in ("database_prestate_sha256", "backup_receipt_sha256", "runtime_manifest_sha256"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value[key])):
            raise EnergyApplyError(f"invalid authorization binding: {key}")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", str(value["backup_device_uuid"])):
        raise EnergyApplyError("authorization backup device UUID is invalid")
    now = datetime.now(timezone.utc)
    try:
        authorized = _parse_time(value["authorized_at_utc"], "authorization authorized_at")
        expires = _parse_time(value["expires_at_utc"], "authorization expires_at")
    except EnergyApplyError:
        raise
    if not authorized <= now <= expires:
        raise EnergyApplyError("authorization is not currently valid")
    return value, "sha256:" + actual


def _projection(row):
    value = {}
    for key in PROJECTION_COLUMNS:
        raw = row[key]
        if key in JSON_COLUMNS and raw is not None:
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise EnergyApplyError("stored governed JSON is invalid") from exc
        value[key] = raw
    return value


def _protected_projection(row):
    """Return the current-facts protected object for an excluded job.

    JSON-bearing fields intentionally remain raw SQLite TEXT here. The separate
    governed projection hash parses product_observation_json; this second guard
    protects the persisted representation plus task/active-result identity.
    """
    return {key: row[key] for key in EXCLUDED_PROTECTED_COLUMNS}


def _target_hash(row):
    raw = row["target_key"] or row["target_url"] or ""
    return "sha256:" + _sha_bytes(str(raw).encode("utf-8"))


def _selected_product_id(manifest):
    selected = manifest.get("selected") if isinstance(manifest, dict) else None
    if not isinstance(selected, dict):
        raise EnergyApplyError("selection manifest has no selected product")
    value = selected.get("id") or selected.get("product_id") or selected.get("source_product_id")
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise EnergyApplyError("selection manifest product identity is missing")
    return str(value).strip()


def _verify_live_record(data_dir, conn, record, expected_projection):
    job_id = record["job_id"]
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise EnergyApplyError(f"allowlisted job is missing: {job_id}")
    pre = record["precondition"]
    for field in PRECONDITION_JOB_FIELDS:
        actual = row[field]
        expected = pre["job_id"] if field == "id" else pre.get(field)
        if actual != expected:
            raise EnergyApplyError(f"job precondition drift: {job_id}:{field}")
    if _target_hash(row) != pre["target_identity_sha256"]:
        raise EnergyApplyError(f"target identity drift: {job_id}")
    facts = record["_source_facts"]
    if row["target_url"] != facts.get("target_url") or row["market_region"] != facts.get("market_region"):
        raise EnergyApplyError(f"job source facts drift: {job_id}")
    attempt = conn.execute(
        "SELECT status FROM job_attempts WHERE job_id=? AND attempt_no=?",
        (job_id, pre["active_attempt_no"]),
    ).fetchone()
    if attempt is None or attempt["status"] != pre["active_result_status"]:
        raise EnergyApplyError(f"active attempt owner drift: {job_id}")
    if _projection(row) != expected_projection:
        raise EnergyApplyError(f"governed projection drift: {job_id}")
    run_root = (Path(data_dir) / "runs").resolve()
    run_dir = (run_root / pre["active_run_path"]).resolve()
    try:
        run_dir.relative_to(run_root)
    except ValueError as exc:
        raise EnergyApplyError(f"active run path escapes runs root: {job_id}") from exc
    manifest_path = run_dir / "selection_manifest.json"
    validation_path = run_dir / "validation_report.json"
    required = {"reviews.csv", "reviews.raw.jsonl", "selection_manifest.json", "validation_report.json"}
    actual = {item.name for item in run_dir.iterdir() if item.is_file()}
    if actual != required or any(item.is_symlink() or not item.is_file() for item in run_dir.iterdir()):
        raise EnergyApplyError(f"active four-piece artifact set mismatch: {job_id}")
    if _sha_file(manifest_path) != pre["selection_manifest_sha256"].removeprefix("sha256:"):
        raise EnergyApplyError(f"selection manifest drift: {job_id}")
    if _sha_file(validation_path) != pre["validation_report_sha256"].removeprefix("sha256:"):
        raise EnergyApplyError(f"validation report drift: {job_id}")
    manifest = _load_json(manifest_path, "selection manifest")
    if _selected_product_id(manifest).casefold() != str(pre["selected_product_id"]).casefold():
        raise EnergyApplyError(f"selected product identity drift: {job_id}")
    return row


def _active_artifact_hashes(data_dir, plan):
    root = (Path(data_dir) / "runs").resolve()
    result = {}
    for record in plan["records"]:
        run = (root / record["precondition"]["active_run_path"]).resolve()
        try:
            run.relative_to(root)
        except ValueError as exc:
            raise EnergyApplyError("active artifact path escapes runs root") from exc
        result[record["job_id"]] = {
            item.name: "sha256:" + _sha_file(item)
            for item in sorted(run.iterdir(), key=lambda value: value.name)
        }
    return result


def _verify_excluded(conn, plan):
    records = plan["excluded_guard"]["records"]
    ids = [item["job_id"] for item in records]
    expected = {item["job_id"]: item for item in records}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
    if len(rows) != len(ids):
        raise EnergyApplyError("excluded guard jobs are missing")
    actual = {}
    for row in rows:
        job_id = row["id"]
        projection_sha = _sha_value(_projection(row))
        protected_sha = _sha_value(_protected_projection(row))
        if projection_sha != expected[job_id]["governed_projection_sha256"]:
            raise EnergyApplyError(f"excluded governed projection drift: {job_id}")
        if protected_sha != expected[job_id]["protected_fields_sha256"]:
            raise EnergyApplyError(f"excluded protected fields drift: {job_id}")
        actual[job_id] = {
            "governed_projection_sha256": projection_sha,
            "protected_fields_sha256": protected_sha,
        }
    return _sha_value(actual)


def _logical_rows(conn, table, *, exclude_columns=()):
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[1] not in exclude_columns]
    order = "id" if "id" in columns else columns[0]
    rows = [dict(row) for row in conn.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}")]
    return _sha_value(rows)


def _db_checks(conn):
    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    if quick != "ok" or integrity != "ok" or foreign:
        raise EnergyApplyError("SQLite integrity or foreign-key check failed")
    return {"quick_check": quick, "integrity_check": integrity, "foreign_key_violations": foreign}


def _write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".energy-receipt-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _event_rows(conn, code, auth_sha, plan_digest):
    rows = conn.execute(
        "SELECT job_id,attempt_no,detail_json FROM job_events WHERE code=? ORDER BY job_id,id",
        (code,),
    ).fetchall()
    matched = []
    for row in rows:
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if detail.get("authorization_sha256") == auth_sha and detail.get("plan_logical_digest") == plan_digest:
            matched.append((row, detail))
    return matched


def _expected_event_detail(record, authorization_sha, plan_digest, before_key, after_key):
    return {
        "authorization_sha256": authorization_sha,
        "executor_version": EXECUTOR_VERSION,
        "idempotency_key": record["idempotency_key"],
        "plan_logical_digest": plan_digest,
        "prior_projection_sha256": _sha_value(record[before_key]),
        "result_projection_sha256": _sha_value(record[after_key]),
    }


def _verify_exact_event_set(conn, code, authorization_sha, plan, binding, before_key, after_key):
    rows = conn.execute(
        "SELECT id,job_id,attempt_no,detail_json FROM job_events WHERE code=? ORDER BY id",
        (code,),
    ).fetchall()
    matched = []
    for row in rows:
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise EnergyApplyError("governed event detail JSON is invalid") from exc
        if detail.get("authorization_sha256") == authorization_sha:
            matched.append((row, detail))
    expected_by_job = {record["job_id"]: record for record in plan["records"]}
    if len(matched) != len(expected_by_job) or {row["job_id"] for row, _ in matched} != set(expected_by_job):
        raise EnergyApplyError("exact governed audit event set mismatch")
    if len({row["job_id"] for row, _ in matched}) != len(matched):
        raise EnergyApplyError("duplicate governed audit event job")
    for row, detail in matched:
        record = expected_by_job[row["job_id"]]
        if row["attempt_no"] != record["precondition"]["active_attempt_no"]:
            raise EnergyApplyError("governed audit event attempt mismatch")
        expected = _expected_event_detail(
            record, authorization_sha, binding.logical_digest, before_key, after_key
        )
        if detail != expected:
            raise EnergyApplyError("governed audit event detail mismatch")
    return matched


def _validate_transaction_receipt(value, *, action, authorization_sha, plan, binding,
                                  expected_mode="applied"):
    required = {
        "schema_version", "action", "mode", "authorization_sha256",
        "plan_artifact_id", "plan_logical_digest", "evidence_artifact_digest",
        "business_rows", "event_delta", "job_ids", "excluded_job_count",
        "pre_projection_sha256", "post_projection_sha256", "protected_jobs_sha256",
        "attempts_sha256", "database_checks", "production_write_count",
        "generated_at_utc", "receipt_content_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise EnergyApplyError("transaction receipt fields mismatch")
    if (
        value["schema_version"] != RECEIPT_SCHEMA_VERSION
        or value["action"] != action
        or value["mode"] != expected_mode
        or value["authorization_sha256"] != authorization_sha
        or value["plan_artifact_id"] != binding.artifact_id
        or value["plan_logical_digest"] != binding.logical_digest
        or value["evidence_artifact_digest"] != binding.evidence_digest
        or value["business_rows"] != binding.expected_records
        or value["event_delta"] != binding.expected_records
        or value["production_write_count"] != binding.expected_records * 2
        or value["excluded_job_count"] != binding.expected_excluded
        or value["job_ids"] != sorted(record["job_id"] for record in plan["records"])
        or value["database_checks"] != {
            "quick_check": "ok", "integrity_check": "ok", "foreign_key_violations": 0,
        }
    ):
        raise EnergyApplyError("transaction receipt lineage or counts mismatch")
    before_key, after_key = ("old", "new") if action == "apply" else ("new", "old")
    if (
        value["pre_projection_sha256"] != _sha_value({r["job_id"]: r[before_key] for r in plan["records"]})
        or value["post_projection_sha256"] != _sha_value({r["job_id"]: r[after_key] for r in plan["records"]})
    ):
        raise EnergyApplyError("transaction receipt projection hashes mismatch")
    content = dict(value)
    supplied = content.pop("receipt_content_sha256")
    if _sha_value(content) != supplied:
        raise EnergyApplyError("transaction receipt content digest mismatch")
    _parse_time(value["generated_at_utc"], "receipt generated_at")
    return value


def _pending_path(receipt_dir, authorization_sha):
    return Path(receipt_dir) / ("." + authorization_sha.removeprefix("sha256:") + ".committed-pending.json")


def _pending_value(receipt, phase):
    return {
        "schema_version": PENDING_RECEIPT_SCHEMA_VERSION,
        "phase": phase,
        "authorization_sha256": receipt["authorization_sha256"],
        "receipt_content_sha256": receipt["receipt_content_sha256"],
        "receipt": receipt,
    }


def _load_pending(path, authorization_sha, plan, binding, action):
    _require_regular_private(path)
    value = _load_json(path, "committed pending receipt")
    required = {"schema_version", "phase", "authorization_sha256", "receipt_content_sha256", "receipt"}
    if not isinstance(value, dict) or set(value) != required:
        raise EnergyApplyError("committed pending receipt fields mismatch")
    if (
        value["schema_version"] != PENDING_RECEIPT_SCHEMA_VERSION
        or value["phase"] not in {"prepared_for_commit", "committed_pending_receipt"}
        or value["authorization_sha256"] != authorization_sha
    ):
        raise EnergyApplyError("committed pending receipt lineage mismatch")
    receipt = _validate_transaction_receipt(
        value["receipt"], action=action, authorization_sha=authorization_sha,
        plan=plan, binding=binding,
    )
    if value["receipt_content_sha256"] != receipt["receipt_content_sha256"]:
        raise EnergyApplyError("committed pending receipt digest mismatch")
    return value, receipt


def _finalize_receipt(receipt_dir, receipt, pending_path, *, fail_before_final_replace=False):
    receipt_dir = Path(receipt_dir)
    final = receipt_dir / (receipt["receipt_content_sha256"].removeprefix("sha256:") + ".json")
    if fail_before_final_replace:
        raise EnergyApplyError("synthetic post-commit receipt finalization failure")
    if final.exists():
        _require_regular_private(final)
        if _load_json(final, "final transaction receipt") != receipt:
            raise EnergyApplyError("existing final receipt content mismatch")
    else:
        _write_private_json(final, receipt)
    if pending_path.exists():
        pending_path.unlink()
        _fsync_directory(receipt_dir)
    result = dict(receipt)
    result["receipt_path"] = str(final)
    return result


def _is_verified_committed_recovery(db_path, receipt_dir, authorization_sha, plan, binding, action):
    """Recognize an already-committed exact transaction before prestate checks.

    This narrow read-only probe is the only reason the live DB may differ from
    the authorization's prestate SHA.  Full pending/final receipt validation is
    repeated inside the main transaction before any recovery publication.
    """
    before_key, after_key = ("old", "new") if action == "apply" else ("new", "old")
    code = "task_energy_metadata_applied" if action == "apply" else "task_energy_metadata_rollback"
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for record in plan["records"]:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (record["job_id"],)).fetchone()
            if row is None or _projection(row) != record[after_key]:
                return False
        _verify_exact_event_set(
            conn, code, authorization_sha, plan, binding, before_key, after_key
        )
    except (sqlite3.Error, EnergyApplyError):
        return False
    finally:
        conn.close()
    pending = _pending_path(receipt_dir, authorization_sha)
    if pending.is_file() and not pending.is_symlink():
        return True
    for item in Path(receipt_dir).glob("*.json"):
        try:
            if _load_json(item, "recovery receipt probe").get("authorization_sha256") == authorization_sha:
                return True
        except EnergyApplyError:
            continue
    return False


def run_transaction(data_dir, plan_dir, evidence_dir, authorization_path, expected_authorization_sha256,
                    backup_receipt_path,
                    receipt_dir, *, action="apply", execute=False,
                    binding=PRODUCTION_BINDING, fail_after_updates=None,
                    runtime_manifest_path=PRODUCTION_RUNTIME_MANIFEST,
                    authorization_key_path=PRODUCTION_AUTHORIZATION_KEY,
                    fail_after_receipt_commit=False):
    """Inspect or atomically apply/rollback the exact completed-task plan."""
    if action not in {"apply", "rollback"}:
        raise EnergyApplyError("action must be apply or rollback")
    plan = load_plan(plan_dir, binding=binding)
    verify_evidence_artifact(evidence_dir, plan, binding=binding)
    authorization, authorization_sha = load_authorization(
        authorization_path, expected_authorization_sha256, action=action, binding=binding,
        runtime_manifest_path=runtime_manifest_path,
        authorization_key_path=authorization_key_path,
    )
    data_dir = Path(data_dir).resolve()
    db_path = data_dir / "jobs.sqlite3"
    if not db_path.is_file():
        raise EnergyApplyError("jobs SQLite is missing")
    receipt_dir = Path(receipt_dir)
    if receipt_dir.exists() and receipt_dir.is_symlink():
        raise EnergyApplyError("receipt directory symlink is forbidden")
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(receipt_dir, 0o700)
    committed_recovery = _is_verified_committed_recovery(
        db_path, receipt_dir, authorization_sha, plan, binding, action
    )
    _verify_backup_receipt(
        backup_receipt_path, authorization, db_path,
        require_distinct_device=binding.require_distinct_backup_device,
        verified_committed_recovery=committed_recovery,
    )
    code = "task_energy_metadata_applied" if action == "apply" else "task_energy_metadata_rollback"
    before_key, after_key = ("old", "new") if action == "apply" else ("new", "old")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    committed = False
    pending_file = None
    try:
        conn.execute("BEGIN IMMEDIATE" if execute else "BEGIN")
        status_counts = dict(conn.execute(
            "SELECT status,COUNT(*) FROM jobs WHERE status IN ('queued','running','cancelling') GROUP BY status"
        ).fetchall())
        if any(status_counts.values()):
            raise EnergyApplyError("queue four-state must be zero")
        before_protected = _logical_rows(conn, "jobs", exclude_columns=PROJECTION_COLUMNS)
        before_attempts = _logical_rows(conn, "job_attempts")
        before_artifacts = _active_artifact_hashes(data_dir, plan)
        excluded_hash = _verify_excluded(conn, plan)
        existing_events = _event_rows(conn, code, authorization_sha, binding.logical_digest)
        states = []
        for record in plan["records"]:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (record["job_id"],)).fetchone()
            if row is None:
                raise EnergyApplyError(f"allowlisted job is missing: {record['job_id']}")
            projection = _projection(row)
            states.append("before" if projection == record[before_key] else "after" if projection == record[after_key] else "drift")
        if len(set(states)) != 1 or states[0] == "drift":
            raise EnergyApplyError("mixed or drifted governed projection state")
        is_replay = states[0] == "after"
        if is_replay:
            _verify_exact_event_set(
                conn, code, authorization_sha, plan, binding, before_key, after_key
            )
        if not is_replay and existing_events:
            raise EnergyApplyError("prior audit events conflict with pre projection")
        if not is_replay and _sha_file(db_path) != authorization["database_prestate_sha256"].removeprefix("sha256:"):
            raise EnergyApplyError("database prestate SHA mismatch")
        if not is_replay:
            stale_pending = _pending_path(receipt_dir, authorization_sha)
            if stale_pending.exists():
                pending_value, _ = _load_pending(
                    stale_pending, authorization_sha, plan, binding, action
                )
                if pending_value["phase"] != "prepared_for_commit" or not execute:
                    raise EnergyApplyError("unexpected committed pending receipt before transaction")
                stale_pending.unlink()
                _fsync_directory(receipt_dir)
        if action == "rollback":
            apply_sha = authorization["apply_receipt_sha256"].removeprefix("sha256:")
            matching_receipts = [item for item in receipt_dir.glob("*.json") if _sha_file(item) == apply_sha]
            if len(matching_receipts) != 1:
                raise EnergyApplyError("exact apply receipt is missing or ambiguous")
            apply_receipt = _load_json(matching_receipts[0], "apply receipt")
            apply_receipt = _validate_transaction_receipt(
                apply_receipt, action="apply",
                authorization_sha=apply_receipt.get("authorization_sha256"),
                plan=plan, binding=binding,
            )
            if (
                apply_receipt["protected_jobs_sha256"] != before_protected
                or apply_receipt["attempts_sha256"] != before_attempts
            ):
                raise EnergyApplyError("rollback apply receipt protected-state mismatch")
            _verify_exact_event_set(
                conn, "task_energy_metadata_applied",
                apply_receipt["authorization_sha256"], plan, binding, "old", "new",
            )
        current = []
        for record in plan["records"]:
            row = _verify_live_record(data_dir, conn, record, record[after_key] if is_replay else record[before_key])
            current.append((record, row))
        if not is_replay:
            mode = "would_apply" if not execute else "applied"
            if execute:
                assignments = ",".join(f"{name}=?" for name in PROJECTION_COLUMNS)
                for index, (record, row) in enumerate(current, start=1):
                    values = []
                    for name in PROJECTION_COLUMNS:
                        value = record[after_key][name]
                        values.append(_canonical(value) if name in JSON_COLUMNS and value is not None else value)
                    cursor = conn.execute(
                        f"UPDATE jobs SET {assignments} WHERE id=?",
                        (*values, record["job_id"]),
                    )
                    if cursor.rowcount != 1:
                        raise EnergyApplyError("exact business-row update count mismatch")
                    detail = _expected_event_detail(
                        record, authorization_sha, binding.logical_digest, before_key, after_key
                    )
                    conn.execute(
                        "INSERT INTO job_events(job_id,attempt_no,created_at,level,code,message,detail_json) VALUES(?,?,?,?,?,?,?)",
                        (record["job_id"], row["active_attempt_no"], datetime.now(timezone.utc).isoformat(), "info", code,
                         "Governed task-energy projection applied" if action == "apply" else "Governed task-energy projection rolled back",
                         _canonical(detail)),
                    )
                    if fail_after_updates == index:
                        raise EnergyApplyError("synthetic transaction interruption")
        for record in plan["records"]:
            _verify_live_record(data_dir, conn, record, record[after_key] if (execute and not is_replay) or is_replay else record[before_key])
        after_protected = _logical_rows(conn, "jobs", exclude_columns=PROJECTION_COLUMNS)
        after_attempts = _logical_rows(conn, "job_attempts")
        after_artifacts = _active_artifact_hashes(data_dir, plan)
        if before_protected != after_protected or before_attempts != after_attempts or before_artifacts != after_artifacts:
            raise EnergyApplyError("protected task or attempt state drifted")
        if _verify_excluded(conn, plan) != excluded_hash:
            raise EnergyApplyError("excluded guard projection drifted")
        checks = _db_checks(conn)
        if execute:
            _verify_exact_event_set(
                conn, code, authorization_sha, plan, binding, before_key, after_key
            )
        if is_replay:
            pending = _pending_path(receipt_dir, authorization_sha)
            if pending.exists():
                _, stored_receipt = _load_pending(
                    pending, authorization_sha, plan, binding, action
                )
                if (
                    stored_receipt["protected_jobs_sha256"] != after_protected
                    or stored_receipt["attempts_sha256"] != after_attempts
                    or stored_receipt["database_checks"] != checks
                ):
                    raise EnergyApplyError("committed pending receipt state mismatch")
                if execute:
                    _write_private_json(pending, _pending_value(stored_receipt, "committed_pending_receipt"))
                    result = _finalize_receipt(receipt_dir, stored_receipt, pending)
                    result["mode"] = "idempotent_replay"
                    conn.rollback()
                    return result
            matching = []
            for item in receipt_dir.glob("*.json"):
                _require_regular_private(item)
                candidate = _load_json(item, "final transaction receipt")
                if candidate.get("authorization_sha256") == authorization_sha:
                    matching.append((item, candidate))
            if len(matching) != 1:
                raise EnergyApplyError("post projection has no unique physical transaction receipt")
            final_path, stored_receipt = matching[0]
            stored_receipt = _validate_transaction_receipt(
                stored_receipt, action=action, authorization_sha=authorization_sha,
                plan=plan, binding=binding,
            )
            if (
                stored_receipt["protected_jobs_sha256"] != after_protected
                or stored_receipt["attempts_sha256"] != after_attempts
                or stored_receipt["database_checks"] != checks
            ):
                raise EnergyApplyError("physical transaction receipt state mismatch")
            conn.rollback()
            result = dict(stored_receipt)
            result["mode"] = "idempotent_replay"
            result["receipt_path"] = str(final_path)
            return result
        event_delta = len(plan["records"]) if execute and not is_replay else 0
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "action": action,
            "mode": mode if execute else "dry_run",
            "authorization_sha256": authorization_sha,
            "plan_artifact_id": binding.artifact_id,
            "plan_logical_digest": binding.logical_digest,
            "evidence_artifact_digest": binding.evidence_digest,
            "business_rows": len(plan["records"]),
            "event_delta": event_delta,
            "job_ids": sorted(record["job_id"] for record in plan["records"]),
            "excluded_job_count": binding.expected_excluded,
            "pre_projection_sha256": _sha_value({r["job_id"]: r[before_key] for r in plan["records"]}),
            "post_projection_sha256": _sha_value({r["job_id"]: r[after_key] for r in plan["records"]}),
            "protected_jobs_sha256": after_protected,
            "attempts_sha256": after_attempts,
            "database_checks": checks,
            "production_write_count": event_delta + (len(plan["records"]) if event_delta else 0),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        content_sha = _sha_value(receipt)
        receipt["receipt_content_sha256"] = content_sha
        if execute:
            pending = _pending_path(receipt_dir, authorization_sha)
            pending_file = pending
            _write_private_json(pending, _pending_value(receipt, "prepared_for_commit"))
            conn.commit()
            committed = True
            _write_private_json(pending, _pending_value(receipt, "committed_pending_receipt"))
            return _finalize_receipt(
                receipt_dir, receipt, pending,
                fail_before_final_replace=fail_after_receipt_commit,
            )
        else:
            conn.rollback()
        return receipt
    except Exception:
        if not committed:
            conn.rollback()
            if pending_file is not None and pending_file.exists():
                pending_file.unlink()
                _fsync_directory(receipt_dir)
        raise
    finally:
        conn.close()
