"""Governed, atomic import of the approved Wotu voltage-platform audit.

The public CLI pins ``PRODUCTION_AUDIT_BINDING``.  Tests may inject a synthetic
binding into :func:`run_import`, but callers cannot replace the production
binding from command-line input.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .product_identity import (
    PRODUCT_OBSERVATION_SCHEMA_VERSION,
    VOLTAGE_PLATFORM_SCHEMA_VERSION,
    normalize_brand_token,
    normalize_voltage,
    observation_hash,
)


AUDIT_SCHEMA_VERSION = "voxquarry.voltage-platform-readonly-audit/v1"
AUDIT_ARTIFACT_KIND = "voxquarry.wotu-voltage-platform-readonly-audit/v1"
MANIFEST_SCHEMA_VERSION = "voxquarry.operations-handoff-manifest/v1"
RECEIPT_SCHEMA_VERSION = "voxquarry.voltage-platform-audit-import-receipt/v1"
AUDIT_RULE_VERSION = "brand-voltage-production-audit/1.0.0"

AUDIT_FILES = {
    "artifact-manifest.json",
    "audit-summary.json",
    "voltage-platform-audit.json",
    "SHA256SUMS",
}
MANIFEST_KEYS = {
    "allowed_use", "approval_id", "artifact_id", "artifact_kind", "as_of_utc",
    "content_roots", "expires_at_utc", "git_allowed", "immutable",
    "package_payload_bytes", "payload_files", "produced_at_utc",
    "producer_version", "prohibited_use", "reason_counts", "redaction_exclusion",
    "refresh_rule", "retention_policy", "row_counts", "schema_version",
    "source_environment", "source_lineage", "source_query_ids",
}
AUDIT_KEYS = {
    "as_of_utc", "finished_at_utc", "metrics", "production_write_count",
    "read_only", "records", "records_hash", "rule_version", "rules",
    "schema_version", "source_environment", "source_lineage", "started_at_utc",
}
SUMMARY_KEYS = {
    "artifact_id", "as_of_utc", "metrics", "production_write_count",
    "quality_checks", "read_only", "records_hash", "rule_version", "rules",
    "schema_version", "source_environment", "source_lineage",
}
RECORD_KEYS = {
    "active_result_status", "brand", "category", "channel", "classification",
    "evidence_source", "job_id", "model", "normalized_brand",
    "normalized_voltage", "physical_validation", "platform_key", "product_master",
    "reason", "source_record_hash", "status", "target_identity_sha256",
}
PHYSICAL_KEYS = {
    "brand_conflict", "brand_present", "model_conflict", "model_present",
    "negative_energy_context", "positive_dc_context", "relative_path", "sha256",
    "title_sha256", "voltage_matches",
}
PRODUCT_MASTER_KEYS = {
    "canonical_voltage", "energy_type", "match_count", "model_id",
    "record_snapshot_hash", "voltage_platform_null_reason",
}
VOLTAGE_MATCH_KEYS = {"canonical_voltage", "end", "matched_text", "start"}

MIGRATION_COLUMNS = {
    "product_observation_json": "TEXT",
    "product_observation_hash": "TEXT",
    "product_enrichment_status": "TEXT",
    "product_quality_flags_json": "TEXT",
    "voltage_platform_schema_version": "TEXT",
    "voltage_platform_status": "TEXT",
    "voltage_platform_key": "TEXT",
    "voltage_platform_brand": "TEXT",
    "voltage_platform_voltage": "TEXT",
    "voltage_platform_label": "TEXT",
    "voltage_platform_evidence_source": "TEXT",
    "voltage_platform_null_reason": "TEXT",
}
BASE_JOB_COLUMNS = {
    "id", "channel", "status", "active_result_status", "brand", "model",
    "category", "target_key", "target_url",
}


@dataclass(frozen=True)
class AuditBinding:
    artifact_id: str
    manifest_sha256: str
    data_sha256: str
    summary_sha256: str
    sums_sha256: str
    records_hash: str
    wotu_database_sha256: str
    product_master_snapshot_id: str
    product_master_body_sha256: str
    expected_known: int
    expected_unknown: int
    expected_non_dc: int
    expected_jobs: int
    expected_attempts: int
    expected_events: int
    expected_active_jobs: int
    expected_status_counts: tuple[tuple[str, int], ...]


PRODUCTION_AUDIT_BINDING = AuditBinding(
    artifact_id=(
        "wotu-voltage-platform-audit-1039-"
        "06d155f069e4e08cd07b326103f95f199d39e749db127e220c72fa7ad2085390"
    ),
    manifest_sha256="d2d73e2b0fda1daff99feac6d2a766b01504013bbeb3200fb49993b9d5f7044c",
    data_sha256="06d155f069e4e08cd07b326103f95f199d39e749db127e220c72fa7ad2085390",
    summary_sha256="dbf5ab1dd29bcf1f9a8045561021275265144104fec2c1d46a9128278737fd34",
    sums_sha256="bed113b9e5a700c6328d338c53488fcc78f1a039ab9b705b7d036409ff703f7d",
    records_hash="sha256:860ec1958c22fbad4f7433bce1253adfc152d0414ed4ab5a61e02997aeed5831",
    wotu_database_sha256="731bcc818400d38f7c27fc33456b1c3d8a4fbf4e4d0a2669911b84ac9426fdea",
    product_master_snapshot_id="pms_a6d19ecf372809195968bd796d5d6847cab80caf9178c8bf2b2d1986c74abc90",
    product_master_body_sha256="4b123bf0d905a0595c3239b596b1919a097393f475d86554067a3bc77d3b09e6",
    expected_known=296,
    expected_unknown=205,
    expected_non_dc=538,
    expected_jobs=1039,
    expected_attempts=1261,
    expected_events=2526,
    expected_active_jobs=0,
    expected_status_counts=(("completed", 948), ("failed", 73), ("partial_success", 18)),
)


class AuditImportError(RuntimeError):
    """Fail-closed audit validation or import conflict."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(path: Path):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AuditImportError(f"duplicate JSON key in {path.name}: {key}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditImportError(f"unreadable audit JSON: {path.name}") from exc


