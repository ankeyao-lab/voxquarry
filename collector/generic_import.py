"""Governed, actor-scoped orchestration for generic review imports.

This module never publishes review data itself.  It keeps exact uploaded bytes
in a content-addressed cache, freezes a dry-run plan, records explicit user
confirmation, and emits a deterministic receipt around the existing channel
adapters.  Job/attempt/result writes remain owned by Store.publish_external_import.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


CACHE_SCHEMA_VERSION = "voxquarry.generic-review-import-cache/v1"
PLAN_REQUEST_SCHEMA_VERSION = "voxquarry.generic-review-import-plan-request/v1"
PLAN_SCHEMA_VERSION = "voxquarry.generic-review-import-plan/v1"
CONFIRM_SCHEMA_VERSION = "voxquarry.generic-review-import-confirm/v1"
RECEIPT_SCHEMA_VERSION = "voxquarry.generic-review-import-receipt/v1"
MAX_PLAN_ITEMS = 100
HIGH_RISK_REVIEW_COUNT = 50_000
IDENTITY_RE = re.compile(r"[A-Za-z0-9_.-]{6,128}")
ITEM_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
SHA_RE = re.compile(r"sha256:[0-9a-f]{64}")
CACHE_ID_RE = re.compile(r"grc_[0-9a-f]{32}")
PLAN_ID_RE = re.compile(r"gip_[0-9a-f]{32}")
RECEIPT_ID_RE = re.compile(r"gir_[0-9a-f]{32}")
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class GenericImportError(ValueError):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value) -> str:
    payload = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_identity(name: str, value: str, pattern=IDENTITY_RE) -> str:
    value = str(value or "")
    if not pattern.fullmatch(value):
        raise GenericImportError(
            "generic_import_request_invalid",
            f"{name} has an invalid governed identity",
            422,
        )
    return value


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GenericImportError(
            "generic_import_artifact_unreadable",
            f"governed import artifact is unreadable: {path.name}",
        ) from exc


def _secure_private_directory(path: Path) -> None:
    """Create or owner-verify a private directory without following symlinks."""
    path = Path(path)
    try:
        path.mkdir(mode=PRIVATE_DIR_MODE)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GenericImportError(
            "generic_import_storage_unsafe",
            f"governed import directory is unsafe: {path.name}",
        ) from exc
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid():
            raise GenericImportError(
                "generic_import_storage_unsafe",
                f"governed import directory owner/type is invalid: {path.name}",
            )
        os.fchmod(descriptor, PRIVATE_DIR_MODE)
        verified = os.fstat(descriptor)
        if stat.S_IMODE(verified.st_mode) != PRIVATE_DIR_MODE:
            raise GenericImportError(
                "generic_import_storage_unsafe",
                f"governed import directory is not private: {path.name}",
            )
    finally:
        os.close(descriptor)


def _secure_private_file(path: Path) -> None:
    """Owner-verify and tighten a regular file through a no-follow descriptor."""
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GenericImportError(
            "generic_import_storage_unsafe",
            f"governed import file is unsafe: {path.name}",
        ) from exc
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or current.st_uid != os.geteuid():
            raise GenericImportError(
                "generic_import_storage_unsafe",
                f"governed import file owner/type is invalid: {path.name}",
            )
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        verified = os.fstat(descriptor)
        if stat.S_IMODE(verified.st_mode) != PRIVATE_FILE_MODE:
            raise GenericImportError(
                "generic_import_storage_unsafe",
                f"governed import file is not private: {path.name}",
            )
    finally:
        os.close(descriptor)


def open_private_binary_writer(path: Path):
    """Open a new private file and set 0600 before any sensitive bytes are written."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(Path(path), flags, PRIVATE_FILE_MODE)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        return os.fdopen(descriptor, "wb")
    except Exception:
        os.close(descriptor)
        raise


