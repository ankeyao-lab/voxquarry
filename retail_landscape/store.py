from __future__ import annotations

import hashlib
import json
import stat
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from .category_registry import (
    CATEGORY_REGISTRY_VERSION,
    derive_wotu_category_code,
    normalize_category_name,
    validate_category_manifest,
)
from .field_catalog import CATEGORIES, all_fields


ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"
PRODUCT_COLUMNS = {
    "brand",
    "model_number",
    "product_name",
    "product_family",
    "platform_series",
    "power_source_type",
    "energy_type",
    "voltage_platform",
    "notes",
    "source_type",
    "source_url",
    "source_snapshot_hash",
    "observed_at",
    "product_kind",
    "review_status",
}
PREVIEW_WRITABLE_STATUSES = {"draft", "pending_review"}
VALID_ASSET_ROLES = {"hero", "gallery", "document", "video", "other"}
VALID_ENERGY_TYPES = {"Gas", "AC", "DC"}
VALID_PRODUCT_KINDS = {"single_product", "bundle"}
VALID_BUNDLE_SCOPES = {"same_category", "cross_category"}
VALID_BUNDLE_MEMBER_ROLES = {
    "primary_product", "included_product", "battery", "charger", "accessory", "other",
}
ISO_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
PRODUCT_MASTER_SCHEMA_VERSION = "retail-product-master/v2"
PRODUCT_MASTER_HASH_ALGORITHM = "sha256-canonical-json/v1"
ANALYSIS_POLICY_VERSION = "retail-analysis-eligibility/v1"
ANALYSIS_INELIGIBLE_REASONS = {
    "cross_category_bundle",
    "bundle_primary_unresolved",
    "bundle_primary_category_mismatch",
}

MARKET_REGION_SEEDS = [
    ("mr_na", "north_america", "North America", "北美", 10),
    ("mr_eu", "europe", "Europe", "欧洲", 20),
    ("mr_au", "australia", "Australia", "澳洲", 30),
    ("mr_asia", "asia", "Asia", "亚洲", 40),
]

RETAILER_SEEDS = [
    ("rtl_homedepot", "home_depot", "Home Depot", ["homedepot.com"]),
    ("rtl_lowes", "lowes", "Lowe's", ["lowes.com"]),
    ("rtl_ace", "ace_hardware", "Ace Hardware", ["acehardware.com"]),
    ("rtl_amazon", "amazon", "Amazon", ["amazon.com"]),
    ("rtl_tractorsupply", "tractor_supply", "Tractor Supply", ["tractorsupply.com"]),
    ("rtl_ryobi", "ryobi_official", "RYOBI Official", ["ryobitools.com", "ryobitools.eu"]),
    ("rtl_ego", "ego_official", "EGO Official", ["egopowerplus.com"]),
    ("rtl_einhell", "einhell_official", "Einhell Official", ["einhell.de", "einhell.com"]),
    ("rtl_milwaukee", "milwaukee_official", "Milwaukee Official", ["milwaukeetool.com"]),
    ("rtl_dewalt", "dewalt_official", "DEWALT Official", ["dewalt.com"]),
    ("rtl_craftsman", "craftsman_official", "CRAFTSMAN Official", ["craftsman.com"]),
    ("rtl_portercable", "porter_cable_official", "PORTER-CABLE Official", ["portercable.com"]),
]

UNIT_SEEDS = [
    ("unit_v", "volt", "伏特", "V", "voltage"),
    ("unit_a", "ampere", "安培", "A", "current"),
    ("unit_w", "watt", "瓦特", "W", "power"),
    ("unit_gal", "us_gallon", "美制加仑", "gal", "volume"),
    ("unit_hp", "horsepower", "马力", "HP", "power"),
    ("unit_cfm", "cfm", "立方英尺每分钟", "CFM", "air_flow"),
    ("unit_inh2o", "inch_water", "英寸水柱", "in.H2O", "pressure"),
    ("unit_aw", "air_watt", "空气瓦特", "AW", "suction_power"),
    ("unit_in", "inch", "英寸", "in", "length"),
    ("unit_ft", "foot", "英尺", "ft", "length"),
    ("unit_mph", "mph", "英里每小时", "MPH", "speed"),
    ("unit_db", "decibel", "分贝", "dB", "sound"),
    ("unit_lb", "pound", "磅", "lb", "mass"),
    ("unit_min", "minute", "分钟", "min", "duration"),
    ("unit_year", "year", "年", "year", "duration"),
    ("unit_count", "count", "计数", "count", "count"),
    ("unit_psi", "psi", "磅力每平方英寸", "PSI", "pressure"),
    ("unit_bar", "bar", "巴", "bar", "pressure"),
    ("unit_gpm", "gpm", "美制加仑每分钟", "GPM", "flow"),
    ("unit_lpm", "lpm", "升每分钟", "L/min", "flow"),
    ("unit_l", "litre", "升", "L", "volume"),
    ("unit_m", "metre", "米", "m", "length"),
    ("unit_kg", "kilogram", "千克", "kg", "mass"),
    ("unit_ah", "amp_hour", "安时", "Ah", "charge"),
    ("unit_wh", "watt_hour", "瓦时", "Wh", "energy"),
    ("unit_degree", "degree", "度", "degree", "angle"),
    ("unit_dba", "dba", "A计权分贝", "dBA", "sound"),
    ("unit_kmh", "kmh", "千米每小时", "km/h", "speed"),
    ("unit_gph", "gph", "美制加仑每小时", "GPH", "flow"),
    ("unit_lph", "lph", "升每小时", "L/h", "flow"),
]


StoreRuntimeMode = Literal["read_write", "read_only"]


class ReadOnlyStorePreconditionError(RuntimeError):
    """The immutable product-master reader has no safe database to serve."""


