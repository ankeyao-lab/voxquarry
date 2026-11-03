#!/usr/bin/env python3
"""Run the explicit, one-shot initializer for a retail product-master database.

This command is intentionally separate from the runtime reader.  It does not
accept a default path, so an operator must name the database being changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retail_landscape.store import RetailStore, expected_migration_versions


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(database: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"{database.stem}-pre-init-{timestamp}.sqlite3"
    if backup.exists():
        raise FileExistsError(f"refusing to overwrite existing backup: {backup}")
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as source:
        with sqlite3.connect(backup) as destination:
            source.backup(destination)
    return backup


def initialize_with_audit(
    database: Path,
    *,
    backup_dir: Path | None = None,
) -> dict[str, object]:
    """Initialize one explicit database and return its auditable result."""
    existed = database.is_file() and database.stat().st_size > 0
    if existed and backup_dir is None:
        raise ValueError("--backup-dir is required when initializing an existing database")
    before_hash = file_sha256(database) if existed else None
    backup = backup_database(database, backup_dir) if existed and backup_dir else None
    store = RetailStore.initialize_database(database)
    # Re-open by the production reader's exact path to make the command's
    # successful exit an auditable readiness assertion, not merely a write-job
    # success signal.
    readonly = RetailStore(database, runtime_mode="read_only")
    with readonly.connect() as conn:
        audit: dict[str, object] = {
            "database": str(database.resolve()),
            "schema_migrations": sorted(
                str(row["version"])
                for row in conn.execute("SELECT version FROM schema_migrations")
            ),
            "expected_schema_migrations": sorted(expected_migration_versions()),
            "product_count": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "brand_count": conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0],
            "voltage_platform_count": conn.execute(
                "SELECT COUNT(*) FROM voltage_platforms"
            ).fetchone()[0],
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "runtime_read_only_verified": True,
            "database_sha256_before": before_hash,
            "database_sha256_after": file_sha256(database),
            "backup_path": str(backup.resolve()) if backup else None,
            "backup_sha256": file_sha256(backup) if backup else None,
        }
    # Keep a reference alive until all auditing is complete; it also makes the
    # intended write-capable ownership of this command unambiguous.
    del store
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize/migrate/seed/backfill a retail product-master SQLite database"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Required for a pre-existing database; receives a consistent SQLite backup",
    )
    parser.add_argument("--audit-output", type=Path)
    args = parser.parse_args()

    try:
        audit = initialize_with_audit(args.database, backup_dir=args.backup_dir)
    except (ValueError, FileExistsError) as exc:
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
