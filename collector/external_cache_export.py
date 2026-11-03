"""Deterministic, streaming export for Home Depot and Walmart cache tasks."""

import hashlib
import json
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .external_import import external_import_format_requirements
from .source_product_identity import source_product_identity_from_url


EXPORT_SCHEMA_VERSION = "voxquarry.external-review-cache-export/v2"
EXPORT_CHANNELS = {"homedepot", "walmart"}
BUSINESS_DATA_STATUSES = {"completed", "partial_success"}
REQUIRED_ARTIFACTS = {
    "reviews.csv",
    "reviews.raw.jsonl",
    "validation_report.json",
    "selection_manifest.json",
}


def _publication_source_product_id(item):
    current = str(item.get("source_product_id") or "").strip()
    if current:
        return current
    identity = source_product_identity_from_url(
        item.get("channel"),
        item.get("target_key") or item.get("target_url"),
    )
    if identity.get("status") == "resolved":
        return identity["source_product_id"]
    return None


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def _safe_active_root(store, item):
    active_path = str(item.get("active_run_path") or "")
    expected = f"{item['id']}/{item['channel']}"
    attempt_no = item.get("active_attempt_no")
    archived = (
        f"{item['id']}/attempts/{attempt_no}/{item['channel']}"
        if isinstance(attempt_no, int) and attempt_no > 0 else None
    )
    if active_path not in {expected, archived}:
        raise ValueError(
            f"cache task {item['id']} active result path is not its stable or active attempt root"
        )
    if active_path == archived:
        owner = next((attempt for attempt in store.list_attempts(item["id"])
                      if attempt["attempt_no"] == attempt_no), None)
        if not owner or owner["status"] != item.get("active_result_status"):
            raise ValueError(f"cache task {item['id']} active attempt does not own its result")
    runs_root = (store.data_dir / "runs").resolve()
    candidate = runs_root
    for part in Path(active_path).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"cache task {item['id']} active result contains a symlink")
    run_dir = (runs_root / active_path).resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise ValueError(f"cache task {item['id']} active result escapes runs") from exc
    return run_dir


def _business_artifacts(store, item, snapshot_root):
    source_run_dir = _safe_active_root(store, item)
    run_dir = snapshot_root / item["id"] / item["channel"]
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in sorted(REQUIRED_ARTIFACTS):
        source = source_run_dir / name
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"cache task {item['id']} is missing artifact {name}")
        shutil.copyfile(source, run_dir / name)
    missing = sorted(name for name in REQUIRED_ARTIFACTS if not (run_dir / name).is_file())
    if missing:
        raise ValueError(
            f"cache task {item['id']} is missing artifact {missing[0]}"
        )
    # The export is another publication boundary. Rebuild the governed import
    # from manifest + raw evidence, and compare its full CSV and validation
    # projections before exposing any business records. File existence and
    # hashes alone do not prove that the four artifacts agree.
    expected_source_product_id = _publication_source_product_id(item)
    if not expected_source_product_id:
        raise ValueError(
            f"cache task {item['id']} has no verified source product identity"
        )
    try:
        manifest_header = json.loads((run_dir / "selection_manifest.json").read_text())
        if (item["channel"] == "homedepot" and "schema_version" not in manifest_header
                and "selected" in manifest_header):
            from .external_cache_legacy import verify_legacy_home_depot
            verified = verify_legacy_home_depot(run_dir, item, expected_source_product_id)
        else:
            verified = store._verify_external_import_artifact_projection(
                run_dir,
                expected_channel=item["channel"],
                expected_target_key=item["target_key"],
                expected_market_region=item["market_region"],
                expected_source_product_id=expected_source_product_id,
                allow_legacy_declared_incomplete=True,
            )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"cache task {item['id']} {exc}") from exc
    report = verified["report"]
    manifest = verified["manifest"]
    expected_status = {
        "completed": {"COMPLETE", "COMPLETE_FALLBACK_SOURCE"},
        "partial_success": {"PARTIAL_SUCCESS"},
    }[item["active_result_status"]]
    if report.get("status") not in expected_status:
        raise ValueError(
            f"cache task {item['id']} validation status disagrees with active result"
        )
    if report.get("channel") != item["channel"] or manifest.get("channel") != item["channel"]:
        raise ValueError(f"cache task {item['id']} artifact channel mismatch")
    artifact_hashes = {}
    estimated_bytes = 0
    for name in sorted(REQUIRED_ARTIFACTS):
        digest, size = _sha256_file(run_dir / name)
        artifact_hashes[name] = {"sha256": digest, "bytes": size}
        estimated_bytes += size
    collected = int(verified["validated"]["source_records_collected"])
    raw_count = 0
    try:
        with (run_dir / "reviews.raw.jsonl").open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    raise ValueError(f"cache task {item['id']} raw JSONL has an empty line")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"cache task {item['id']} raw JSONL has a non-object row")
                raw_count += 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cache task {item['id']} raw JSONL is unreadable") from exc
    if raw_count != collected:
        raise ValueError(f"cache task {item['id']} raw count disagrees with validation")
    return {
        "run_dir": run_dir,
        "report": report,
        "manifest": manifest,
        "artifact_hashes": artifact_hashes,
        "record_count": raw_count,
        "estimated_bytes": estimated_bytes,
    }


