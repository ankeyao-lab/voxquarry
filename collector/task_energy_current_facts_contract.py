"""Canonical hashing contract for PW task-energy current-facts/v4.

This module is shared by the read-only Operations producer and synthetic
cross-component tests.  It deliberately does not import Store or open SQLite.
"""

from __future__ import annotations

import hashlib
import json


PROTECTED_PRECONDITION_SCHEMA_VERSION = (
    "voxquarry.completed-task-energy-excluded-protected-fields/v2"
)
PROTECTED_JSON_MODE = "sqlite-raw-text/no-trim-no-parse-no-reserialize/v1"

PROJECTION_FIELDS = (
    "energy_schema_version", "energy_type", "energy_readiness_status",
    "energy_evidence_source", "energy_canonical_voltage", "energy_unknown_reason",
    "product_observation_json", "product_observation_hash",
    "voltage_platform_schema_version", "voltage_platform_status",
    "voltage_platform_key", "voltage_platform_brand", "voltage_platform_voltage",
    "voltage_platform_label", "voltage_platform_evidence_source",
    "voltage_platform_null_reason",
)
EXECUTOR_PRECONDITION_FIELDS = (
    "active_attempt_no", "active_result_status", "active_run_path", "attempt_count",
    "brand", "category", "channel", "id", "last_attempt_status", "model",
    "source_product_id", "source_product_identity_status", "status", "target_key",
)
SOURCE_FACT_FIELDS = ("target_url", "market_region")
PROTECTED_FIELDS = tuple(sorted(set(
    EXECUTOR_PRECONDITION_FIELDS + SOURCE_FACT_FIELDS + PROJECTION_FIELDS
)))


class CurrentFactsContractError(ValueError):
    pass


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha_value(value):
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def governed_projection(row):
    """Return the 16-column semantic projection.

    product_observation_json is parsed exactly once into an object.  No other
    field is normalized.
    """
    value = {name: row[name] for name in PROJECTION_FIELDS}
    raw = value["product_observation_json"]
    if raw is not None:
        if not isinstance(raw, str):
            raise CurrentFactsContractError("product_observation_json must be SQLite TEXT or NULL")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CurrentFactsContractError("product_observation_json is invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise CurrentFactsContractError("product_observation_json is not a JSON object")
        value["product_observation_json"] = parsed
    return value


def protected_precondition(row):
    """Return the protected object with SQLite TEXT preserved byte-for-byte.

    The JSON field is not trimmed, parsed, normalized, or reserialized.  The
    canonical wrapper sorts object keys, but the raw TEXT remains a JSON string
    value, so whitespace/key-order byte changes produce a different digest.
    """
    value = {name: row[name] for name in PROTECTED_FIELDS}
    raw = value["product_observation_json"]
    if raw is not None and not isinstance(raw, str):
        raise CurrentFactsContractError("product_observation_json must be SQLite TEXT or NULL")
    return value


def record_hashes(row):
    projection = governed_projection(row)
    protected = protected_precondition(row)
    return {
        "governed_projection": projection,
        "projection_sha256": sha_value(projection),
        "protected_precondition_schema_version": PROTECTED_PRECONDITION_SCHEMA_VERSION,
        "protected_precondition_sha256": sha_value(protected),
    }