def expected_migration_versions() -> set[str]:
    return {migration.stem for migration in MIGRATIONS.glob("*.sql")}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(str(part).strip().casefold() for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def canonical_json(value: Any) -> str:
    """VoxQuarry canonical JSON v1 used by content-addressed contracts.

    Objects sort keys by Unicode code point, strings use JSON escaping without
    ASCII coercion, arrays preserve order, and finite decimals use non-exponent
    shortest fixed notation (so 18 and 18.0 hash identically).
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("canonical JSON does not allow NaN or Infinity")
        decimal = Decimal(str(value))
        if decimal == 0:
            return "0"
        fixed = format(decimal.normalize(), "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return "{" + ",".join(
            f"{canonical_json(key)}:{canonical_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def content_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


def product_master_record_invariant_errors(record: dict[str, Any]) -> list[str]:
    """Return stable error codes for cross-field v2 contract violations.

    JSON Schema carries the same invariants for consumers. Keeping this small
    runtime guard makes the approved projection fail closed before hashing if a
    future implementation change produces an internally inconsistent record.
    Evidence completeness is deliberately not checked here: nullable evidence
    is part of the reusable data shape, while approval enforces completeness.
    """
    errors: list[str] = []
    analysis = record.get("analysis_eligibility") or {}
    eligible = analysis.get("eligible")
    reason = analysis.get("reason")
    if eligible is True and reason is not None:
        errors.append("eligible_reason_must_be_null")
    if eligible is False and reason not in ANALYSIS_INELIGIBLE_REASONS:
        errors.append("ineligible_reason_required")

    product_kind = record.get("product_kind")
    bundle = record.get("bundle")
    category_code = (record.get("category") or {}).get("code")
    if product_kind == "single_product":
        if bundle is not None:
            errors.append("single_product_bundle_must_be_null")
        if eligible is not True or reason is not None:
            errors.append("single_product_must_be_eligible")
    elif product_kind == "bundle" and not isinstance(bundle, dict):
        errors.append("bundle_definition_required")

    if isinstance(bundle, dict):
        scope = bundle.get("scope")
        if (scope == "cross_category") != (category_code == "cross_category_bundle"):
            errors.append("cross_category_scope_mismatch")
        if scope == "cross_category" and (
            eligible is not False or reason != "cross_category_bundle"
        ):
            errors.append("cross_category_must_be_ineligible")
        for member in bundle.get("members") or []:
            status = member.get("resolution_status")
            member_model_id = member.get("member_model_id")
            member_label = member.get("member_label")
            if status == "resolved" and not member_model_id:
                errors.append("resolved_member_model_id_required")
            if status == "unresolved":
                if member_model_id is not None:
                    errors.append("unresolved_member_model_id_must_be_null")
                if not isinstance(member_label, str) or not member_label.strip():
                    errors.append("unresolved_member_label_required")
    return errors


def normalize_brand_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.strip().casefold().split())


def preview_review_status(value: Any, *, entity_name: str, default: str = "draft") -> str:
    """Validate writes accepted by the unauthenticated stage-1 preview API."""
    status = str(value or default).strip()
    if status not in PREVIEW_WRITABLE_STATUSES:
        raise ValueError(
            f"{entity_name} 在阶段 1 仅允许 draft/pending_review；"
            "approved/published 必须通过人工审核发布流程"
        )
    return status


def normalize_voltage(value: Any, energy_type: str | None) -> str | None:
    if energy_type != "DC":
        return None
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:V|Volt(?:s)?)?\b", text, re.IGNORECASE)
    if not match:
        raise ValueError("DC 电压平台必须是标准电压，如 18V 或 56V")
    number = float(match.group(1))
    if number <= 0:
        raise ValueError("DC 电压平台必须大于 0V")
    return f"{int(number) if number.is_integer() else number:g}V"


def numeric_value(value: Any, *, field_label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_label} 的参数值必须是数字")
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_label} 的参数值必须是数字") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise ValueError(f"{field_label} 的参数值必须是有限数字")
    return result


def currency_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if not ISO_CURRENCY_CODE.fullmatch(code):
        raise ValueError("零售价币种必须是三位 ISO 代码，如 USD、EUR 或 CNY")
    return code


def retail_price_value(value: Any) -> float:
    amount = numeric_value(value, field_label="零售价")
    if amount < 0:
        raise ValueError("零售价不能为负数")
    return amount


class RetailStore:
    """Retail persistence boundary.

    ``read_write`` is deliberately the default for the local CRUD preview and
    the controlled initializer.  A product-master runtime must pass
    ``read_only`` explicitly: that mode never creates a directory, applies a
    migration, seeds a catalog, or performs identity backfill.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        runtime_mode: StoreRuntimeMode = "read_write",
        require_filesystem_read_only: bool = False,
        required_category_manifest_id: str | None = None,
    ):
        self.path = Path(path)
        if runtime_mode not in {"read_write", "read_only"}:
            raise ValueError(f"unsupported retail store runtime_mode: {runtime_mode}")
        self.runtime_mode: StoreRuntimeMode = runtime_mode
        self.require_filesystem_read_only = require_filesystem_read_only
        self.required_category_manifest_id = required_category_manifest_id
        if runtime_mode == "read_only":
            self._verify_read_only_preconditions()
        else:
            self.initialize()

    @classmethod
    def initialize_database(cls, path: Path | str) -> "RetailStore":
        """Run the controlled, idempotent migration/seed/backfill phase.

        This is the only entry point intended for a one-shot init or migration
        job.  Runtime readers must use ``runtime_mode='read_only'`` instead.
        """
        return cls(path, runtime_mode="read_write")

    def initialize(self) -> None:
        """Apply local schema/catalog/identity prerequisites in one transaction phase."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self._seed_catalog()
        self._backfill_product_master_identities()

    def connect(self) -> sqlite3.Connection:
        if self.runtime_mode == "read_only":
            if not self.path.is_file():
                raise ReadOnlyStorePreconditionError(
                    f"read-only retail store database is missing: {self.path}"
                )
            try:
                conn = sqlite3.connect(
                    f"{self.path.resolve().as_uri()}?mode=ro",
                    uri=True,
                )
            except sqlite3.Error as exc:
                raise ReadOnlyStorePreconditionError(
                    f"cannot open retail store database read-only: {self.path}"
                ) from exc
        else:
            conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.runtime_mode == "read_only":
            conn.execute("PRAGMA query_only = ON")
        return conn

    def _verify_read_only_preconditions(self) -> None:
        """Fail closed rather than repairing a candidate runtime database."""
        if not self.path.is_file():
            raise ReadOnlyStorePreconditionError(
                f"read-only retail store database is missing: {self.path}; run the controlled initializer"
            )
        if self.require_filesystem_read_only:
            write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            if self.path.stat().st_mode & write_bits:
                raise ReadOnlyStorePreconditionError(
                    "read-only retail store database still has filesystem write bits; "
                    "mount or seal the runtime data file read-only"
                )
        try:
            with self.connect() as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise ReadOnlyStorePreconditionError(
                        f"read-only retail store integrity_check failed: {integrity}"
                    )
                actual_migrations = {
                    str(row["version"])
                    for row in conn.execute("SELECT version FROM schema_migrations")
                }
                expected_migrations = expected_migration_versions()
                missing_migrations = sorted(expected_migrations - actual_migrations)
                unexpected_migrations = sorted(actual_migrations - expected_migrations)
                if missing_migrations or unexpected_migrations:
                    detail = []
                    if missing_migrations:
                        detail.append("missing=" + ",".join(missing_migrations))
                    if unexpected_migrations:
                        detail.append("unexpected=" + ",".join(unexpected_migrations))
                    raise ReadOnlyStorePreconditionError(
                        "read-only retail store schema is not the expected version ("
                        + "; ".join(detail)
                        + "); run the controlled initializer with the matching release"
                    )
                violations = self._read_only_precondition_violations(conn)
        except ReadOnlyStorePreconditionError:
            raise
        except sqlite3.Error as exc:
            raise ReadOnlyStorePreconditionError(
                "read-only retail store is not initialized with the required schema; "
                "run the controlled initializer"
            ) from exc
        if violations:
            raise ReadOnlyStorePreconditionError(
                "read-only retail store prerequisite check failed: " + "; ".join(violations)
                + "; run the controlled initializer"
            )

    def _read_only_precondition_violations(self, conn: sqlite3.Connection) -> list[str]:
        """Return concise, non-mutating readiness failures for a reader."""
        violations: list[str] = []
        expected_seed_sets = {
            "market_regions": {item[1] for item in MARKET_REGION_SEEDS},
            "retailers": {item[1] for item in RETAILER_SEEDS},
            "units": {item[1] for item in UNIT_SEEDS},
            "categories": {str(item["code"]) for item in CATEGORIES},
        }
        for table, expected_codes in expected_seed_sets.items():
            found_codes = {
                str(row["code"])
                for row in conn.execute(f"SELECT code FROM {table}")
            }
            missing = sorted(expected_codes - found_codes)
            if missing:
                violations.append(f"{table}.missing=" + ",".join(missing))

        expected_fields = {
            (str(item["category_code"]), str(item["code"]))
            for item in all_fields()
        }
        actual_fields = {
            (str(row["category_code"]), str(row["code"]))
            for row in conn.execute(
                """
                SELECT c.code AS category_code, f.code
                FROM field_definitions f JOIN categories c ON c.id=f.category_id
                """
            )
        }
        missing_fields = sorted(expected_fields - actual_fields)
        if missing_fields:
            preview = ",".join(f"{category}.{code}" for category, code in missing_fields[:5])
            suffix = "..." if len(missing_fields) > 5 else ""
            violations.append(f"field_definitions.missing={preview}{suffix}")

        registry_gaps = conn.execute(
            """
            SELECT COUNT(*) FROM categories c
            LEFT JOIN category_source_mappings m
              ON m.category_id=c.id AND m.active=1
            WHERE c.registry_status!='approved' OR m.category_id IS NULL
            """
        ).fetchone()[0]
        if registry_gaps:
            violations.append(f"categories.registry_lineage_missing={registry_gaps}")
        if self.required_category_manifest_id:
            imported = conn.execute(
                """
                SELECT imported_category_count, expected_category_count
                FROM category_catalog_imports WHERE manifest_id=?
                """,
                (self.required_category_manifest_id,),
            ).fetchone()
            if not imported:
                violations.append(
                    "category_catalog_imports.required_manifest_missing="
                    + self.required_category_manifest_id
                )
            elif imported["imported_category_count"] != imported["expected_category_count"]:
                violations.append(
                    "category_catalog_imports.coverage_mismatch="
                    + self.required_category_manifest_id
                )

        unresolved_brands = conn.execute(
            """
            SELECT COUNT(*) FROM products p
            LEFT JOIN brands b ON b.id=p.brand_id
            WHERE trim(COALESCE(p.brand, '')) != '' AND (p.brand_id IS NULL OR b.id IS NULL)
            """
        ).fetchone()[0]
        if unresolved_brands:
            violations.append(f"products.brand_identity_missing={unresolved_brands}")
        unresolved_platforms = conn.execute(
            """
            SELECT COUNT(*) FROM products p
            LEFT JOIN voltage_platforms vp ON vp.id=p.voltage_platform_id
            WHERE p.energy_type='DC' AND trim(COALESCE(p.voltage_platform, '')) != ''
              AND (p.voltage_platform_id IS NULL OR vp.id IS NULL)
            """
        ).fetchone()[0]
        if unresolved_platforms:
            violations.append(f"products.voltage_platform_identity_missing={unresolved_platforms}")
        cross_category_without_bundle = conn.execute(
            """
            SELECT COUNT(*) FROM products p JOIN categories c ON c.id=p.category_id
            LEFT JOIN product_bundles b ON b.product_id=p.id
            WHERE c.code='cross_category_bundle' AND (p.product_kind!='bundle' OR b.scope!='cross_category')
            """
        ).fetchone()[0]
        if cross_category_without_bundle:
            violations.append(f"products.cross_category_bundle_missing={cross_category_without_bundle}")
        return violations

    def _migrate(self) -> None:
        with self.connect() as conn:
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                version = migration.stem
                exists = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (version,),
                ).fetchone() if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone() else None
                if exists:
                    continue
                conn.executescript(migration.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )

    def _seed_catalog(self) -> None:
        category_ids = {item["code"]: item["id"] for item in CATEGORIES}
        now = utc_now()
        with self.connect() as conn:
            for region_id, code, name, name_zh, sort_order in MARKET_REGION_SEEDS:
                conn.execute(
                    """
                    INSERT INTO market_regions(id, code, name, name_zh, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(code) DO UPDATE SET
                      name=excluded.name, name_zh=excluded.name_zh,
                      sort_order=excluded.sort_order, active=1
                    WHERE market_regions.name IS NOT excluded.name
                       OR market_regions.name_zh IS NOT excluded.name_zh
                       OR market_regions.sort_order IS NOT excluded.sort_order
                       OR market_regions.active IS NOT 1
                    """,
                    (region_id, code, name, name_zh, sort_order),
                )
            for retailer_id, code, name, domains in RETAILER_SEEDS:
                conn.execute(
                    """
                    INSERT INTO retailers(id, code, name, domains_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(code) DO UPDATE SET
                      name=excluded.name, domains_json=excluded.domains_json,
                      active=1, updated_at=excluded.updated_at
                    WHERE retailers.name IS NOT excluded.name
                       OR retailers.domains_json IS NOT excluded.domains_json
                       OR retailers.active IS NOT 1
                    """,
                    (retailer_id, code, name, json.dumps(domains), now, now),
                )
            for unit_id, code, name, symbol, dimension in UNIT_SEEDS:
                conn.execute(
                    """
                    INSERT INTO units(id, code, name, symbol, dimension, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(code) DO UPDATE SET
                      name=excluded.name, symbol=excluded.symbol,
                      dimension=excluded.dimension, active=1, updated_at=excluded.updated_at
                    WHERE units.name IS NOT excluded.name
                       OR units.symbol IS NOT excluded.symbol
                       OR units.dimension IS NOT excluded.dimension
                       OR units.active IS NOT 1
                    """,
                    (unit_id, code, name, symbol, dimension, now, now),
                )
            for item in CATEGORIES:
                conn.execute(
                    """
                    INSERT INTO categories(
                      id, code, name, name_zh, description, enabled,
                      registry_status, registry_version, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?)
                    ON CONFLICT(code) DO UPDATE SET
                      name=excluded.name, name_zh=excluded.name_zh,
                      description=excluded.description, enabled=excluded.enabled,
                      registry_status='approved', registry_version=excluded.registry_version,
                      updated_at=excluded.updated_at
                    WHERE categories.name IS NOT excluded.name
                       OR categories.name_zh IS NOT excluded.name_zh
                       OR categories.description IS NOT excluded.description
                       OR categories.enabled IS NOT excluded.enabled
                       OR categories.registry_status IS NOT 'approved'
                       OR categories.registry_version IS NOT excluded.registry_version
                    """,
                    (
                        item["id"], item["code"], item["name"], item["name_zh"],
                        item["description"], 1 if item.get("enabled", True) else 0,
                        CATEGORY_REGISTRY_VERSION, now,
                    ),
                )
                stored = conn.execute(
                    "SELECT id FROM categories WHERE code=?", (item["code"],)
                ).fetchone()
                if not stored or stored["id"] != item["id"]:
                    raise ValueError(
                        f"built-in category identity conflict: {item['code']}"
                    )
                source_hash = content_hash({
                    "registry_version": CATEGORY_REGISTRY_VERSION,
                    "category": item,
                })
                conn.execute(
                    """
                    INSERT INTO category_source_mappings(
                      source_system, source_category_id, category_id, source_name,
                      source_name_key, source_created_at, source_updated_at,
                      source_record_json, source_snapshot_hash, observed_at, manifest_id,
                      active, created_at, updated_at
                    ) VALUES ('chengmu_field_catalog', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)
                    ON CONFLICT(source_system, source_category_id) DO UPDATE SET
                      source_name=excluded.source_name,
                      source_name_key=excluded.source_name_key,
                      source_updated_at=excluded.source_updated_at,
                      source_record_json=excluded.source_record_json,
                      source_snapshot_hash=excluded.source_snapshot_hash,
                      observed_at=excluded.observed_at,
                      active=1,
                      updated_at=excluded.updated_at
                    WHERE category_source_mappings.category_id=excluded.category_id
                      AND (
                        category_source_mappings.source_name IS NOT excluded.source_name
                        OR category_source_mappings.source_name_key IS NOT excluded.source_name_key
                        OR category_source_mappings.source_record_json IS NOT excluded.source_record_json
                        OR category_source_mappings.source_snapshot_hash IS NOT excluded.source_snapshot_hash
                        OR category_source_mappings.active IS NOT 1
                      )
                    """,
                    (
                        item["code"], item["id"], item["name"],
                        normalize_category_name(item["name"]), now, now,
                        json.dumps(item, ensure_ascii=False, sort_keys=True),
                        source_hash, now, now, now,
                    ),
                )
                stored_mapping = conn.execute(
                    """
                    SELECT category_id FROM category_source_mappings
                    WHERE source_system='chengmu_field_catalog' AND source_category_id=?
                    """,
                    (item["code"],),
                ).fetchone()
                if not stored_mapping or stored_mapping["category_id"] != item["id"]:
                    raise ValueError(
                        f"built-in category source mapping conflict: {item['code']}"
                    )
                for alias in [item["name"], *item.get("source_categories", [])]:
                    alias_value = str(alias).strip()
                    alias_key = normalize_category_name(alias_value)
                    existing_alias = conn.execute(
                        "SELECT category_id FROM category_aliases "
                        "WHERE source_system='chengmu_field_catalog' AND alias_key=?",
                        (alias_key,),
                    ).fetchone()
                    if existing_alias and existing_alias["category_id"] != item["id"]:
                        raise ValueError(f"built-in category alias conflict: {alias_value}")
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO category_aliases(
                          id, category_id, alias, alias_key, source_system,
                          manifest_id, created_at
                        ) VALUES (?, ?, ?, ?, 'chengmu_field_catalog', NULL, ?)
                        """,
                        (
                            stable_id("cta", item["id"], alias_key), item["id"],
                            alias_value, alias_key, now,
                        ),
                    )
            for field in all_fields():
                conn.execute(
                    """
                    INSERT INTO field_definitions(
                      id, category_id, code, label, field_group, value_type,
                      requirement, canonical_unit, source_aliases_json, unit_id, is_matrix, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(category_id, code) DO UPDATE SET
                      label=excluded.label, field_group=excluded.field_group,
                      value_type=excluded.value_type, requirement=excluded.requirement,
                      canonical_unit=excluded.canonical_unit,
                      source_aliases_json=excluded.source_aliases_json,
                      unit_id=COALESCE(field_definitions.unit_id, excluded.unit_id),
                      is_matrix=excluded.is_matrix
                    WHERE field_definitions.label IS NOT excluded.label
                       OR field_definitions.field_group IS NOT excluded.field_group
                       OR field_definitions.value_type IS NOT excluded.value_type
                       OR field_definitions.requirement IS NOT excluded.requirement
                       OR field_definitions.canonical_unit IS NOT excluded.canonical_unit
                       OR field_definitions.source_aliases_json IS NOT excluded.source_aliases_json
                       OR field_definitions.unit_id IS NULL AND excluded.unit_id IS NOT NULL
                       OR field_definitions.is_matrix IS NOT excluded.is_matrix
                    """,
                    (
                        field["id"], category_ids[field["category_code"]], field["code"],
                        field["label"], field["group"], field["value_type"],
                        field["requirement"], field["canonical_unit"],
                        json.dumps(field["source_aliases"], ensure_ascii=False),
                        self._unit_id_for_symbol(
                            conn,
                            field["canonical_unit"] or ("count" if field.get("is_matrix") else None),
                        ),
                        1 if field.get("is_matrix") else 0,
                    ),
                )

    @staticmethod
    def _unit_id_for_symbol(conn: sqlite3.Connection, symbol: str | None) -> str | None:
        if not symbol:
            return None
        row = conn.execute("SELECT id FROM units WHERE lower(symbol)=lower(?)", (symbol,)).fetchone()
        return str(row["id"]) if row else None

    def _resolve_brand_identity(
        self,
        conn: sqlite3.Connection,
        display_name: str,
    ) -> dict[str, Any]:
        canonical_name = " ".join(unicodedata.normalize("NFKC", display_name).strip().split())
        key = normalize_brand_key(canonical_name)
        if not key:
            raise ValueError("品牌不能为空")
        row = conn.execute(
            """
            SELECT b.* FROM brand_aliases a
            JOIN brands b ON b.id=a.brand_id
            WHERE a.alias_key=? AND b.active=1
            """,
            (key,),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM brands WHERE normalized_key=? AND active=1",
                (key,),
            ).fetchone()
        if row:
            return dict(row)
        now = utc_now()
        brand_id = stable_id("brd", "brand/v1", key)
        conn.execute(
            """
            INSERT INTO brands(id, canonical_name, normalized_key, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (brand_id, canonical_name, key, now, now),
        )
        return dict(conn.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone())

    def _resolve_voltage_platform(
        self,
        conn: sqlite3.Connection,
        brand_id: str | None,
        standard_voltage: str | None,
    ) -> dict[str, Any] | None:
        if not brand_id or not standard_voltage:
            return None
        row = conn.execute(
            "SELECT * FROM voltage_platforms WHERE brand_id=? AND standard_voltage=?",
            (brand_id, standard_voltage),
        ).fetchone()
        if row:
            return dict(row)
        number = float(standard_voltage.removesuffix("V"))
        platform_id = stable_id(
            "vpf", "brand-voltage/v1", brand_id, standard_voltage,
        )
        conn.execute(
            """
            INSERT INTO voltage_platforms(
              id, brand_id, standard_voltage, voltage_value, voltage_unit,
              identity_version, created_at
            ) VALUES (?, ?, ?, ?, 'V', 'brand-voltage/v1', ?)
            """,
            (platform_id, brand_id, standard_voltage, number, utc_now()),
        )
        return dict(conn.execute("SELECT * FROM voltage_platforms WHERE id=?", (platform_id,)).fetchone())

    def _backfill_product_master_identities(self) -> None:
        """Idempotently populate persisted master identities for legacy local rows.

        The production migration runbook executes the same deterministic logic
        only after backup and audit. This constructor operates on the selected
        local database and never reaches an external or production database.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.brand, p.energy_type, p.voltage_platform,
                       p.product_kind, c.code AS category_code
                FROM products p JOIN categories c ON c.id=p.category_id
                """
            ).fetchall()
            for row in rows:
                brand = self._resolve_brand_identity(conn, str(row["brand"]))
                voltage = None
                if row["energy_type"] == "DC" and row["voltage_platform"]:
                    try:
                        voltage = normalize_voltage(row["voltage_platform"], "DC")
                    except ValueError:
                        voltage = None
                platform = self._resolve_voltage_platform(conn, brand["id"], voltage)
                product_kind = (
                    "bundle" if row["category_code"] == "cross_category_bundle"
                    else row["product_kind"]
                )
                conn.execute(
                    """
                    UPDATE products SET brand_id=?, voltage_platform_id=?, product_kind=?
                    WHERE id=? AND (
                      brand_id IS NOT ? OR voltage_platform_id IS NOT ? OR product_kind IS NOT ?
                    )
                    """,
                    (
                        brand["id"], platform["id"] if platform else None, product_kind, row["id"],
                        brand["id"], platform["id"] if platform else None, product_kind,
                    ),
                )
                if row["category_code"] == "cross_category_bundle":
                    now = utc_now()
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO product_bundles(
                          product_id, scope, primary_model_id, created_at, updated_at
                        ) VALUES (?, 'cross_category', NULL, ?, ?)
                        """,
                        (row["id"], now, now),
                    )

    def rename_brand(
        self,
        brand_id: str,
        canonical_name: str,
        *,
        aliases: list[str] | None = None,
        actor: str = "local_user",
    ) -> dict[str, Any]:
        canonical_name = " ".join(unicodedata.normalize("NFKC", canonical_name).strip().split())
        new_key = normalize_brand_key(canonical_name)
        if not new_key:
            raise ValueError("品牌名称不能为空")
        now = utc_now()
        with self.connect() as conn:
            current = conn.execute(
                "SELECT * FROM brands WHERE id=? AND active=1", (brand_id,),
            ).fetchone()
            if not current:
                raise KeyError(brand_id)
            conflict = conn.execute(
                "SELECT id FROM brands WHERE normalized_key=? AND id!=? AND active=1",
                (new_key, brand_id),
            ).fetchone()
            alias_conflict = conn.execute(
                "SELECT brand_id FROM brand_aliases WHERE alias_key=? AND brand_id!=?",
                (new_key, brand_id),
            ).fetchone()
            if conflict or alias_conflict:
                self._audit(
                    conn, "brand", brand_id, "rename_conflict", actor,
                    {"requested_name": canonical_name, "requested_key": new_key},
                )
                conn.commit()
                raise ValueError("品牌规范名或别名已属于另一稳定品牌 ID")
            duplicate_models = conn.execute(
                """
                SELECT category_id, lower(model_number) AS model_key, COUNT(*) AS row_count
                FROM products WHERE brand_id=? AND review_status!='retired'
                GROUP BY category_id, lower(model_number) HAVING COUNT(*) > 1
                LIMIT 1
                """,
                (brand_id,),
            ).fetchone()
            if duplicate_models:
                self._audit(
                    conn, "brand", brand_id, "rename_model_conflict", actor,
                    {
                        "requested_name": canonical_name,
                        "category_id": duplicate_models["category_id"],
                        "model_key": duplicate_models["model_key"],
                    },
                )
                conn.commit()
                raise ValueError("品牌改名会造成同品类型号身份冲突")
            alias_values = [current["canonical_name"], *(aliases or [])]
            for alias in alias_values:
                alias_display = " ".join(unicodedata.normalize("NFKC", str(alias)).strip().split())
                alias_key = normalize_brand_key(alias_display)
                if not alias_key or alias_key == new_key:
                    continue
                owner = conn.execute(
                    "SELECT brand_id FROM brand_aliases WHERE alias_key=?", (alias_key,),
                ).fetchone()
                canonical_owner = conn.execute(
                    "SELECT id FROM brands WHERE normalized_key=? AND id!=? AND active=1",
                    (alias_key, brand_id),
                ).fetchone()
                if (owner and owner["brand_id"] != brand_id) or canonical_owner:
                    self._audit(
                        conn, "brand", brand_id, "alias_conflict", actor,
                        {"alias": alias_display, "alias_key": alias_key},
                    )
                    conn.commit()
                    raise ValueError("品牌别名已属于另一稳定品牌 ID")
                conn.execute(
                    """
                    INSERT INTO brand_aliases(alias_key, brand_id, alias_display, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(alias_key) DO UPDATE SET alias_display=excluded.alias_display
                    """,
                    (alias_key, brand_id, alias_display, now),
                )
            conn.execute(
                "UPDATE brands SET canonical_name=?, normalized_key=?, updated_at=? WHERE id=?",
                (canonical_name, new_key, now, brand_id),
            )
            conn.execute(
                """
                UPDATE products SET brand=?, entity_version=entity_version+1, updated_at=?
                WHERE brand_id=?
                """,
                (canonical_name, now, brand_id),
            )
            self._audit(
                conn, "brand", brand_id, "renamed", actor,
                {
                    "before": current["canonical_name"],
                    "after": canonical_name,
                    "aliases": alias_values,
                },
            )
            result = conn.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
        return dict(result)

    def category(self, code: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM categories WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None

    def import_category_catalog(
        self,
        manifest: dict[str, Any],
        *,
        actor: str,
    ) -> dict[str, Any]:
        """Atomically persist one approved Wotu category-catalog manifest.

        The manifest is immutable and content addressed. Existing category IDs
        and codes never change; normalized name, alias, source-ID, ID, or slug
        conflicts abort the whole import.
        """
        if self.runtime_mode != "read_write":
            raise PermissionError("category catalog import requires the controlled write process")
        actor = actor.strip()
        if not actor:
            raise ValueError("category catalog import actor is required")
        validate_category_manifest(manifest, content_hash=content_hash)
        manifest_id = str(manifest["manifest_id"])
        manifest_hash = str(manifest["manifest_content_hash"])
        source_system = str(manifest["source_system"])
        now = utc_now()
        core_codes = {str(item["code"]) for item in CATEGORIES}

        with self.connect() as conn:
            prior = conn.execute(
                "SELECT manifest_content_hash, result_json FROM category_catalog_imports "
                "WHERE manifest_id=?",
                (manifest_id,),
            ).fetchone()
            if prior:
                if prior["manifest_content_hash"] != manifest_hash:
                    raise ValueError("category catalog manifest_id content conflict")
                result = json.loads(str(prior["result_json"]))
                result["status"] = "already_applied"
                return result

            categories_by_code = {
                str(row["code"]): dict(row)
                for row in conn.execute("SELECT * FROM categories")
            }
            categories_by_id = {
                str(row["id"]): dict(row)
                for row in conn.execute("SELECT * FROM categories")
            }
            alias_owners = {
                (str(row["source_system"]), str(row["alias_key"])): str(row["category_id"])
                for row in conn.execute(
                    "SELECT source_system, alias_key, category_id FROM category_aliases"
                )
            }
            source_mappings = {
                (str(row["source_system"]), str(row["source_category_id"])): dict(row)
                for row in conn.execute("SELECT * FROM category_source_mappings")
            }
            source_name_owners = {
                (str(row["source_system"]), str(row["source_name_key"])): str(row["source_category_id"])
                for row in conn.execute(
                    "SELECT source_system, source_category_id, source_name_key "
                    "FROM category_source_mappings WHERE active=1"
                )
            }

            plans: list[dict[str, Any]] = []
            for entry in sorted(
                manifest["categories"],
                key=lambda item: str(item["source_category_id"]),
            ):
                source_category_id = str(entry["source_category_id"]).strip()
                source_name = str(entry["source_name"]).strip()
                explicit_code = str(entry.get("canonical_code") or "").strip() or None
                supplied_id = str(entry.get("category_id") or "").strip() or None
                canonical_name = str(entry.get("canonical_name") or source_name).strip()
                name_zh = str(entry.get("name_zh") or canonical_name).strip()
                mapping = source_mappings.get((source_system, source_category_id))
                source_name_key = normalize_category_name(source_name)
                if mapping:
                    mapped_category = categories_by_id.get(str(mapping["category_id"]))
                    if not mapped_category:
                        raise ValueError(f"orphan source category mapping: {source_category_id}")
                    code = str(mapped_category["code"])
                    if explicit_code and explicit_code != code:
                        raise ValueError(
                            f"source category code drift: {source_category_id} -> {explicit_code}"
                        )
                elif explicit_code:
                    code = explicit_code
                else:
                    code = derive_wotu_category_code(source_name)
                by_code = categories_by_code.get(code)

                if mapping:
                    category_id = str(mapping["category_id"])
                    mapped_category = categories_by_id.get(category_id)
                    if supplied_id and supplied_id != category_id:
                        raise ValueError(f"source category ID drift: {source_category_id}")
                elif by_code:
                    category_id = str(by_code["id"])
                    if supplied_id and supplied_id != category_id:
                        raise ValueError(f"category slug/ID conflict: {code}")
                else:
                    category_id = supplied_id or stable_id(
                        "cat", CATEGORY_REGISTRY_VERSION, source_system, source_category_id,
                    )
                    collision = categories_by_id.get(category_id)
                    if collision and collision["code"] != code:
                        raise ValueError(f"category ID conflict: {category_id}")

                source_name_owner = source_name_owners.get((source_system, source_name_key))
                if source_name_owner and source_name_owner != source_category_id:
                    raise ValueError(f"source category name conflict: {source_name}")

                alias_values = [source_name, canonical_name, *(entry.get("aliases") or [])]
                if by_code and by_code.get("name"):
                    alias_values.append(str(by_code["name"]))
                alias_rows: list[tuple[str, str]] = []
                for alias in alias_values:
                    alias_value = str(alias).strip()
                    alias_key = normalize_category_name(alias_value)
                    owner = alias_owners.get((source_system, alias_key))
                    if owner and owner != category_id:
                        raise ValueError(f"category alias conflict: {alias_value}")
                    alias_owners[(source_system, alias_key)] = category_id
                    alias_rows.append((alias_value, alias_key))

                plan = {
                    "source_category_id": source_category_id,
                    "source_name": source_name,
                    "source_name_key": source_name_key,
                    "source_created_at": str(entry["source_created_at"]),
                    "source_updated_at": str(entry["source_updated_at"]),
                    "source_record_json": json.dumps(
                        {
                            "source_category_id": source_category_id,
                            "source_name": source_name,
                            "source_created_at": str(entry["source_created_at"]),
                            "source_updated_at": str(entry["source_updated_at"]),
                            "aliases": entry.get("aliases") or [],
                            "source_metadata": entry.get("source_metadata") or {},
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "category_id": category_id,
                    "code": code,
                    "canonical_name": canonical_name,
                    "name_zh": name_zh,
                    "aliases": alias_rows,
                    "is_new": code not in categories_by_code,
                    "is_core": code in core_codes,
                }
                plans.append(plan)
                source_name_owners[(source_system, source_name_key)] = source_category_id
                categories_by_code.setdefault(code, {
                    "id": category_id, "code": code, "name": canonical_name,
                })
                categories_by_id.setdefault(category_id, categories_by_code[code])

            placeholder = {
                "manifest_id": manifest_id,
                "manifest_content_hash": manifest_hash,
                "source_snapshot_hash": manifest["source_snapshot_hash"],
                "expected_category_count": manifest["expected_category_count"],
                "imported_category_count": len(plans),
                "created": sum(1 for plan in plans if plan["is_new"]),
                "matched": sum(1 for plan in plans if not plan["is_new"]),
                "status": "applied",
            }
            conn.execute(
                """
                INSERT INTO category_catalog_imports(
                  manifest_id, schema_version, source_system, source_type,
                  source_reference, source_snapshot_hash, observed_at,
                  approved_by, approved_at, manifest_content_hash,
                  expected_category_count, imported_category_count,
                  result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest_id, manifest["schema_version"], source_system,
                    manifest["source_type"], manifest["source_reference"],
                    manifest["source_snapshot_hash"], manifest["observed_at"],
                    manifest["approved_by"], manifest["approved_at"], manifest_hash,
                    manifest["expected_category_count"], len(plans),
                    json.dumps(placeholder, ensure_ascii=False, sort_keys=True), now,
                ),
            )
            for plan in plans:
                if plan["is_new"]:
                    conn.execute(
                        """
                        INSERT INTO categories(
                          id, code, name, name_zh, description, enabled,
                          registry_status, registry_version, updated_at
                        ) VALUES (?, ?, ?, ?, '', 0, 'approved', ?, ?)
                        """,
                        (
                            plan["category_id"], plan["code"], plan["canonical_name"],
                            plan["name_zh"], CATEGORY_REGISTRY_VERSION, now,
                        ),
                    )
                elif not plan["is_core"]:
                    conn.execute(
                        """
                        UPDATE categories SET name=?, name_zh=?, registry_status='approved',
                          registry_version=?, updated_at=? WHERE id=?
                        """,
                        (
                            plan["canonical_name"], plan["name_zh"],
                            CATEGORY_REGISTRY_VERSION, now, plan["category_id"],
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE categories SET registry_status='approved',
                          registry_version=?, updated_at=? WHERE id=?
                        """,
                        (CATEGORY_REGISTRY_VERSION, now, plan["category_id"]),
                    )

                conn.execute(
                    """
                    INSERT INTO category_source_mappings(
                      source_system, source_category_id, category_id, source_name,
                      source_name_key, source_created_at, source_updated_at,
                      source_record_json, source_snapshot_hash, observed_at, manifest_id,
                      active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(source_system, source_category_id) DO UPDATE SET
                      source_name=excluded.source_name,
                      source_name_key=excluded.source_name_key,
                      source_created_at=excluded.source_created_at,
                      source_updated_at=excluded.source_updated_at,
                      source_record_json=excluded.source_record_json,
                      source_snapshot_hash=excluded.source_snapshot_hash,
                      observed_at=excluded.observed_at,
                      manifest_id=excluded.manifest_id,
                      active=1,
                      updated_at=excluded.updated_at
                    WHERE category_source_mappings.category_id=excluded.category_id
                    """,
                    (
                        source_system, plan["source_category_id"], plan["category_id"],
                        plan["source_name"], plan["source_name_key"],
                        plan["source_created_at"], plan["source_updated_at"],
                        plan["source_record_json"],
                        manifest["source_snapshot_hash"], manifest["observed_at"],
                        manifest_id, now, now,
                    ),
                )
                stored_mapping = conn.execute(
                    """
                    SELECT category_id FROM category_source_mappings
                    WHERE source_system=? AND source_category_id=?
                    """,
                    (source_system, plan["source_category_id"]),
                ).fetchone()
                if not stored_mapping or stored_mapping["category_id"] != plan["category_id"]:
                    raise ValueError(
                        f"source category mapping conflict: {plan['source_category_id']}"
                    )
                for alias_value, alias_key in plan["aliases"]:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO category_aliases(
                          id, category_id, alias, alias_key, source_system,
                          manifest_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            stable_id("cta", plan["category_id"], alias_key),
                            plan["category_id"], alias_value, alias_key,
                            source_system, manifest_id, now,
                        ),
                    )
            self._audit(
                conn, "category_catalog_import", manifest_id, "imported", actor,
                placeholder,
            )
        return placeholder

    def category_catalog_import(self, manifest_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM category_catalog_imports WHERE manifest_id=?",
                (manifest_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json"))
        return result

    def resolve_category_identity(
        self,
        *,
        source_system: str,
        source_category_id: str | None = None,
        source_name: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a governed external identity without display-name fallback."""
        source_system = source_system.strip()
        source_category_id = (source_category_id or "").strip() or None
        source_name = (source_name or "").strip() or None
        if not source_system or not (source_category_id or source_name):
            raise ValueError("source_system and source_category_id or source_name are required")
        with self.connect() as conn:
            by_id = None
            if source_category_id:
                by_id = conn.execute(
                    """
                    SELECT c.* FROM category_source_mappings m
                    JOIN categories c ON c.id=m.category_id
                    WHERE m.source_system=? AND m.source_category_id=?
                      AND m.active=1 AND c.registry_status='approved'
                    """,
                    (source_system, source_category_id),
                ).fetchone()
            by_name = None
            if source_name:
                by_name = conn.execute(
                    """
                    SELECT c.* FROM category_aliases a
                    JOIN categories c ON c.id=a.category_id
                    WHERE a.source_system=? AND a.alias_key=?
                      AND c.registry_status='approved'
                    """,
                    (source_system, normalize_category_name(source_name)),
                ).fetchone()
        if by_id and by_name and by_id["id"] != by_name["id"]:
            raise ValueError("source category ID/name resolve to different Chengmu identities")
        resolved = by_id or by_name
        if not resolved:
            raise KeyError(source_category_id or source_name)
        return dict(resolved)

    def list_categories(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*,
                  COUNT(DISTINCT p.id) AS product_count,
                  COUNT(DISTINCT p.brand_id) AS brand_count,
                  COUNT(DISTINCT l.id) AS listing_count,
                  COUNT(DISTINCT t.task_id) AS task_count,
                  COUNT(DISTINCT r.report_id) AS report_count,
                  COUNT(DISTINCT CASE
                    WHEN p.energy_type='DC' THEN p.voltage_platform_id END
                  ) AS voltage_platform_count,
                  (
                    SELECT COALESCE('/retail/media/' || a.storage_path, a.source_url) FROM product_assets a
                    JOIN products hp ON hp.id=a.product_id
                    WHERE hp.category_id=c.id AND hp.review_status!='retired'
                      AND a.asset_role='hero'
                    ORDER BY CASE a.review_status WHEN 'published' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                      a.sort_order, a.created_at
                    LIMIT 1
                  ) AS hero_image_url,
                  COUNT(DISTINCT CASE WHEN p.review_status='pending_review' THEN p.id END) AS pending_count
                FROM categories c
                LEFT JOIN products p ON p.category_id=c.id AND p.review_status!='retired'
                LEFT JOIN product_listings l ON l.product_id=p.id
                LEFT JOIN review_task_links t ON t.product_listing_id=l.id
                LEFT JOIN report_links r ON r.category_id=c.id
                GROUP BY c.id
                ORDER BY c.enabled DESC,
                  CASE c.code WHEN 'wet_dry_vacuum' THEN 1 WHEN 'light' THEN 2 WHEN 'fan' THEN 3 ELSE 4 END
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def brand_summary(self) -> list[dict[str, Any]]:
        """Return persisted brand identities with their brand-scoped platforms."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT b.id AS brand_id, b.canonical_name AS brand,
                  b.normalized_key AS brand_key,
                  COUNT(DISTINCT p.id) AS product_count,
                  COUNT(DISTINCT p.voltage_platform_id) AS voltage_platform_count
                FROM brands b
                LEFT JOIN products p ON p.brand_id=b.id AND p.review_status!='retired'
                WHERE b.active=1
                GROUP BY b.id
                ORDER BY b.canonical_name COLLATE NOCASE
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            with self.connect() as conn:
                platforms = [dict(platform) for platform in conn.execute(
                    """
                    SELECT DISTINCT vp.id AS platform_id, vp.standard_voltage,
                           vp.voltage_value, vp.voltage_unit, vp.identity_version
                    FROM voltage_platforms vp
                    JOIN products p ON p.voltage_platform_id=vp.id
                    WHERE vp.brand_id=? AND p.review_status!='retired'
                    ORDER BY vp.voltage_value
                    """,
                    (item["brand_id"],),
                ).fetchall()]
            item["platforms"] = platforms
            item["voltage_platforms"] = [platform["standard_voltage"] for platform in platforms]
            result.append(item)
        return result

    def field_definitions(self, category_code: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT f.* FROM field_definitions f
                JOIN categories c ON c.id=f.category_id
                WHERE c.code=? AND f.active=1
                ORDER BY CASE f.field_group WHEN '基础信息' THEN 0 ELSE 1 END, f.rowid
                """,
                (category_code,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source_aliases"] = json.loads(item.pop("source_aliases_json"))
            result.append(item)
        return result

    def market_regions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_regions WHERE active=1 ORDER BY sort_order, name"
            ).fetchall()
        return [dict(row) for row in rows]

    def retailers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM retailers WHERE active=1 ORDER BY name COLLATE NOCASE"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["domains"] = json.loads(item.pop("domains_json"))
            result.append(item)
        return result

    def units(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM units WHERE active=1 ORDER BY dimension, symbol COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def retailer_for_url(self, url: str) -> dict[str, Any] | None:
        host = (urlparse(url).hostname or "").casefold()
        if not host:
            return None
        for retailer in self.retailers():
            if any(host == domain or host.endswith(f".{domain}") for domain in retailer["domains"]):
                return retailer
        return None

    def save_retailer(self, payload: dict[str, Any], retailer_id: str | None = None) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("零售商名称不能为空")
        domains = [
            str(value).casefold().removeprefix("www.")
            for value in payload.get("domains") or []
            if str(value).strip()
        ]
        code = str(payload.get("code") or re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or stable_id("ret", name))
        entity_id = retailer_id or stable_id("rtl", code)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO retailers(id, code, name, domains_json, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  code=excluded.code, name=excluded.name, domains_json=excluded.domains_json,
                  active=1, updated_at=excluded.updated_at
                """,
                (entity_id, code, name, json.dumps(domains), now, now),
            )
        return next(item for item in self.retailers() if item["id"] == entity_id)

    def delete_retailer(self, retailer_id: str) -> None:
        with self.connect() as conn:
            bound = conn.execute(
                "SELECT COUNT(*) FROM product_retailers WHERE retailer_id=?",
                (retailer_id,),
            ).fetchone()[0]
            if bound:
                raise ValueError("零售商已绑定竞品，不能删除")
            conn.execute("UPDATE retailers SET active=0, updated_at=? WHERE id=?", (utc_now(), retailer_id))

    def save_unit(self, payload: dict[str, Any], unit_id: str | None = None) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").strip()
        name = str(payload.get("name") or symbol).strip()
        if not symbol:
            raise ValueError("单位符号不能为空")
        code = str(payload.get("code") or re.sub(r"[^a-z0-9]+", "_", symbol.casefold()).strip("_") or stable_id("unit", symbol))
        entity_id = unit_id or stable_id("unt", code)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO units(id, code, name, symbol, dimension, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  code=excluded.code, name=excluded.name, symbol=excluded.symbol,
                  dimension=excluded.dimension, active=1, updated_at=excluded.updated_at
                """,
                (entity_id, code, name, symbol, payload.get("dimension"), now, now),
            )
        return next(item for item in self.units() if item["id"] == entity_id)

    def delete_unit(self, unit_id: str) -> None:
        with self.connect() as conn:
            bound = conn.execute(
                "SELECT (SELECT COUNT(*) FROM field_definitions WHERE unit_id=?) + "
                "(SELECT COUNT(*) FROM spec_values WHERE unit_id=?)",
                (unit_id, unit_id),
            ).fetchone()[0]
            if bound:
                raise ValueError("单位已绑定参数，不能删除")
            conn.execute("UPDATE units SET active=0, updated_at=? WHERE id=?", (utc_now(), unit_id))

    def save_parameter(
        self,
        category_code: str,
        payload: dict[str, Any],
        field_id: str | None = None,
    ) -> dict[str, Any]:
        category = self.category(category_code)
        if not category:
            raise ValueError("未知品类")
        if category.get("registry_status") != "approved":
            raise ValueError("品类尚未通过受治理目录审核")
        label = str(payload.get("label") or "").strip()
        if not label:
            raise ValueError("参数名称不能为空")
        value_type = str(payload.get("value_type") or "number")
        if value_type not in {"number", "integer"}:
            raise ValueError("矩阵参数类型仅支持整数或浮点数")
        code = str(payload.get("code") or re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_") or stable_id("param", label))
        entity_id = field_id or stable_id("fld", category_code, code)
        unit_id = str(payload.get("unit_id") or "") or None
        if not unit_id:
            raise ValueError("矩阵参数必须选择全局单位")
        with self.connect() as conn:
            canonical_unit = None
            if unit_id:
                unit = conn.execute("SELECT symbol FROM units WHERE id=? AND active=1", (unit_id,)).fetchone()
                if not unit:
                    raise ValueError("未知单位")
                canonical_unit = unit["symbol"]
            conn.execute(
                """
                INSERT INTO field_definitions(
                  id, category_id, code, label, field_group, value_type, requirement,
                  canonical_unit, source_aliases_json, unit_id, is_matrix, active
                ) VALUES (?, ?, ?, ?, '品类规格', ?, 'optional', ?, '[]', ?, 1, 1)
                ON CONFLICT(id) DO UPDATE SET
                  code=excluded.code, label=excluded.label, value_type=excluded.value_type,
                  canonical_unit=excluded.canonical_unit, unit_id=excluded.unit_id,
                  is_matrix=1, active=1
                """,
                (entity_id, category["id"], code, label, value_type, canonical_unit, unit_id),
            )
        return next(item for item in self.field_definitions(category_code) if item["id"] == entity_id)

    def delete_parameter(self, category_code: str, field_id: str) -> None:
        category = self.category(category_code)
        if not category:
            raise ValueError("未知品类")
        with self.connect() as conn:
            bound = conn.execute(
                "SELECT COUNT(*) FROM spec_values WHERE field_definition_id=?",
                (field_id,),
            ).fetchone()[0]
            if bound:
                conn.execute("UPDATE field_definitions SET active=0 WHERE id=?", (field_id,))
            else:
                conn.execute("DELETE FROM field_definitions WHERE id=? AND category_id=?", (field_id, category["id"]))

    def _product_payload(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        product = dict(row)
        listings = [dict(item) for item in conn.execute(
            "SELECT * FROM product_listings WHERE product_id=? ORDER BY market_region, retailer",
            (product["id"],),
        ).fetchall()]
        for listing in listings:
            listing["prices"] = [dict(item) for item in conn.execute(
                "SELECT * FROM price_observations WHERE product_listing_id=? "
                "ORDER BY COALESCE(observed_at, '') DESC, created_at DESC, id",
                (listing["id"],),
            ).fetchall()]
            listing["tasks"] = [dict(item) for item in conn.execute(
                "SELECT * FROM review_task_links WHERE product_listing_id=? "
                "ORDER BY COALESCE(result_observed_at, '') DESC, task_id",
                (listing["id"],),
            ).fetchall()]
        latest_complete_results = []
        task_bound_listings = 0
        for listing in listings:
            tasks = listing["tasks"]
            if not tasks:
                continue
            task_bound_listings += 1
            complete = next((
                task for task in tasks
                if str(task.get("result_status") or "").upper() == "COMPLETE"
                and task.get("unique_reviews") is not None
            ), None)
            if complete:
                latest_complete_results.append(complete)
        spec_rows = conn.execute(
            """
            SELECT f.id AS field_definition_id, f.code, f.label, f.value_type,
                   f.requirement, f.canonical_unit, f.unit_id AS definition_unit_id,
                   f.is_matrix,
                   s.raw_value, s.normalized_value, s.raw_unit, s.normalized_unit,
                   s.numeric_value, s.unit_id, s.value_state, s.review_status, s.source_url,
                   s.source_type, s.source_snapshot_hash, s.observed_at
            FROM field_definitions f
            LEFT JOIN spec_values s ON s.field_definition_id=f.id AND s.product_id=?
            WHERE f.category_id=? AND f.active=1 ORDER BY f.rowid
            """,
            (product["id"], product["category_id"]),
        ).fetchall()
        specs = {item["code"]: dict(item) for item in spec_rows}
        reports = [dict(item) for item in conn.execute(
            "SELECT * FROM report_links WHERE category_id=? ORDER BY generated_at DESC",
            (product["category_id"],),
        ).fetchall()]
        assets = [dict(item) for item in conn.execute(
            "SELECT * FROM product_assets WHERE product_id=? ORDER BY CASE asset_role WHEN 'hero' THEN 0 WHEN 'gallery' THEN 1 WHEN 'video' THEN 2 WHEN 'document' THEN 3 ELSE 4 END, sort_order, created_at",
            (product["id"],),
        ).fetchall()]
        for asset in assets:
            asset["media_url"] = (
                f"/retail/media/{asset['storage_path']}"
                if asset.get("storage_path") else asset.get("source_url")
            )
        markets = [dict(item) for item in conn.execute(
            """
            SELECT m.* FROM market_regions m
            JOIN product_market_regions pm ON pm.market_region_id=m.id
            WHERE pm.product_id=? ORDER BY m.sort_order, m.name
            """,
            (product["id"],),
        ).fetchall()]
        retailers = [dict(item) for item in conn.execute(
            """
            SELECT r.* FROM retailers r
            JOIN product_retailers pr ON pr.retailer_id=r.id
            WHERE pr.product_id=? ORDER BY r.name COLLATE NOCASE
            """,
            (product["id"],),
        ).fetchall()]
        decisions = [dict(item) for item in conn.execute(
            "SELECT * FROM review_decisions WHERE product_id=? ORDER BY created_at DESC",
            (product["id"],),
        ).fetchall()]
        brand_identity_row = conn.execute(
            "SELECT * FROM brands WHERE id=?", (product.get("brand_id"),),
        ).fetchone() if product.get("brand_id") else None
        platform_identity_row = conn.execute(
            "SELECT * FROM voltage_platforms WHERE id=?", (product.get("voltage_platform_id"),),
        ).fetchone() if product.get("voltage_platform_id") else None
        bundle_row = conn.execute(
            "SELECT * FROM product_bundles WHERE product_id=?", (product["id"],),
        ).fetchone()
        bundle_members = [dict(item) for item in conn.execute(
            """
            SELECT bm.*, mp.model_number AS member_model_number,
                   mp.review_status AS member_review_status,
                   mc.id AS member_category_id, mc.code AS member_category_code,
                   mc.name AS member_category_name
            FROM product_bundle_members bm
            LEFT JOIN products mp ON mp.id=bm.member_product_id
            LEFT JOIN categories mc ON mc.id=mp.category_id
            WHERE bm.bundle_product_id=?
            ORDER BY bm.sort_order, bm.id
            """,
            (product["id"],),
        ).fetchall()]
        for member in bundle_members:
            member["role"] = member.pop("member_role")
            member["member_model_id"] = member.pop("member_product_id")
        bundle_payload = None
        if bundle_row:
            bundle_payload = {
                "scope": bundle_row["scope"],
                "primary_model_id": bundle_row["primary_model_id"],
                "members": bundle_members,
            }
        must_have = ["brand", "model_number"]
        review_blockers = [key for key in must_have if not product.get(key)]
        if product.get("category_registry_status") != "approved":
            review_blockers.append("category_registry")
        enrichment_gaps = []
        if not product.get("product_name"):
            enrichment_gaps.append("product_name")
        if not any(item["asset_role"] == "hero" for item in assets):
            enrichment_gaps.append("hero_asset")
        if not markets:
            enrichment_gaps.append("launch_region")
        if not retailers:
            enrichment_gaps.append("retailer")
        if not listings:
            enrichment_gaps.append("online_url")
        if product.get("product_kind") == "single_product" and not product.get("energy_type"):
            enrichment_gaps.append("energy_type")
        if not all(product.get(key) for key in ("source_url", "source_type", "source_snapshot_hash", "observed_at")):
            review_blockers.append("source_evidence")
        listing_evidence_gap = any(
            not all(listing.get(key) for key in (
                "retailer", "market_region", "online_url", "source_type", "source_snapshot_hash", "observed_at",
            ))
            for listing in listings
        )
        if listing_evidence_gap:
            review_blockers.append("listing_evidence")
        evidence_keys = ("source_url", "source_type", "source_snapshot_hash", "observed_at")
        fact_evidence_gaps = [
            {
                "code": spec["code"],
                "label": spec["label"],
                "missing_fields": [key for key in evidence_keys if not spec.get(key)],
            }
            for spec in specs.values()
            if spec.get("normalized_value") not in (None, "")
            and not all(spec.get(key) for key in evidence_keys)
        ]
        if fact_evidence_gaps:
            review_blockers.append("fact_evidence")
        price_evidence_gap = any(
            not all(price.get(key) not in (None, "") for key in (
                "raw_value", "normalized_amount", "currency_code", "source_url",
                "source_type", "source_snapshot_hash", "observed_at",
            ))
            for listing in listings
            for price in listing.get("prices", [])
        )
        if price_evidence_gap:
            review_blockers.append("price_evidence")
        if listings and not any(listing.get("prices") for listing in listings):
            enrichment_gaps.append("retail_price")
        missing_recommended = []
        if product.get("energy_type") == "DC" and not product.get("voltage_platform_id"):
            # v2 explicitly represents a sourced DC model with no verified
            # voltage as voltage_platform=null/reason=voltage_unverified.
            # Never invent a platform, but do not reject the model identity.
            enrichment_gaps.append("voltage_platform")
        if product.get("product_kind") == "bundle":
            if not bundle_payload:
                review_blockers.append("bundle_definition")
            else:
                resolved = [member for member in bundle_members if member["resolution_status"] == "resolved"]
                resolved_categories = {
                    member["member_category_code"] for member in resolved
                    if member.get("member_category_code")
                }
                if bundle_payload["scope"] == "cross_category" and len(resolved_categories) < 2:
                    review_blockers.append("bundle_cross_category_members")
                if bundle_payload["scope"] == "same_category":
                    primary = next((
                        member for member in resolved
                        if member["member_model_id"] == bundle_payload.get("primary_model_id")
                    ), None)
                    if not primary:
                        review_blockers.append("bundle_primary_unresolved")
                    elif primary.get("member_category_id") != product["category_id"]:
                        review_blockers.append("bundle_primary_category")
                unresolved_evidence_gap = any(
                    member["resolution_status"] == "unresolved"
                    and not all(member.get(key) for key in (
                        "member_label", "source_url", "source_type",
                        "source_snapshot_hash", "observed_at",
                    ))
                    for member in bundle_members
                )
                if unresolved_evidence_gap:
                    review_blockers.append("bundle_member_evidence")
                if any(
                    member.get("member_review_status") not in {"approved", "published"}
                    for member in resolved
                ):
                    review_blockers.append("bundle_member_review")
        if product.get("product_kind") == "single_product":
            analysis_eligibility = {
                "eligible": True,
                "reason": None,
                "policy_version": ANALYSIS_POLICY_VERSION,
            }
        elif bundle_payload and bundle_payload["scope"] == "cross_category":
            analysis_eligibility = {
                "eligible": False,
                "reason": "cross_category_bundle",
                "policy_version": ANALYSIS_POLICY_VERSION,
            }
        elif "bundle_primary_unresolved" in review_blockers:
            analysis_eligibility = {
                "eligible": False,
                "reason": "bundle_primary_unresolved",
                "policy_version": ANALYSIS_POLICY_VERSION,
            }
        elif "bundle_primary_category" in review_blockers:
            analysis_eligibility = {
                "eligible": False,
                "reason": "bundle_primary_category_mismatch",
                "policy_version": ANALYSIS_POLICY_VERSION,
            }
        else:
            analysis_eligibility = {
                "eligible": True,
                "reason": None,
                "policy_version": ANALYSIS_POLICY_VERSION,
            }
        current_prices = []
        for listing in listings:
            seen_currencies: set[str] = set()
            for price in listing.get("prices", []):
                code = str(price.get("currency_code") or "")
                if code in seen_currencies:
                    continue
                seen_currencies.add(code)
                current_prices.append({
                    **price,
                    "listing_id": listing["id"],
                    "retailer": listing.get("retailer"),
                    "retailer_id": listing.get("retailer_id"),
                    "online_url": listing.get("online_url"),
                    "listing_market_region": listing.get("market_region"),
                })
        product.update(
            listings=listings,
            specs=specs,
            reports=reports,
            assets=assets,
            brand_identity=dict(brand_identity_row) if brand_identity_row else None,
            voltage_platform_identity=dict(platform_identity_row) if platform_identity_row else None,
            bundle=bundle_payload,
            analysis_eligibility=analysis_eligibility,
            markets=markets,
            retailers=retailers,
            retail_prices=current_prices,
            review_decisions=decisions,
            model_uid=product["id"],
            task_count=sum(len(item["tasks"]) for item in listings),
            review_count=(
                sum(int(item["unique_reviews"]) for item in latest_complete_results)
                if latest_complete_results else None
            ),
            review_source_total=(
                sum(int(item["source_total"]) for item in latest_complete_results if item.get("source_total") is not None)
                if latest_complete_results else None
            ),
            review_count_status=(
                "complete" if task_bound_listings and len(latest_complete_results) == task_bound_listings
                else "partial" if latest_complete_results else "missing"
            ),
            review_count_scope="sum_latest_complete_result_per_listing",
            review_count_as_of=max(
                (str(item.get("result_observed_at") or "") for item in latest_complete_results),
                default=None,
            ) or None,
            report_count=len(reports),
            missing_required=review_blockers,
            review_blockers=review_blockers,
            review_blocker_details={"fact_evidence": fact_evidence_gaps},
            enrichment_gaps=enrichment_gaps,
            missing_recommended=missing_recommended,
            completeness=max(0, round(100 * (7 - len(set(enrichment_gaps))) / 7)),
        )
        return product


    def product_master_projection(
        self,
        category_code: str | None = None,
        *,
        model_ids: list[str] | None = None,
        voltage_platform_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return the approved, content-addressed Chengmu master projection.

        Consumers persist model_id/entity_version/record_snapshot_hash and the
        enclosing snapshot_id. They never read Chengmu's internal tables.
        """
        if category_code and not self.category(category_code):
            raise KeyError(category_code)
        model_ids = sorted(set(model_ids or []))
        voltage_platform_ids = sorted(set(voltage_platform_ids or []))
        with self.connect() as conn:
            if model_ids:
                known = {
                    str(row["id"]) for row in conn.execute(
                        f"SELECT id FROM products WHERE id IN ({','.join('?' for _ in model_ids)})",
                        model_ids,
                    ).fetchall()
                }
                missing = sorted(set(model_ids) - known)
                if missing:
                    raise ValueError(f"unknown model_id: {', '.join(missing)}")
            if voltage_platform_ids:
                known = {
                    str(row["id"]) for row in conn.execute(
                        f"SELECT id FROM voltage_platforms WHERE id IN ({','.join('?' for _ in voltage_platform_ids)})",
                        voltage_platform_ids,
                    ).fetchall()
                }
                missing = sorted(set(voltage_platform_ids) - known)
                if missing:
                    raise ValueError(f"unknown voltage_platform_id: {', '.join(missing)}")
            clauses = ["p.review_status IN ('approved', 'published')"]
            params: list[Any] = []
            if category_code:
                clauses.append("c.code=?")
                params.append(category_code)
            if model_ids:
                clauses.append(f"p.id IN ({','.join('?' for _ in model_ids)})")
                params.extend(model_ids)
            if voltage_platform_ids:
                clauses.append(f"p.voltage_platform_id IN ({','.join('?' for _ in voltage_platform_ids)})")
                params.extend(voltage_platform_ids)
            rows = conn.execute(
                f"""
                SELECT p.*, c.code AS category_code, c.name AS category_name,
                       c.name_zh AS category_name_zh,
                       c.registry_status AS category_registry_status
                FROM products p JOIN categories c ON c.id=p.category_id
                WHERE {' AND '.join(clauses)}
                ORDER BY c.code, lower(p.brand), lower(p.model_number), p.id
                """,
                params,
            ).fetchall()
            products = [self._product_payload(conn, row) for row in rows]
        records: list[dict[str, Any]] = []
        for product in products:
            brand_identity = product.get("brand_identity") or {}
            platform_identity = product.get("voltage_platform_identity")
            if product.get("energy_type") != "DC":
                platform_null_reason = "not_dc"
            elif not brand_identity.get("id"):
                platform_null_reason = "brand_unresolved"
            elif not platform_identity:
                platform_null_reason = "voltage_unverified"
            else:
                platform_null_reason = None
            bundle = product.get("bundle")
            bundle_projection = None
            if bundle:
                bundle_projection = {
                    "scope": bundle["scope"],
                    "primary_model_id": bundle.get("primary_model_id"),
                    "members": [
                        {
                            "role": member["role"],
                            "member_model_id": member.get("member_model_id"),
                            "member_label": member.get("member_label"),
                            "resolution_status": member["resolution_status"],
                            "quantity": member["quantity"],
                            "evidence": {
                                "source_url": member.get("source_url"),
                                "source_type": member.get("source_type"),
                                "source_snapshot_hash": member.get("source_snapshot_hash"),
                                "observed_at": member.get("observed_at"),
                            },
                        }
                        for member in bundle["members"]
                    ],
                }
            record = {
                "model_id": product["id"],
                "entity_version": product["entity_version"],
                "product_kind": product.get("product_kind") or "single_product",
                "category": {
                    "category_id": product["category_id"],
                    "code": product["category_code"],
                    "name": product["category_name"],
                    "name_zh": product["category_name_zh"],
                },
                "brand": {
                    "brand_id": brand_identity.get("id"),
                    "canonical_name": brand_identity.get("canonical_name") or product["brand"],
                },
                "model_number": product["model_number"],
                "product_name": product.get("product_name"),
                "energy_type": product.get("energy_type"),
                "voltage_platform": (
                    {
                        "voltage_platform_id": platform_identity["id"],
                        "brand_id": platform_identity["brand_id"],
                        "canonical_voltage": platform_identity["standard_voltage"],
                        "voltage_value": platform_identity["voltage_value"],
                        "voltage_unit": platform_identity["voltage_unit"],
                        "identity_version": platform_identity["identity_version"],
                    }
                    if platform_identity else None
                ),
                "voltage_platform_null_reason": platform_null_reason,
                "bundle": bundle_projection,
                "analysis_eligibility": product["analysis_eligibility"],
                "review_status": product["review_status"],
                "reviewed_at": product.get("reviewed_at"),
            }
            invariant_errors = product_master_record_invariant_errors(record)
            if invariant_errors:
                raise RuntimeError(
                    f"invalid {PRODUCT_MASTER_SCHEMA_VERSION} record {product['id']}: "
                    + ", ".join(invariant_errors)
                )
            record["record_snapshot_hash"] = content_hash(record)
            records.append(record)
        snapshot_content = {
            "schema_version": PRODUCT_MASTER_SCHEMA_VERSION,
            "authority": "chengmu",
            "scope": {
                "review_status": ["approved", "published"],
                "category_code": category_code,
                "model_ids": model_ids,
                "voltage_platform_ids": voltage_platform_ids,
            },
            "records": records,
        }
        snapshot_digest = content_hash(snapshot_content).removeprefix("sha256:")
        return {
            "schema_version": PRODUCT_MASTER_SCHEMA_VERSION,
            "snapshot_id": f"pms_{snapshot_digest}",
            "snapshot_generated_at": max(
                (str(product.get("updated_at") or "") for product in products),
                default="",
            ) or None,
            "hash_algorithm": PRODUCT_MASTER_HASH_ALGORITHM,
            "authority": "chengmu",
            "scope": snapshot_content["scope"],
            "ordering": "category.code, brand.canonical_name(casefold), model_number(casefold), model_id",
            "pagination": {"mode": "complete", "total_records": len(records)},
            "records": records,
        }

    def product_master_http_response(
        self,
        *,
        category_code: str | None = None,
        model_ids: list[str] | None = None,
        voltage_platform_ids: list[str] | None = None,
        if_none_match: str | None = None,
    ) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
        """Framework-neutral v2 GET semantics used by the HTTP adapter.

        Keeping the ETag/404 decision outside FastAPI lets the storage contract
        be verified in the minimal project runtime, while the adapter remains a
        thin serialization layer.
        """
        try:
            payload = self.product_master_projection(
                category_code,
                model_ids=model_ids,
                voltage_platform_ids=voltage_platform_ids,
            )
        except KeyError:
            return 404, {
                "error": {
                    "code": "category_not_found",
                    "message": "The requested Chengmu category code does not exist.",
                    "category_code": category_code,
                }
            }, {}
        except ValueError as exc:
            message = str(exc)
            field = "voltage_platform_id" if message.startswith("unknown voltage_platform_id") else "model_id"
            return 404, {
                "error": {
                    "code": "master_identity_not_found",
                    "message": message,
                    "field": field,
                }
            }, {}
        etag = f'"{payload["snapshot_id"]}"'
        headers = {
            "ETag": etag,
            "X-VoxQuarry-Schema-Version": payload["schema_version"],
        }
        if if_none_match in {etag, payload["snapshot_id"]}:
            return 304, None, headers
        return 200, payload, headers

    def list_products(
        self,
        category_code: str,
        *,
        q: str = "",
        market_region: str = "",
        review_status: str = "",
        brand: str = "",
        energy_type: str = "",
        voltage_platform: str = "",
        retailer_id: str = "",
    ) -> list[dict[str, Any]]:
        category = self.category(category_code)
        if not category:
            return []
        clauses = ["p.category_id=?", "p.review_status!='retired'"]
        params: list[Any] = [category["id"]]
        if q:
            clauses.append("(lower(p.brand) LIKE ? OR lower(p.model_number) LIKE ? OR lower(COALESCE(p.product_name,'')) LIKE ?)")
            needle = f"%{q.casefold()}%"
            params.extend([needle, needle, needle])
        if review_status:
            clauses.append("p.review_status=?")
            params.append(review_status)
        if brand:
            clauses.append("lower(p.brand)=?")
            params.append(brand.casefold())
        if energy_type:
            clauses.append("p.energy_type=?")
            params.append(energy_type)
        if voltage_platform:
            clauses.append("p.voltage_platform=?")
            params.append(voltage_platform)
        if market_region:
            clauses.append(
                "EXISTS (SELECT 1 FROM product_market_regions pm "
                "JOIN market_regions mr ON mr.id=pm.market_region_id "
                "WHERE pm.product_id=p.id AND (mr.id=? OR mr.name=?))"
            )
            params.extend([market_region, market_region])
        if retailer_id:
            clauses.append("EXISTS (SELECT 1 FROM product_retailers pr WHERE pr.product_id=p.id AND pr.retailer_id=?)")
            params.append(retailer_id)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT p.*, c.code AS category_code, c.name AS category_name, c.name_zh AS category_name_zh, c.registry_status AS category_registry_status FROM products p JOIN categories c ON c.id=p.category_id WHERE {' AND '.join(clauses)} ORDER BY p.brand, p.model_number",
                params,
            ).fetchall()
            return [self._product_payload(conn, row) for row in rows]

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT p.*, c.code AS category_code, c.name AS category_name, c.name_zh AS category_name_zh, c.registry_status AS category_registry_status FROM products p JOIN categories c ON c.id=p.category_id WHERE p.id=?",
                (product_id,),
            ).fetchone()
            return self._product_payload(conn, row) if row else None

    @staticmethod
    def _validate_product_kind(
        category_code: str,
        product_kind: str,
        bundle: Any,
    ) -> None:
        if product_kind not in VALID_PRODUCT_KINDS:
            raise ValueError("product_kind 仅允许 single_product 或 bundle")
        if product_kind == "single_product":
            if category_code == "cross_category_bundle":
                raise ValueError("Cross-category Bundle 品类必须使用 product_kind=bundle")
            if bundle:
                raise ValueError("single_product 不能提交 bundle 关系")
            return
        data = bundle if isinstance(bundle, dict) else {}
        default_scope = "cross_category" if category_code == "cross_category_bundle" else "same_category"
        scope = str(data.get("scope") or default_scope)
        if scope not in VALID_BUNDLE_SCOPES:
            raise ValueError("bundle.scope 仅允许 same_category 或 cross_category")
        if scope == "cross_category" and category_code != "cross_category_bundle":
            raise ValueError("cross_category bundle 必须使用 cross_category_bundle 品类")
        if scope == "same_category" and category_code == "cross_category_bundle":
            raise ValueError("cross_category_bundle 品类不能标记为 same_category")

    def _replace_bundle(
        self,
        conn: sqlite3.Connection,
        product_id: str,
        product_kind: str,
        bundle: Any,
    ) -> None:
        if product_kind == "single_product":
            conn.execute("DELETE FROM product_bundle_members WHERE bundle_product_id=?", (product_id,))
            conn.execute("DELETE FROM product_bundles WHERE product_id=?", (product_id,))
            return
        data = bundle if isinstance(bundle, dict) else {}
        category = conn.execute(
            """
            SELECT c.code FROM products p JOIN categories c ON c.id=p.category_id
            WHERE p.id=?
            """,
            (product_id,),
        ).fetchone()
        if not category:
            raise KeyError(product_id)
        scope = str(data.get("scope") or (
            "cross_category" if category["code"] == "cross_category_bundle" else "same_category"
        ))
        self._validate_product_kind(category["code"], product_kind, {"scope": scope})
        primary_model_id = str(data.get("primary_model_id") or "").strip() or None
        if primary_model_id:
            if primary_model_id == product_id:
                raise ValueError("组套不能将自身作为主型号")
            if not conn.execute(
                "SELECT 1 FROM products WHERE id=? AND review_status!='retired'", (primary_model_id,),
            ).fetchone():
                raise ValueError("bundle.primary_model_id 不存在")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO product_bundles(product_id, scope, primary_model_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
              scope=excluded.scope, primary_model_id=excluded.primary_model_id,
              updated_at=excluded.updated_at
            """,
            (product_id, scope, primary_model_id, now, now),
        )
        conn.execute("DELETE FROM product_bundle_members WHERE bundle_product_id=?", (product_id,))
        for index, raw_member in enumerate(data.get("members") or []):
            if not isinstance(raw_member, dict):
                raise ValueError("bundle.members 必须是结构化对象")
            role = str(raw_member.get("role") or "other").strip()
            if role not in VALID_BUNDLE_MEMBER_ROLES:
                raise ValueError("未知 bundle member role")
            member_model_id = str(raw_member.get("member_model_id") or "").strip() or None
            resolution_status = str(
                raw_member.get("resolution_status")
                or ("resolved" if member_model_id else "unresolved")
            ).strip()
            if resolution_status not in {"resolved", "unresolved"}:
                raise ValueError("bundle member resolution_status 仅允许 resolved/unresolved")
            if resolution_status == "resolved":
                if not member_model_id:
                    raise ValueError("resolved bundle member 必须提供 member_model_id")
                if member_model_id == product_id:
                    raise ValueError("组套不能包含自身")
                if not conn.execute(
                    "SELECT 1 FROM products WHERE id=? AND review_status!='retired'", (member_model_id,),
                ).fetchone():
                    raise ValueError("bundle member_model_id 不存在")
                member_label = str(raw_member.get("member_label") or "").strip() or None
            else:
                if member_model_id:
                    raise ValueError("unresolved bundle member 不得伪造 member_model_id")
                member_label = str(raw_member.get("member_label") or "").strip() or None
                if not member_label:
                    raise ValueError("unresolved bundle member 必须保留原始成员名称")
            if role == "primary_product" and member_model_id:
                if primary_model_id and primary_model_id != member_model_id:
                    raise ValueError("primary_model_id 与 primary_product 成员冲突")
                primary_model_id = member_model_id
            member_id = str(raw_member.get("id") or stable_id(
                "bmb", product_id, role, member_model_id or member_label or str(index),
            ))
            conn.execute(
                """
                INSERT INTO product_bundle_members(
                  id, bundle_product_id, member_role, member_product_id, member_label,
                  resolution_status, quantity, sort_order, source_url, source_type,
                  source_snapshot_hash, observed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    member_id, product_id, role, member_model_id, member_label,
                    resolution_status, int(raw_member.get("quantity") or 1),
                    int(raw_member.get("sort_order", index)), raw_member.get("source_url"),
                    raw_member.get("source_type"), raw_member.get("source_snapshot_hash"),
                    raw_member.get("observed_at"), now,
                ),
            )
        if primary_model_id != data.get("primary_model_id"):
            conn.execute(
                "UPDATE product_bundles SET primary_model_id=?, updated_at=? WHERE product_id=?",
                (primary_model_id, now, product_id),
            )

    def create_product(self, payload: dict[str, Any], *, actor: str = "local_user") -> dict[str, Any]:
        category_code = str(payload.get("category_code") or "").strip()
        brand = str(payload.get("brand") or "").strip()
        model = str(payload.get("model_number") or "").strip()
        if not category_code or not brand or not model:
            raise ValueError("品类、品牌和型号为草稿必填字段")
        category = self.category(category_code)
        if not category:
            raise ValueError("未知品类")
        if category.get("registry_status") != "approved":
            raise ValueError("品类尚未通过受治理目录审核")
        product_kind = str(
            payload.get("product_kind")
            or ("bundle" if category_code == "cross_category_bundle" else "single_product")
        ).strip()
        self._validate_product_kind(category_code, product_kind, payload.get("bundle"))
        now = utc_now()
        status = preview_review_status(
            payload.get("review_status"), entity_name="产品", default="pending_review"
        )
        energy_type = str(payload.get("energy_type") or "").strip() or None
        if energy_type and energy_type not in VALID_ENERGY_TYPES:
            raise ValueError("能源形式仅允许 Gas、AC 或 DC")
        voltage = normalize_voltage(payload.get("voltage_platform"), energy_type)
        with self.connect() as conn:
            brand_identity = self._resolve_brand_identity(conn, brand)
            product_id = str(payload.get("id") or stable_id(
                "pvr", "product-variant/v2", category["id"], brand_identity["id"], model,
            ))
            platform = self._resolve_voltage_platform(conn, brand_identity["id"], voltage)
            conn.execute(
                """
                INSERT INTO products(
                  id, category_id, brand, model_number, product_name, product_family,
                  platform_series, power_source_type, energy_type, voltage_platform, notes, review_status,
                  source_type, source_snapshot_hash, source_url, observed_at,
                  reviewed_by, reviewed_at, created_at, updated_at, brand_id,
                  voltage_platform_id, product_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id, category["id"], brand_identity["canonical_name"], model,
                    payload.get("product_name"), payload.get("product_family"),
                    payload.get("platform_series"), payload.get("power_source_type"),
                    energy_type, voltage, str(payload.get("notes") or ""), status,
                    payload.get("source_type"),
                    payload.get("source_snapshot_hash"), payload.get("source_url"),
                    payload.get("observed_at"), None,
                    None, now, now, brand_identity["id"],
                    platform["id"] if platform else None, product_kind,
                ),
            )
            self._replace_bundle(conn, product_id, product_kind, payload.get("bundle"))
            self._replace_listings(conn, product_id, payload.get("listings") or [])
            self._replace_relationships(conn, product_id, payload)
            self._upsert_specs(conn, product_id, category["id"], payload.get("specs") or {})
            if "assets" in payload:
                self._replace_assets(conn, product_id, payload.get("assets") or [])
            self._audit(conn, "product", product_id, "created", actor, payload)
        return self.get_product(product_id)  # type: ignore[return-value]

    def update_product(self, product_id: str, payload: dict[str, Any], *, actor: str = "local_user") -> dict[str, Any]:
        existing = self.get_product(product_id)
        if not existing:
            raise KeyError(product_id)
        changes = {key: payload[key] for key in PRODUCT_COLUMNS if key in payload}
        product_kind = str(changes.get("product_kind", existing.get("product_kind") or "single_product"))
        self._validate_product_kind(
            existing["category_code"], product_kind,
            payload.get("bundle", existing.get("bundle")),
        )
        if "review_status" in changes:
            changes["review_status"] = preview_review_status(
                changes["review_status"], entity_name="产品"
            )
        energy_type = str(changes.get("energy_type", existing.get("energy_type")) or "").strip() or None
        if energy_type and energy_type not in VALID_ENERGY_TYPES:
            raise ValueError("能源形式仅允许 Gas、AC 或 DC")
        if "energy_type" in changes or "voltage_platform" in changes:
            changes["energy_type"] = energy_type
            changes["voltage_platform"] = normalize_voltage(
                changes.get("voltage_platform", existing.get("voltage_platform")), energy_type
            )
        if changes or any(key in payload for key in ("listings", "market_region_ids", "retailer_ids", "specs", "assets", "bundle")):
            changes["review_status"] = "pending_review"
            changes["reviewed_by"] = None
            changes["reviewed_at"] = None
        now = utc_now()
        with self.connect() as conn:
            brand_name = str(changes.get("brand", existing.get("brand")) or "").strip()
            brand_identity = self._resolve_brand_identity(conn, brand_name)
            platform = self._resolve_voltage_platform(conn, brand_identity["id"], changes.get(
                "voltage_platform", existing.get("voltage_platform")
            )) if energy_type == "DC" else None
            resolved_platform_id = platform["id"] if platform else None
            if "brand" in payload or existing.get("brand_id") != brand_identity["id"]:
                changes["brand"] = brand_identity["canonical_name"]
                changes["brand_id"] = brand_identity["id"]
            if (
                any(key in payload for key in ("brand", "energy_type", "voltage_platform"))
                or existing.get("voltage_platform_id") != resolved_platform_id
            ):
                changes["voltage_platform_id"] = resolved_platform_id
            if "product_kind" in payload or existing.get("product_kind") != product_kind:
                changes["product_kind"] = product_kind
            if changes:
                assignments = ", ".join(f"{key}=?" for key in changes)
                conn.execute(
                    f"UPDATE products SET {assignments}, entity_version=entity_version+1, updated_at=? WHERE id=?",
                    [*changes.values(), now, product_id],
                )
            if "bundle" in payload or "product_kind" in payload:
                self._replace_bundle(conn, product_id, product_kind, payload.get("bundle"))
            if "listings" in payload:
                self._replace_listings(conn, product_id, payload.get("listings") or [])
            self._replace_relationships(conn, product_id, payload)
            self._upsert_specs(conn, product_id, existing["category_id"], payload.get("specs") or {})
            if "assets" in payload:
                self._replace_assets(conn, product_id, payload.get("assets") or [])
            before = {
                key: existing.get(key)
                for key in PRODUCT_COLUMNS
                if key != "review_status"
            }
            before.update({
                "listings": existing.get("listings", []),
                "markets": existing.get("markets", []),
                "retailers": existing.get("retailers", []),
                "specs": existing.get("specs", {}),
                "assets": existing.get("assets", []),
            })
            after = {**before, **{key: value for key, value in changes.items() if key != "review_status"}}
            for key in ("listings", "market_region_ids", "retailer_ids", "specs", "assets"):
                if key in payload:
                    after[key] = payload[key]
            self._audit(
                conn,
                "product",
                product_id,
                "updated",
                actor,
                {"submitted": payload, "before": before, "after": after},
            )
        return self.get_product(product_id)  # type: ignore[return-value]

    def delete_product(self, product_id: str, *, actor: str = "local_user") -> dict[str, Any]:
        existing = self.get_product(product_id)
        if not existing:
            raise KeyError(product_id)
        with self.connect() as conn:
            bundle_references = conn.execute(
                "SELECT COUNT(*) FROM product_bundle_members WHERE member_product_id=?",
                (product_id,),
            ).fetchone()[0]
            if (
                existing["review_status"] in {"draft", "pending_review"}
                and not existing["task_count"]
                and not bundle_references
            ):
                conn.execute("DELETE FROM spec_values WHERE product_id=?", (product_id,))
                conn.execute("DELETE FROM product_assets WHERE product_id=?", (product_id,))
                conn.execute("DELETE FROM product_listings WHERE product_id=?", (product_id,))
                conn.execute("DELETE FROM products WHERE id=?", (product_id,))
                action = "deleted"
            else:
                conn.execute(
                    "UPDATE products SET review_status='retired', entity_version=entity_version+1, updated_at=? WHERE id=?",
                    (utc_now(), product_id),
                )
                action = "retired"
            self._audit(conn, "product", product_id, action, actor, {"previous_status": existing["review_status"]})
        return {"product_id": product_id, "action": action}

    def review_product(
        self,
        product_id: str,
        *,
        approved: bool,
        reviewer: str = "local_preview",
        note: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        product = self.get_product(product_id)
        if not product:
            raise KeyError(product_id)
        if expected_version is not None and product["entity_version"] != expected_version:
            raise ValueError("资料已变化，请刷新审核预览后重试")
        # The local preview deliberately has no account system yet. Keep the
        # database's NOT NULL audit column useful with a stable technical actor;
        # production authentication will replace this value with the user ID.
        reviewer = (reviewer or "").strip() or "local_preview"
        if approved and product["review_blockers"]:
            raise ValueError(f"审核前仍有真实性或身份阻断：{', '.join(product['review_blockers'])}")
        previous = str(product["review_status"])
        target = "approved" if approved else "pending_review"
        now = utc_now()
        decision = "approve" if approved else "reopen"
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE products SET review_status=?, reviewed_by=?, reviewed_at=?,
                  entity_version=entity_version+1, updated_at=? WHERE id=? AND entity_version=?
                """,
                (target, reviewer if approved else None, now if approved else None, now, product_id, product["entity_version"]),
            )
            if updated.rowcount != 1:
                raise ValueError("资料已变化，请刷新审核预览后重试")
            for table in ("product_listings", "spec_values", "product_assets"):
                conn.execute(
                    f"UPDATE {table} SET review_status=? WHERE product_id=?",
                    (target, product_id),
                )
            conn.execute(
                """
                UPDATE price_observations
                SET review_status=?, reviewed_by=?, reviewed_at=?
                WHERE product_listing_id IN (
                  SELECT id FROM product_listings WHERE product_id=?
                )
                """,
                (
                    target, reviewer if approved else None,
                    now if approved else None, product_id,
                ),
            )
            conn.execute(
                "UPDATE product_market_regions SET review_status=? WHERE product_id=?",
                (target, product_id),
            )
            conn.execute(
                "UPDATE product_retailers SET review_status=? WHERE product_id=?",
                (target, product_id),
            )
            decision_id = stable_id(
                "rvd", product_id, str(product["entity_version"]), decision, now, reviewer,
            )
            conn.execute(
                """
                INSERT INTO review_decisions(
                  id, product_id, decision, previous_status, new_status,
                  reviewer, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (decision_id, product_id, decision, previous, target, reviewer, note.strip(), now),
            )
            self._audit(
                conn, "product", product_id, f"review_{decision}", reviewer,
                {"previous_status": previous, "new_status": target, "note": note.strip()},
            )
        return self.get_product(product_id)  # type: ignore[return-value]

    def review_preview(self, product_id: str) -> dict[str, Any]:
        """Return a human-readable diff payload for the model review modal.

        Imported staging rows may have no audit event because they came from a
        read-only snapshot. In that case all currently present facts are shown
        as additions. CRUD updates carry before/after snapshots in their audit
        event, allowing the UI to separate new, changed and removed values.
        """
        product = self.get_product(product_id)
        if not product:
            raise KeyError(product_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT action, payload_json, created_at FROM audit_events "
                "WHERE entity_type='product' AND entity_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (product_id,),
            ).fetchone()
        latest = dict(row) if row else None

        def present(value: Any) -> bool:
            return value not in (None, "", [], {}, "—")

        labels = {
            "brand": "品牌", "model_number": "型号", "product_name": "产品名称",
            "product_family": "产品家族", "platform_series": "平台系列",
            "power_source_type": "动力来源", "energy_type": "能源形式",
            "voltage_platform": "电压平台", "notes": "备注", "listings": "线上 Listing",
            "market_region_ids": "上市地区", "retailer_ids": "零售商",
            "markets": "上市地区", "retailers": "零售商",
            "specs": "规格参数", "assets": "图片与多模态素材",
        }

        market_names = {item["id"]: item.get("name_zh") or item.get("name") for item in self.market_regions()}
        retailer_names = {item["id"]: item.get("name") for item in self.retailers()}
        field_labels = {item["code"]: item.get("label") or item["code"] for item in self.field_definitions(product["category_code"])}

        def display(key: str, value: Any) -> str:
            if key in {"markets", "market_region_ids"}:
                entries = value if isinstance(value, list) else [value]
                names = []
                for entry in entries:
                    if isinstance(entry, dict):
                        names.append(entry.get("name_zh") or entry.get("name") or entry.get("id"))
                    else:
                        names.append(market_names.get(str(entry), str(entry)))
                return "、".join(str(name) for name in names if name)
            if key in {"retailers", "retailer_ids"}:
                entries = value if isinstance(value, list) else [value]
                names = []
                for entry in entries:
                    if isinstance(entry, dict):
                        names.append(entry.get("name") or retailer_names.get(str(entry.get("id")), entry.get("id")))
                    else:
                        names.append(retailer_names.get(str(entry), str(entry)))
                return "、".join(str(name) for name in names if name)
            if key == "listings":
                lines = []
                for listing in value if isinstance(value, list) else []:
                    if not isinstance(listing, dict):
                        continue
                    retailer = listing.get("retailer") or retailer_names.get(str(listing.get("retailer_id"))) or "待识别平台"
                    market = listing.get("market_region") or market_names.get(str(listing.get("market_region_id")))
                    prefix = " · ".join(str(item) for item in (retailer, market) if item)
                    url = listing.get("online_url") or listing.get("source_url") or "待补链接"
                    prices = []
                    for price in listing.get("prices") or []:
                        if not isinstance(price, dict):
                            continue
                        amount = price.get("normalized_amount")
                        code = price.get("currency_code") or ""
                        if amount not in (None, ""):
                            prices.append(f"{code} {amount:g}" if isinstance(amount, float) else f"{code} {amount}")
                    price_text = f" · 零售价 {' / '.join(prices)}" if prices else " · 零售价待补"
                    lines.append(f"{prefix}{price_text}：{url}" if prefix else f"{price_text.lstrip(' ·')}：{url}")
                return "\n".join(lines)
            if key == "specs":
                lines = []
                for code, fact in (value.items() if isinstance(value, dict) else []):
                    if not isinstance(fact, dict):
                        fact = {"normalized_value": fact}
                    number = fact.get("numeric_value")
                    normalized = fact.get("normalized_value")
                    raw = fact.get("raw_value")
                    shown = number if number not in (None, "") else normalized if normalized not in (None, "") else raw
                    if shown in (None, ""):
                        continue
                    unit = fact.get("normalized_unit") or fact.get("canonical_unit") or fact.get("raw_unit") or ""
                    lines.append(f"{field_labels.get(code, code)}：{shown}{(' ' + str(unit)) if unit else ''}")
                return "\n".join(lines)
            if key == "assets":
                role_names = {"hero": "Hero 主图", "gallery": "橱窗图", "video": "视频", "document": "文档"}
                lines = []
                for asset in value if isinstance(value, list) else []:
                    if not isinstance(asset, dict):
                        continue
                    role = role_names.get(str(asset.get("asset_role")), "其他素材")
                    name = asset.get("original_filename") or asset.get("alt_text") or asset.get("source_url") or "未命名附件"
                    lines.append(f"{role}：{name}")
                return "\n".join(lines)
            if isinstance(value, dict):
                return "、".join(f"{item_key}：{item_value}" for item_key, item_value in value.items())
            if isinstance(value, list):
                return "、".join(str(item) for item in value)
            return str(value)

        additions: list[dict[str, Any]] = []
        modifications: list[dict[str, Any]] = []
        deletions: list[dict[str, Any]] = []

        def add_change(bucket: list[dict[str, Any]], key: str, before: Any, after: Any) -> None:
            bucket.append({
                "field": key,
                "label": labels.get(key, key),
                "before": display(key, before) if present(before) else "—",
                "after": display(key, after) if present(after) else "—",
            })

        payload: dict[str, Any] = {}
        if latest:
            try:
                payload = json.loads(latest.get("payload_json") or "{}")
            except json.JSONDecodeError:
                payload = {}
        if latest and latest.get("action") == "updated" and payload.get("before") is not None:
            before = payload.get("before") or {}
            after = payload.get("after") or {}
            submitted = payload.get("submitted") or {}
            keys = set(submitted) | set(after) | set(before)
            for key in sorted(keys):
                old = before.get(key)
                new = after.get(key)
                if not present(old) and present(new):
                    add_change(additions, key, old, new)
                elif present(old) and not present(new):
                    add_change(deletions, key, old, new)
                elif old != new:
                    add_change(modifications, key, old, new)
        else:
            raw_specs = product.get("specs", {}) or {}
            compact_specs = {
                code: value
                for code, value in raw_specs.items()
                if any(
                    present(value.get(key))
                    for key in ("raw_value", "normalized_value", "numeric_value")
                )
            }
            current = {
                key: product.get(key)
                for key in PRODUCT_COLUMNS
                if key != "review_status"
            }
            current.update({
                "listings": product.get("listings", []),
                "markets": product.get("markets", []),
                "retailers": product.get("retailers", []),
                "specs": compact_specs,
                "assets": product.get("assets", []),
            })
            for key, value in current.items():
                if present(value):
                    add_change(additions, key, None, value)

        return {
            "product_id": product_id,
            "entity_version": product["entity_version"],
            "brand": product.get("brand"),
            "model_number": product.get("model_number"),
            "product_name": product.get("product_name"),
            "review_status": product.get("review_status"),
            "latest_action": latest.get("action") if latest else None,
            "latest_at": latest.get("created_at") if latest else None,
            "review_blockers": product.get("review_blockers", product.get("missing_required", [])),
            "review_blocker_details": product.get("review_blocker_details", {}),
            "enrichment_gaps": product.get("enrichment_gaps", []),
            "new": additions,
            "modified": modifications,
            "deleted": deletions,
        }

    def bulk_review(
        self,
        product_ids: list[str],
        *,
        approved: bool,
        reviewer: str = "local_preview",
        note: str = "",
        expected_versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item).strip() for item in product_ids if str(item).strip()))
        if not ids:
            raise ValueError("至少选择一个型号")
        products = [self.get_product(product_id) for product_id in ids]
        if any(product is None for product in products):
            raise KeyError("product not found")
        if expected_versions is not None and any(
            p and expected_versions.get(p["id"]) != p["entity_version"] for p in products
        ):
            raise ValueError("资料已变化，请刷新审核预览后重试")
        blocked = [
            {
                "product_id": product["id"],
                "brand": product["brand"],
                "model_number": product["model_number"],
                "review_blockers": product.get("review_blockers", product.get("missing_required", [])),
            }
            for product in products
            if product and product.get("missing_required")
        ]
        if approved and blocked:
            examples = "；".join(
                f"{item['brand']} {item['model_number']}: {', '.join(item['review_blockers'])}"
                for item in blocked[:3]
            )
            suffix = f"；另有 {len(blocked) - 3} 个型号" if len(blocked) > 3 else ""
            raise ValueError(f"{len(blocked)} 个型号仍有真实性或身份阻断：{examples}{suffix}")
        reviewed = [
            self.review_product(product_id, approved=approved, reviewer=reviewer, note=note,
                                expected_version=expected_versions.get(product_id) if expected_versions is not None else None)
            for product_id in ids
        ]
        return {
            "count": len(reviewed),
            "approved": approved,
            "products": reviewed,
            "review_blocker_count": len(blocked),
            "review_blockers": blocked,
        }

    def add_attachment_asset(
        self,
        product_id: str,
        *,
        storage_path: str,
        original_filename: str,
        content_hash: str,
        file_size: int,
        make_hero: bool,
        source_url: str | None = None,
        source_type: str = "local_attachment",
        actor: str = "local_user",
    ) -> dict[str, Any]:
        if not self.get_product(product_id):
            raise KeyError(product_id)
        now = utc_now()
        asset_id = stable_id("ast", product_id, content_hash)
        role = "hero" if make_hero else "gallery"
        evidence_url = source_url or f"attachment://{content_hash}"
        if source_url and "#asset-sha256=" not in source_url:
            evidence_url = f"{source_url}#asset-sha256={content_hash.removeprefix('sha256:')[:16]}"
        with self.connect() as conn:
            if make_hero:
                conn.execute(
                    "UPDATE product_assets SET asset_role='gallery' WHERE product_id=? AND asset_role='hero'",
                    (product_id,),
                )
            conn.execute(
                """
                INSERT INTO product_assets(
                  id, product_id, asset_role, media_type, source_url, alt_text,
                  sort_order, source_type, source_snapshot_hash, observed_at, review_status, created_at,
                  storage_path, original_filename, content_hash, file_size
                ) VALUES (?, ?, ?, 'image', ?, ?, 0, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  asset_role=excluded.asset_role, source_url=excluded.source_url,
                  storage_path=excluded.storage_path, original_filename=excluded.original_filename,
                  file_size=excluded.file_size, review_status='pending_review'
                """,
                (
                    asset_id, product_id, role, evidence_url,
                    f"{original_filename}", source_type, content_hash, now, now, storage_path,
                    original_filename, content_hash, file_size,
                ),
            )
            conn.execute(
                "UPDATE products SET review_status='pending_review', reviewed_by=NULL, reviewed_at=NULL, updated_at=? WHERE id=?",
                (now, product_id),
            )
            self._audit(conn, "product_asset", asset_id, "attached", actor, {"role": role, "content_hash": content_hash})
        return self.get_product(product_id)  # type: ignore[return-value]

    def set_hero_asset(self, product_id: str, asset_id: str, *, actor: str = "local_user") -> dict[str, Any]:
        with self.connect() as conn:
            asset = conn.execute(
                "SELECT id FROM product_assets WHERE id=? AND product_id=? AND media_type='image'",
                (asset_id, product_id),
            ).fetchone()
            if not asset:
                raise KeyError(asset_id)
            conn.execute(
                "UPDATE product_assets SET asset_role='gallery' WHERE product_id=? AND asset_role='hero'",
                (product_id,),
            )
            conn.execute(
                "UPDATE product_assets SET asset_role='hero', review_status='pending_review' WHERE id=?",
                (asset_id,),
            )
            conn.execute(
                "UPDATE products SET review_status='pending_review', reviewed_by=NULL, reviewed_at=NULL, updated_at=? WHERE id=?",
                (utc_now(), product_id),
            )
            self._audit(conn, "product_asset", asset_id, "set_hero", actor, {})
        return self.get_product(product_id)  # type: ignore[return-value]

    def add_link_asset(
        self,
        product_id: str,
        *,
        role: str,
        url: str,
        actor: str = "local_user",
    ) -> dict[str, Any]:
        if role not in {"video", "document"}:
            raise ValueError("链接素材仅支持视频或文档")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("视频或文档必须是完整 http/https URL")
        if not self.get_product(product_id):
            raise KeyError(product_id)
        now = utc_now()
        asset_id = stable_id("ast", product_id, role, url)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO product_assets(
                  id, product_id, asset_role, media_type, source_url, sort_order,
                  source_type, observed_at, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, 'user_link', ?, 'pending_review', ?)
                ON CONFLICT(id) DO UPDATE SET review_status='pending_review'
                """,
                (asset_id, product_id, role, role, url, now, now),
            )
            conn.execute(
                "UPDATE products SET review_status='pending_review', reviewed_by=NULL, reviewed_at=NULL, updated_at=? WHERE id=?",
                (now, product_id),
            )
            self._audit(conn, "product_asset", asset_id, "linked", actor, {"role": role, "url": url})
        return self.get_product(product_id)  # type: ignore[return-value]

    def delete_asset(self, product_id: str, asset_id: str, *, actor: str = "local_user") -> dict[str, Any]:
        with self.connect() as conn:
            deleted = conn.execute(
                "DELETE FROM product_assets WHERE id=? AND product_id=?",
                (asset_id, product_id),
            ).rowcount
            if not deleted:
                raise KeyError(asset_id)
            conn.execute(
                "UPDATE products SET review_status='pending_review', reviewed_by=NULL, reviewed_at=NULL, updated_at=? WHERE id=?",
                (utc_now(), product_id),
            )
            self._audit(conn, "product_asset", asset_id, "detached", actor, {})
        return self.get_product(product_id)  # type: ignore[return-value]

    def _upsert_listings(self, conn: sqlite3.Connection, product_id: str, listings: list[dict[str, Any]]) -> None:
        for item in listings:
            market = str(item.get("market_region") or "").strip()
            retailer = str(item.get("retailer") or "").strip()
            url = str(item.get("online_url") or "").strip()
            if not market and not retailer and not url:
                continue
            if not (retailer and url):
                raise ValueError("Listing 必须同时包含零售商和线上链接")
            review_status = preview_review_status(
                item.get("review_status"), entity_name="Listing"
            )
            retailer_id = str(item.get("retailer_id") or "").strip() or None
            if not retailer_id:
                detected = self.retailer_for_url(url)
                retailer_id = detected["id"] if detected else None
                retailer = detected["name"] if detected else retailer
            listing_id = str(item.get("id") or stable_id("lst", product_id, market, retailer, url))
            conn.execute(
                """
                INSERT INTO product_listings(
                  id, product_id, market_region, retailer, channel, online_url,
                  target_key, retail_status, review_status, source_type, source_snapshot_hash, observed_at,
                  retailer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, market_region, retailer, online_url) DO UPDATE SET
                  channel=COALESCE(excluded.channel, product_listings.channel),
                  target_key=COALESCE(excluded.target_key, product_listings.target_key),
                  retail_status=excluded.retail_status, review_status=excluded.review_status,
                  source_type=COALESCE(excluded.source_type, product_listings.source_type),
                  source_snapshot_hash=COALESCE(excluded.source_snapshot_hash, product_listings.source_snapshot_hash),
                  observed_at=COALESCE(excluded.observed_at, product_listings.observed_at),
                  retailer_id=COALESCE(excluded.retailer_id, product_listings.retailer_id)
                """,
                (
                    listing_id, product_id, market, retailer, item.get("channel"), url,
                    item.get("target_key"), item.get("retail_status", "unknown"),
                    review_status, item.get("source_type") or "retailer_listing", item.get("source_snapshot_hash"),
                    item.get("observed_at"), retailer_id,
                ),
            )
            persisted = conn.execute(
                """
                SELECT id, market_region, online_url, source_type, source_snapshot_hash, observed_at
                FROM product_listings
                WHERE product_id=? AND market_region=? AND retailer=? AND online_url=?
                """,
                (product_id, market, retailer, url),
            ).fetchone()
            if persisted and "prices" in item:
                self._replace_prices(
                    conn,
                    dict(persisted),
                    item.get("prices") or [],
                )

    def _replace_prices(
        self,
        conn: sqlite3.Connection,
        listing: dict[str, Any],
        prices: list[dict[str, Any]],
    ) -> None:
        listing_id = str(listing["id"])
        conn.execute(
            "DELETE FROM price_observations WHERE product_listing_id=?",
            (listing_id,),
        )
        now = utc_now()
        for item in prices:
            if not isinstance(item, dict):
                raise ValueError("零售价必须按 Listing 提交结构化记录")
            review_status = preview_review_status(
                item.get("review_status"), entity_name="零售价", default="pending_review"
            )
            amount = retail_price_value(item.get("normalized_amount"))
            code = currency_code(item.get("currency_code"))
            raw_source = item.get("raw_value")
            if raw_source in (None, ""):
                raw_source = item.get("normalized_amount")
            raw_value = str(raw_source if raw_source is not None else "").strip()
            if not raw_value:
                raise ValueError("零售价必须保留渠道原始值")
            observed_at = str(
                item.get("observed_at") or listing.get("observed_at") or now
            ).strip()
            source_url = str(
                item.get("source_url") or listing.get("online_url") or ""
            ).strip() or None
            source_type = str(item.get("source_type") or "retailer_listing").strip() or None
            snapshot_source = (
                item.get("source_snapshot_hash")
                if "source_snapshot_hash" in item
                else listing.get("source_snapshot_hash")
            )
            source_snapshot_hash = str(snapshot_source or "").strip() or None
            price_id = str(item.get("id") or stable_id(
                "prc", listing_id, code, observed_at, raw_value,
                source_snapshot_hash or "pending",
            ))
            conn.execute(
                """
                INSERT INTO price_observations(
                  id, product_listing_id, price_kind, raw_value,
                  normalized_amount, currency_code, market_region, source_url,
                  source_type, source_snapshot_hash, observed_at, review_status,
                  reviewed_by, reviewed_at, created_at
                ) VALUES (?, ?, 'retail', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    price_id, listing_id, raw_value, amount, code,
                    str(item.get("market_region") or listing.get("market_region") or ""),
                    source_url, source_type, source_snapshot_hash, observed_at,
                    review_status, item.get("reviewed_by"), item.get("reviewed_at"), now,
                ),
            )

    def _replace_listings(
        self,
        conn: sqlite3.Connection,
        product_id: str,
        listings: list[dict[str, Any]],
    ) -> None:
        """Replace user-managed listings while preserving task-bound connector rows.

        A listing already referenced by a Wotu task is immutable in this local CRUD
        surface. Unbound rows may be removed or replaced by the model editor.
        """
        bound_ids = {
            str(row["product_listing_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT t.product_listing_id
                FROM review_task_links t
                JOIN product_listings l ON l.id=t.product_listing_id
                WHERE l.product_id=?
                """,
                (product_id,),
            ).fetchall()
        }
        if bound_ids:
            placeholders = ",".join("?" for _ in bound_ids)
            conn.execute(
                f"DELETE FROM product_listings WHERE product_id=? AND id NOT IN ({placeholders})",
                (product_id, *sorted(bound_ids)),
            )
        else:
            conn.execute("DELETE FROM product_listings WHERE product_id=?", (product_id,))
        self._upsert_listings(conn, product_id, listings)

    def _replace_relationships(
        self,
        conn: sqlite3.Connection,
        product_id: str,
        payload: dict[str, Any],
    ) -> None:
        now = utc_now()
        if "market_region_ids" in payload:
            conn.execute("DELETE FROM product_market_regions WHERE product_id=?", (product_id,))
            for region_id in dict.fromkeys(payload.get("market_region_ids") or []):
                if not conn.execute("SELECT 1 FROM market_regions WHERE id=? AND active=1", (region_id,)).fetchone():
                    raise ValueError("未知上市地区")
                conn.execute(
                    """
                    INSERT INTO product_market_regions(
                      product_id, market_region_id, review_status, source_url,
                      source_snapshot_hash, observed_at
                    ) VALUES (?, ?, 'pending_review', ?, ?, ?)
                    """,
                    (
                        product_id, region_id, payload.get("source_url"),
                        payload.get("source_snapshot_hash"), payload.get("observed_at") or now,
                    ),
                )
        if "retailer_ids" in payload:
            conn.execute("DELETE FROM product_retailers WHERE product_id=?", (product_id,))
            retailer_ids = list(dict.fromkeys(payload.get("retailer_ids") or []))
            for listing in payload.get("listings") or []:
                retailer_id = str(listing.get("retailer_id") or "").strip()
                if not retailer_id and listing.get("online_url"):
                    detected = self.retailer_for_url(str(listing["online_url"]))
                    retailer_id = detected["id"] if detected else ""
                if retailer_id and retailer_id not in retailer_ids:
                    retailer_ids.append(retailer_id)
            for retailer_id in retailer_ids:
                if not conn.execute("SELECT 1 FROM retailers WHERE id=? AND active=1", (retailer_id,)).fetchone():
                    raise ValueError("未知零售商")
                conn.execute(
                    "INSERT INTO product_retailers(product_id, retailer_id, review_status) VALUES (?, ?, 'pending_review')",
                    (product_id, retailer_id),
                )

    def _upsert_specs(self, conn: sqlite3.Connection, product_id: str, category_id: str, specs: dict[str, Any]) -> None:
        definitions = {
            row["code"]: dict(row)
            for row in conn.execute("SELECT * FROM field_definitions WHERE category_id=?", (category_id,)).fetchall()
        }
        for code, value in specs.items():
            if code not in definitions:
                continue
            item = value if isinstance(value, dict) else {"normalized_value": value}
            normalized = item.get("normalized_value")
            raw = item.get("raw_value", normalized)
            if raw in (None, "") and normalized in (None, ""):
                continue
            definition = definitions[code]
            review_status = preview_review_status(
                item.get("review_status"), entity_name=f"规格 {code}"
            )
            number = None
            unit_id = str(item.get("unit_id") or definition.get("unit_id") or "").strip() or None
            unit_symbol = item.get("normalized_unit") or definition["canonical_unit"]
            if definition.get("is_matrix"):
                number = numeric_value(normalized, field_label=definition["label"])
                if not unit_id:
                    raise ValueError(f"{definition['label']} 必须选择单位")
                unit = conn.execute("SELECT symbol FROM units WHERE id=? AND active=1", (unit_id,)).fetchone()
                if not unit:
                    raise ValueError(f"{definition['label']} 使用了未知单位")
                unit_symbol = unit["symbol"]
            spec_id = stable_id("spv", product_id, definition["id"])
            conn.execute(
                """
                INSERT INTO spec_values(
                  id, product_id, field_definition_id, raw_value, normalized_value,
                  raw_unit, normalized_unit, value_state, review_status, source_url,
                  source_type, source_snapshot_hash, observed_at, numeric_value, unit_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, field_definition_id) DO UPDATE SET
                  raw_value=excluded.raw_value, normalized_value=excluded.normalized_value,
                  raw_unit=excluded.raw_unit, normalized_unit=excluded.normalized_unit,
                  value_state=excluded.value_state, review_status=excluded.review_status,
                  source_url=excluded.source_url, source_type=excluded.source_type,
                  source_snapshot_hash=excluded.source_snapshot_hash,
                  observed_at=excluded.observed_at, numeric_value=excluded.numeric_value,
                  unit_id=excluded.unit_id
                """,
                (
                    spec_id, product_id, definition["id"], str(raw) if raw is not None else None,
                    str(normalized) if normalized is not None else None, item.get("raw_unit"),
                    unit_symbol,
                    item.get("value_state", "known"), review_status,
                    item.get("source_url"), item.get("source_type"), item.get("source_snapshot_hash"), item.get("observed_at"),
                    number, unit_id,
                ),
            )

    def _replace_assets(self, conn: sqlite3.Connection, product_id: str, assets: list[dict[str, Any]]) -> None:
        prepared: list[dict[str, Any]] = []
        hero_count = 0
        for index, raw_item in enumerate(assets):
            item = raw_item if isinstance(raw_item, dict) else {"source_url": raw_item}
            source_url = str(item.get("source_url") or "").strip()
            if not source_url:
                continue
            parsed = urlparse(source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("素材链接必须是完整的 http/https URL")
            role = str(item.get("asset_role") or "gallery").strip()
            if role not in VALID_ASSET_ROLES:
                raise ValueError("未知素材角色")
            review_status = preview_review_status(
                item.get("review_status"), entity_name="产品素材"
            )
            if role in {"hero", "gallery"} or item.get("media_type", "image") == "image":
                raise ValueError("图片素材必须通过附件接口上传，不能保存为图片链接")
            if role == "hero":
                hero_count += 1
            prepared.append({
                **item,
                "source_url": source_url,
                "asset_role": role,
                "review_status": review_status,
                "sort_order": int(item.get("sort_order", index)),
            })
        if hero_count > 1:
            raise ValueError("每个竞品只能有一张 hero 图片")
        conn.execute("DELETE FROM product_assets WHERE product_id=?", (product_id,))
        now = utc_now()
        for item in prepared:
            asset_id = str(item.get("id") or stable_id("ast", product_id, item["asset_role"], item["source_url"]))
            conn.execute(
                """
                INSERT INTO product_assets(
                  id, product_id, asset_role, media_type, source_url, thumbnail_url,
                  alt_text, sort_order, source_type, source_snapshot_hash, observed_at,
                  review_status, reviewed_by, reviewed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id, product_id, item["asset_role"], item.get("media_type", "image"),
                    item["source_url"], item.get("thumbnail_url"), item.get("alt_text"),
                    item["sort_order"], item.get("source_type"), item.get("source_snapshot_hash"),
                    item.get("observed_at"), item["review_status"],
                    item.get("reviewed_by"), item.get("reviewed_at"), now,
                ),
            )

    def _audit(self, conn: sqlite3.Connection, entity_type: str, entity_id: str, action: str, actor: str, payload: dict[str, Any]) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        event_id = stable_id("aud", entity_type, entity_id, action, created_at, json.dumps(payload, ensure_ascii=False, sort_keys=True))
        conn.execute(
            "INSERT INTO audit_events(id, entity_type, entity_id, action, actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, entity_type, entity_id, action, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), created_at),
        )

    def summary(self) -> dict[str, Any]:
        categories = self.list_categories()
        return {
            "schema_version": "retail-landscape/v1.5",
            "categories": categories,
            "totals": {
                "products": sum(item["product_count"] for item in categories if item["enabled"]),
                "listings": sum(item["listing_count"] for item in categories if item["enabled"]),
                "tasks": sum(item["task_count"] for item in categories if item["enabled"]),
                "reports": sum(item["report_count"] for item in categories if item["enabled"]),
            },
        }

    def voltage_breakdown(self, category_code: str) -> list[dict[str, Any]]:
        category = self.category(category_code)
        if not category:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT b.id AS brand_id, b.canonical_name AS brand,
                       'DC' AS energy_type, vp.id AS voltage_platform_id,
                       vp.standard_voltage AS voltage_platform,
                       vp.voltage_value, vp.voltage_unit,
                       COUNT(*) AS product_count
                FROM products p
                JOIN brands b ON b.id=p.brand_id
                JOIN voltage_platforms vp ON vp.id=p.voltage_platform_id
                WHERE p.category_id=? AND p.review_status!='retired'
                  AND p.energy_type='DC'
                GROUP BY b.id, vp.id
                ORDER BY b.canonical_name COLLATE NOCASE, vp.voltage_value
                """,
                (category["id"],),
            ).fetchall()
        return [dict(row) for row in rows]