def _expect_keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise AuditImportError(f"{label} fields do not match contract: {actual}")


def _expect_sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise AuditImportError(f"{label} must be a lowercase sha256 value")


def _audit_brand(value):
    import unicodedata

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = "".join(
        char for char in normalized if unicodedata.category(char) != "Cf"
    )
    return " ".join(normalized.strip().casefold().split())


def _target_hash(row):
    raw = row["target_key"] or row["target_url"] or ""
    return "sha256:" + _sha256_bytes(str(raw).encode("utf-8"))


def _source_hash(row):
    record = {
        "job_id": row["id"],
        "channel": row["channel"],
        "status": row["status"],
        "active_result_status": row["active_result_status"],
        "brand": row["brand"] or "",
        "model": (row["model"] or None),
        "category": row["category"] or "",
        "target_identity_sha256": _target_hash(row),
    }
    return "sha256:" + _sha256_bytes(_canonical_json(record).encode("utf-8"))


def _verify_sums(artifact_dir: Path, binding: AuditBinding):
    actual_files = {item.name for item in artifact_dir.iterdir()}
    if actual_files != AUDIT_FILES:
        raise AuditImportError(f"audit package files do not match contract: {sorted(actual_files)}")
    sums_path = artifact_dir / "SHA256SUMS"
    if _sha256_file(sums_path) != binding.sums_sha256:
        raise AuditImportError("SHA256SUMS is not the approved immutable file")
    expected = {
        "artifact-manifest.json": binding.manifest_sha256,
        "audit-summary.json": binding.summary_sha256,
        "voltage-platform-audit.json": binding.data_sha256,
    }
    lines = sums_path.read_text(encoding="utf-8").splitlines()
    parsed = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        if not match or match.group(2) in parsed:
            raise AuditImportError("SHA256SUMS has invalid or duplicate entries")
        parsed[match.group(2)] = match.group(1)
    if parsed != expected:
        raise AuditImportError("SHA256SUMS entries do not match the approved binding")
    for name, digest in expected.items():
        if _sha256_file(artifact_dir / name) != digest:
            raise AuditImportError(f"audit payload digest mismatch: {name}")