def _atomic_json(path: Path, value) -> None:
    _secure_private_directory(path.parent)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with open_private_binary_writer(temp) as handle:
            handle.write(canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _secure_private_file(path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class GovernedImportRegistry:
    """Content-addressed cache, confirmation plans and immutable receipts."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve() / "generic-review-import"
        self.cache_dir = self.root / "caches"
        self.request_dir = self.root / "requests"
        self.plan_dir = self.root / "plans"
        self.receipt_dir = self.root / "receipts"
        self.upload_staging_dir = self.root / "upload-staging"
        self.lock = threading.RLock()
        self._prepare_private_tree()

    def _prepare_private_tree(self) -> None:
        """Tighten same-owner legacy paths and reject unsafe storage entries."""
        _secure_private_directory(self.root)
        for path in (
            self.cache_dir,
            self.request_dir,
            self.plan_dir,
            self.receipt_dir,
            self.upload_staging_dir,
        ):
            _secure_private_directory(path)

        def secure_descendants(directory: Path) -> None:
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                raise GenericImportError(
                    "generic_import_storage_unsafe",
                    f"governed import directory is unreadable: {directory.name}",
                ) from exc
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    raise GenericImportError(
                        "generic_import_storage_unsafe",
                        f"governed import storage contains a symlink: {entry.name}",
                    )
                if entry.is_dir(follow_symlinks=False):
                    _secure_private_directory(path)
                    secure_descendants(path)
                elif entry.is_file(follow_symlinks=False):
                    _secure_private_file(path)
                else:
                    raise GenericImportError(
                        "generic_import_storage_unsafe",
                        f"governed import storage contains a special file: {entry.name}",
                    )

        secure_descendants(self.root)

    def create_upload_staging(self) -> Path:
        with self.lock:
            self._prepare_private_tree()
            path = Path(
                tempfile.mkdtemp(prefix="upload-", dir=self.upload_staging_dir)
            )
            _secure_private_directory(path)
            return path

    @staticmethod
    def _request_key(actor_id: str, request_id: str) -> str:
        return canonical_sha256({"actor_id": actor_id, "request_id": request_id})

    @staticmethod
    def _cache_key(actor_id: str, package_sha256: str) -> str:
        digest = canonical_sha256(
            {"actor_id": actor_id, "package_sha256": package_sha256}
        )
        return f"grc_{digest[:32]}"

    def _cache_paths(self, cache_id: str):
        _validate_identity("cache_id", cache_id, CACHE_ID_RE)
        return (
            self.cache_dir / f"{cache_id}.package.json",
            self.cache_dir / f"{cache_id}.metadata.json",
        )

    def register_file_cache(
        self,
        *,
        actor_id: str,
        run_id: str,
        request_id: str,
        source_path: Path,
        package_sha256: str,
        package_bytes: int,
        preview: dict,
    ) -> dict:
        actor_id = _validate_identity("actor_id", actor_id)
        run_id = _validate_identity("run_id", run_id)
        request_id = _validate_identity("request_id", request_id)
        if not SHA_RE.fullmatch(str(package_sha256 or "")):
            raise GenericImportError(
                "generic_import_package_hash_invalid",
                "package_sha256 must be a lowercase sha256 value",
                422,
            )
        source_path = Path(source_path)
        if source_path.is_symlink():
            raise GenericImportError(
                "generic_import_source_unsafe", "upload staging file cannot be a symlink"
            )
        try:
            source_stat = source_path.stat()
        except OSError as exc:
            raise GenericImportError(
                "generic_import_source_unreadable", "upload staging file is unreadable"
            ) from exc
        if not stat.S_ISREG(source_stat.st_mode):
            raise GenericImportError(
                "generic_import_source_unsafe", "upload staging path must be a regular file"
            )
        if source_stat.st_size != int(package_bytes):
            raise GenericImportError(
                "generic_import_package_changed", "upload size changed after validation"
            )
        actual_hash = "sha256:" + file_sha256(source_path)
        if actual_hash != package_sha256:
            raise GenericImportError(
                "generic_import_package_changed", "upload hash changed after validation"
            )
        if not isinstance(preview, dict) or preview.get("status") != "accepted":
            raise GenericImportError(
                "generic_import_preview_not_accepted",
                "only a fully accepted dry-run can enter the governed cache",
                422,
            )
        if preview.get("writes_performed") != 0:
            raise GenericImportError(
                "generic_import_preview_wrote_business_data",
                "dry-run reported business writes and cannot be cached",
            )
        preview_hash = "sha256:" + canonical_sha256(preview)
        cache_id = self._cache_key(actor_id, package_sha256)
        package_path, metadata_path = self._cache_paths(cache_id)
        request_path = self.request_dir / f"upload-{self._request_key(actor_id, request_id)}.json"
        metadata = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "cache_id": cache_id,
            "actor_id": actor_id,
            "run_id": run_id,
            "upload_request_id": request_id,
            "package_sha256": package_sha256,
            "package_bytes": int(package_bytes),
            "channel": preview.get("channel"),
            "import_id": preview.get("import_id"),
            "target_key": preview.get("target_key"),
            "source_product_id": preview.get("source_product_id"),
            "publishable_status": preview.get("publishable_status"),
            "source_total": preview.get("source_total"),
            "source_records_collected": preview.get("source_records_collected"),
            "source_missing_records": preview.get("source_missing_records"),
            "eligible_reviews": preview.get("eligible_reviews"),
            "preview_sha256": preview_hash,
            "created_at": utc_now(),
            "business_writes_performed": 0,
        }
        with self.lock:
            if request_path.exists():
                prior = _read_json(request_path)
                if (
                    prior.get("package_sha256") != package_sha256
                    or prior.get("cache_id") != cache_id
                ):
                    raise GenericImportError(
                        "generic_import_request_id_conflict",
                        "upload request_id was already used for a different package",
                    )
                return self.get_cache(actor_id, cache_id)
            if package_path.exists() or metadata_path.exists():
                if not package_path.is_file() or not metadata_path.is_file():
                    raise GenericImportError(
                        "generic_import_cache_conflict", "governed cache is incomplete"
                    )
                existing = self.get_cache(actor_id, cache_id)
                if existing["package_sha256"] != package_sha256:
                    raise GenericImportError(
                        "generic_import_cache_conflict", "governed cache hash conflicts"
                    )
            else:
                _secure_private_directory(package_path.parent)
                temp = package_path.with_name(f".{package_path.name}.{secrets.token_hex(8)}.tmp")
                try:
                    with source_path.open("rb") as source, open_private_binary_writer(temp) as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                        target.flush()
                        os.fsync(target.fileno())
                    if "sha256:" + file_sha256(temp) != package_sha256:
                        raise GenericImportError(
                            "generic_import_package_changed",
                            "cached bytes do not match the validated upload",
                        )
                    os.replace(temp, package_path)
                    _secure_private_file(package_path)
                finally:
                    try:
                        temp.unlink()
                    except FileNotFoundError:
                        pass
                _atomic_json(metadata_path, metadata)
            _atomic_json(
                request_path,
                {
                    "actor_id": actor_id,
                    "request_id": request_id,
                    "cache_id": cache_id,
                    "package_sha256": package_sha256,
                },
            )
        return self.get_cache(actor_id, cache_id)

    def get_cache(self, actor_id: str, cache_id: str) -> dict:
        actor_id = _validate_identity("actor_id", actor_id)
        package_path, metadata_path = self._cache_paths(cache_id)
        if package_path.is_symlink() or metadata_path.is_symlink():
            raise GenericImportError(
                "generic_import_cache_unsafe", "governed cache cannot contain symlinks"
            )
        if not package_path.is_file() or not metadata_path.is_file():
            raise GenericImportError(
                "generic_import_cache_not_found", "governed cache was not found", 404
            )
        metadata = _read_json(metadata_path)
        if metadata.get("schema_version") != CACHE_SCHEMA_VERSION:
            raise GenericImportError(
                "generic_import_cache_schema_mismatch", "governed cache schema changed"
            )
        if metadata.get("actor_id") != actor_id:
            raise GenericImportError(
                "generic_import_cache_actor_mismatch",
                "governed cache is owned by another actor",
                403,
            )
        package_hash = "sha256:" + file_sha256(package_path)
        if (
            package_hash != metadata.get("package_sha256")
            or package_path.stat().st_size != metadata.get("package_bytes")
        ):
            raise GenericImportError(
                "generic_import_cache_changed", "governed cache bytes changed"
            )
        result = dict(metadata)
        result["package_path"] = str(package_path)
        return result

    def list_caches(self, actor_id: str) -> list[dict]:
        actor_id = _validate_identity("actor_id", actor_id)
        if not self.cache_dir.exists():
            return []
        results = []
        for path in sorted(self.cache_dir.glob("grc_*.metadata.json")):
            try:
                item = self.get_cache(actor_id, path.name.removesuffix(".metadata.json"))
            except GenericImportError as exc:
                if exc.code == "generic_import_cache_actor_mismatch":
                    continue
                raise
            item.pop("package_path", None)
            results.append(item)
        return results

    def create_plan(
        self,
        *,
        actor_id: str,
        run_id: str,
        request_id: str,
        cache_refs: list[dict],
        evaluations: dict[str, dict] | None = None,
    ) -> dict:
        actor_id = _validate_identity("actor_id", actor_id)
        run_id = _validate_identity("run_id", run_id)
        request_id = _validate_identity("request_id", request_id)
        if not isinstance(cache_refs, list) or not 1 <= len(cache_refs) <= MAX_PLAN_ITEMS:
            raise GenericImportError(
                "generic_import_plan_size_invalid",
                f"a plan must contain 1-{MAX_PLAN_ITEMS} cache references",
                422,
            )
        evaluations = dict(evaluations or {})
        item_ids = []
        items = []
        for position, ref in enumerate(cache_refs, 1):
            if not isinstance(ref, dict):
                raise GenericImportError(
                    "generic_import_plan_item_invalid", "each cache reference must be an object", 422
                )
            item_id = _validate_identity("item_id", ref.get("item_id"), ITEM_RE)
            if item_id in item_ids:
                raise GenericImportError(
                    "generic_import_plan_item_duplicate", "plan item_id values must be unique", 422
                )
            item_ids.append(item_id)
            expected_hash = str(ref.get("package_sha256") or "")
            try:
                cached = self.get_cache(actor_id, str(ref.get("cache_id") or ""))
                if expected_hash != cached["package_sha256"]:
                    raise GenericImportError(
                        "generic_import_package_hash_mismatch",
                        "cache reference package hash does not match",
                    )
                evaluation = evaluations.get(item_id)
                if evaluation is not None:
                    if evaluation.get("status") != "accepted":
                        error = evaluation.get("error") or {}
                        raise GenericImportError(
                            str(error.get("code") or "generic_import_dry_run_blocked"),
                            str(error.get("message") or "current dry-run blocked this package"),
                        )
                    if (
                        evaluation.get("upload_sha256")
                        and "sha256:" + str(evaluation["upload_sha256"])
                        != cached["package_sha256"]
                    ):
                        raise GenericImportError(
                            "generic_import_package_hash_mismatch",
                            "current dry-run package hash does not match cache",
                        )
                    fresh = evaluation
                else:
                    fresh = cached
                items.append(
                    {
                        "position": position,
                        "item_id": item_id,
                        "status": "accepted",
                        "cache_id": cached["cache_id"],
                        "package_sha256": cached["package_sha256"],
                        "channel": fresh.get("channel"),
                        "import_id": fresh.get("import_id") or cached.get("import_id"),
                        "target_key": fresh.get("target_key"),
                        "source_product_id": fresh.get("source_product_id"),
                        "publishable_status": fresh.get("publishable_status"),
                        "source_total": fresh.get("source_total"),
                        "source_records_collected": fresh.get("source_records_collected"),
                        "source_missing_records": fresh.get("source_missing_records"),
                        "eligible_reviews": fresh.get("eligible_reviews"),
                    }
                )
            except GenericImportError as exc:
                items.append(
                    {
                        "position": position,
                        "item_id": item_id,
                        "status": "blocked",
                        "cache_id": str(ref.get("cache_id") or ""),
                        "package_sha256": expected_hash or None,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
        accepted = [item for item in items if item["status"] == "accepted"]
        total = sum(int(item.get("source_total") or 0) for item in accepted)
        high_risk = len(accepted) > 1 or total >= HIGH_RISK_REVIEW_COUNT
        scope = {
            "actor_id": actor_id,
            "run_id": run_id,
            "items": [
                {
                    "item_id": item["item_id"],
                    "cache_id": item["cache_id"],
                    "package_sha256": item["package_sha256"],
                }
                for item in accepted
            ],
        }
        scope_sha = "sha256:" + canonical_sha256(scope)
        request_key = self._request_key(actor_id, request_id)
        plan_id = f"gip_{request_key[:32]}"
        plan_path = self.plan_dir / f"{plan_id}.json"
        request_fingerprint = "sha256:" + canonical_sha256(
            {"actor_id": actor_id, "run_id": run_id, "cache_refs": cache_refs}
        )
        plan = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_id": plan_id,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "actor_id": actor_id,
            "run_id": run_id,
            "status": "confirmation_required" if accepted else "nothing_publishable",
            "created_at": utc_now(),
            "items": items,
            "summary": {
                "item_count": len(items),
                "accepted_count": len(accepted),
                "blocked_count": len(items) - len(accepted),
                "declared_review_count": total,
            },
            "confirmation": (
                {
                    "confirmation_id": f"gic_{secrets.token_hex(16)}",
                    "scope_sha256": scope_sha,
                    "accepted_item_ids": [item["item_id"] for item in accepted],
                    "requires_second_confirmation": high_risk,
                    "reason": (
                        "multiple_products_or_at_least_50000_declared_reviews"
                        if high_risk
                        else "business_object_creation"
                    ),
                }
                if accepted
                else None
            ),
            "business_writes_performed": 0,
        }
        with self.lock:
            if plan_path.exists():
                prior = _read_json(plan_path)
                if prior.get("request_fingerprint") != request_fingerprint:
                    raise GenericImportError(
                        "generic_import_request_id_conflict",
                        "plan request_id was already used for a different scope",
                    )
                return prior
            _atomic_json(plan_path, plan)
        return plan

    def get_plan(self, plan_id: str) -> dict:
        _validate_identity("plan_id", plan_id, PLAN_ID_RE)
        path = self.plan_dir / f"{plan_id}.json"
        if not path.is_file() or path.is_symlink():
            raise GenericImportError(
                "generic_import_plan_not_found", "governed import plan was not found", 404
            )
        plan = _read_json(path)
        if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
            raise GenericImportError(
                "generic_import_plan_schema_mismatch", "governed import plan schema changed"
            )
        return plan

    def claim_confirmation(
        self,
        *,
        actor_id: str,
        run_id: str,
        plan_id: str,
        execution_request_id: str,
        confirmation_id: str,
        scope_sha256: str,
        item_ids: list[str],
        confirmed: bool,
        second_confirmation: bool,
    ) -> dict:
        actor_id = _validate_identity("actor_id", actor_id)
        run_id = _validate_identity("run_id", run_id)
        execution_request_id = _validate_identity("execution_request_id", execution_request_id)
        if confirmed is not True:
            raise GenericImportError(
                "generic_import_confirmation_required", "explicit confirmation is required", 422
            )
        with self.lock:
            plan = self.get_plan(plan_id)
            if plan.get("actor_id") != actor_id or plan.get("run_id") != run_id:
                raise GenericImportError(
                    "generic_import_plan_actor_mismatch", "plan does not belong to this actor/run", 403
                )
            confirmation = plan.get("confirmation") or {}
            if confirmation.get("confirmation_id") != confirmation_id:
                raise GenericImportError(
                    "generic_import_confirmation_mismatch", "confirmation identity does not match"
                )
            if confirmation.get("scope_sha256") != scope_sha256:
                raise GenericImportError(
                    "generic_import_confirmation_scope_mismatch", "confirmation scope hash does not match"
                )
            accepted_ids = confirmation.get("accepted_item_ids") or []
            if (
                not isinstance(item_ids, list)
                or not item_ids
                or len(item_ids) != len(set(item_ids))
                or any(item_id not in accepted_ids for item_id in item_ids)
            ):
                raise GenericImportError(
                    "generic_import_confirmation_items_invalid",
                    "confirmation must select a unique non-empty subset of accepted items",
                    422,
                )
            selected = [item_id for item_id in accepted_ids if item_id in set(item_ids)]
            if confirmation.get("requires_second_confirmation") and second_confirmation is not True:
                raise GenericImportError(
                    "generic_import_second_confirmation_required",
                    "second confirmation is required for this plan",
                    422,
                )
            if plan.get("status") == "completed":
                if (
                    plan.get("execution_request_id") != execution_request_id
                    or plan.get("selected_item_ids") != selected
                ):
                    raise GenericImportError(
                        "generic_import_plan_execution_conflict",
                        "completed plan does not match this execution request and item scope",
                    )
                receipt_id = _validate_identity(
                    "receipt_id", plan.get("receipt_id"), RECEIPT_ID_RE
                )
                receipt = _read_json(self.receipt_dir / f"{receipt_id}.json")
                return {"replayed_receipt": receipt, "selected_item_ids": selected}
            if plan.get("status") == "executing":
                if (
                    plan.get("execution_request_id") != execution_request_id
                    or plan.get("selected_item_ids") != selected
                ):
                    raise GenericImportError(
                        "generic_import_plan_execution_conflict", "plan is already executing another scope"
                    )
                return plan
            if plan.get("status") != "confirmation_required":
                raise GenericImportError(
                    "generic_import_plan_not_confirmable", "plan has no publishable items"
                )
            plan["status"] = "executing"
            plan["execution_request_id"] = execution_request_id
            plan["selected_item_ids"] = selected
            plan["confirmed_at"] = utc_now()
            plan["item_results"] = {}
            _atomic_json(self.plan_dir / f"{plan_id}.json", plan)
            return plan

    def record_item_result(self, plan_id: str, item_id: str, result: dict) -> dict:
        item_id = _validate_identity("item_id", item_id, ITEM_RE)
        if not isinstance(result, dict) or result.get("status") not in {
            "published",
            "already_published",
            "blocked",
        }:
            raise GenericImportError(
                "generic_import_item_result_invalid", "item result has an invalid status"
            )
        with self.lock:
            plan = self.get_plan(plan_id)
            if plan.get("status") != "executing" or item_id not in plan.get("selected_item_ids", []):
                raise GenericImportError(
                    "generic_import_plan_not_executing", "item is not in the executing confirmation scope"
                )
            prior = (plan.get("item_results") or {}).get(item_id)
            if prior is not None:
                if prior != result:
                    raise GenericImportError(
                        "generic_import_item_result_conflict", "item already has a different result"
                    )
                return plan
            plan.setdefault("item_results", {})[item_id] = result
            _atomic_json(self.plan_dir / f"{plan_id}.json", plan)
            return plan

    def finalize_receipt(self, plan_id: str) -> dict:
        with self.lock:
            plan = self.get_plan(plan_id)
            if plan.get("status") == "completed":
                receipt_id = _validate_identity(
                    "receipt_id", plan.get("receipt_id"), RECEIPT_ID_RE
                )
                receipt = _read_json(self.receipt_dir / f"{receipt_id}.json")
                expected = "sha256:" + canonical_sha256(
                    {key: value for key, value in receipt.items() if key != "receipt_sha256"}
                )
                if receipt.get("receipt_sha256") != expected:
                    raise GenericImportError(
                        "generic_import_receipt_changed", "stored import receipt hash changed"
                    )
                return receipt
            if plan.get("status") != "executing":
                raise GenericImportError(
                    "generic_import_plan_not_executing", "plan is not ready for a receipt"
                )
            selected = plan.get("selected_item_ids") or []
            results = plan.get("item_results") or {}
            if any(item_id not in results for item_id in selected):
                raise GenericImportError(
                    "generic_import_execution_incomplete", "not every selected item has a result"
                )
            ordered_results = [
                {"item_id": item_id, **results[item_id]} for item_id in selected
            ]
            published = sum(
                item["status"] in {"published", "already_published"}
                for item in ordered_results
            )
            logical = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "plan_id": plan_id,
                "actor_id": plan["actor_id"],
                "run_id": plan["run_id"],
                "execution_request_id": plan["execution_request_id"],
                "confirmation_id": plan["confirmation"]["confirmation_id"],
                "confirmation_scope_sha256": plan["confirmation"]["scope_sha256"],
                "confirmed_item_ids": selected,
                "created_at": utc_now(),
                "results": ordered_results,
                "business_writes": {
                    "published_count": published,
                    "blocked_count": len(ordered_results) - published,
                },
            }
            receipt_id = f"gir_{canonical_sha256(logical)[:32]}"
            receipt = {**logical, "receipt_id": receipt_id}
            receipt["receipt_sha256"] = "sha256:" + canonical_sha256(receipt)
            receipt_path = self.receipt_dir / f"{receipt_id}.json"
            if receipt_path.exists():
                prior = _read_json(receipt_path)
                if prior != receipt:
                    raise GenericImportError(
                        "generic_import_receipt_conflict", "receipt identity already has different content"
                    )
            else:
                _atomic_json(receipt_path, receipt)
            plan["status"] = "completed"
            plan["completed_at"] = receipt["created_at"]
            plan["receipt_id"] = receipt_id
            plan["receipt_sha256"] = receipt["receipt_sha256"]
            _atomic_json(self.plan_dir / f"{plan_id}.json", plan)
            return receipt
