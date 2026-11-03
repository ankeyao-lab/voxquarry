#!/usr/bin/env python3
"""Explicit init, dry-run, import, and reconciliation for shared master data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT.parent))
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from collector.master_data import MasterDataError, MasterDataStore  # noqa: E402


DEFAULT_MANIFEST = SERVICE_ROOT / "master_data_bootstrap_manifest.json"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Manage the independent shared master-data SQLite. Runtime startup "
            "never initializes or imports this database."
        )
    )
    result.add_argument("--data-dir", required=True, type=Path)
    subparsers = result.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="explicitly create schema and empty catalog metadata")

    for command in ("dry-run", "apply"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--legacy-db-copy", required=True, type=Path)
        sub.add_argument("--manifest", default=DEFAULT_MANIFEST, type=Path)
        if command == "apply":
            sub.add_argument("--actor", required=True)
            sub.add_argument("--expected-plan-sha256", required=True)
            sub.add_argument("--expected-legacy-copy-sha256", required=True)
            sub.add_argument(
                "--legacy-db-live",
                required=True,
                type=Path,
                help="live jobs.sqlite3 whose governed-table hash must match the reviewed copy",
            )

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--legacy-db-copy", required=True, type=Path)
    reconcile.add_argument("--manifest", default=DEFAULT_MANIFEST, type=Path)

    activate = subparsers.add_parser("activate-legacy")
    activate.add_argument("--legacy-db-live", required=True, type=Path)
    activate.add_argument("--actor", required=True)
    activate.add_argument("--expected-plan-sha256", required=True)
    activate.add_argument(
        "--expected-legacy-master-content-hash", required=True
    )

    deactivate = subparsers.add_parser("deactivate-legacy")
    deactivate.add_argument("--legacy-db-live", required=True, type=Path)
    deactivate.add_argument("--actor", required=True)
    deactivate.add_argument("--expected-catalog-content-hash", required=True)

    recover = subparsers.add_parser("recover-legacy-rebinds")
    recover.add_argument("--legacy-db-live", required=True, type=Path)
    recover.add_argument("--actor", required=True)

    subparsers.add_parser("integration-status")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            store = MasterDataStore(args.data_dir, initialize=True)
            output = {
                "initialized": True,
                "database": str(store.db_path),
                "migrations": store.migration_versions(),
                "integrity": store.integrity(),
                "catalog": store.summary(),
                "integration_status": store.integration_status(),
            }
        else:
            live_path = getattr(args, "legacy_db_live", None)
            store = MasterDataStore(
                args.data_dir,
                legacy_db_path=live_path,
                initialize=False,
            )
            if args.command == "dry-run":
                output = store.import_plan(
                    legacy_db_copy=args.legacy_db_copy,
                    manifest_path=args.manifest,
                )
            elif args.command == "apply":
                output = store.apply_import(
                    legacy_db_copy=args.legacy_db_copy,
                    manifest_path=args.manifest,
                    actor=args.actor,
                    expected_plan_sha256=args.expected_plan_sha256,
                    expected_legacy_copy_sha256=args.expected_legacy_copy_sha256,
                )
            elif args.command == "reconcile":
                output = store.reconcile_legacy_copy(
                    args.legacy_db_copy, manifest_path=args.manifest
                )
            elif args.command == "activate-legacy":
                output = store.activate_legacy_projection(
                    actor=args.actor,
                    expected_plan_sha256=args.expected_plan_sha256,
                    expected_legacy_master_content_hash=(
                        args.expected_legacy_master_content_hash
                    ),
                )
            elif args.command == "deactivate-legacy":
                output = store.deactivate_legacy_projection(
                    actor=args.actor,
                    expected_catalog_hash=args.expected_catalog_content_hash,
                )
            elif args.command == "recover-legacy-rebinds":
                output = store.recover_legacy_rebinds(actor=args.actor)
            else:
                output = store.integration_status()
    except (MasterDataError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