def verify_artifact(artifact_dir: Path, binding: AuditBinding = PRODUCTION_AUDIT_BINDING):
    artifact_dir = Path(artifact_dir)
    if not artifact_dir.is_dir():
        raise AuditImportError("audit artifact directory does not exist")
    _verify_sums(artifact_dir, binding)
    manifest = _load_json(artifact_dir / "artifact-manifest.json")
    summary = _load_json(artifact_dir / "audit-summary.json")
    audit = _load_json(artifact_dir / "voltage-platform-audit.json")
    _expect_keys(manifest, MANIFEST_KEYS, "artifact manifest")
    _expect_keys(summary, SUMMARY_KEYS, "audit summary")
    _expect_keys(audit, AUDIT_KEYS, "audit payload")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise AuditImportError("manifest schema_version is not governed")
    if manifest.get("artifact_kind") != AUDIT_ARTIFACT_KIND:
        raise AuditImportError("manifest artifact_kind is not governed")
    if manifest.get("artifact_id") != binding.artifact_id:
        raise AuditImportError("artifact_id is not the approved audit")
    if manifest.get("immutable") is not True or manifest.get("git_allowed") is not False:
        raise AuditImportError("audit immutability policy is invalid")
    if set((manifest.get("payload_files") or {})) != {
        "audit-summary.json", "voltage-platform-audit.json",
    }:
        raise AuditImportError("manifest payload file set is invalid")
    for name, digest in {
        "audit-summary.json": binding.summary_sha256,
        "voltage-platform-audit.json": binding.data_sha256,
    }.items():
        descriptor = manifest["payload_files"].get(name)
        if not isinstance(descriptor, dict) or set(descriptor) != {"bytes", "sha256"}:
            raise AuditImportError(f"manifest descriptor is invalid: {name}")
        if descriptor.get("sha256") != digest:
            raise AuditImportError(f"manifest descriptor digest mismatch: {name}")
    lineage = manifest.get("source_lineage") or {}
    if lineage != {
        "product_master_body_sha256": binding.product_master_body_sha256,
        "product_master_snapshot_id": binding.product_master_snapshot_id,
        "wotu_database_sha256": binding.wotu_database_sha256,
    }:
        raise AuditImportError("manifest source lineage does not match approved baseline")
    expected_counts = {
        "tasks": binding.expected_jobs,
        "active_result_tasks": binding.expected_jobs - dict(binding.expected_status_counts).get("failed", 0),
        "known_platform_tasks": binding.expected_known,
        "unknown_tasks": binding.expected_unknown,
        "excluded_non_platform_tasks": binding.expected_non_dc,
    }
    if manifest.get("row_counts") != expected_counts:
        raise AuditImportError("manifest row counts do not match approved binding")
    for document, label in ((summary, "summary"), (audit, "audit")):
        if document.get("schema_version") != AUDIT_SCHEMA_VERSION:
            raise AuditImportError(f"{label} schema_version is not governed")
        if document.get("rule_version") != AUDIT_RULE_VERSION:
            raise AuditImportError(f"{label} rule_version is not governed")
        if document.get("read_only") is not True or document.get("production_write_count") != 0:
            raise AuditImportError(f"{label} is not a read-only source audit")
        if document.get("records_hash") != binding.records_hash:
            raise AuditImportError(f"{label} records hash is not approved")
    if summary.get("artifact_id") != binding.artifact_id:
        raise AuditImportError("summary artifact_id does not match manifest")
    records = audit.get("records")
    if not isinstance(records, list) or len(records) != binding.expected_jobs:
        raise AuditImportError("audit record count does not match approved binding")
    computed_records_hash = "sha256:" + _sha256_bytes(
        _canonical_json(records).encode("utf-8")
    )
    if computed_records_hash != binding.records_hash:
        raise AuditImportError("audit records canonical hash mismatch")
    ids = []
    classifications = {"known": 0, "unknown": 0, "excluded": 0}
    platform_brands = {}
    for index, record in enumerate(records):
        _expect_keys(record, RECORD_KEYS, f"record[{index}]")
        _expect_keys(record.get("physical_validation"), PHYSICAL_KEYS, f"record[{index}].physical_validation")
        _expect_keys(record.get("product_master"), PRODUCT_MASTER_KEYS, f"record[{index}].product_master")
        for match_index, match in enumerate(record["physical_validation"].get("voltage_matches") or []):
            _expect_keys(match, VOLTAGE_MATCH_KEYS, f"record[{index}].voltage_matches[{match_index}]")
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise AuditImportError(f"record[{index}] job_id is invalid")
        ids.append(job_id)
        _expect_sha(record.get("target_identity_sha256"), f"record[{index}].target_identity_sha256")
        _expect_sha(record.get("source_record_hash"), f"record[{index}].source_record_hash")
        classification = record.get("classification")
        if classification not in classifications:
            raise AuditImportError(f"record[{index}] classification is invalid")
        classifications[classification] += 1
        brand = record.get("brand")
        normalized_brand = record.get("normalized_brand")
        if not isinstance(brand, str) or not brand or normalized_brand != _audit_brand(brand):
            raise AuditImportError(f"record[{index}] normalized brand is invalid")
        voltage = record.get("normalized_voltage")
        platform_key = record.get("platform_key")
        if classification == "known":
            if normalize_voltage(voltage) != voltage:
                raise AuditImportError(f"record[{index}] canonical voltage is invalid")
            if platform_key != f"{normalized_brand}|{voltage.casefold()}":
                raise AuditImportError(f"record[{index}] audit platform key is invalid")
            if record.get("reason") not in {
                "known_governed_product_master", "known_physical_title_dc_voltage",
            } or record.get("evidence_source") not in {
                "retail-product-master/v2", "active_validation_product_title",
            }:
                raise AuditImportError(f"record[{index}] known evidence is invalid")
            projected_key = f"{normalize_brand_token(brand)}|{voltage}"
            prior_brand = platform_brands.setdefault(projected_key, brand.casefold())
            if prior_brand != brand.casefold():
                raise AuditImportError("cross-brand platform key collision")
        elif classification == "unknown":
            if voltage is not None or platform_key is not None or record.get("evidence_source") is not None:
                raise AuditImportError(f"record[{index}] unknown classification leaks a platform")
            if not str(record.get("reason") or "").startswith("unknown_"):
                raise AuditImportError(f"record[{index}] unknown reason is invalid")
        else:
            if voltage is not None or platform_key is not None or record.get("evidence_source") is not None:
                raise AuditImportError(f"record[{index}] non-DC classification leaks a platform")
            if record.get("reason") != "excluded_non_dc_product":
                raise AuditImportError(f"record[{index}] non-DC reason is invalid")
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        raise AuditImportError("audit jobs must be unique and BINARY sorted")
    if classifications != {
        "known": binding.expected_known,
        "unknown": binding.expected_unknown,
        "excluded": binding.expected_non_dc,
    }:
        raise AuditImportError("audit classification counts do not match approved binding")
    metrics = audit.get("metrics") or {}
    if any(metrics.get(key) != expected for key, expected in {
        "total_tasks": binding.expected_jobs,
        "known_platform_tasks": binding.expected_known,
        "unknown_tasks": binding.expected_unknown,
        "excluded_non_platform_tasks": binding.expected_non_dc,
    }.items()):
        raise AuditImportError("audit metrics do not reconcile to records")
    return {"manifest": manifest, "summary": summary, "audit": audit}