def build_external_cache_export_plan(store, *, channels, task_ids=None):
    selected_channels = tuple(sorted(set(channels or ())))
    if not selected_channels or any(channel not in EXPORT_CHANNELS for channel in selected_channels):
        raise ValueError("cache export channels must be homedepot and/or walmart")
    items = store.list_external_import_tasks(selected_channels, job_ids=task_ids)
    snapshot_owner = tempfile.TemporaryDirectory(prefix="voxquarry-cache-export-")
    snapshot_root = Path(snapshot_owner.name)
    tasks = []
    status_counts = Counter()
    record_count = 0
    estimated_bytes = 0
    requirements = external_import_format_requirements()
    try:
        for item in items:
            active_status = item.get("active_result_status")
            status = active_status or item.get("status") or "unknown"
            status_counts[status] += 1
            task = {
                "task_id": item["id"],
                "channel": item["channel"],
                "status": item.get("status"),
                "active_result_status": active_status,
                "target_url": item.get("target_url"),
                "canonical_target_key": item.get("target_key"),
                "source_product_id": _publication_source_product_id(item),
                "product_metadata": {
                    "brand": item.get("brand"),
                    "model": item.get("model"),
                    "category": item.get("category"),
                    "market_region": item.get("market_region"),
                    "energy_type": item.get("energy_type"),
                    "voltage_platform_key": item.get("voltage_platform_key"),
                    "voltage": item.get("energy_canonical_voltage"),
                },
                "market_region": item.get("market_region"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("finished_at") or item.get("started_at") or item.get("created_at"),
                "error": item.get("error") if status == "failed" else None,
                "business_data_included": active_status in BUSINESS_DATA_STATUSES,
                "business_data_policy": (
                    "included"
                    if active_status in BUSINESS_DATA_STATUSES
                    else "diagnostic_manifest_only"
                ),
            }
            if active_status in BUSINESS_DATA_STATUSES:
                artifacts = _business_artifacts(store, item, snapshot_root)
                task.update(
                    {
                        "source_total": artifacts["report"].get("source_total"),
                        "source_records_collected": artifacts["record_count"],
                        "source_missing_records": artifacts["report"].get("source_missing_records"),
                        "source_reconciled": artifacts["report"].get("source_reconciled"),
                        "stop_reason": artifacts["report"].get("stop_reason") or None,
                        "artifact_hashes": artifacts["artifact_hashes"],
                        "_raw_path": artifacts["run_dir"] / "reviews.raw.jsonl",
                    }
                )
                record_count += artifacts["record_count"]
                estimated_bytes += artifacts["estimated_bytes"]
            elif status == "queued":
                task["collection_request"] = {
                    "execution_policy": "external_import_only",
                    "import_schema_version": requirements["schema_version"],
                    "task_match": requirements["task_match"],
                }
            tasks.append(task)
    except Exception:
        snapshot_owner.cleanup()
        raise
    core_tasks = []
    for task in tasks:
        core_tasks.append({key: value for key, value in task.items() if key != "_raw_path"})
    summary = {
        "task_count": len(tasks),
        "record_count": record_count,
        "estimated_source_bytes": estimated_bytes,
        "status_counts": dict(sorted(status_counts.items())),
        "channels": list(selected_channels),
    }
    core = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "summary": summary,
        "tasks": core_tasks,
    }
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "summary": summary,
        "logical_content_sha256": "sha256:" + hashlib.sha256(_canonical_json(core)).hexdigest(),
        "tasks": tasks,
        "_snapshot_owner": snapshot_owner,
    }


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def release_external_cache_export_plan(plan):
    owner = plan.pop("_snapshot_owner", None)
    if owner is not None:
        owner.cleanup()


def _stream_external_cache_json(plan):
    """Yield one valid JSON object without loading all review rows in memory."""
    try:
        generated_at = datetime.now(timezone.utc).isoformat()
        groups = defaultdict(list)
        for task in plan["tasks"]:
            groups[task["channel"]].append(task)
        yield "{"
        yield '"schema_version":' + _json(plan["schema_version"])
        yield ',"generated_at":' + _json(generated_at)
        yield ',"logical_content_sha256":' + _json(plan["logical_content_sha256"])
        yield ',"summary":' + _json(plan["summary"])
        yield ',"channels":['
        first_channel = True
        for channel in sorted(groups):
            if not first_channel:
                yield ","
            first_channel = False
            yield '{"channel":' + _json(channel) + ',"tasks":['
            first_task = True
            for task in groups[channel]:
                if not first_task:
                    yield ","
                first_task = False
                public_task = {key: value for key, value in task.items() if not key.startswith("_")}
                prefix = _json(public_task)
                if task.get("business_data_included"):
                    yield prefix[:-1] + ',"reviews":['
                    first_review = True
                    with task["_raw_path"].open("r", encoding="utf-8") as handle:
                        for line in handle:
                            if not first_review:
                                yield ","
                            first_review = False
                            yield line.strip()
                    yield "]}"
                else:
                    yield prefix
            yield "]}"
        yield "]}"
    finally:
        release_external_cache_export_plan(plan)


def stream_external_cache_export(plan):
    """Batch JSON fragments so HTTP streaming does not dispatch per review row."""
    fragments = _stream_external_cache_json(plan)
    pending = []
    characters = 0
    try:
        for fragment in fragments:
            pending.append(fragment)
            characters += len(fragment)
            if characters >= 64 * 1024:
                yield "".join(pending)
                pending.clear()
                characters = 0
        if pending:
            yield "".join(pending)
    finally:
        # A disconnected client can close us while the JSON generator is paused.
        fragments.close()
