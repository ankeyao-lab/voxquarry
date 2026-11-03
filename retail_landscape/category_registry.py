from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, Callable


CATEGORY_REGISTRY_VERSION = "category-registry/v1"
CATEGORY_CATALOG_SCHEMA_VERSION = "voxquarry.category-catalog-manifest/v1"
CATEGORY_CATALOG_SOURCE_TYPE = "wotu_master_categories_snapshot"
CATEGORY_CODE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
CATEGORY_ID = re.compile(r"^cat_[a-z0-9_]+$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def normalize_category_name(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().split()).casefold()


def derive_wotu_category_code(source_name: object) -> str:
    """Create the versioned initial slug for a previously unseen Wotu identity.

    The result is only used once, then persisted. It never replaces an existing
    source mapping and never resolves collisions by adding guessed suffixes.
    """
    normalized = unicodedata.normalize("NFKD", str(source_name or ""))
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")
    if not slug:
        raise ValueError("cannot derive category code from source_name")
    return "wotu_" + slug


def category_manifest_content(manifest: dict[str, Any]) -> dict[str, Any]:
    categories = sorted(
        manifest.get("categories") or [],
        key=lambda item: str(item.get("source_category_id") or ""),
    )
    return {
        "schema_version": manifest.get("schema_version"),
        "source_system": manifest.get("source_system"),
        "source_type": manifest.get("source_type"),
        "source_reference": manifest.get("source_reference"),
        "source_snapshot_hash": manifest.get("source_snapshot_hash"),
        "observed_at": manifest.get("observed_at"),
        "approved_by": manifest.get("approved_by"),
        "approved_at": manifest.get("approved_at"),
        "expected_category_count": manifest.get("expected_category_count"),
        "categories": categories,
    }


def finalize_category_manifest(
    manifest: dict[str, Any],
    *,
    content_hash: Callable[[Any], str],
) -> dict[str, Any]:
    result = dict(manifest)
    digest = content_hash(category_manifest_content(result))
    result["manifest_content_hash"] = digest
    result["manifest_id"] = "ccm_" + digest.removeprefix("sha256:")
    return result


def validate_category_manifest(
    manifest: dict[str, Any],
    *,
    content_hash: Callable[[Any], str],
) -> None:
    allowed_manifest_keys = {
        "schema_version", "manifest_id", "manifest_content_hash", "source_system",
        "source_type", "source_reference", "source_snapshot_hash", "observed_at",
        "approved_by", "approved_at", "expected_category_count", "categories",
    }
    unknown_manifest_keys = sorted(set(manifest) - allowed_manifest_keys)
    if unknown_manifest_keys:
        raise ValueError(
            "category catalog contains unknown fields: " + ", ".join(unknown_manifest_keys)
        )
    if manifest.get("schema_version") != CATEGORY_CATALOG_SCHEMA_VERSION:
        raise ValueError("unsupported category catalog schema_version")
    if manifest.get("source_system") != "wotu":
        raise ValueError("category catalog source_system must be wotu")
    if manifest.get("source_type") != CATEGORY_CATALOG_SOURCE_TYPE:
        raise ValueError("category catalog source_type is not approved")
    for key in (
        "source_reference", "observed_at", "approved_by", "approved_at",
    ):
        if not str(manifest.get(key) or "").strip():
            raise ValueError(f"category catalog {key} is required")
    for key in ("observed_at", "approved_at"):
        try:
            parsed = datetime.fromisoformat(str(manifest[key]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"category catalog {key} must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"category catalog {key} must include a timezone")
    if not SHA256.fullmatch(str(manifest.get("source_snapshot_hash") or "")):
        raise ValueError("category catalog source_snapshot_hash must be sha256:<64 hex>")
    categories = manifest.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("category catalog categories must be a non-empty array")
    expected_count = manifest.get("expected_category_count")
    if not isinstance(expected_count, int) or expected_count != len(categories):
        raise ValueError("expected_category_count must equal categories length")

    source_ids: set[str] = set()
    codes: set[str] = set()
    names: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for index, entry in enumerate(categories):
        if not isinstance(entry, dict):
            raise ValueError(f"categories[{index}] must be an object")
        allowed_entry_keys = {
            "source_category_id", "source_name", "canonical_code", "category_id",
            "canonical_name", "name_zh", "aliases", "source_created_at",
            "source_updated_at", "source_metadata",
        }
        unknown_entry_keys = sorted(set(entry) - allowed_entry_keys)
        if unknown_entry_keys:
            raise ValueError(
                f"categories[{index}] contains unknown fields: "
                + ", ".join(unknown_entry_keys)
            )
        source_id = str(entry.get("source_category_id") or "").strip()
        source_name = str(entry.get("source_name") or "").strip()
        code = str(entry.get("canonical_code") or "").strip() or derive_wotu_category_code(source_name)
        category_id = str(entry.get("category_id") or "").strip()
        if not source_id or not source_name:
            raise ValueError(f"categories[{index}] source_category_id/source_name required")
        for timestamp_key in ("source_created_at", "source_updated_at"):
            timestamp = str(entry.get(timestamp_key) or "").strip()
            if not timestamp:
                raise ValueError(f"categories[{index}] {timestamp_key} required")
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"categories[{index}] {timestamp_key} must be ISO-8601"
                ) from exc
            if parsed.tzinfo is None:
                raise ValueError(f"categories[{index}] {timestamp_key} must include a timezone")
        if source_id in source_ids:
            raise ValueError(f"duplicate source_category_id: {source_id}")
        source_ids.add(source_id)
        if not CATEGORY_CODE.fullmatch(code):
            raise ValueError(f"invalid canonical_code: {code}")
        if code in codes:
            raise ValueError(f"duplicate canonical_code: {code}")
        codes.add(code)
        if category_id and not CATEGORY_ID.fullmatch(category_id):
            raise ValueError(f"invalid category_id: {category_id}")
        canonical_name = str(entry.get("canonical_name") or source_name).strip()
        name_key = normalize_category_name(canonical_name)
        if not name_key:
            raise ValueError(f"categories[{index}] canonical_name required")
        previous = names.get(name_key)
        if previous and previous != code:
            raise ValueError(f"duplicate normalized category name: {canonical_name}")
        names[name_key] = code
        raw_aliases = entry.get("aliases")
        if not isinstance(raw_aliases, list):
            raise ValueError(f"categories[{index}] aliases must be an array")
        source_metadata = entry.get("source_metadata", {})
        if not isinstance(source_metadata, dict):
            raise ValueError(f"categories[{index}] source_metadata must be an object")
        for alias in [source_name, canonical_name, *raw_aliases]:
            alias_value = str(alias or "").strip()
            alias_key = normalize_category_name(alias_value)
            if not alias_key:
                raise ValueError(f"categories[{index}] contains an empty alias")
            owner = aliases.get(alias_key)
            if owner and owner != code:
                raise ValueError(f"duplicate normalized category alias: {alias_value}")
            aliases[alias_key] = code

    digest = content_hash(category_manifest_content(manifest))
    if manifest.get("manifest_content_hash") != digest:
        raise ValueError("category catalog manifest_content_hash mismatch")
    if manifest.get("manifest_id") != "ccm_" + digest.removeprefix("sha256:"):
        raise ValueError("category catalog manifest_id mismatch")