def _db_table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _db_facts(conn):
    facts = {
        "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "attempts": conn.execute("SELECT COUNT(*) FROM job_attempts").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0],
        "active_jobs": conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('running','cancelling')"
        ).fetchone()[0],
        "status": dict(conn.execute(
            "SELECT status,COUNT(*) FROM jobs GROUP BY status ORDER BY status"
        ).fetchall()),
        "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_check_count": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
    }
    return facts


def _validate_db_facts(facts, binding):
    expected = {
        "jobs": binding.expected_jobs,
        "attempts": binding.expected_attempts,
        "events": binding.expected_events,
        "active_jobs": binding.expected_active_jobs,
        "status": dict(binding.expected_status_counts),
        "integrity_check": "ok",
        "foreign_key_check_count": 0,
    }
    if facts != expected:
        raise AuditImportError(f"database baseline facts drifted: {facts}")


def _build_projection(record):
    classification = record["classification"]
    audit_evidence = f"{AUDIT_SCHEMA_VERSION}:{record['source_record_hash']}"
    energy_type = record["product_master"].get("energy_type")
    if classification == "known" and not energy_type:
        energy_type = "DC"
    observation = {
        "schema_version": PRODUCT_OBSERVATION_SCHEMA_VERSION,
        "brand": record["brand"] or None,
        "model": record["model"] or None,
        "category": record["category"] or None,
        "energy_type": energy_type or None,
        "voltage_raw": record["normalized_voltage"] if classification == "known" else None,
        "normalized_voltage": record["normalized_voltage"] if classification == "known" else None,
        "evidence_source": audit_evidence,
    }
    brand_token = normalize_brand_token(record["brand"])
    if classification == "known":
        status = "known"
        voltage = record["normalized_voltage"]
        key = f"{brand_token}|{voltage}"
        label = f"{record['brand']} {voltage}"
        null_reason = None
    elif classification == "excluded":
        status = "non_dc"
        voltage = key = None
        label = "非 DC"
        null_reason = "not_dc"
    else:
        status = "unknown"
        voltage = key = label = None
        null_reason = "brand_unknown" if not brand_token else "voltage_unknown"
    quality_flags = sorted({
        f"voltage_platform_audit_reason:{record['reason']}",
        f"voltage_platform_audit_source:{record['source_record_hash']}",
    })
    return {
        "product_observation_json": json.dumps(observation, ensure_ascii=False, separators=(",", ":")),
        "product_observation_hash": observation_hash(observation),
        "product_enrichment_status": "unknown",
        "product_quality_flags_json": json.dumps(quality_flags, ensure_ascii=False, separators=(",", ":")),
        "voltage_platform_schema_version": VOLTAGE_PLATFORM_SCHEMA_VERSION,
        "voltage_platform_status": status,
        "voltage_platform_key": key,
        "voltage_platform_brand": record["brand"],
        "voltage_platform_voltage": voltage,
        "voltage_platform_label": label,
        "voltage_platform_evidence_source": audit_evidence,
        "voltage_platform_null_reason": null_reason,
    }


