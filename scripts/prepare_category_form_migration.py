#!/usr/bin/env python3
"""Prepare, audit and validate a category/form migration on NEW local DB copies.

Never edits input databases, review artifacts, cached collection results, or
product-master observation hashes. Deployment of the pair requires a separate
approved maintenance action and backup; this tool does not deploy anything.
"""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Review Scraper/review-scraper-service"))
from collector.master_data import MasterDataStore
from reporting.product_form import FORM_SCHEMA_VERSION, product_form_fact

SCHEMA = "voxquarry.category-form-migration/v1"
RENAMES = {"Backpack Sprayer": "Sprayer", "Handheld Sprayer": "Sprayer", "Handheld Spreader": "Spreader"}
ACTOR = "voxquarry-migration"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def read_db(path: Path):
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def write_db(path: Path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def snapshot_db(source: Path, destination: Path) -> None:
    with read_db(source) as src, closing(sqlite3.connect(destination)) as dst:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"invalid SQLite: {source.name}")
        src.backup(dst)


def prepare(source_data: Path, output_data: Path) -> dict:
    if output_data.exists():
        raise ValueError("output must be a new directory; existing artifacts are immutable")
    with read_db(source_data / "jobs.sqlite3") as conn:
        originals = [dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY id")]
    changes = []
    assignments = {}
    for job in originals:
        fact = product_form_fact(job)
        category = str(job.get("category") or "")
        target = RENAMES.get(category, category)
        if fact["value"] == "unknown" and target == category:
            continue
        if job["status"] in {"running", "queued", "cancelling"}:
            raise ValueError(f"affected task is active: {job['id']}")
        if target != category and job.get("product_master_model_id"):
            raise ValueError(f"bound product needs its own governed category revision: {job['id']}")
        key = (job.get("channel") or "", job.get("source_product_id") or job.get("target_key") or job["id"])
        previous = assignments.get(key)
        if previous and previous != fact["value"]:
            raise ValueError(f"conflicting product forms for {key!r}")
        assignments[key] = fact["value"]
        changes.append({"job_id": job["id"], "from_category": category, "to_category": target,
                        "form": fact, "channel": key[0], "product_key": key[1]})
    output_data.mkdir(parents=True)
    input_hashes = {}
    for name in ("jobs.sqlite3", "master_data.sqlite3"):
        snapshot_db(source_data / name, output_data / name)
        input_hashes[name] = digest(output_data / name)
    master = MasterDataStore(output_data, legacy_db_path=output_data / "jobs.sqlite3")
    if master.legacy_projection_active():
        raise ValueError("active shared legacy projection needs the governed rebind workflow")
    before = {name: master.resolve("categories", name)["record"] for name in ("Sprayer", *RENAMES)}
    # Stable IDs and codes are retained. Retired identities point to Sprayer;
    # their old names remain historical identities, not ambiguous new aliases.
    master_changes = []
    for name in ("Backpack Sprayer", "Handheld Sprayer"):
        entity = before[name]
        result = master.set_active("categories", entity["id"], False, actor=ACTOR,
                                   expected_revision=entity["revision"], replacement_id=before["Sprayer"]["id"])
        master_changes.append({"id": entity["id"], "code": entity["code"], "before": name,
                               "active": False, "replacement_id": result["replacement_id"]})
    entity = before["Handheld Spreader"]
    result = master.update("categories", entity["id"], {"name": "Spreader", "aliases": sorted(set([*entity["aliases"], "Handheld Spreader"]))},
                           actor=ACTOR, expected_revision=entity["revision"])
    master_changes.append({"id": entity["id"], "code": entity["code"], "before": entity["name"], "after": result["name"], "active": True})
    now = datetime.now(timezone.utc).isoformat()
    with write_db(output_data / "jobs.sqlite3") as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for column in ("product_form_schema_version", "product_form_factor", "product_form_evidence_source", "product_form_source_category"):
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        conn.execute("""CREATE TABLE product_form_observation_projection (
            channel TEXT NOT NULL, product_key TEXT NOT NULL, form_factor TEXT NOT NULL,
            schema_version TEXT NOT NULL, evidence_source TEXT NOT NULL, source_category TEXT NOT NULL,
            migration_id TEXT NOT NULL, PRIMARY KEY(channel,product_key))""")
        conn.execute("""CREATE TABLE category_form_migration_audit (
            migration_id TEXT NOT NULL, job_id TEXT NOT NULL, before_category TEXT NOT NULL,
            after_category TEXT NOT NULL, product_form_factor TEXT NOT NULL,
            evidence_source TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(migration_id,job_id))""")
        for row in changes:
            fact = row["form"]
            conn.execute("""UPDATE jobs SET category=?,product_form_schema_version=?,product_form_factor=?,
                product_form_evidence_source=?,product_form_source_category=? WHERE id=? AND category=?""",
                (row["to_category"], FORM_SCHEMA_VERSION, fact["value"], fact["evidence_source"], row["from_category"], row["job_id"], row["from_category"]))
            conn.execute("INSERT OR IGNORE INTO product_form_observation_projection VALUES (?,?,?,?,?,?,?)",
                (row["channel"], row["product_key"], fact["value"], FORM_SCHEMA_VERSION, fact["evidence_source"], row["from_category"], ACTOR))
            conn.execute("INSERT INTO category_form_migration_audit VALUES (?,?,?,?,?,?,?)",
                (ACTOR, row["job_id"], row["from_category"], row["to_category"], fact["value"], fact["evidence_source"], now))
        for old, new in RENAMES.items():
            present = conn.execute("SELECT id FROM master_categories WHERE name=? COLLATE NOCASE", (new,)).fetchone()
            if present:
                conn.execute("DELETE FROM master_categories WHERE name=? COLLATE NOCASE", (old,))
            else:
                conn.execute("UPDATE master_categories SET name=?,updated_at=? WHERE name=? COLLATE NOCASE", (new, now, old))
        # Bump the existing cache invalidation mechanism if present.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(job_scope_revision)")}
        if "revision" in columns:
            conn.execute("UPDATE job_scope_revision SET revision=revision+1")
    with read_db(output_data / "jobs.sqlite3") as conn:
        after = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM jobs")}
        if len(after) != len(originals):
            raise AssertionError("job count changed")
        for original in originals:
            target = after[original["id"]]
            for field, value in original.items():
                if field not in {"category", "product_form_schema_version", "product_form_factor", "product_form_evidence_source", "product_form_source_category"} and target[field] != value:
                    raise AssertionError(f"unapproved data change: {original['id']} {field}")
            if target["category"] != RENAMES.get(original["category"], original["category"]):
                raise AssertionError("unexpected category mapping")
            product_form_fact(target)
        after_counts = dict(Counter(row["category"] for row in after.values()))
    checks = {}
    for name in ("jobs.sqlite3", "master_data.sqlite3"):
        # Freeze all committed WAL pages before hashing the transferable pair.
        with write_db(output_data / name) as conn:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0] != 0:
                raise ValueError(f"database checkpoint is busy: {name}")
        with read_db(output_data / name) as conn:
            checks[name] = {"integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
                            "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
                            "sha256": digest(output_data / name)}
    # Re-open through the real catalog seal verification, not only SQLite.
    MasterDataStore(output_data, legacy_db_path=output_data / "jobs.sqlite3")
    receipt = {"schema_version": SCHEMA, "migration_id": ACTOR, "status": "verified_local_candidate",
               "input_database_hashes": input_hashes, "output_databases": checks,
               "job_count_before": len(originals), "job_count_after": len(after),
               "forms_assigned": len(changes), "product_form_assignments": len(assignments),
               "category_changes": {name: sum(row["from_category"] == name for row in changes) for name in RENAMES},
               "category_counts_before": dict(Counter(row["category"] for row in originals)),
               "category_counts_after": after_counts, "master_changes": master_changes,
               "preserved": ["all job IDs and original fields outside the approved category/form scope", "collection results and observation hashes", "raw reviews", "stable master IDs and codes", "Fan and Misting Fan identities"],
               "created_at": now, "changes": changes}
    (output_data / "category-form-migration-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", type=Path, required=True)
    parser.add_argument("--output-data", type=Path, required=True)
    args = parser.parse_args()
    receipt = prepare(args.source_data, args.output_data)
    print(json.dumps({key: value for key, value in receipt.items() if key not in {"changes", "category_counts_before", "category_counts_after"}}, ensure_ascii=False, indent=2))
