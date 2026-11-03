#!/usr/bin/env python3
"""Restore a named local SQLite backup with an exact target-hash guard."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.initialize_retail_product_master_store import file_sha256


def restore_database(
    database: Path,
    backup: Path,
    *,
    expected_current_sha256: str,
) -> str:
    """Restore one named backup after guarding the exact current target."""
    if not database.is_file() or not backup.is_file():
        raise FileNotFoundError("database and backup must both exist")
    current_hash = file_sha256(database)
    if current_hash != expected_current_sha256:
        raise ValueError("current database hash does not match --expected-current-sha256")
    temporary = database.with_name(database.name + ".restore-tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary restore target already exists: {temporary}")
    with sqlite3.connect(f"{backup.resolve().as_uri()}?mode=ro", uri=True) as source:
        with sqlite3.connect(temporary) as destination:
            source.backup(destination)
    with sqlite3.connect(f"{temporary.resolve().as_uri()}?mode=ro", uri=True) as restored:
        if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("restored backup failed integrity_check")
    os.replace(temporary, database)
    return file_sha256(database)


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a retail product-master SQLite backup")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--expected-current-sha256", required=True)
    args = parser.parse_args()
    try:
        restored_hash = restore_database(
            args.database,
            args.backup,
            expected_current_sha256=args.expected_current_sha256,
        )
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    print(f"restored_sha256={restored_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