def _validate_row_types(row):
    for key in BASE_JOB_COLUMNS:
        value = row[key]
        if value is not None and not isinstance(value, str):
            raise AuditImportError(f"job {row['id']} field {key} is not TEXT/NULL")


def _read_and_plan(conn, records, binding, *, require_migration_columns=False):
    columns = _db_table_columns(conn, "jobs")
    if not BASE_JOB_COLUMNS.issubset(columns):
        raise AuditImportError("jobs table is missing baseline identity columns")
    if require_migration_columns and not set(MIGRATION_COLUMNS).issubset(columns):
        raise AuditImportError("jobs table is missing voltage-platform migration columns")
    facts = _db_facts(conn)
    _validate_db_facts(facts, binding)
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY id COLLATE BINARY"
    ).fetchall()
    record_by_id = {record["job_id"]: record for record in records}
    if [row["id"] for row in rows] != list(record_by_id):
        raise AuditImportError("database stable job IDs do not match the approved audit")
    plans = []
    for row in rows:
        _validate_row_types(row)
        record = record_by_id[row["id"]]
        if _target_hash(row) != record["target_identity_sha256"]:
            raise AuditImportError(f"job {row['id']} target identity drifted")
        if _source_hash(row) != record["source_record_hash"]:
            raise AuditImportError(f"job {row['id']} source identity/status drifted")
        projection = _build_projection(record)
        if set(MIGRATION_COLUMNS).issubset(columns):
            existing = {key: row[key] for key in MIGRATION_COLUMNS}
            all_null = all(value is None for value in existing.values())
            all_equal = existing == projection
            if not all_null and not all_equal:
                raise AuditImportError(f"job {row['id']} has conflicting governed platform fields")
            action = "unchanged" if all_equal else "update"
        else:
            action = "update"
        plans.append((row["id"], projection, action))
    return facts, plans


def _connect_readonly(database):
    conn = sqlite3.connect(f"{Path(database).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _backup_database(database: Path, backup_dir: Path):
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"jobs-pre-voltage-platform-audit-{timestamp}.sqlite3"
    if target.exists():
        raise AuditImportError(f"refusing to overwrite backup: {target}")
    source = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    with sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AuditImportError("SQLite backup failed integrity_check")
    return target, _sha256_file(target)


def _receipt_hash(receipt):
    content = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    return "sha256:" + _sha256_bytes(_canonical_json(content).encode("utf-8"))


def _write_receipt(receipt_dir, receipt):
    receipt_dir = Path(receipt_dir)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt["receipt_hash"] = _receipt_hash(receipt)
    filename = f"{receipt['run_id']}-{receipt['phase']}.json"
    target = receipt_dir / filename
    if target.exists():
        raise AuditImportError(f"refusing to overwrite receipt: {target}")
    target.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _receipt_base(binding, run_id, phase, facts, plans, authorization_id=None):
    counts = {"known": 0, "unknown": 0, "non_dc": 0}
    for _job_id, projection, _action in plans:
        counts[projection["voltage_platform_status"]] += 1
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "run_id": run_id,
        "phase": phase,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifact_id": binding.artifact_id,
        "artifact_hashes": {
            "manifest": binding.manifest_sha256,
            "data": binding.data_sha256,
            "summary": binding.summary_sha256,
            "SHA256SUMS": binding.sums_sha256,
        },
        "source_database_baseline_sha256": binding.wotu_database_sha256,
        "authorization_id": authorization_id,
        "database_facts": facts,
        "classification_counts": counts,
        "planned_rows": len(plans),
        "rows_to_update": sum(action == "update" for _, _, action in plans),
        "rows_unchanged": sum(action == "unchanged" for _, _, action in plans),
        "receipt_hash": None,
    }


