#!/usr/bin/env python3
"""Controlled import for an immutable, operations-approved Wotu category catalog."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retail_landscape.category_registry import validate_category_manifest
from retail_landscape.store import RetailStore, content_hash
from scripts.initialize_retail_product_master_store import backup_database, file_sha256


def import_catalog_with_audit(
    database: Path,
    manifest_path: Path,
    *,
    actor: str,
    backup_dir: Path | None = None,
) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("category catalog manifest must be a JSON object")
    # Validate the complete immutable envelope before the first database write.
    validate_category_manifest(manifest, content_hash=content_hash)
    existed = database.is_file() and database.stat().st_size > 0
    if existed and backup_dir is None:
        raise ValueError("--backup-dir is required when importing into an existing database")
    before_hash = file_sha256(database) if existed else None
    backup = backup_database(database, backup_dir) if existed and backup_dir else None
    store = RetailStore.initialize_database(database)
    result = store.import_category_catalog(manifest, actor=actor)
    with store.connect() as conn:
        wotu_mapping_count = conn.execute(
            "SELECT COUNT(*) FROM category_source_mappings "
            "WHERE source_system='wotu' AND active=1"
        ).fetchone()[0]
        approved_category_count = conn.execute(
            "SELECT COUNT(*) FROM categories WHERE registry_status='approved'"
        ).fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    audit: dict[str, object] = {
        "database": str(database.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_file_sha256": file_sha256(manifest_path),
        "manifest_id": manifest["manifest_id"],
        "manifest_content_hash": manifest["manifest_content_hash"],
        "source_snapshot_hash": manifest["source_snapshot_hash"],
        "database_sha256_before": before_hash,
        "database_sha256_after": file_sha256(database),
        "backup_path": str(backup.resolve()) if backup else None,
        "backup_sha256": file_sha256(backup) if backup else None,
        "expected_category_count": manifest["expected_category_count"],
        "wotu_mapping_count": wotu_mapping_count,
        "approved_category_count": approved_category_count,
        "integrity_check": integrity,
        "import_result": result,
    }
    if wotu_mapping_count < int(manifest["expected_category_count"]):
        raise RuntimeError("Wotu category registry coverage is below manifest expectation")
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import an approved Wotu category catalog into Chengmu"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--audit-output", type=Path)
    args = parser.parse_args()
    try:
        audit = import_catalog_with_audit(
            args.database,
            args.manifest,
            actor=args.actor,
            backup_dir=args.backup_dir,
        )
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    if args.audit_output:
        args.audit_output.parent.mkdir(parents=True, exist_ok=True)
        args.audit_output.write_text(
            json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(audit, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
