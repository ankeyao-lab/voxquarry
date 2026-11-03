#!/usr/bin/env python3
"""Convert a read-only operations catalog artifact into the governed import envelope.

Both input and output stay outside Git. The conversion validates the operations
artifact, retains every per-category non-identity field as hashed source
metadata, and never invents aliases or semantic category mappings.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retail_landscape.category_registry import (
    CATEGORY_CATALOG_SCHEMA_VERSION,
    CATEGORY_CATALOG_SOURCE_TYPE,
    finalize_category_manifest,
    normalize_category_name,
    validate_category_manifest,
)
from retail_landscape.store import content_hash
from scripts.initialize_retail_product_master_store import file_sha256


OPERATIONS_SCHEMA_VERSION = "voxquarry.production-wotu-category-catalog/v1"


def prepare_category_catalog_manifest(
    source: dict[str, Any],
    *,
    source_file_sha256: str,
    approved_by: str,
    approved_at: str,
) -> dict[str, Any]:
    """Return a content-addressed Chengmu import manifest from one audited artifact."""
    allowed_top_keys = {
        "approval_id", "artifact_id", "as_of_utc", "candidate_sha",
        "mapping_policy", "records", "schema_version", "source_database_sha256",
        "source_environment", "source_query_version", "summary",
    }
    unknown_top_keys = sorted(set(source) - allowed_top_keys)
    if unknown_top_keys:
        raise ValueError(
            "operations category catalog contains unknown fields: "
            + ", ".join(unknown_top_keys)
        )
    if source.get("schema_version") != OPERATIONS_SCHEMA_VERSION:
        raise ValueError("unsupported operations category catalog schema_version")
    if source.get("source_environment") != "production":
        raise ValueError("operations category catalog source_environment must be production")
    source_database_sha256 = str(source.get("source_database_sha256") or "")
    if len(source_database_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in source_database_sha256
    ):
        raise ValueError("operations source_database_sha256 must be 64 lowercase hex")
    artifact_id = str(source.get("artifact_id") or "").strip()
    observed_at = str(source.get("as_of_utc") or "").strip()
    if not artifact_id or not observed_at:
        raise ValueError("operations artifact_id/as_of_utc are required")
    if len(source_file_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in source_file_sha256
    ):
        raise ValueError("source_file_sha256 must be 64 lowercase hex characters")
    records = source.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("operations records must be a non-empty array")
    summary = source.get("summary")
    if not isinstance(summary, dict) or summary.get("rows") != len(records):
        raise ValueError("operations summary.rows must equal records length")
    unknown_summary_keys = sorted(set(summary) - {
        "rows", "candidate_exact_supported", "candidate_catalog_gap",
        "normalized_name_conflicts", "empty_names",
    })
    if unknown_summary_keys:
        raise ValueError(
            "operations summary contains unknown fields: " + ", ".join(unknown_summary_keys)
        )

    categories: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    source_names: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"operations records[{index}] must be an object")
        unknown_record_keys = sorted(set(record) - {
            "source_record", "aliases", "alias_source",
            "candidate_8d459_exact_binding_support", "candidate_catalog_evidence",
            "coverage", "issues",
        })
        if unknown_record_keys:
            raise ValueError(
                f"operations records[{index}] contains unknown fields: "
                + ", ".join(unknown_record_keys)
            )
        source_record = record.get("source_record")
        if not isinstance(source_record, dict):
            raise ValueError(f"operations records[{index}].source_record must be an object")
        unknown_source_keys = sorted(set(source_record) - {"id", "name", "created_at", "updated_at"})
        if unknown_source_keys:
            raise ValueError(
                f"operations records[{index}].source_record contains unknown fields: "
                + ", ".join(unknown_source_keys)
            )
        source_id = str(source_record.get("id") or "").strip()
        source_name = str(source_record.get("name") or "").strip()
        created_at = str(source_record.get("created_at") or "").strip()
        updated_at = str(source_record.get("updated_at") or "").strip()
        aliases = record.get("aliases")
        if not source_id or not source_name or not created_at or not updated_at:
            raise ValueError(
                f"operations records[{index}] source id/name/created_at/updated_at required"
            )
        if source_id in source_ids:
            raise ValueError(f"duplicate operations source category id: {source_id}")
        name_key = normalize_category_name(source_name)
        if name_key in source_names:
            raise ValueError(f"duplicate operations normalized category name: {source_name}")
        if not isinstance(aliases, list):
            raise ValueError(f"operations records[{index}].aliases must be an array")
        if any(not str(alias or "").strip() for alias in aliases):
            raise ValueError(f"operations records[{index}] contains an empty alias")
        source_ids.add(source_id)
        source_names.add(name_key)
        coverage = record.get("coverage")
        if not isinstance(coverage, dict):
            raise ValueError(f"operations records[{index}].coverage must be an object")
        unknown_coverage_keys = sorted(set(coverage) - {
            "jobs_total", "stable_unsuperseded", "active_result_completed",
            "active_result_partial_success", "active_result_none",
        })
        if unknown_coverage_keys:
            raise ValueError(
                f"operations records[{index}].coverage contains unknown fields: "
                + ", ".join(unknown_coverage_keys)
            )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
               for value in coverage.values()):
            raise ValueError(f"operations records[{index}].coverage values must be nonnegative integers")
        issues = record.get("issues")
        if not isinstance(issues, list) or any(not isinstance(value, str) for value in issues):
            raise ValueError(f"operations records[{index}].issues must be a string array")
        if not isinstance(record.get("alias_source"), str):
            raise ValueError(f"operations records[{index}].alias_source must be a string")
        category = {
            "source_category_id": source_id,
            "source_name": source_name,
            "source_created_at": created_at,
            "source_updated_at": updated_at,
            "aliases": [str(alias).strip() for alias in aliases],
            # Retain all non-identity facts from the approved source row. They
            # are audit evidence only and never affect matching or stable IDs.
            "source_metadata": {
                "operations_source_record": source_record,
                **{
                    key: value for key, value in record.items() if key != "source_record"
                },
            },
        }
        exact_binding = record.get("candidate_8d459_exact_binding_support")
        evidence = record.get("candidate_catalog_evidence")
        if exact_binding is True:
            if not isinstance(evidence, dict):
                raise ValueError(
                    f"operations records[{index}] exact binding lacks catalog evidence"
                )
            unknown_evidence_keys = sorted(set(evidence) - {"category_id", "code", "name"})
            if unknown_evidence_keys:
                raise ValueError(
                    f"operations records[{index}] exact binding evidence contains unknown fields: "
                    + ", ".join(unknown_evidence_keys)
                )
            canonical_code = str(evidence.get("code") or "").strip()
            category_id = str(evidence.get("category_id") or "").strip()
            canonical_name = str(evidence.get("name") or "").strip()
            if not canonical_code or not category_id or not canonical_name:
                raise ValueError(
                    f"operations records[{index}] exact binding evidence is incomplete"
                )
            category.update({
                "canonical_code": canonical_code,
                "category_id": category_id,
                "canonical_name": canonical_name,
            })
        elif exact_binding is not False:
            raise ValueError(
                f"operations records[{index}] exact binding support must be boolean"
            )
        categories.append(category)

    manifest = finalize_category_manifest(
        {
            "schema_version": CATEGORY_CATALOG_SCHEMA_VERSION,
            "source_system": "wotu",
            "source_type": CATEGORY_CATALOG_SOURCE_TYPE,
            "source_reference": artifact_id,
            "source_snapshot_hash": "sha256:" + source_file_sha256,
            "observed_at": observed_at,
            "approved_by": approved_by,
            "approved_at": approved_at,
            "expected_category_count": len(categories),
            "categories": categories,
        },
        content_hash=content_hash,
    )
    validate_category_manifest(manifest, content_hash=content_hash)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a governed Chengmu category catalog outside Git"
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approved-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    actual_sha = file_sha256(args.source)
    if actual_sha != args.expected_source_sha256:
        parser.error(
            f"source SHA-256 mismatch: expected {args.expected_source_sha256}, got {actual_sha}"
        )
    if args.output.exists():
        parser.error("output already exists; immutable manifests are never overwritten")
    try:
        source = json.loads(args.source.read_text(encoding="utf-8"))
        manifest = prepare_category_catalog_manifest(
            source,
            source_file_sha256=actual_sha,
            approved_by=args.approved_by,
            approved_at=args.approved_at,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "manifest_id": manifest["manifest_id"],
        "manifest_content_hash": manifest["manifest_content_hash"],
        "source_snapshot_hash": manifest["source_snapshot_hash"],
        "expected_category_count": manifest["expected_category_count"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