def run_import(
    artifact_dir,
    database,
    receipt_dir,
    *,
    apply=False,
    authorization_id=None,
    backup_dir=None,
    binding=PRODUCTION_AUDIT_BINDING,
):
    """Verify, dry-run, or atomically apply the governed audit projection."""
    verified = verify_artifact(Path(artifact_dir), binding)
    database = Path(database)
    if not database.is_file():
        raise AuditImportError("existing jobs SQLite is required; empty databases are refused")
    with _connect_readonly(database) as conn:
        conn.execute("BEGIN")
        facts, plans = _read_and_plan(conn, verified["audit"]["records"], binding)
        conn.rollback()
    run_id = datetime.now(timezone.utc).strftime("vpa-%Y%m%dT%H%M%S%fZ")
    if not apply:
        receipt = _receipt_base(binding, run_id, "dry-run", facts, plans)
        receipt_path = _write_receipt(receipt_dir, receipt)
        return {**receipt, "receipt_path": str(receipt_path), "applied": False}
    if not isinstance(authorization_id, str) or not re.fullmatch(
        r"product-planning-voltage-platform-backfill-[A-Za-z0-9._:-]+",
        authorization_id,
    ):
        raise AuditImportError("explicit product-planning backfill authorization_id is required")
    if backup_dir is None:
        raise AuditImportError("--backup-dir is required for apply")
    backup_path, backup_sha256 = _backup_database(database, Path(backup_dir))
    pre = _receipt_base(binding, run_id, "pre-apply", facts, plans, authorization_id)
    pre["backup"] = {"filename": backup_path.name, "sha256": backup_sha256}
    pre_path = _write_receipt(receipt_dir, pre)
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked_facts, locked_plans = _read_and_plan(
            conn, verified["audit"]["records"], binding
        )
        for column, declaration in MIGRATION_COLUMNS.items():
            if column not in _db_table_columns(conn, "jobs"):
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
        assignments = ",".join(f"{column}=?" for column in MIGRATION_COLUMNS)
        changed = 0
        for job_id, projection, action in locked_plans:
            if action == "unchanged":
                continue
            cursor = conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                tuple(projection[column] for column in MIGRATION_COLUMNS) + (job_id,),
            )
            if cursor.rowcount != 1:
                raise AuditImportError(f"job {job_id} update was not singular")
            changed += 1
        post_locked_facts, post_plans = _read_and_plan(
            conn, verified["audit"]["records"], binding,
            require_migration_columns=True,
        )
        if any(action != "unchanged" for _, _, action in post_plans):
            raise AuditImportError("post-apply projection did not converge")
        if locked_facts != post_locked_facts:
            raise AuditImportError("apply changed protected job/attempt/event facts")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    with _connect_readonly(database) as check:
        check.execute("BEGIN")
        post_facts, post_plans = _read_and_plan(
            check, verified["audit"]["records"], binding,
            require_migration_columns=True,
        )
        check.rollback()
    post = _receipt_base(binding, run_id, "post-apply", post_facts, post_plans, authorization_id)
    post["backup"] = {"filename": backup_path.name, "sha256": backup_sha256}
    post["rows_changed"] = changed
    post["pre_receipt_hash"] = pre["receipt_hash"]
    post_path = _write_receipt(receipt_dir, post)
    return {
        **post,
        "applied": True,
        "pre_receipt_path": str(pre_path),
        "receipt_path": str(post_path),
        "backup_path": str(backup_path),
    }
