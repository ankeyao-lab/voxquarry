#!/usr/bin/env python3
"""Build a tiny synthetic acceptance database for the v2 read service."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retail_landscape.category_registry import (
    CATEGORY_CATALOG_SCHEMA_VERSION,
    CATEGORY_CATALOG_SOURCE_TYPE,
    finalize_category_manifest,
)
from retail_landscape.store import RetailStore, content_hash


def synthetic_57_category_manifest() -> dict[str, object]:
    """Return production-scale synthetic facts; no production names are embedded."""
    categories: list[dict[str, object]] = [
        {
            "source_category_id": "fixture-wotu-wet-dry-vacuum",
            "source_name": "Wet/Dry Vacuum",
            "canonical_code": "wet_dry_vacuum",
            "category_id": "cat_wet_dry_vacuum",
            "canonical_name": "Wet/Dry Vacuum",
            "source_created_at": "2020-01-01T00:00:00+00:00",
            "source_updated_at": "2026-08-17T00:00:00+00:00",
            "aliases": [],
        },
        {
            "source_category_id": "fixture-wotu-light",
            "source_name": "LED Lighting",
            "canonical_code": "light",
            "category_id": "cat_light",
            "canonical_name": "Light",
            "source_created_at": "2020-01-01T00:00:00+00:00",
            "source_updated_at": "2026-08-17T00:00:00+00:00",
            "aliases": [],
        },
        {
            "source_category_id": "fixture-wotu-fan",
            "source_name": "Fan",
            "canonical_code": "fan",
            "category_id": "cat_fan",
            "canonical_name": "Fan",
            "source_created_at": "2020-01-01T00:00:00+00:00",
            "source_updated_at": "2026-08-17T00:00:00+00:00",
            "aliases": [],
        },
        {
            "source_category_id": "fixture-wotu-pressure-washer",
            "source_name": "Pressure Washer",
            "canonical_code": "pressure_washer",
            "category_id": "cat_pressure_washer",
            "canonical_name": "Pressure Washer",
            "source_created_at": "2020-01-01T00:00:00+00:00",
            "source_updated_at": "2026-08-17T00:00:00+00:00",
            "aliases": [],
        },
    ]
    categories.extend({
        "source_category_id": f"fixture-wotu-{index:03d}",
        "source_name": f"Synthetic governed category {index:03d}",
        "source_created_at": "2020-01-01T00:00:00+00:00",
        "source_updated_at": "2026-08-17T00:00:00+00:00",
        "aliases": [],
    } for index in range(1, 54))
    manifest = {
        "schema_version": CATEGORY_CATALOG_SCHEMA_VERSION,
        "source_system": "wotu",
        "source_type": CATEGORY_CATALOG_SOURCE_TYPE,
        "source_reference": "synthetic-fixture://wotu/master_categories",
        "source_snapshot_hash": content_hash(categories),
        "observed_at": "2026-08-17T00:00:00+00:00",
        "approved_by": "synthetic_fixture_approver",
        "approved_at": "2026-08-17T00:00:00+00:00",
        "expected_category_count": len(categories),
        "categories": categories,
    }
    return finalize_category_manifest(manifest, content_hash=content_hash)


def build_acceptance_fixture(database: Path, *, seal_read_only: bool = False) -> dict[str, object]:
    if database.exists():
        raise FileExistsError(f"acceptance fixture database already exists: {database}")
    store = RetailStore.initialize_database(database)
    category_manifest = synthetic_57_category_manifest()
    category_import = store.import_category_catalog(
        category_manifest,
        actor="synthetic_fixture_builder",
    )

    def approved(brand: str, model: str, voltage: str) -> dict[str, object]:
        created = store.create_product({
            "category_code": "wet_dry_vacuum",
            "brand": brand,
            "model_number": model,
            "product_name": f"Synthetic acceptance {brand} {model}",
            "energy_type": "DC",
            "voltage_platform": voltage,
            "source_url": f"https://fixture.invalid/{model.casefold()}",
            "source_type": "synthetic_acceptance_fixture",
            "source_snapshot_hash": f"sha256:fixture-{model.casefold()}",
            "observed_at": "2026-08-17T00:00:00+00:00",
        })
        return store.review_product(created["id"], approved=True, reviewer="fixture_builder")

    milwaukee_18 = approved("Milwaukee", "FIX-M18", "18 V")
    other_18 = approved("Fixture Brand", "FIX-F18", "18 volts")
    milwaukee_28 = approved("Milwaukee", "FIX-M28", "28V")
    fan_member = store.create_product({
        "category_code": "fan",
        "brand": "Fixture Fan Brand",
        "model_number": "FIX-FAN-1",
        "product_name": "Synthetic acceptance fan member",
        "energy_type": "AC",
        "source_url": "https://fixture.invalid/fix-fan-1",
        "source_type": "synthetic_acceptance_fixture",
        "source_snapshot_hash": "sha256:fixture-fan-1",
        "observed_at": "2026-08-17T00:00:00+00:00",
    })
    fan_member = store.review_product(
        fan_member["id"], approved=True, reviewer="fixture_builder"
    )
    bundle = store.create_product({
        "category_code": "cross_category_bundle",
        "brand": "Fixture Bundle Co",
        "model_number": "FIX-CROSS-1",
        "product_name": "Synthetic cross-category acceptance bundle",
        "product_kind": "bundle",
        "source_url": "https://fixture.invalid/fix-cross-1",
        "source_type": "synthetic_acceptance_fixture",
        "source_snapshot_hash": "sha256:fixture-cross-1",
        "observed_at": "2026-08-17T00:00:00+00:00",
        "bundle": {
            "scope": "cross_category",
            "members": [
                {"role": "included_product", "member_model_id": milwaukee_18["id"]},
                {"role": "included_product", "member_model_id": fan_member["id"]},
            ],
        },
    })
    bundle = store.review_product(bundle["id"], approved=True, reviewer="fixture_builder")
    projection = store.product_master_projection()
    platforms = {
        item["model_id"]: (item.get("voltage_platform") or {}).get("voltage_platform_id")
        for item in projection["records"]
    }
    result = {
        "fixture_schema": "retail-product-master-acceptance/v1",
        "database": str(database.resolve()),
        "snapshot_id": projection["snapshot_id"],
        "category_manifest_id": category_manifest["manifest_id"],
        "category_import": category_import,
        "model_ids": [item["model_id"] for item in projection["records"]],
        "assertions": {
            "different_brand_18v_platforms": (
                platforms[milwaukee_18["id"]] != platforms[other_18["id"]]
            ),
            "same_brand_different_voltage_platforms": (
                platforms[milwaukee_18["id"]] != platforms[milwaukee_28["id"]]
            ),
            "cross_category_ineligible": not bundle["analysis_eligibility"]["eligible"],
            "cross_category_reason": bundle["analysis_eligibility"]["reason"],
        },
    }
    if not all([
        result["assertions"]["different_brand_18v_platforms"],
        result["assertions"]["same_brand_different_voltage_platforms"],
        result["assertions"]["cross_category_ineligible"],
        result["assertions"]["cross_category_reason"] == "cross_category_bundle",
    ]):
        raise RuntimeError("synthetic acceptance fixture invariant failed")
    if seal_read_only:
        os.chmod(database, 0o440)
        result["sealed_mode"] = "0440"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a synthetic Chengmu v2 acceptance database")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--seal-read-only", action="store_true")
    args = parser.parse_args()
    result = build_acceptance_fixture(args.database, seal_read_only=args.seal_read_only)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
