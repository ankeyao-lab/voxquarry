from __future__ import annotations

import importlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from collector.master_data import (  # noqa: E402
    MasterDataConflict,
    MasterDataError,
    MasterDataStore,
    TYPE_SPECS,
    stable_id,
)
from collector.store import Store  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

TEST_PROXY_TOKEN = "test-proxy-token-0123456789abcdef"
TEST_PROD_ACTOR = "basic:test-admin"


def minimal_manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "voxquarry.shared-master-data-bootstrap/v1",
                "manifest_id": "test-minimal",
                "evidence": ["test fixture"],
                "records": {},
                "legacy_overrides": {},
                "unresolved": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def copy_sqlite(source: Path, target: Path) -> None:
    source_conn = sqlite3.connect(source)
    try:
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
            target_conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            target_conn.close()
    finally:
        source_conn.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)


class SharedMasterDataStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = MasterDataStore(self.root, initialize=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create(self, entity_type: str, **payload):
        return self.catalog.create(entity_type, payload, actor="test:admin")

    def update(self, entity_type: str, item: dict, **payload):
        return self.catalog.update(
            entity_type,
            item["id"],
            payload,
            actor="test:admin",
            expected_revision=item["revision"],
        )

    def test_migrations_are_idempotent_and_runtime_open_requires_initialized_db(self) -> None:
        before = self.catalog.summary()
        reopened = MasterDataStore(self.root, initialize=True)
        self.assertEqual(before, reopened.summary())
        self.assertEqual(
            [
                "0001_shared_master_data",
                "0002_catalog_scope_activation",
                "0003_legacy_projection_content_seal",
                "0004_rebind_completion_actor",
            ],
            reopened.migration_versions(),
        )
        self.assertEqual("ok", reopened.integrity()["integrity_check"])

        missing = self.root / "missing"
        with self.assertRaisesRegex(ValueError, "existing shared"):
            MasterDataStore(missing)
        self.assertFalse(missing.exists())

    def test_runtime_open_rejects_forward_schema_and_content_tamper(self) -> None:
        brand = self.create("brands", code="tamper_probe", name="Tamper Probe")
        with sqlite3.connect(self.catalog.db_path) as conn:
            conn.execute(
                """
                INSERT INTO schema_migrations(version,checksum,applied_at)
                VALUES('9999_future','future','2026-09-02T00:00:00+00:00')
                """
            )
        with self.assertRaisesRegex(ValueError, "migration set"):
            MasterDataStore(self.root)
        with self.assertRaisesRegex(RuntimeError, "future migrations"):
            MasterDataStore(self.root, initialize=True)
        with sqlite3.connect(self.catalog.db_path) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version='9999_future'")
            conn.execute(
                "UPDATE master_entities SET name='Tampered' WHERE id=?",
                (brand["id"],),
            )
        with self.assertRaisesRegex(ValueError, "content hash mismatch"):
            MasterDataStore(self.root)
        with self.assertRaisesRegex(ValueError, "content hash mismatch"):
            MasterDataStore(self.root, initialize=True)

    def test_catalog_cli_rejects_symlink_without_touching_target(self) -> None:
        target_root = self.root / "target"
        target = MasterDataStore(target_root, initialize=True).db_path
        before_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        before_mtime = target.stat().st_mtime_ns
        link_root = self.root / "link"
        link_root.mkdir()
        (link_root / "master_data.sqlite3").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            MasterDataStore(link_root, initialize=True)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            MasterDataStore(link_root, initialize=False)
        self.assertEqual(before_sha, hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(before_mtime, target.stat().st_mtime_ns)

    def test_runtime_connection_never_recreates_a_missing_catalog(self) -> None:
        hidden = self.root / "master_data.sqlite3.runtime-missing"
        self.catalog.db_path.replace(hidden)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.catalog.summary()
            self.assertFalse(self.catalog.db_path.exists())
        finally:
            hidden.replace(self.catalog.db_path)

    def test_stable_id_nfkc_name_and_alias_uniqueness(self) -> None:
        brand = self.create(
            "brands",
            code="acme",
            name="ＡＣＭＥ",
            aliases=["Acme Tools", "acme_tools"],
        )
        renamed = self.update("brands", brand, name="ACME Power")
        self.assertEqual(brand["id"], renamed["id"])
        with self.assertRaisesRegex(MasterDataConflict, "resolves to another"):
            self.create("brands", code="acme_two", name="ＡＣＭＥ　Ｐｏｗｅｒ")
        with self.assertRaisesRegex(MasterDataConflict, "resolves to another"):
            self.create("brands", code="alias_collision", name="Acme Tools")
        with self.assertRaisesRegex(MasterDataConflict, "resolves to another"):
            self.create("brands", code="acme_tools", name="Different Name")
        self.assertFalse(
            self.catalog.validate_candidate("brands", code="acme_tools")["valid"]
        )
        code_owner = self.create("brands", code="shared_code", name="Code Owner")
        with self.assertRaisesRegex(MasterDataConflict, "resolves to another"):
            self.create("brands", code="another", name="shared_code")
        self.assertTrue(code_owner["active"])
        with self.assertRaisesRegex(MasterDataError, "alias must differ"):
            self.create("brands", code="same_alias", name="Same", aliases=["Ｓａｍｅ"])
        with self.assertRaisesRegex(MasterDataError, "immutable"):
            self.update("brands", renamed, code="different")

    def test_voltage_hierarchy_units_currency_and_all_type_payloads(self) -> None:
        brand = self.create("brands", code="ego", name="EGO")
        category = self.create("categories", code="ope", name="OPE")
        child = self.create(
            "categories", code="mower", name="Mower", parent_id=category["id"]
        )
        with self.assertRaisesRegex(MasterDataConflict, "cycle"):
            self.update("categories", category, parent_id=child["id"])
        with self.assertRaisesRegex(MasterDataConflict, "active master-data references"):
            self.catalog.set_active(
                "categories", category["id"], False, actor="test:admin",
                expected_revision=category["revision"],
            )

        dimension = self.create("dimensions", code="voltage", name="Voltage")
        volt = self.create(
            "units",
            code="volt",
            name="Volt",
            dimension_id=dimension["id"],
            symbol="V",
            is_base=True,
            conversion_factor=1,
            conversion_offset=0,
        )
        for index, raw in enumerate(("20", "120V", "60 volts", "20.5V")):
            platform = self.create(
                "voltage-platforms",
                code=f"ego_{index}",
                name=f"EGO {raw}",
                brand_id=brand["id"],
                canonical_voltage=raw,
                unit_id=volt["id"],
            )
            self.assertEqual(("20V", "120V", "60V", "20.5V")[index], platform["canonical_voltage"])
        with self.assertRaisesRegex(MasterDataConflict, "canonical_voltage"):
            self.create(
                "voltage-platforms",
                code="ego_duplicate",
                name="EGO duplicate",
                brand_id=brand["id"],
                canonical_voltage="20V",
                unit_id=volt["id"],
            )

        region = self.create(
            "market-regions", code="north_america", name="North America"
        )
        retailer = self.create(
            "retailers",
            code="lowes",
            name="Lowe's",
            official_domain="https://www.lowes.com/path",
            market_region_ids=[region["id"]],
        )
        self.assertEqual("lowes.com", retailer["official_domain"])
        channel = self.create(
            "collection-channels",
            code="lowes",
            name="Lowe's Reviews",
            channel_type="retailer_web",
            retailer_id=retailer["id"],
            market_region_ids=[region["id"]],
            supported_content=["review"],
        )
        self.assertTrue(channel["active"])
        with self.assertRaisesRegex(MasterDataConflict, "active master-data references"):
            self.catalog.set_active(
                "retailers", retailer["id"], False, actor="test:admin",
                expected_revision=retailer["revision"],
            )
        with self.assertRaisesRegex(MasterDataConflict, "active master-data references"):
            self.catalog.set_active(
                "market-regions", region["id"], False, actor="test:admin",
                expected_revision=region["revision"],
            )
        region_two = self.create("market-regions", code="europe", name="Europe")
        sorted_retailer = self.create(
            "retailers",
            code="sorted",
            name="Sorted Retailer",
            market_region_ids=[region_two["id"], region["id"], region_two["id"]],
        )
        self.assertEqual(
            sorted({region["id"], region_two["id"]}),
            sorted_retailer["market_region_ids"],
        )
        energy = self.create("energy-types", code="battery", name="Battery")
        self.assertTrue(energy["active"])
        lifecycle = self.create(
                "product-lifecycle-statuses",
                code="active",
                name="Active",
                is_terminal=False,
            )
        self.assertFalse(lifecycle["is_terminal"])
        currency = self.create(
            "currencies",
            code="USD",
            name="US Dollar",
            symbol="$",
            decimal_places=6,
        )
        self.assertEqual("USD", currency["code"])
        currency = self.update("currencies", currency, name="United States Dollar")
        self.assertEqual("USD", currency["code"])
        with self.assertRaisesRegex(MasterDataError, "between 0 and 6"):
            self.create(
                "currencies", code="EUR", name="Euro", symbol="€", decimal_places=7
            )
        with self.assertRaisesRegex(MasterDataError, "channel_type"):
            self.create(
                "collection-channels",
                code="bad",
                name="Bad",
                channel_type="connector",
                supported_content=["review"],
            )
        with self.assertRaisesRegex(MasterDataError, "brand_id is required"):
            self.create(
                "collection-channels",
                code="brand_missing",
                name="Missing Brand",
                channel_type="brand_official",
                supported_content=["review"],
            )
        with self.assertRaisesRegex(MasterDataError, "retailer_id is required"):
            self.create(
                "collection-channels",
                code="retailer_missing",
                name="Missing Retailer",
                channel_type="retailer_web",
                supported_content=["review"],
            )
        records = {
            "brands": brand,
            "categories": child,
            "energy-types": energy,
            "voltage-platforms": platform,
            "market-regions": region,
            "retailers": retailer,
            "collection-channels": channel,
            "dimensions": dimension,
            "units": volt,
            "product-lifecycle-statuses": lifecycle,
            "currencies": currency,
        }
        for entity_type, record in records.items():
            self.assertTrue(
                set(TYPE_SPECS[entity_type].get("fields", set())).issubset(record),
                entity_type,
            )

    def test_unit_and_inactive_reference_guards(self) -> None:
        dimension = self.create("dimensions", code="length", name="Length")
        with self.assertRaisesRegex(MasterDataError, "base unit"):
            self.create(
                "units",
                code="foot",
                name="Foot",
                dimension_id=dimension["id"],
                symbol="ft",
                is_base=False,
                conversion_factor=0.3048,
            )
        meter = self.create(
            "units",
            code="meter",
            name="Meter",
            dimension_id=dimension["id"],
            symbol="m",
            is_base=True,
            conversion_factor=1,
        )
        foot = self.create(
            "units",
            code="foot",
            name="Foot",
            dimension_id=dimension["id"],
            symbol="ft",
            is_base=False,
            conversion_factor=0.3048,
        )
        self.assertEqual(0.0, foot["conversion_offset"])
        fake_volt = self.create(
            "units",
            code="length_v",
            name="Length Unit Displayed As V",
            dimension_id=dimension["id"],
            symbol="V",
            is_base=False,
            conversion_factor=1,
        )
        dimension_probe_brand = self.create(
            "brands", code="dimension_probe", name="Dimension Probe"
        )
        with self.assertRaisesRegex(MasterDataError, "voltage dimension"):
            self.create(
                "voltage-platforms",
                code="wrong_dimension_platform",
                name="Wrong Dimension Platform",
                brand_id=dimension_probe_brand["id"],
                canonical_voltage="20V",
                unit_id=fake_volt["id"],
            )
        with self.assertRaisesRegex(MasterDataConflict, "exactly one"):
            self.update("units", meter, is_base=False)
        with self.assertRaisesRegex(MasterDataConflict, "exactly one"):
            self.catalog.set_active(
                "units",
                meter["id"],
                False,
                actor="test:admin",
                expected_revision=meter["revision"],
            )
        with self.assertRaisesRegex(MasterDataConflict, "active master-data references"):
            self.catalog.set_active(
                "dimensions",
                dimension["id"],
                False,
                actor="test:admin",
                expected_revision=dimension["revision"],
            )

        unused_dimension = self.create("dimensions", code="unused", name="Unused")
        inactive_dimension = self.catalog.set_active(
            "dimensions",
            unused_dimension["id"],
            False,
            actor="test:admin",
            expected_revision=unused_dimension["revision"],
        )
        with self.assertRaisesRegex(MasterDataError, "inactive"):
            self.create(
                "units",
                code="bad_unit",
                name="Bad Unit",
                dimension_id=inactive_dimension["id"],
                symbol="x",
                is_base=True,
                conversion_factor=1,
            )
        self.assertTrue(meter["active"])

        brand = self.create("brands", code="inactive_brand", name="Inactive Brand")
        brand = self.catalog.set_active(
            "brands",
            brand["id"],
            False,
            actor="test:admin",
            expected_revision=brand["revision"],
        )
        voltage_dimension = self.create("dimensions", code="voltage", name="Voltage")
        volt = self.create(
            "units",
            code="volt",
            name="Volt",
            dimension_id=voltage_dimension["id"],
            symbol="V",
            is_base=True,
            conversion_factor=1,
        )
        with self.assertRaisesRegex(MasterDataError, "inactive"):
            self.create(
                "voltage-platforms",
                code="inactive_platform",
                name="Inactive Platform",
                brand_id=brand["id"],
                canonical_voltage="20V",
                unit_id=volt["id"],
            )

        active_brand = self.create("brands", code="spaced_brand", name="Spaced Brand")
        platform = self.create(
            "voltage-platforms",
            code="spaced_platform",
            name="Spaced Platform",
            brand_id=f" {active_brand['id']} ",
            canonical_voltage="40V",
            unit_id=f" {volt['id']} ",
        )
        self.assertEqual(active_brand["id"], platform["brand_id"])
        self.assertEqual(volt["id"], platform["unit_id"])
        with self.assertRaisesRegex(MasterDataConflict, "active master-data references"):
            self.catalog.set_active(
                "brands",
                active_brand["id"],
                False,
                actor="test:admin",
                expected_revision=active_brand["revision"],
            )
        with self.assertRaisesRegex(MasterDataConflict, "conversion identity"):
            self.update("units", volt, symbol="volt")

        archive_dimension = self.create(
            "dimensions", code="archive_dimension", name="Archive Dimension"
        )
        archive_unit = self.create(
            "units", code="archive_unit", name="Archive Unit",
            dimension_id=archive_dimension["id"], symbol="au", is_base=True,
            conversion_factor=1,
        )
        archive_unit = self.catalog.set_active(
            "units", archive_unit["id"], False, actor="test:admin",
            expected_revision=archive_unit["revision"],
        )
        archive_dimension = self.catalog.set_active(
            "dimensions", archive_dimension["id"], False,
            actor="test:admin", expected_revision=archive_dimension["revision"],
        )
        archive_unit = self.update(
            "units", archive_unit, definition="Historical conversion evidence"
        )
        self.assertEqual(
            archive_dimension["id"], archive_unit["dimension_id"]
        )
        with self.assertRaisesRegex(MasterDataError, "inactive"):
            self.catalog.set_active(
                "units", archive_unit["id"], True, actor="test:admin",
                expected_revision=archive_unit["revision"],
            )

    def test_strict_json_types_lengths_and_output_nulls(self) -> None:
        with self.assertRaisesRegex(MasterDataError, "active must be a boolean"):
            self.create("brands", code="bad_bool", name="Bad", active="false")
        with self.assertRaisesRegex(MasterDataError, "non-negative integer"):
            self.create("brands", code="bad_sort", name="Bad Sort", sort_order=-1)
        with self.assertRaisesRegex(MasterDataError, "non-negative integer"):
            self.create("brands", code="bad_sort_text", name="Bad Sort", sort_order="1")
        with self.assertRaisesRegex(MasterDataError, "at most 2000"):
            self.create("brands", code="long_name", name="x" * 2001)
        with self.assertRaisesRegex(MasterDataError, "must be strings"):
            self.create(
                "brands",
                code="bad_mapping",
                name="Bad Mapping",
                source_mappings=[{"system": "source", "source_value": 7}],
            )
        with self.assertRaisesRegex(MasterDataError, "array"):
            self.create(
                "retailers",
                code="bad_regions",
                name="Bad Regions",
                market_region_ids="mkt_fake",
            )
        with self.assertRaisesRegex(MasterDataError, "hostname"):
            self.create(
                "retailers",
                code="bad_domain",
                name="Bad Domain",
                official_domain=f"{'x' * 64}.example.com",
            )
        with self.assertRaisesRegex(MasterDataError, "at most 32"):
            self.create(
                "currencies", code="ZZZ", name="Too Long Symbol",
                symbol="x" * 33,
            )
        brand = self.create(
            "brands", code="blank_optional", name="Blank Optional",
            short_name="   ", official_domain=None,
        )
        self.assertIsNone(brand["short_name"])
        self.assertIsNone(brand["official_domain"])

    def test_disable_replacement_enable_and_append_only_audit(self) -> None:
        first = self.create("energy-types", code="old", name="Old")
        replacement = self.create("energy-types", code="new", name="New")
        disabled = self.catalog.set_active(
            "energy-types",
            first["id"],
            False,
            replacement_id=replacement["id"],
            actor="test:admin",
            expected_revision=first["revision"],
        )
        self.assertFalse(disabled["active"])
        self.assertEqual(replacement["id"], disabled["replacement_id"])
        with self.assertRaisesRegex(MasterDataError, "same record"):
            self.catalog.set_active(
                "energy-types", replacement["id"], False,
                replacement_id=replacement["id"], actor="test:admin",
                expected_revision=replacement["revision"],
            )
        with self.assertRaisesRegex(MasterDataConflict, "references"):
            self.catalog.set_active(
                "energy-types",
                replacement["id"],
                False,
                actor="test:admin",
                expected_revision=replacement["revision"],
            )
        enabled = self.catalog.set_active(
            "energy-types",
            disabled["id"],
            True,
            actor="test:admin",
            expected_revision=disabled["revision"],
        )
        self.assertIsNone(enabled["replacement_id"])
        actions = [item["action"] for item in self.catalog.audit(entity_id=first["id"])["items"]]
        self.assertIn("disable", actions)
        self.assertIn("enable", actions)
        with self.catalog._connect() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute("DELETE FROM master_audit_events")

    def test_etag_catalog_concurrency_resolve_and_source_mapping(self) -> None:
        etag = self.catalog.etag().strip('"')
        brand = self.catalog.create(
            "brands",
            {
                "code": "ego",
                "name": "EGO",
                "aliases": ["EGO Power+"],
                "source_mappings": [{"system": "chengmu", "source_value": "brand_ego"}],
            },
            actor="test:admin",
            expected_catalog_hash=etag,
        )
        with self.assertRaisesRegex(MasterDataConflict, "ETag conflict"):
            self.catalog.update(
                "brands",
                brand["id"],
                {"name": "EGO 2"},
                actor="test:admin",
                expected_catalog_hash=etag,
            )
        self.assertEqual("alias", self.catalog.resolve("brands", "ego power+")["match_kind"])
        self.assertEqual(
            "source_mapping",
            self.catalog.resolve(
                "brands", "brand_ego", source_system="chengmu"
            )["match_kind"],
        )


class ImportLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous_data_dir = os.environ.get("DATA_DIR")
        self.previous_catalog_required = os.environ.get(
            "MASTER_DATA_CATALOG_REQUIRED"
        )
        self.previous_legacy_write_protection = os.environ.get(
            "MASTER_DATA_PROTECT_LEGACY_WRITES"
        )
        self.previous_activation_auth = {
            key: os.environ.get(key)
            for key in (
                "MASTER_DATA_ADMIN_WRITES_ENABLED",
                "MASTER_DATA_ADMIN_PROXY_TOKEN",
                "MASTER_DATA_TRUSTED_ACTORS",
                "MASTER_DATA_DEV_MODE",
            )
        }
        os.environ["DATA_DIR"] = str(self.root)
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "false"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "false"
        self.legacy = Store()
        self.copy = self.root / "jobs-copy.sqlite3"
        copy_sqlite(self.legacy.db_path, self.copy)
        self.manifest = minimal_manifest(self.root / "manifest.json")
        self.catalog = MasterDataStore(
            self.root, legacy_db_path=self.legacy.db_path, initialize=True
        )
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "true"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "true"
        os.environ["MASTER_DATA_ADMIN_WRITES_ENABLED"] = "true"
        os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = TEST_PROXY_TOKEN
        os.environ["MASTER_DATA_TRUSTED_ACTORS"] = TEST_PROD_ACTOR
        os.environ["MASTER_DATA_DEV_MODE"] = "false"

    def tearDown(self) -> None:
        if self.previous_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.previous_data_dir
        if self.previous_catalog_required is None:
            os.environ.pop("MASTER_DATA_CATALOG_REQUIRED", None)
        else:
            os.environ[
                "MASTER_DATA_CATALOG_REQUIRED"
            ] = self.previous_catalog_required
        if self.previous_legacy_write_protection is None:
            os.environ.pop("MASTER_DATA_PROTECT_LEGACY_WRITES", None)
        else:
            os.environ[
                "MASTER_DATA_PROTECT_LEGACY_WRITES"
            ] = self.previous_legacy_write_protection
        for key, value in self.previous_activation_auth.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def plan(self):
        return self.catalog.import_plan(
            legacy_db_copy=self.copy, manifest_path=self.manifest
        )

    def apply(self, plan):
        return self.catalog.apply_import(
            legacy_db_copy=self.copy,
            manifest_path=self.manifest,
            actor="test:import",
            expected_plan_sha256=plan["plan_sha256"],
            expected_legacy_copy_sha256=plan["legacy_copy_sha256"],
        )

    def test_plan_rejects_catalog_and_live_legacy_drift(self) -> None:
        plan = self.plan()
        self.catalog.create(
            "energy-types",
            {"code": "battery", "name": "Battery"},
            actor="test:admin",
        )
        with self.assertRaisesRegex(MasterDataConflict, "plan hash changed"):
            self.apply(plan)

        fresh_plan = self.plan()
        with sqlite3.connect(self.legacy.db_path) as conn:
            conn.execute(
                """
                INSERT INTO master_brands(name,created_at,updated_at)
                VALUES('Live Drift','2026-01-01','2026-01-01')
                """
            )
        with self.assertRaisesRegex(MasterDataConflict, "live legacy"):
            self.apply(fresh_plan)

    def test_import_plan_is_byte_for_byte_zero_write(self) -> None:
        before_sha = hashlib.sha256(self.catalog.db_path.read_bytes()).hexdigest()
        before_mtime = self.catalog.db_path.stat().st_mtime_ns
        plan = self.plan()
        self.assertEqual(0, plan["counts"]["conflicts"])
        self.assertEqual(
            before_sha,
            hashlib.sha256(self.catalog.db_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(before_mtime, self.catalog.db_path.stat().st_mtime_ns)

    def test_cli_wires_full_projection_lifecycle(self) -> None:
        cli = SERVICE_ROOT / "tools" / "manage_master_data.py"
        cli_data = self.root / "cli-catalog"

        def run_cli(*arguments: str) -> dict:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "--data-dir",
                    str(cli_data),
                    *arguments,
                ],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "MASTER_DATA_CATALOG_REQUIRED": "true",
                    "MASTER_DATA_PROTECT_LEGACY_WRITES": "true",
                },
            )
            self.assertEqual(
                0,
                completed.returncode,
                msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
            )
            return json.loads(completed.stdout)

        initialized = run_cli("init")
        self.assertTrue(initialized["initialized"])
        plan = run_cli(
            "dry-run",
            "--legacy-db-copy", str(self.copy),
            "--manifest", str(self.manifest),
        )
        applied = run_cli(
            "apply",
            "--legacy-db-copy", str(self.copy),
            "--legacy-db-live", str(self.legacy.db_path),
            "--manifest", str(self.manifest),
            "--actor", "test:cli-import",
            "--expected-plan-sha256", plan["plan_sha256"],
            "--expected-legacy-copy-sha256", plan["legacy_copy_sha256"],
        )
        activated = run_cli(
            "activate-legacy",
            "--legacy-db-live", str(self.legacy.db_path),
            "--actor", "test:cli-activate",
            "--expected-plan-sha256", plan["plan_sha256"],
            "--expected-legacy-master-content-hash",
            plan["legacy_master_content_hash"],
        )
        self.assertTrue(activated["activated"])
        deactivated = run_cli(
            "deactivate-legacy",
            "--legacy-db-live", str(self.legacy.db_path),
            "--actor", "test:cli-deactivate",
            "--expected-catalog-content-hash",
            applied["catalog"]["catalog_content_hash"],
        )
        self.assertFalse(deactivated["activated"])
        self.assertEqual(
            "inactive",
            deactivated["integration_status"]["consumer_scopes"][0]["status"],
        )

    def test_governed_manifest_marks_missing_channel_subjects_unresolved(self) -> None:
        governed = SERVICE_ROOT / "master_data_bootstrap_manifest.json"
        plan = self.catalog.import_plan(
            legacy_db_copy=self.copy, manifest_path=governed
        )
        self.assertEqual(0, plan["counts"]["conflicts"])
        channels = {
            item["payload"]["code"]: item
            for item in plan["records"]
            if item["type"] == "collection-channels"
        }
        self.assertEqual("create", channels["amazon_live"]["action"])
        self.assertEqual("unresolved", channels["skil"]["action"])
        self.assertIn("brand_id", channels["skil"]["error"])

    def test_manifest_schema_rejects_typo_and_wrong_unresolved_shapes(self) -> None:
        valid = json.loads(self.manifest.read_text(encoding="utf-8"))
        invalid_cases = []

        typo = dict(valid)
        typo["unresolve"] = typo.pop("unresolved")
        invalid_cases.append((typo, "keys must be exactly"))

        wrong_array = dict(valid)
        wrong_array["unresolved"] = "oops"
        invalid_cases.append((wrong_array, "unresolved must be an array"))

        wrong_item = dict(valid)
        wrong_item["unresolved"] = [
            {"scope": "legacy.brands", "values": "Unbranded", "reason": "review"}
        ]
        invalid_cases.append((wrong_item, "values must be an array"))

        extra_override_key = dict(valid)
        extra_override_key["legacy_overrides"] = {
            "brands": {
                "Unbranded": {
                    "action": "unresolved",
                    "reason": "review",
                    "approved": True,
                }
            }
        }
        invalid_cases.append((extra_override_key, "exactly action and reason"))

        duplicate_override = dict(valid)
        duplicate_override["legacy_overrides"] = {
            "brands": {
                "Foo": {"action": "unresolved", "reason": "first"},
                "Ｆｏｏ": {"action": "unresolved", "reason": "second"},
            }
        }
        invalid_cases.append((duplicate_override, "normalized unique"))

        for index, (payload, message) in enumerate(invalid_cases):
            path = self.root / f"invalid-manifest-{index}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(index=index), self.assertRaisesRegex(
                MasterDataError, message
            ):
                self.catalog.import_plan(
                    legacy_db_copy=self.copy, manifest_path=path
                )

        unmatched = dict(valid)
        unmatched["legacy_overrides"] = {
            "brands": {
                "Not In Reviewed Copy": {
                    "action": "unresolved",
                    "reason": "must not disappear silently",
                }
            }
        }
        unmatched_path = self.root / "unmatched-override.json"
        unmatched_path.write_text(json.dumps(unmatched), encoding="utf-8")
        plan = self.catalog.import_plan(
            legacy_db_copy=self.copy, manifest_path=unmatched_path
        )
        self.assertTrue(
            any(
                item["scope"] == "legacy.brands"
                and item["values"] == ["Not In Reviewed Copy"]
                for item in plan["unresolved"]
            )
        )

    def test_manifest_rejects_duplicate_normalized_records_without_last_wins(self) -> None:
        for index, records in enumerate(
            (
                [
                    {"code": "battery", "name": "Battery", "definition": "first"},
                    {"code": "ＢＡＴＴＥＲＹ", "name": "Battery Two"},
                ],
                [
                    {"code": "battery_two", "name": "Battery"},
                    {"code": "battery_one", "name": "Ｂａｔｔｅｒｙ"},
                ],
                [
                    {"code": "battery", "name": "Battery"},
                    {"code": "battery", "name": "Battery"},
                ],
            )
        ):
            manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
            manifest["records"] = {"energy-types": records}
            path = self.root / f"duplicate-record-{index}.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.subTest(index=index), self.assertRaisesRegex(
                MasterDataError, "NFKC-normalized unique"
            ):
                self.catalog.import_plan(
                    legacy_db_copy=self.copy, manifest_path=path
                )

    def test_existing_legacy_placeholders_are_imported_inactive_and_block_activation(self) -> None:
        with sqlite3.connect(self.legacy.db_path) as conn:
            for table, name in (
                ("master_brands", "Unbranded"),
                ("master_categories", "Unknown Category"),
            ):
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {table}(name,created_at,updated_at)
                    VALUES(?,?,?)
                    """,
                    (name, "2026-01-01", "2026-01-01"),
                )
        copy_sqlite(self.legacy.db_path, self.copy)
        brand = self.catalog.create(
            "brands", {"code": "unbranded", "name": "Unbranded"},
            actor="test:existing",
        )
        category = self.catalog.create(
            "categories",
            {"code": "unknown_category", "name": "Unknown Category"},
            actor="test:existing",
        )
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["legacy_overrides"] = {
            "brands": {
                "Unbranded": {
                    "action": "import_inactive",
                    "reason": "not a canonical brand",
                }
            },
            "categories": {
                "Unknown Category": {
                    "action": "import_inactive",
                    "reason": "unresolved placeholder",
                }
            },
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        plan = self.plan()
        mapped = {
            item["payload"]["name"]: item
            for item in plan["records"]
            if item["payload"]["name"] in {"Unbranded", "Unknown Category"}
        }
        self.assertEqual("map_existing", mapped["Unbranded"]["action"])
        self.assertIn("active", mapped["Unbranded"]["difference_fields"])
        self.apply(plan)
        self.assertFalse(self.catalog.detail("brands", brand["id"])["active"])
        self.assertFalse(
            self.catalog.detail("categories", category["id"])["active"]
        )
        self.assertIn(
            "import_map",
            {
                item["action"]
                for item in self.catalog.audit(entity_id=brand["id"])["items"]
            },
        )
        with self.assertRaisesRegex(MasterDataConflict, "binding parity"):
            self.catalog.activate_legacy_projection(
                actor="test:activate",
                expected_plan_sha256=plan["plan_sha256"],
                expected_legacy_master_content_hash=(
                    plan["legacy_master_content_hash"]
                ),
            )

    def test_existing_manifest_record_deactivates_and_unknown_field_is_rejected(self) -> None:
        energy = self.catalog.create(
            "energy-types",
            {"code": "legacy_energy", "name": "Legacy Energy"},
            actor="test:existing",
        )
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["records"] = {
            "energy-types": [
                {
                    "code": "legacy_energy", "name": "Legacy Energy",
                    "active": False, "definition": "reviewed retirement",
                }
            ]
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        plan = self.plan()
        update = next(
            item for item in plan["records"]
            if item["type"] == "energy-types"
        )
        self.assertEqual("update_existing", update["action"])
        self.apply(plan)
        self.assertFalse(
            self.catalog.detail("energy-types", energy["id"])["active"]
        )
        self.assertIn(
            "import_update",
            {
                item["action"]
                for item in self.catalog.audit(entity_id=energy["id"])["items"]
            },
        )

        invalid = json.loads(self.manifest.read_text(encoding="utf-8"))
        invalid["records"]["energy-types"][0]["unknown_field"] = True
        invalid_path = self.root / "existing-unknown-field.json"
        invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaisesRegex(MasterDataError, "unsupported fields"):
            self.catalog.import_plan(
                legacy_db_copy=self.copy, manifest_path=invalid_path
            )

    def test_manifest_dependencies_are_validated_in_topological_order(self) -> None:
        region_id = stable_id("market-regions", "test_region")
        retailer_id = stable_id("retailers", "test_retailer")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["records"] = {
            "collection-channels": [
                {
                    "code": "test_channel", "name": "Test Channel",
                    "channel_type": "retailer_web",
                    "retailer_id": retailer_id,
                    "supported_content": ["review"],
                }
            ],
            "retailers": [
                {
                    "code": "test_retailer", "name": "Test Retailer",
                    "market_region_ids": [region_id],
                }
            ],
            "market-regions": [
                {"code": "test_region", "name": "Test Region"}
            ],
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        plan = self.plan()
        self.assertEqual(0, plan["counts"]["conflicts"])
        ordered_types = [
            item["type"] for item in plan["records"]
            if item["payload"].get("code") in {
                "test_region", "test_retailer", "test_channel",
            }
        ]
        self.assertEqual(
            ["market-regions", "retailers", "collection-channels"],
            ordered_types,
        )
        self.apply(plan)
        self.assertEqual(
            1, self.catalog.list(
                "collection-channels", q="test_channel", status="active"
            )["total"],
        )

    def test_copy_validation_rejects_fk_violation_and_live_inode_alias(self) -> None:
        invalid_copy = self.root / "invalid-fk-copy.sqlite3"
        with sqlite3.connect(invalid_copy) as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys=OFF;
                CREATE TABLE master_brands(
                    id INTEGER PRIMARY KEY,name TEXT,created_at TEXT,updated_at TEXT
                );
                CREATE TABLE master_categories(
                    id INTEGER PRIMARY KEY,name TEXT,created_at TEXT,updated_at TEXT
                );
                CREATE TABLE invalid_reference(
                    brand_id INTEGER REFERENCES master_brands(id)
                );
                INSERT INTO invalid_reference(brand_id) VALUES(999);
                """
            )
        with self.assertRaisesRegex(MasterDataError, "foreign_key_check"):
            self.catalog.import_plan(
                legacy_db_copy=invalid_copy, manifest_path=self.manifest
            )

        plan = self.plan()
        original_live = self.catalog.legacy_db_path
        self.catalog.legacy_db_path = self.copy
        try:
            with self.assertRaisesRegex(MasterDataConflict, "distinct file"):
                self.apply(plan)
        finally:
            self.catalog.legacy_db_path = original_live

        live_hardlink = self.root / "jobs-live-hardlink.sqlite3"
        os.link(self.legacy.db_path, live_hardlink)
        with self.assertRaisesRegex(MasterDataConflict, "distinct file"):
            self.catalog.apply_import(
                legacy_db_copy=live_hardlink,
                manifest_path=self.manifest,
                actor="test:import",
                expected_plan_sha256="not-used",
                expected_legacy_copy_sha256="not-used",
            )

        copy_symlink = self.root / "jobs-copy-link.sqlite3"
        copy_symlink.symlink_to(self.copy)
        with self.assertRaisesRegex(MasterDataError, "non-symlink"):
            self.catalog.import_plan(
                legacy_db_copy=copy_symlink, manifest_path=self.manifest
            )
        live_symlink = self.root / "jobs-live-link.sqlite3"
        live_symlink.symlink_to(self.legacy.db_path)
        original_live = self.catalog.legacy_db_path
        before_sha = hashlib.sha256(self.legacy.db_path.read_bytes()).hexdigest()
        before_mtime = self.legacy.db_path.stat().st_mtime_ns
        self.catalog.legacy_db_path = live_symlink
        try:
            with self.assertRaisesRegex(MasterDataConflict, "legacy-db-live"):
                self.apply(plan)
        finally:
            self.catalog.legacy_db_path = original_live
        self.assertEqual(
            before_sha, hashlib.sha256(self.legacy.db_path.read_bytes()).hexdigest()
        )
        self.assertEqual(before_mtime, self.legacy.db_path.stat().st_mtime_ns)

    def test_map_existing_audit_preserves_before_and_after_snapshots(self) -> None:
        with sqlite3.connect(self.legacy.db_path) as conn:
            source_name = conn.execute(
                "SELECT name FROM master_brands ORDER BY id LIMIT 1"
            ).fetchone()[0]
        existing = self.catalog.create(
            "brands",
            {"code": "reviewed_existing", "name": source_name},
            actor="test:pre-review",
        )
        plan = self.plan()
        mapped = [
            item for item in plan["records"]
            if item["type"] == "brands"
            and item["payload"]["name"] == source_name
        ]
        self.assertEqual("map_existing", mapped[0]["action"])
        self.apply(plan)
        event = next(
            item for item in self.catalog.audit(entity_id=existing["id"])["items"]
            if item["action"] == "import_map"
        )
        self.assertEqual([], event["before"]["source_mappings"])
        self.assertEqual(existing["revision"], event["before"]["revision"])
        self.assertGreater(event["after"]["revision"], event["before"]["revision"])
        self.assertEqual(
            {"wotu", "wotu-v1"},
            {item["system"] for item in event["after"]["source_mappings"]},
        )

    def test_legacy_unresolved_blocks_scope_activation(self) -> None:
        with sqlite3.connect(self.legacy.db_path) as conn:
            source_name = conn.execute(
                "SELECT name FROM master_brands ORDER BY id LIMIT 1"
            ).fetchone()[0]
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["legacy_overrides"] = {
            "brands": {
                source_name: {
                    "action": "unresolved",
                    "reason": "test review is incomplete",
                }
            }
        }
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        plan = self.plan()
        self.apply(plan)
        with self.assertRaisesRegex(MasterDataConflict, "still has unresolved"):
            self.catalog.activate_legacy_projection(
                actor="test:activate",
                expected_plan_sha256=plan["plan_sha256"],
                expected_legacy_master_content_hash=(
                    plan["legacy_master_content_hash"]
                ),
            )

    def test_import_inactive_blocks_activation_to_preserve_binding_behavior(self) -> None:
        with sqlite3.connect(self.legacy.db_path) as conn:
            source_name = conn.execute(
                "SELECT name FROM master_brands ORDER BY id LIMIT 1"
            ).fetchone()[0]
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["legacy_overrides"] = {
            "brands": {
                source_name: {
                    "action": "import_inactive",
                    "reason": "test placeholder is not valid for new binding",
                }
            }
        }
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        legacy_consumer = Store()
        before = legacy_consumer.list_master_data()
        plan = self.plan()
        self.apply(plan)
        with self.assertRaisesRegex(
            MasterDataConflict, "legacy binding parity failed"
        ):
            self.catalog.activate_legacy_projection(
                actor="test:activate",
                expected_plan_sha256=plan["plan_sha256"],
                expected_legacy_master_content_hash=(
                    plan["legacy_master_content_hash"]
                ),
            )
        self.assertFalse(legacy_consumer.legacy_master_data_activated())
        self.assertEqual(before, legacy_consumer.list_master_data())
        self.assertEqual(
            source_name,
            legacy_consumer.canonical_master_value("brand", source_name),
        )

    def test_import_does_not_switch_legacy_and_activation_is_explicit(self) -> None:
        plan = self.plan()
        result = self.apply(plan)
        self.assertTrue(result["reconcile"]["ok"])
        pre_activation_store = Store()
        self.assertFalse(pre_activation_store.legacy_master_data_activated())
        before_total = self.catalog.list("brands", status="all")["total"]
        with self.assertRaisesRegex(MasterDataConflict, "scope is inactive"):
            self.catalog.legacy_create(
                "brand", "Must Not Persist", actor="test:legacy"
            )
        self.assertEqual(
            before_total, self.catalog.list("brands", status="all")["total"]
        )
        status = self.catalog.integration_status()
        self.assertEqual("inactive", status["consumer_scopes"][0]["status"])

        self.catalog.activate_legacy_projection(
            actor="test:activate",
            expected_plan_sha256=plan["plan_sha256"],
            expected_legacy_master_content_hash=plan["legacy_master_content_hash"],
        )
        self.assertTrue(pre_activation_store.legacy_master_data_activated())
        activated = Store()
        self.assertTrue(activated.legacy_master_data_activated())
        before_frozen = self.catalog._read_legacy_snapshot(
            self.legacy.db_path, immutable=False
        )[2]
        item = self.catalog.create(
            "brands", {"code": "round_trip", "name": "Round Trip"},
            actor="test:admin",
        )
        legacy_id = next(
            row["id"] for row in activated.list_master_data()["brands"]
            if row["name"] == "Round Trip"
        )
        self.assertEqual(
            before_frozen,
            self.catalog._read_legacy_snapshot(
                self.legacy.db_path, immutable=False
            )[2],
        )
        disabled = self.catalog.set_active(
            "brands", item["id"], False, actor="test:admin",
            expected_revision=item["revision"],
        )
        self.assertNotIn(
            "Round Trip",
            {row["name"] for row in activated.list_master_data()["brands"]},
        )
        enabled = self.catalog.set_active(
            "brands", item["id"], True, actor="test:admin",
            expected_revision=disabled["revision"],
        )
        self.assertTrue(enabled["active"])
        self.assertEqual(
            legacy_id,
            next(
                row["id"] for row in activated.list_master_data()["brands"]
                if row["name"] == "Round Trip"
            ),
        )
        with self.assertRaisesRegex(MasterDataConflict, "post-activation"):
            self.catalog.deactivate_legacy_projection(
                actor="test:rollback",
                expected_catalog_hash=self.catalog.summary()["catalog_content_hash"],
            )

    def test_activation_requires_required_mode_and_missing_catalog_fails(self) -> None:
        plan = self.plan()
        self.apply(plan)
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "false"
        with self.assertRaisesRegex(
            MasterDataError, "CATALOG_REQUIRED=true"
        ):
            self.catalog.activate_legacy_projection(
                actor="test:activate",
                expected_plan_sha256=plan["plan_sha256"],
                expected_legacy_master_content_hash=(
                    plan["legacy_master_content_hash"]
                ),
            )
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "true"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "false"
        with self.assertRaisesRegex(
            MasterDataError, "PROTECT_LEGACY_WRITES=true"
        ):
            self.catalog.activate_legacy_projection(
                actor="test:activate",
                expected_plan_sha256=plan["plan_sha256"],
                expected_legacy_master_content_hash=(
                    plan["legacy_master_content_hash"]
                ),
            )
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "true"
        os.environ["MASTER_DATA_ADMIN_WRITES_ENABLED"] = "false"
        with self.assertRaisesRegex(
            MasterDataError, "ADMIN_WRITES_ENABLED=true"
        ):
            self.catalog.activate_legacy_projection(
                actor="test:activate",
                expected_plan_sha256=plan["plan_sha256"],
                expected_legacy_master_content_hash=(
                    plan["legacy_master_content_hash"]
                ),
            )
        os.environ["MASTER_DATA_ADMIN_WRITES_ENABLED"] = "true"
        previous_proxy_token = os.environ.pop(
            "MASTER_DATA_ADMIN_PROXY_TOKEN", None
        )
        try:
            with self.assertRaisesRegex(
                MasterDataError, "ADMIN_PROXY_TOKEN"
            ):
                self.catalog.activate_legacy_projection(
                    actor="test:activate",
                    expected_plan_sha256=plan["plan_sha256"],
                    expected_legacy_master_content_hash=(
                        plan["legacy_master_content_hash"]
                    ),
                )
        finally:
            if previous_proxy_token is not None:
                os.environ[
                    "MASTER_DATA_ADMIN_PROXY_TOKEN"
                ] = previous_proxy_token
        os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = "x" * 31
        weak_status = self.catalog.integration_status()
        self.assertFalse(weak_status["admin_proxy_configured"])
        with self.assertRaisesRegex(MasterDataError, "at least 32 bytes"):
            self.catalog.activate_legacy_projection(
                actor="test:activate",
                expected_plan_sha256=plan["plan_sha256"],
                expected_legacy_master_content_hash=(
                    plan["legacy_master_content_hash"]
                ),
            )
        os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = TEST_PROXY_TOKEN
        previous_trusted_actors = os.environ.pop(
            "MASTER_DATA_TRUSTED_ACTORS", None
        )
        try:
            with self.assertRaisesRegex(
                MasterDataError, "TRUSTED_ACTORS"
            ):
                self.catalog.activate_legacy_projection(
                    actor="test:activate",
                    expected_plan_sha256=plan["plan_sha256"],
                    expected_legacy_master_content_hash=(
                        plan["legacy_master_content_hash"]
                    ),
                )
        finally:
            if previous_trusted_actors is not None:
                os.environ[
                    "MASTER_DATA_TRUSTED_ACTORS"
                ] = previous_trusted_actors
        os.environ["MASTER_DATA_TRUSTED_ACTORS"] = "admin-ui,test:bare"
        invalid_actor_status = self.catalog.integration_status()
        self.assertEqual(0, invalid_actor_status["trusted_actor_count"])
        with self.assertRaisesRegex(MasterDataError, "basic:<htpasswd-user>"):
            self.catalog.activate_legacy_projection(
                actor="test:activate",
                expected_plan_sha256=plan["plan_sha256"],
                expected_legacy_master_content_hash=(
                    plan["legacy_master_content_hash"]
                ),
            )
        os.environ["MASTER_DATA_TRUSTED_ACTORS"] = TEST_PROD_ACTOR
        os.environ["MASTER_DATA_DEV_MODE"] = "true"
        with self.assertRaisesRegex(MasterDataError, "DEV_MODE"):
            self.catalog.activate_legacy_projection(
                actor="test:activate",
                expected_plan_sha256=plan["plan_sha256"],
                expected_legacy_master_content_hash=(
                    plan["legacy_master_content_hash"]
                ),
            )
        os.environ["MASTER_DATA_DEV_MODE"] = "false"
        activated = self.catalog.activate_legacy_projection(
            actor="test:activate",
            expected_plan_sha256=plan["plan_sha256"],
            expected_legacy_master_content_hash=plan["legacy_master_content_hash"],
        )
        self.assertTrue(activated["activated"])
        hidden_catalog = self.root / "master_data.sqlite3.missing-probe"
        self.catalog.db_path.replace(hidden_catalog)
        try:
            with self.assertRaisesRegex(ValueError, "required.*missing"):
                Store()
        finally:
            hidden_catalog.replace(self.catalog.db_path)

    def test_never_active_deactivate_is_zero_write_and_catalog_reopens(self) -> None:
        before_sha = hashlib.sha256(self.catalog.db_path.read_bytes()).hexdigest()
        before_mtime = self.catalog.db_path.stat().st_mtime_ns
        with self.assertRaisesRegex(MasterDataConflict, "not active"):
            self.catalog.deactivate_legacy_projection(
                actor="test:rollback",
                expected_catalog_hash=(
                    self.catalog.summary()["catalog_content_hash"]
                ),
            )
        self.assertEqual(
            before_sha, hashlib.sha256(self.catalog.db_path.read_bytes()).hexdigest()
        )
        self.assertEqual(before_mtime, self.catalog.db_path.stat().st_mtime_ns)
        reopened = MasterDataStore(
            self.root, legacy_db_path=self.legacy.db_path
        )
        self.assertFalse(reopened.legacy_projection_active())

    def test_deactivate_then_fresh_import_refreshes_inactive_projection_seal(self) -> None:
        first = self.plan()
        self.apply(first)
        self.catalog.activate_legacy_projection(
            actor="test:activate",
            expected_plan_sha256=first["plan_sha256"],
            expected_legacy_master_content_hash=(
                first["legacy_master_content_hash"]
            ),
        )
        self.catalog.deactivate_legacy_projection(
            actor="test:rollback",
            expected_catalog_hash=self.catalog.summary()["catalog_content_hash"],
        )
        inactive_store = Store()
        inactive_store.create_master_data("brand", "After Rollback Brand")
        copy_sqlite(self.legacy.db_path, self.copy)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["manifest_id"] = "test-after-rollback"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        second = self.plan()
        self.apply(second)
        reopened = MasterDataStore(
            self.root, legacy_db_path=self.legacy.db_path
        )
        self.assertFalse(reopened.legacy_projection_active())
        activated = reopened.activate_legacy_projection(
            actor="test:reactivate",
            expected_plan_sha256=second["plan_sha256"],
            expected_legacy_master_content_hash=(
                second["legacy_master_content_hash"]
            ),
        )
        self.assertTrue(activated["activated"])

    def test_inactive_task_write_linearizes_before_scope_activation(self) -> None:
        plan = self.plan()
        self.apply(plan)
        inactive_store = Store()
        brand = inactive_store.list_master_data()["brands"][0]["name"]
        runtime_catalog = inactive_store.shared_master_data
        original = runtime_catalog.legacy_binding_resolver
        entered = threading.Event()
        release = threading.Event()
        activation_done = threading.Event()
        errors = []

        @contextmanager
        def blocked_resolver():
            with original() as resolve:
                def blocked(raw_brand, raw_category):
                    result = resolve(raw_brand, raw_category)
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("inactive binding barrier timed out")
                    return result

                yield blocked

        runtime_catalog.legacy_binding_resolver = blocked_resolver

        def create_task():
            try:
                inactive_store.create_job(
                    "inactive-activation-race", "scrape", "lowes",
                    target_url="https://www.lowes.com/pd/inactive-race/100",
                    brand=brand,
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def activate():
            try:
                self.catalog.activate_legacy_projection(
                    actor="test:activate",
                    expected_plan_sha256=plan["plan_sha256"],
                    expected_legacy_master_content_hash=(
                        plan["legacy_master_content_hash"]
                    ),
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                activation_done.set()

        task_thread = threading.Thread(target=create_task)
        activation_thread = threading.Thread(target=activate)
        try:
            task_thread.start()
            self.assertTrue(entered.wait(5))
            activation_thread.start()
            self.assertFalse(activation_done.wait(0.1))
            release.set()
            task_thread.join(5)
            activation_thread.join(5)
        finally:
            release.set()
            runtime_catalog.legacy_binding_resolver = original
        self.assertFalse(task_thread.is_alive())
        self.assertFalse(activation_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(
            brand, inactive_store.get_job("inactive-activation-race")["brand"]
        )
        self.assertTrue(inactive_store.legacy_master_data_activated())

    def test_inactive_legacy_crud_blocks_stale_activation_receipt(self) -> None:
        plan = self.plan()
        self.apply(plan)
        inactive_store = Store()
        runtime_catalog = inactive_store.shared_master_data
        original = runtime_catalog.legacy_scope_snapshot
        entered = threading.Event()
        release = threading.Event()
        activation_done = threading.Event()
        create_errors = []
        activation_errors = []

        @contextmanager
        def blocked_scope():
            with original() as active:
                self.assertFalse(active)
                entered.set()
                if not release.wait(5):
                    raise AssertionError("inactive CRUD barrier timed out")
                yield active

        runtime_catalog.legacy_scope_snapshot = blocked_scope

        def legacy_create():
            try:
                inactive_store.create_master_data(
                    "brand", "Concurrent Legacy Brand"
                )
            except Exception as exc:  # pragma: no cover - asserted below
                create_errors.append(exc)

        def activate():
            try:
                self.catalog.activate_legacy_projection(
                    actor="test:activate",
                    expected_plan_sha256=plan["plan_sha256"],
                    expected_legacy_master_content_hash=(
                        plan["legacy_master_content_hash"]
                    ),
                )
            except Exception as exc:
                activation_errors.append(exc)
            finally:
                activation_done.set()

        create_thread = threading.Thread(target=legacy_create)
        activation_thread = threading.Thread(target=activate)
        try:
            create_thread.start()
            self.assertTrue(entered.wait(5))
            activation_thread.start()
            self.assertFalse(activation_done.wait(0.1))
            release.set()
            create_thread.join(5)
            activation_thread.join(5)
        finally:
            release.set()
            runtime_catalog.legacy_scope_snapshot = original
        self.assertEqual([], create_errors)
        self.assertEqual(1, len(activation_errors))
        self.assertIsInstance(activation_errors[0], MasterDataConflict)
        self.assertIn("content hash", str(activation_errors[0]))
        self.assertFalse(inactive_store.legacy_master_data_activated())

    def test_rollback_checks_frozen_legacy_hash_and_unresolved_blocks_activation(self) -> None:
        plan = self.plan()
        self.apply(plan)
        self.catalog.activate_legacy_projection(
            actor="test:activate",
            expected_plan_sha256=plan["plan_sha256"],
            expected_legacy_master_content_hash=plan["legacy_master_content_hash"],
        )
        running_store = Store()
        safe_hash = self.catalog.summary()["catalog_content_hash"]
        self.catalog.deactivate_legacy_projection(
            actor="test:rollback", expected_catalog_hash=safe_hash
        )
        self.assertFalse(running_store.legacy_master_data_activated())
        self.catalog.activate_legacy_projection(
            actor="test:activate-again",
            expected_plan_sha256=plan["plan_sha256"],
            expected_legacy_master_content_hash=plan["legacy_master_content_hash"],
        )
        self.assertTrue(running_store.legacy_master_data_activated())
        brand = self.catalog.list("brands", status="active")["items"][0]
        with self.catalog._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO legacy_job_rebind_intents(
                    id,entity_type,entity_id,old_name,new_name,status,
                    actor,created_at
                ) VALUES('pending-test','brands',?,?,?,'pending',?,?)
                """,
                (
                    brand["id"], brand["name"], brand["name"],
                    "test:failure", "2026-01-01T00:00:00+00:00",
                ),
            )
            self.catalog._refresh_legacy_projection_seal(conn)
            conn.commit()
        with self.assertRaisesRegex(MasterDataConflict, "rebind recovery"):
            running_store.list_master_data()
        self.catalog.recover_legacy_rebinds(actor="test:recover")
        self.assertTrue(running_store.legacy_master_data_activated())
        with sqlite3.connect(self.legacy.db_path) as conn:
            conn.execute(
                """
                INSERT INTO master_categories(name,created_at,updated_at)
                VALUES('Frozen Drift','2026-01-01','2026-01-01')
                """
            )
        with self.assertRaisesRegex(MasterDataConflict, "frozen legacy"):
            self.catalog.deactivate_legacy_projection(
                actor="test:rollback",
                expected_catalog_hash=self.catalog.summary()["catalog_content_hash"],
            )


class LegacyProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous_data_dir = os.environ.get("DATA_DIR")
        self.previous_catalog_required = os.environ.get(
            "MASTER_DATA_CATALOG_REQUIRED"
        )
        self.previous_legacy_write_protection = os.environ.get(
            "MASTER_DATA_PROTECT_LEGACY_WRITES"
        )
        self.previous_activation_auth = {
            key: os.environ.get(key)
            for key in (
                "MASTER_DATA_ADMIN_WRITES_ENABLED",
                "MASTER_DATA_ADMIN_PROXY_TOKEN",
                "MASTER_DATA_TRUSTED_ACTORS",
                "MASTER_DATA_DEV_MODE",
            )
        }
        os.environ["DATA_DIR"] = str(self.root)
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "false"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "false"
        self.legacy_store = Store()
        self.copy = self.root / "jobs-copy.sqlite3"
        copy_sqlite(self.legacy_store.db_path, self.copy)
        self.manifest = minimal_manifest(self.root / "manifest.json")
        catalog = MasterDataStore(
            self.root,
            legacy_db_path=self.legacy_store.db_path,
            initialize=True,
        )
        plan = catalog.import_plan(
            legacy_db_copy=self.copy, manifest_path=self.manifest
        )
        catalog.apply_import(
            legacy_db_copy=self.copy,
            manifest_path=self.manifest,
            actor="test:import",
            expected_plan_sha256=plan["plan_sha256"],
            expected_legacy_copy_sha256=plan["legacy_copy_sha256"],
        )
        # Importing prepares the catalog but does not switch existing callers.
        pre_activation = Store()
        self.assertFalse(pre_activation.legacy_master_data_activated())
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "true"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "true"
        os.environ["MASTER_DATA_ADMIN_WRITES_ENABLED"] = "true"
        os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = TEST_PROXY_TOKEN
        os.environ["MASTER_DATA_TRUSTED_ACTORS"] = TEST_PROD_ACTOR
        os.environ["MASTER_DATA_DEV_MODE"] = "false"
        catalog.activate_legacy_projection(
            actor="test:activate",
            expected_plan_sha256=plan["plan_sha256"],
            expected_legacy_master_content_hash=plan["legacy_master_content_hash"],
        )
        self.store = Store()
        self.assertTrue(self.store.legacy_master_data_activated())

    def tearDown(self) -> None:
        if self.previous_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.previous_data_dir
        if self.previous_catalog_required is None:
            os.environ.pop("MASTER_DATA_CATALOG_REQUIRED", None)
        else:
            os.environ[
                "MASTER_DATA_CATALOG_REQUIRED"
            ] = self.previous_catalog_required
        if self.previous_legacy_write_protection is None:
            os.environ.pop("MASTER_DATA_PROTECT_LEGACY_WRITES", None)
        else:
            os.environ[
                "MASTER_DATA_PROTECT_LEGACY_WRITES"
            ] = self.previous_legacy_write_protection
        for key, value in self.previous_activation_auth.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _assert_store_startup_fails_without_file_mutation(
        self, message: str
    ) -> None:
        paths = (
            self.root / "master_data.sqlite3",
            self.root / "jobs.sqlite3",
        )
        before = {
            path: (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
            for path in paths
        }
        with self.assertRaisesRegex(ValueError, message):
            Store()
        for path in paths:
            self.assertEqual(
                before[path][0], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            self.assertEqual(before[path][1], path.stat().st_mtime_ns)

    def _run_binding_race(self, task_write, catalog_write):
        catalog = self.store.shared_master_data
        original = catalog.legacy_binding_resolver
        entered = threading.Event()
        release = threading.Event()
        writer_done = threading.Event()
        task_errors = []
        writer_errors = []

        @contextmanager
        def blocked_resolver():
            with original() as resolve:
                def blocked(brand, category):
                    result = resolve(brand, category)
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("binding barrier timed out")
                    return result

                yield blocked

        catalog.legacy_binding_resolver = blocked_resolver

        def run_task():
            try:
                task_write()
            except Exception as exc:  # pragma: no cover - asserted below
                task_errors.append(exc)

        def run_writer():
            try:
                catalog_write()
            except Exception as exc:
                writer_errors.append(exc)
            finally:
                writer_done.set()

        task_thread = threading.Thread(target=run_task)
        writer_thread = threading.Thread(target=run_writer)
        try:
            task_thread.start()
            self.assertTrue(entered.wait(5))
            writer_thread.start()
            self.assertFalse(writer_done.wait(0.1))
            release.set()
            task_thread.join(5)
            writer_thread.join(5)
            self.assertFalse(task_thread.is_alive(), "task write deadlocked")
            self.assertFalse(writer_thread.is_alive(), "catalog write deadlocked")
        finally:
            release.set()
            catalog.legacy_binding_resolver = original
        self.assertEqual([], task_errors)
        return writer_errors

    def test_new_admin_and_legacy_crud_are_immediate_bidirectional_projections(self) -> None:
        catalog = self.store.shared_master_data
        before_hash = catalog.summary()["catalog_content_hash"]
        brand = catalog.create(
            "brands",
            {"code": "projection_brand", "name": "Projection Brand"},
            actor="test:admin",
            expected_catalog_hash=before_hash,
        )
        legacy = self.store.list_master_data()
        legacy_brand = next(item for item in legacy["brands"] if item["name"] == "Projection Brand")
        self.assertEqual("Projection Brand", self.store.canonical_master_value("brand", "Projection Brand"))
        self.assertEqual(
            {"id", "name", "created_at", "updated_at"}, set(legacy_brand)
        )

        self.store.create_job(
            "v2-rename-history",
            "scrape",
            "lowes",
            target_url="https://www.lowes.com/pd/v2-rename/100",
            brand="Projection Brand",
        )

        brand = catalog.update(
            "brands",
            brand["id"],
            {"name": "Projection Brand Renamed"},
            actor="test:admin",
            expected_revision=brand["revision"],
        )
        self.assertEqual(
            "Projection Brand Renamed",
            self.store.canonical_master_value("brand", "Projection Brand Renamed"),
        )
        self.assertEqual(
            "Projection Brand Renamed",
            catalog.resolve("brands", "Projection Brand")["record"]["name"],
        )
        self.assertEqual(
            brand["id"],
            catalog.resolve("brands", "Projection Brand")["record"]["id"],
        )
        self.assertEqual(
            "Projection Brand",
            self.store.get_job("v2-rename-history")["brand"],
        )

        legacy_created = self.store.create_master_data(
            "brand", "Legacy Created", actor="test:legacy"
        )
        self.store.create_job(
            "legacy-rename-history",
            "scrape",
            "lowes",
            target_url="https://www.lowes.com/pd/legacy-rename/100",
            brand="Legacy Created",
        )
        v2 = catalog.list("brands", q="Legacy Created", status="all")
        self.assertEqual(1, v2["total"])
        legacy_updated = self.store.update_master_data(
            "brand", legacy_created["id"], "Legacy Renamed", actor="test:legacy"
        )
        self.assertEqual("Legacy Renamed", legacy_updated["name"])
        self.assertEqual(
            "Legacy Renamed",
            self.store.get_job("legacy-rename-history")["brand"],
        )
        self.assertEqual(1, catalog.list("brands", q="Legacy Renamed", status="all")["total"])
        with self.assertRaisesRegex(ValueError, "已有 1 条"):
            self.store.delete_master_data(
                "brand", legacy_created["id"], actor="test:legacy"
            )
        unbound = self.store.create_master_data(
            "brand", "Legacy Unbound", actor="test:legacy"
        )
        deleted = self.store.delete_master_data(
            "brand", unbound["id"], actor="test:legacy"
        )
        self.assertEqual("Legacy Unbound", deleted["name"])
        self.assertEqual(
            0,
            catalog.list("brands", q="Legacy Unbound", status="active")["total"],
        )

        current_hash = catalog.summary()["catalog_content_hash"]
        with self.assertRaisesRegex(MasterDataConflict, "post-activation"):
            catalog.deactivate_legacy_projection(
                actor="test:rollback",
                expected_catalog_hash=current_hash,
            )

    def test_legacy_rename_intent_failure_boundaries_and_recovery_actor(self) -> None:
        catalog = self.store.shared_master_data
        rolled_back = self.store.create_master_data(
            "brand", "Intent Rollback", actor="test:legacy"
        )
        original_advance = catalog._advance_catalog

        def fail_advance(*_args, **_kwargs):
            raise RuntimeError("injected catalog revision failure")

        catalog._advance_catalog = fail_advance
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.store.update_master_data(
                    "brand", rolled_back["id"], "Should Roll Back",
                    actor="test:legacy",
                )
        finally:
            catalog._advance_catalog = original_advance
        self.assertIn(
            "Intent Rollback",
            {row["name"] for row in self.store.list_master_data()["brands"]},
        )

        pending_brand = self.store.create_master_data(
            "brand", "Pending Rename", actor="test:legacy"
        )
        self.store.create_job(
            "pending-rebind-job", "scrape", "lowes",
            target_url="https://www.lowes.com/pd/pending-rebind/100",
            brand="Pending Rename",
        )
        original_apply = catalog._apply_legacy_rebind_intent

        def fail_apply(*_args, **_kwargs):
            raise RuntimeError("injected rebind apply failure")

        catalog._apply_legacy_rebind_intent = fail_apply
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.store.update_master_data(
                    "brand", pending_brand["id"], "Recovered Rename",
                    actor="test:legacy",
                )
        finally:
            catalog._apply_legacy_rebind_intent = original_apply
        pending_status = catalog.integration_status()
        self.assertEqual(1, pending_status["pending_legacy_rebinds"])
        self.assertTrue(
            any(
                "rebind recovery" in blocker
                for blocker in pending_status["deployment_blockers"]
            )
        )
        with self.assertRaisesRegex(MasterDataConflict, "rebind recovery"):
            Store()
        catalog.recover_legacy_rebinds(actor="test:recover")
        self.assertEqual(
            "Recovered Rename",
            self.store.get_job("pending-rebind-job")["brand"],
        )
        with catalog._connect() as conn:
            completed = conn.execute(
                """
                SELECT status,completed_by FROM legacy_job_rebind_intents
                WHERE entity_id=(
                    SELECT entity_id FROM legacy_projection
                    WHERE entity_type='brands' AND legacy_integer_id=?
                )
                """,
                (pending_brand["id"],),
            ).fetchone()
        self.assertEqual("completed", completed["status"])
        self.assertEqual("test:recover", completed["completed_by"])

    def test_jobs_commit_before_intent_completion_recovers_idempotently(self) -> None:
        catalog = self.store.shared_master_data
        brand = self.store.create_master_data(
            "brand", "Completion Boundary", actor="test:legacy"
        )
        self.store.create_job(
            "completion-boundary-job", "scrape", "lowes",
            target_url="https://www.lowes.com/pd/completion-boundary/100",
            brand="Completion Boundary",
        )
        original_refresh = catalog._refresh_legacy_projection_seal
        calls = 0

        def fail_second_refresh(conn):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected completion seal failure")
            return original_refresh(conn)

        catalog._refresh_legacy_projection_seal = fail_second_refresh
        try:
            with self.assertRaisesRegex(RuntimeError, "completion seal"):
                self.store.update_master_data(
                    "brand", brand["id"], "Completion Recovered",
                    actor="test:legacy",
                )
        finally:
            catalog._refresh_legacy_projection_seal = original_refresh
        self.assertEqual(
            "Completion Recovered",
            self.store.get_job("completion-boundary-job")["brand"],
        )
        with self.assertRaisesRegex(MasterDataConflict, "rebind recovery"):
            Store()
        catalog.recover_legacy_rebinds(actor="test:recover-completion")
        self.assertTrue(Store().legacy_master_data_activated())

    def test_rebind_intent_tamper_fails_projection_seal(self) -> None:
        catalog = self.store.shared_master_data
        brand = self.store.create_master_data(
            "brand", "Intent Tamper", actor="test:legacy"
        )
        self.store.update_master_data(
            "brand", brand["id"], "Intent Tamper Renamed",
            actor="test:legacy",
        )
        with sqlite3.connect(catalog.db_path) as conn:
            conn.execute(
                """
                UPDATE legacy_job_rebind_intents SET new_name='Tampered Intent'
                WHERE entity_id=(
                    SELECT entity_id FROM legacy_projection
                    WHERE entity_type='brands' AND legacy_integer_id=?
                )
                """,
                (brand["id"],),
            )
        self._assert_store_startup_fails_without_file_mutation(
            "projection content seal mismatch"
        )

    def test_rebind_intent_delete_fails_projection_seal(self) -> None:
        catalog = self.store.shared_master_data
        brand = self.store.create_master_data(
            "brand", "Intent Delete", actor="test:legacy"
        )
        self.store.update_master_data(
            "brand", brand["id"], "Intent Delete Renamed",
            actor="test:legacy",
        )
        with sqlite3.connect(catalog.db_path) as conn:
            conn.execute(
                """
                DELETE FROM legacy_job_rebind_intents
                WHERE entity_id=(
                    SELECT entity_id FROM legacy_projection
                    WHERE entity_type='brands' AND legacy_integer_id=?
                )
                """,
                (brand["id"],),
            )
        self._assert_store_startup_fails_without_file_mutation(
            "projection content seal mismatch"
        )

    def test_legacy_delete_counts_v2_rename_alias_bindings(self) -> None:
        catalog = self.store.shared_master_data
        legacy = self.store.create_master_data(
            "brand", "Alias Bound A", actor="test:legacy"
        )
        self.store.create_job(
            "alias-bound-job", "scrape", "lowes",
            target_url="https://www.lowes.com/pd/alias-bound/100",
            brand="Alias Bound A",
        )
        entity = catalog.resolve("brands", "Alias Bound A")["record"]
        catalog.update(
            "brands", entity["id"], {"name": "Alias Bound B"},
            actor="test:admin", expected_revision=entity["revision"],
        )
        with self.assertRaisesRegex(MasterDataConflict, "已有 1 条"):
            self.store.delete_master_data(
                "brand", legacy["id"], actor="test:legacy"
            )

    def test_legacy_patch_rebinds_all_v2_rename_alias_history(self) -> None:
        catalog = self.store.shared_master_data
        legacy = self.store.create_master_data(
            "brand", "Patch Alias A", actor="test:legacy"
        )
        self.store.create_job(
            "patch-alias-history", "scrape", "lowes",
            target_url="https://www.lowes.com/pd/patch-alias-history/100",
            brand="Patch Alias A",
        )
        entity = catalog.resolve("brands", "Patch Alias A")["record"]
        catalog.update(
            "brands", entity["id"], {"name": "Patch Alias B"},
            actor="test:admin", expected_revision=entity["revision"],
        )
        self.assertEqual(
            "Patch Alias A", self.store.get_job("patch-alias-history")["brand"]
        )
        self.store.update_master_data(
            "brand", legacy["id"], "Patch Alias C", actor="test:legacy"
        )
        self.assertEqual(
            "Patch Alias C", self.store.get_job("patch-alias-history")["brand"]
        )

    def test_task_create_linearizes_with_v2_rename_and_disable(self) -> None:
        catalog = self.store.shared_master_data
        brand = catalog.create(
            "brands", {"code": "race_v2", "name": "Race V2"},
            actor="test:admin",
        )
        writer_errors = self._run_binding_race(
            lambda: self.store.create_job(
                "race-v2-before", "scrape", "lowes",
                target_url="https://www.lowes.com/pd/race-v2-before/100",
                brand="Race V2",
            ),
            lambda: catalog.update(
                "brands", brand["id"], {"name": "Race V2 Renamed"},
                actor="test:admin", expected_revision=brand["revision"],
            ),
        )
        self.assertEqual([], writer_errors)
        self.assertEqual(
            "Race V2", self.store.get_job("race-v2-before")["brand"]
        )
        self.store.create_job(
            "race-v2-after", "scrape", "lowes",
            target_url="https://www.lowes.com/pd/race-v2-after/100",
            brand="Race V2",
        )
        self.assertEqual(
            "Race V2 Renamed", self.store.get_job("race-v2-after")["brand"]
        )

        disabled = catalog.detail("brands", brand["id"])
        writer_errors = self._run_binding_race(
            lambda: self.store.create_job(
                "race-disable-before", "scrape", "lowes",
                target_url="https://www.lowes.com/pd/race-disable-before/100",
                brand="Race V2 Renamed",
            ),
            lambda: catalog.set_active(
                "brands", disabled["id"], False, actor="test:admin",
                expected_revision=disabled["revision"],
            ),
        )
        self.assertEqual([], writer_errors)
        with self.assertRaisesRegex(MasterDataError, "未在主数据中维护"):
            self.store.create_job(
                "race-disable-after", "scrape", "lowes",
                target_url="https://www.lowes.com/pd/race-disable-after/100",
                brand="Race V2 Renamed",
            )

    def test_task_create_linearizes_with_legacy_rename_and_delete(self) -> None:
        legacy = self.store.create_master_data(
            "brand", "Race Legacy", actor="test:legacy"
        )
        writer_errors = self._run_binding_race(
            lambda: self.store.create_job(
                "race-legacy-rename", "scrape", "lowes",
                target_url="https://www.lowes.com/pd/race-legacy-rename/100",
                brand="Race Legacy",
            ),
            lambda: self.store.update_master_data(
                "brand", legacy["id"], "Race Legacy Renamed",
                actor="test:legacy",
            ),
        )
        self.assertEqual([], writer_errors)
        self.assertEqual(
            "Race Legacy Renamed",
            self.store.get_job("race-legacy-rename")["brand"],
        )

        delete_candidate = self.store.create_master_data(
            "brand", "Race Delete", actor="test:legacy"
        )
        writer_errors = self._run_binding_race(
            lambda: self.store.create_job(
                "race-legacy-delete", "scrape", "lowes",
                target_url="https://www.lowes.com/pd/race-legacy-delete/100",
                brand="Race Delete",
            ),
            lambda: self.store.delete_master_data(
                "brand", delete_candidate["id"], actor="test:legacy"
            ),
        )
        self.assertEqual(1, len(writer_errors))
        self.assertIsInstance(writer_errors[0], MasterDataConflict)
        self.assertIn("已有 1 条", str(writer_errors[0]))

    def test_alias_binding_canonicalizes_job_only_and_enrich_skips_unwritten(self) -> None:
        catalog = self.store.shared_master_data
        brand = catalog.create(
            "brands",
            {
                "code": "evidence_brand", "name": "Evidence Canonical",
                "aliases": ["Evidence Raw Alias"],
            },
            actor="test:admin",
        )
        observation = {"brand": "Evidence Raw Alias", "category": "Source Raw"}
        platform = {"brand": "Evidence Raw Alias", "status": "ready"}
        self.store.create_job(
            "raw-evidence-job", "scrape", "lowes",
            target_url="https://www.lowes.com/pd/raw-evidence/100",
            brand="Evidence Raw Alias", product_observation=observation,
            voltage_platform=platform,
        )
        job = self.store.get_job("raw-evidence-job")
        self.assertEqual(brand["name"], job["brand"])
        self.assertEqual("Evidence Raw Alias", job["product_observation"]["brand"])
        self.assertEqual("Evidence Raw Alias", job["voltage_platform_brand"])
        unchanged = self.store.enrich_metadata(
            "raw-evidence-job", brand="Not Governed Anywhere"
        )
        self.assertEqual(brand["name"], unchanged["brand"])
        updated = self.store.update_job(
            "raw-evidence-job", brand="Evidence Raw Alias"
        )
        self.assertEqual(brand["name"], updated["brand"])

    def test_existing_tampered_active_catalog_stops_store_startup(self) -> None:
        catalog = self.store.shared_master_data
        brand = catalog.create(
            "brands",
            {"code": "post_activation_probe", "name": "Post Activation Probe"},
            actor="test:admin",
        )
        with sqlite3.connect(catalog.db_path) as conn:
            conn.execute(
                "UPDATE master_entities SET name='Tampered Probe' WHERE id=?",
                (brand["id"],),
            )
        with self.assertRaisesRegex(ValueError, "content hash mismatch"):
            Store()

    def test_active_projection_deleted_mapping_fails_sealed_startup(self) -> None:
        catalog = self.store.shared_master_data
        with catalog._connect() as conn:
            row = conn.execute(
                """
                SELECT p.entity_id FROM legacy_projection p
                JOIN legacy_projection_compat_snapshot s
                  ON s.entity_id=p.entity_id
                ORDER BY p.entity_type,p.legacy_integer_id LIMIT 1
                """
            ).fetchone()
        with sqlite3.connect(catalog.db_path) as conn:
            conn.execute(
                "DELETE FROM legacy_projection WHERE entity_id=?",
                (row["entity_id"],),
            )
        self._assert_store_startup_fails_without_file_mutation(
            "projection content seal mismatch"
        )

    def test_active_projection_integer_id_tamper_fails_sealed_startup(self) -> None:
        catalog = self.store.shared_master_data
        with catalog._connect() as conn:
            row = conn.execute(
                """
                SELECT entity_id,legacy_integer_id FROM legacy_projection
                ORDER BY entity_type,legacy_integer_id LIMIT 1
                """
            ).fetchone()
            next_id = int(conn.execute(
                "SELECT MAX(legacy_integer_id) FROM legacy_projection"
            ).fetchone()[0]) + 1000
        with sqlite3.connect(catalog.db_path) as conn:
            conn.execute(
                """
                UPDATE legacy_projection SET legacy_integer_id=?
                WHERE entity_id=?
                """,
                (next_id, row["entity_id"]),
            )
        self._assert_store_startup_fails_without_file_mutation(
            "projection content seal mismatch"
        )

    def test_active_projection_snapshot_tamper_fails_sealed_startup(self) -> None:
        catalog = self.store.shared_master_data
        with catalog._connect() as conn:
            row = conn.execute(
                """
                SELECT entity_id FROM legacy_projection_compat_snapshot
                ORDER BY entity_id LIMIT 1
                """
            ).fetchone()
        with sqlite3.connect(catalog.db_path) as conn:
            conn.execute(
                """
                UPDATE legacy_projection_compat_snapshot
                SET source_name=source_name || ' tampered'
                WHERE entity_id=?
                """,
                (row["entity_id"],),
            )
        self._assert_store_startup_fails_without_file_mutation(
            "projection content seal mismatch"
        )

    def test_active_projection_legal_create_rename_disable_restarts(self) -> None:
        catalog = self.store.shared_master_data
        item = catalog.create(
            "brands",
            {"code": "restart_probe", "name": "Restart Probe"},
            actor="test:admin",
        )
        item = catalog.update(
            "brands",
            item["id"],
            {"name": "Restart Probe Renamed"},
            actor="test:admin",
            expected_revision=item["revision"],
        )
        item = catalog.set_active(
            "brands",
            item["id"],
            False,
            actor="test:admin",
            expected_revision=item["revision"],
        )
        restarted = Store()
        self.assertTrue(restarted.legacy_master_data_activated())
        self.assertFalse(
            restarted.shared_master_data.detail("brands", item["id"])["active"]
        )
        self.assertNotIn(
            "Restart Probe Renamed",
            {row["name"] for row in restarted.list_master_data()["brands"]},
        )

    def test_active_restart_does_not_seed_frozen_legacy_master_tables(self) -> None:
        catalog = self.store.shared_master_data
        brand = catalog.create(
            "brands", {"code": "catalog_only_restart", "name": "Catalog Only"},
            actor="test:admin",
        )
        self.store.create_job(
            "catalog-only-job", "scrape", "lowes",
            target_url="https://www.lowes.com/pd/catalog-only/100",
            brand=brand["name"],
        )
        with sqlite3.connect(self.legacy_store.db_path) as conn:
            before_rows = conn.execute(
                "SELECT id,name FROM master_brands ORDER BY id"
            ).fetchall()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before_sha = hashlib.sha256(
            self.legacy_store.db_path.read_bytes()
        ).hexdigest()
        before_mtime = self.legacy_store.db_path.stat().st_mtime_ns
        restarted = Store()
        self.assertTrue(restarted.legacy_master_data_activated())
        with sqlite3.connect(self.legacy_store.db_path) as conn:
            after_rows = conn.execute(
                "SELECT id,name FROM master_brands ORDER BY id"
            ).fetchall()
        self.assertEqual(before_rows, after_rows)
        self.assertNotIn("Catalog Only", {row[1] for row in after_rows})
        self.assertEqual(
            before_sha,
            hashlib.sha256(self.legacy_store.db_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(before_mtime, self.legacy_store.db_path.stat().st_mtime_ns)

    def test_active_scope_status_flip_or_delete_fails_closed(self) -> None:
        catalog = self.store.shared_master_data
        with sqlite3.connect(catalog.db_path) as conn:
            conn.execute(
                """
                UPDATE catalog_consumer_scopes SET status='inactive'
                WHERE scope='legacy_brand_category_projection'
                """
            )
        self._assert_store_startup_fails_without_file_mutation(
            "deactivation evidence is invalid"
        )

    def test_active_scope_row_delete_fails_closed(self) -> None:
        catalog = self.store.shared_master_data
        with sqlite3.connect(catalog.db_path) as conn:
            conn.execute(
                """
                DELETE FROM catalog_consumer_scopes
                WHERE scope='legacy_brand_category_projection'
                """
            )
        self._assert_store_startup_fails_without_file_mutation(
            "lifecycle row is missing"
        )

    def test_legal_deactivate_reopens_inactive(self) -> None:
        catalog = self.store.shared_master_data
        result = catalog.deactivate_legacy_projection(
            actor="test:rollback",
            expected_catalog_hash=catalog.summary()["catalog_content_hash"],
        )
        self.assertFalse(result["activated"])
        reopened = Store()
        self.assertFalse(reopened.legacy_master_data_activated())

    def test_active_projection_rejects_runtime_safety_flag_drift(self) -> None:
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "false"
        with self.assertRaisesRegex(ValueError, "CATALOG_REQUIRED=true"):
            Store()
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "true"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "false"
        with self.assertRaisesRegex(ValueError, "PROTECT_LEGACY_WRITES=true"):
            Store()
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "true"
        os.environ["MASTER_DATA_ADMIN_WRITES_ENABLED"] = "false"
        with self.assertRaisesRegex(ValueError, "ADMIN_WRITES_ENABLED=true"):
            Store()
        os.environ["MASTER_DATA_ADMIN_WRITES_ENABLED"] = "true"
        proxy_token = os.environ.pop("MASTER_DATA_ADMIN_PROXY_TOKEN")
        try:
            with self.assertRaisesRegex(ValueError, "ADMIN_PROXY_TOKEN"):
                Store()
        finally:
            os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = proxy_token
        os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = "x" * 31
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            Store()
        os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = proxy_token
        trusted = os.environ["MASTER_DATA_TRUSTED_ACTORS"]
        os.environ["MASTER_DATA_TRUSTED_ACTORS"] = "admin-ui,bare"
        with self.assertRaisesRegex(ValueError, "basic:<htpasswd-user>"):
            Store()
        os.environ["MASTER_DATA_TRUSTED_ACTORS"] = trusted
        os.environ["MASTER_DATA_DEV_MODE"] = "true"
        with self.assertRaisesRegex(ValueError, "DEV_MODE=false"):
            Store()
        os.environ["MASTER_DATA_DEV_MODE"] = "false"
        self.assertTrue(Store().legacy_master_data_activated())

    def test_active_projection_missing_jobs_fails_without_recreating_it(self) -> None:
        jobs_path = self.legacy_store.db_path
        hidden = self.root / "jobs.sqlite3.active-missing"
        jobs_path.replace(hidden)
        try:
            with self.assertRaisesRegex(ValueError, "existing regular.*jobs"):
                Store()
            self.assertFalse(jobs_path.exists())
        finally:
            hidden.replace(jobs_path)

    def test_active_projection_wrong_jobs_mount_fails_without_mutation(self) -> None:
        jobs_path = self.legacy_store.db_path
        wrong = self.root / "wrong-jobs.sqlite3"
        copy_sqlite(jobs_path, wrong)
        with sqlite3.connect(wrong) as conn:
            conn.execute(
                """
                INSERT INTO master_brands(name,created_at,updated_at)
                VALUES('Wrong Mount Brand','2026-01-01','2026-01-01')
                """
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        hidden = self.root / "jobs.sqlite3.correct-mount"
        jobs_path.replace(hidden)
        wrong.replace(jobs_path)
        before_sha = hashlib.sha256(jobs_path.read_bytes()).hexdigest()
        before_mtime = jobs_path.stat().st_mtime_ns
        try:
            with self.assertRaisesRegex(ValueError, "frozen legacy master hash"):
                Store()
            self.assertEqual(
                before_sha, hashlib.sha256(jobs_path.read_bytes()).hexdigest()
            )
            self.assertEqual(before_mtime, jobs_path.stat().st_mtime_ns)
        finally:
            jobs_path.unlink()
            hidden.replace(jobs_path)

    def test_active_projection_tampered_legacy_rows_fail_without_mutation(self) -> None:
        jobs_path = self.legacy_store.db_path
        with sqlite3.connect(jobs_path) as conn:
            conn.execute(
                """
                INSERT INTO master_categories(name,created_at,updated_at)
                VALUES('Tampered Legacy Row','2026-01-01','2026-01-01')
                """
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before_sha = hashlib.sha256(jobs_path.read_bytes()).hexdigest()
        before_mtime = jobs_path.stat().st_mtime_ns
        with self.assertRaisesRegex(ValueError, "frozen legacy master hash"):
            Store()
        self.assertEqual(
            before_sha, hashlib.sha256(jobs_path.read_bytes()).hexdigest()
        )
        self.assertEqual(before_mtime, jobs_path.stat().st_mtime_ns)

    def test_legacy_delete_catalog_state_is_atomic_on_failure(self) -> None:
        created = self.store.create_master_data(
            "brand", "Atomic Delete Probe", actor="test:admin"
        )
        catalog = self.store.shared_master_data
        item = catalog._item_for_legacy_id("brands", created["id"])
        before_hash = catalog.summary()["catalog_content_hash"]
        original_advance = catalog._advance_catalog

        def fail_advance(*_args, **_kwargs):
            raise RuntimeError("injected catalog revision failure")

        catalog._advance_catalog = fail_advance
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                catalog.legacy_delete(
                    "brand", created["id"], actor="test:admin"
                )
        finally:
            catalog._advance_catalog = original_advance
        self.assertEqual(before_hash, catalog.summary()["catalog_content_hash"])
        self.assertTrue(catalog.detail("brands", item["id"])["active"])
        self.assertIn(
            "Atomic Delete Probe",
            {row["name"] for row in self.store.list_master_data()["brands"]},
        )

    def test_bound_legacy_delete_is_protected_but_v2_disable_preserves_history(self) -> None:
        brand = self.store.create_master_data("brand", "Bound Brand")
        category = self.store.create_master_data("category", "Bound Category")
        self.store.create_job(
            "bound-job",
            "scrape",
            "lowes",
            target_url="https://www.lowes.com/pd/bound/100",
            brand=brand["name"],
            category=category["name"],
        )
        with self.assertRaisesRegex(ValueError, "已有 1 条"):
            self.store.delete_master_data("brand", brand["id"])
        catalog = self.store.shared_master_data
        item = catalog.list("brands", q="Bound Brand", status="all")["items"][0]
        disabled = catalog.set_active(
            "brands",
            item["id"],
            False,
            actor="test:admin",
            expected_revision=item["revision"],
        )
        self.assertFalse(disabled["active"])
        self.assertEqual("Bound Brand", self.store.get_job("bound-job")["brand"])
        with self.assertRaisesRegex(ValueError, "未在主数据中维护"):
            self.store.canonical_master_value("brand", "Bound Brand")


class SharedMasterDataApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.previous_data_dir = os.environ.get("DATA_DIR")
        cls.previous_admin_env = {
            key: os.environ.get(key)
            for key in (
                "MASTER_DATA_ADMIN_WRITES_ENABLED",
                "MASTER_DATA_TRUSTED_ACTORS",
                "MASTER_DATA_ADMIN_PROXY_TOKEN",
                "MASTER_DATA_CATALOG_REQUIRED",
                "MASTER_DATA_PROTECT_LEGACY_WRITES",
                "MASTER_DATA_DEV_MODE",
                "MASTER_DATA_DEV_ACTOR",
            )
        }
        os.environ["DATA_DIR"] = str(cls.root)
        os.environ["MASTER_DATA_ADMIN_WRITES_ENABLED"] = "true"
        os.environ["MASTER_DATA_TRUSTED_ACTORS"] = TEST_PROD_ACTOR
        os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = TEST_PROXY_TOKEN
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "false"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "false"
        os.environ["MASTER_DATA_DEV_MODE"] = "false"
        os.environ.pop("MASTER_DATA_DEV_ACTOR", None)
        # API contract tests never execute browser collection.  A tiny module
        # stub keeps this focused suite runnable where Patchright's native
        # greenlet wheel is unavailable, without weakening production imports.
        patchright = types.ModuleType("patchright")
        patchright_async = types.ModuleType("patchright.async_api")

        async def unused_async_playwright():  # pragma: no cover - must not run
            raise AssertionError("browser collection is outside master-data API tests")

        patchright_async.async_playwright = unused_async_playwright
        patchright.async_api = patchright_async
        sys.modules["patchright"] = patchright
        sys.modules["patchright.async_api"] = patchright_async
        sys.modules.pop("collector.main", None)
        cls.api = importlib.import_module("collector.main")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.previous_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = cls.previous_data_dir
        for key, value in cls.previous_admin_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.temp.cleanup()

    def test_uninitialized_v2_is_503_and_legacy_remains_available(self) -> None:
        original = os.environ["DATA_DIR"]
        unavailable = self.root / "unavailable"
        os.environ["DATA_DIR"] = str(unavailable)
        try:
            self.api.store = Store()
            self.assertIsNone(self.api.store.shared_master_data)
            with TestClient(self.api.app) as client:
                self.assertEqual(
                    503, client.get("/api/v2/master-data/summary").status_code
                )
                legacy = client.get("/api/master-data")
                self.assertEqual(200, legacy.status_code)
                self.assertTrue(legacy.json()["brands"])
                self.assertFalse((unavailable / "master_data.sqlite3").exists())
        finally:
            os.environ["DATA_DIR"] = original

    def test_missing_catalog_fails_startup_when_explicitly_required(self) -> None:
        original_data_dir = os.environ["DATA_DIR"]
        original_required = os.environ.get("MASTER_DATA_CATALOG_REQUIRED")
        required_root = self.root / "required-but-missing"
        os.environ["DATA_DIR"] = str(required_root)
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "true"
        try:
            with self.assertRaisesRegex(ValueError, "required.*missing"):
                Store()
            self.assertFalse(required_root.exists())
        finally:
            os.environ["DATA_DIR"] = original_data_dir
            if original_required is None:
                os.environ.pop("MASTER_DATA_CATALOG_REQUIRED", None)
            else:
                os.environ["MASTER_DATA_CATALOG_REQUIRED"] = original_required

    def test_required_missing_catalog_does_not_touch_existing_jobs(self) -> None:
        original_data_dir = os.environ["DATA_DIR"]
        original_required = os.environ.get("MASTER_DATA_CATALOG_REQUIRED")
        legacy_root = self.root / "required-existing-legacy"
        os.environ["DATA_DIR"] = str(legacy_root)
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "false"
        legacy = Store()
        before_sha = hashlib.sha256(legacy.db_path.read_bytes()).hexdigest()
        before_mtime = legacy.db_path.stat().st_mtime_ns
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "true"
        try:
            with self.assertRaisesRegex(ValueError, "required.*missing"):
                Store()
            self.assertEqual(
                before_sha,
                hashlib.sha256(legacy.db_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(before_mtime, legacy.db_path.stat().st_mtime_ns)
        finally:
            os.environ["DATA_DIR"] = original_data_dir
            if original_required is None:
                os.environ.pop("MASTER_DATA_CATALOG_REQUIRED", None)
            else:
                os.environ["MASTER_DATA_CATALOG_REQUIRED"] = original_required

    def test_partial_active_scope_cannot_bypass_reviewed_activation_seal(self) -> None:
        original_data_dir = os.environ["DATA_DIR"]
        original_required = os.environ.get("MASTER_DATA_CATALOG_REQUIRED")
        original_protection = os.environ.get(
            "MASTER_DATA_PROTECT_LEGACY_WRITES"
        )
        probe_root = self.root / "partial-active-scope"
        os.environ["DATA_DIR"] = str(probe_root)
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "false"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "false"
        try:
            legacy = Store()
            catalog = MasterDataStore(
                probe_root, legacy_db_path=legacy.db_path, initialize=True
            )
            legacy_hash = catalog._read_legacy_snapshot(
                legacy.db_path, immutable=False
            )[2]
            with catalog._connect() as conn:
                conn.execute(
                    """
                    UPDATE catalog_consumer_scopes
                    SET status='active',legacy_master_content_hash=?,updated_at=?
                    WHERE scope='legacy_brand_category_projection'
                    """,
                    (legacy_hash, "2026-09-02T00:00:00+00:00"),
                )
            before_sha = hashlib.sha256(legacy.db_path.read_bytes()).hexdigest()
            before_mtime = legacy.db_path.stat().st_mtime_ns
            os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "true"
            os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "true"
            with self.assertRaisesRegex(ValueError, "not sealed"):
                Store()
            self.assertEqual(
                before_sha,
                hashlib.sha256(legacy.db_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(before_mtime, legacy.db_path.stat().st_mtime_ns)
        finally:
            os.environ["DATA_DIR"] = original_data_dir
            if original_required is None:
                os.environ.pop("MASTER_DATA_CATALOG_REQUIRED", None)
            else:
                os.environ[
                    "MASTER_DATA_CATALOG_REQUIRED"
                ] = original_required
            if original_protection is None:
                os.environ.pop("MASTER_DATA_PROTECT_LEGACY_WRITES", None)
            else:
                os.environ[
                    "MASTER_DATA_PROTECT_LEGACY_WRITES"
                ] = original_protection

    def test_legacy_write_protection_closes_direct_api_bypass(self) -> None:
        original_data_dir = os.environ["DATA_DIR"]
        original_required = os.environ.get("MASTER_DATA_CATALOG_REQUIRED")
        original_protection = os.environ.get(
            "MASTER_DATA_PROTECT_LEGACY_WRITES"
        )
        original_store = self.api.store
        probe_root = self.root / "legacy-write-protection"
        os.environ["DATA_DIR"] = str(probe_root)
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "false"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "false"
        try:
            self.api.store = Store()
            with TestClient(self.api.app) as client:
                unprotected = client.post(
                    "/api/master-data/brand",
                    json={"name": "Unprotected Compatibility"},
                )
                self.assertEqual(200, unprotected.status_code)

                os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "true"
                self.assertEqual(
                    401,
                    client.post(
                        "/api/master-data/brand",
                        json={"name": "Missing Credentials"},
                    ).status_code,
                )
                self.assertEqual(
                    403,
                    client.post(
                        "/api/master-data/brand",
                        headers={
                            "X-Master-Data-Actor": TEST_PROD_ACTOR,
                            "X-Master-Data-Proxy-Token": "wrong-token",
                        },
                        json={"name": "Wrong Proxy"},
                    ).status_code,
                )
                os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = "x" * 31
                self.assertEqual(
                    503,
                    client.post(
                        "/api/master-data/brand",
                        headers={
                            "X-Master-Data-Actor": TEST_PROD_ACTOR,
                            "X-Master-Data-Proxy-Token": "x" * 31,
                        },
                        json={"name": "Weak Runtime Token"},
                    ).status_code,
                )
                os.environ["MASTER_DATA_ADMIN_PROXY_TOKEN"] = TEST_PROXY_TOKEN
                self.assertEqual(
                    403,
                    client.post(
                        "/api/master-data/brand",
                        headers={
                            "X-Master-Data-Actor": "admin-ui",
                            "X-Master-Data-Proxy-Token": TEST_PROXY_TOKEN,
                        },
                        json={"name": "Production Self Report"},
                    ).status_code,
                )
                trusted = client.post(
                    "/api/master-data/brand",
                    headers={
                        "X-Master-Data-Actor": TEST_PROD_ACTOR,
                        "X-Master-Data-Proxy-Token": TEST_PROXY_TOKEN,
                    },
                    json={"name": "Protected Trusted Write"},
                )
                self.assertEqual(200, trusted.status_code)
                os.environ["MASTER_DATA_DEV_MODE"] = "true"
                os.environ["MASTER_DATA_DEV_ACTOR"] = "admin-ui"
                os.environ.pop("MASTER_DATA_ADMIN_PROXY_TOKEN", None)
                try:
                    dev_write = client.post(
                        "/api/master-data/brand",
                        headers={"X-Master-Data-Actor": "admin-ui"},
                        json={"name": "Explicit Dev Actor"},
                    )
                    self.assertEqual(200, dev_write.status_code)
                finally:
                    os.environ["MASTER_DATA_DEV_MODE"] = "false"
                    os.environ.pop("MASTER_DATA_DEV_ACTOR", None)
                    os.environ[
                        "MASTER_DATA_ADMIN_PROXY_TOKEN"
                    ] = TEST_PROXY_TOKEN
        finally:
            self.api.store = original_store
            os.environ["DATA_DIR"] = original_data_dir
            if original_required is None:
                os.environ.pop("MASTER_DATA_CATALOG_REQUIRED", None)
            else:
                os.environ[
                    "MASTER_DATA_CATALOG_REQUIRED"
                ] = original_required
            if original_protection is None:
                os.environ.pop("MASTER_DATA_PROTECT_LEGACY_WRITES", None)
            else:
                os.environ[
                    "MASTER_DATA_PROTECT_LEGACY_WRITES"
                ] = original_protection

    def test_http_contract_etag_actor_routes_and_legacy_projection(self) -> None:
        previous_required = os.environ.get("MASTER_DATA_CATALOG_REQUIRED")
        previous_legacy_protection = os.environ.get(
            "MASTER_DATA_PROTECT_LEGACY_WRITES"
        )
        self.addCleanup(
            lambda: (
                os.environ.pop("MASTER_DATA_CATALOG_REQUIRED", None)
                if previous_required is None
                else os.environ.__setitem__(
                    "MASTER_DATA_CATALOG_REQUIRED", previous_required
                )
            )
        )
        self.addCleanup(
            lambda: (
                os.environ.pop("MASTER_DATA_PROTECT_LEGACY_WRITES", None)
                if previous_legacy_protection is None
                else os.environ.__setitem__(
                    "MASTER_DATA_PROTECT_LEGACY_WRITES",
                    previous_legacy_protection,
                )
            )
        )
        legacy = Store()
        copy_path = self.root / "api-jobs-copy.sqlite3"
        copy_sqlite(legacy.db_path, copy_path)
        manifest = minimal_manifest(self.root / "api-manifest.json")
        catalog = MasterDataStore(
            self.root, legacy_db_path=legacy.db_path, initialize=True
        )
        plan = catalog.import_plan(
            legacy_db_copy=copy_path, manifest_path=manifest
        )
        catalog.apply_import(
            legacy_db_copy=copy_path,
            manifest_path=manifest,
            actor="test:import",
            expected_plan_sha256=plan["plan_sha256"],
            expected_legacy_copy_sha256=plan["legacy_copy_sha256"],
        )
        os.environ["MASTER_DATA_CATALOG_REQUIRED"] = "true"
        os.environ["MASTER_DATA_PROTECT_LEGACY_WRITES"] = "true"
        catalog.activate_legacy_projection(
            actor="test:activate",
            expected_plan_sha256=plan["plan_sha256"],
            expected_legacy_master_content_hash=plan["legacy_master_content_hash"],
        )
        self.api.store = Store()
        with TestClient(self.api.app) as client:
            root = client.get("/api/v2/master-data")
            summary = client.get("/api/v2/master-data/summary")
            self.assertEqual(root.json(), summary.json())
            etag = summary.headers["etag"]
            self.assertEqual(
                f'"{summary.json()["catalog_content_hash"]}"', etag
            )
            self.assertEqual(304, client.get(
                "/api/v2/master-data/summary", headers={"If-None-Match": etag}
            ).status_code)

            payload = {"code": "api_brand", "name": "API Brand", "aliases": ["API Alias"]}
            proxy = {"X-Master-Data-Proxy-Token": TEST_PROXY_TOKEN}
            self.assertEqual(
                401,
                client.get(
                    "/api/admin/master-data/integration-status",
                    headers={"X-Master-Data-Actor": TEST_PROD_ACTOR},
                ).status_code,
            )
            integration = client.get(
                "/api/admin/master-data/integration-status",
                headers={"X-Master-Data-Actor": TEST_PROD_ACTOR, **proxy},
            )
            self.assertEqual(200, integration.status_code)
            self.assertEqual(
                "active",
                integration.json()["consumer_scopes"][0]["status"],
            )
            self.assertTrue(integration.json()["catalog_required"])
            self.assertTrue(integration.json()["legacy_writes_protected"])
            self.assertEqual([], integration.json()["deployment_blockers"])
            legacy_headers = {
                "X-Master-Data-Actor": TEST_PROD_ACTOR,
                **proxy,
            }
            self.assertEqual(
                404,
                client.patch(
                    "/api/master-data/brand/999999",
                    headers=legacy_headers,
                    json={"name": "Missing"},
                ).status_code,
            )
            self.assertEqual(
                404,
                client.delete(
                    "/api/master-data/brand/999999",
                    headers=legacy_headers,
                ).status_code,
            )
            disposable = client.post(
                "/api/master-data/brand", headers=legacy_headers,
                json={"name": "HTTP Disposable"},
            )
            self.assertEqual(200, disposable.status_code)
            disposable_id = disposable.json()["id"]
            self.assertEqual(
                200,
                client.delete(
                    f"/api/master-data/brand/{disposable_id}",
                    headers=legacy_headers,
                ).status_code,
            )
            self.assertEqual(
                404,
                client.patch(
                    f"/api/master-data/brand/{disposable_id}",
                    headers=legacy_headers,
                    json={"name": "Must Stay Hidden"},
                ).status_code,
            )
            self.assertEqual(
                404,
                client.delete(
                    f"/api/master-data/brand/{disposable_id}",
                    headers=legacy_headers,
                ).status_code,
            )
            mismatch = client.post(
                "/api/jobs",
                json={
                    "action": "discover", "channel": "lowes",
                    "brand": "Definitely Not Governed",
                },
            )
            self.assertEqual(422, mismatch.status_code, mismatch.text)
            self.assertEqual(
                "master_data_mismatch", mismatch.json()["detail"]["code"]
            )
            etag = client.get(
                "/api/v2/master-data/summary"
            ).headers["etag"]
            self.assertEqual(
                401,
                client.post(
                    "/api/admin/master-data/brands",
                    headers={"If-Match": etag},
                    json=payload,
                ).status_code,
            )
            self.assertEqual(
                428,
                client.post(
                    "/api/admin/master-data/brands",
                    headers={
                        "X-Master-Data-Actor": TEST_PROD_ACTOR,
                        **proxy,
                    },
                    json=payload,
                ).status_code,
            )
            self.assertEqual(
                403,
                client.post(
                    "/api/admin/master-data/brands",
                    headers={
                        "X-Master-Data-Actor": "self-reported",
                        "If-Match": etag,
                        **proxy,
                    },
                    json=payload,
                ).status_code,
            )
            os.environ["MASTER_DATA_ADMIN_WRITES_ENABLED"] = "false"
            try:
                self.assertEqual(
                    503,
                    client.post(
                        "/api/admin/master-data/brands",
                        headers={
                            "X-Master-Data-Actor": TEST_PROD_ACTOR,
                            "If-Match": etag,
                            **proxy,
                        },
                        json=payload,
                    ).status_code,
                )
            finally:
                os.environ["MASTER_DATA_ADMIN_WRITES_ENABLED"] = "true"
            created = client.post(
                "/api/admin/master-data/brands",
                headers={
                    "X-Master-Data-Actor": TEST_PROD_ACTOR,
                    "If-Match": etag,
                    **proxy,
                },
                json=payload,
            )
            self.assertEqual(201, created.status_code, created.text)
            self.assertEqual(
                f'"{created.json()["catalog_content_hash"]}"',
                created.headers["etag"],
            )
            self.assertEqual(
                412,
                client.patch(
                    f"/api/admin/master-data/brands/{created.json()['id']}",
                    headers={
                        "X-Master-Data-Actor": TEST_PROD_ACTOR,
                        "If-Match": etag,
                        **proxy,
                    },
                    json={"name": "Stale"},
                ).status_code,
            )
            resolved = client.get(
                "/api/v2/master-data/brands/resolve", params={"value": "API Alias"}
            )
            self.assertEqual(200, resolved.status_code)
            self.assertEqual("alias", resolved.json()["match_kind"])
            validated = client.get(
                "/api/v2/master-data/brands/validate", params={"name": "API Alias"}
            )
            self.assertFalse(validated.json()["valid"])
            formal = client.post(
                "/api/v2/master-data/validate",
                json={"type": "brands", "name": "API Alias"},
            )
            self.assertEqual(200, formal.status_code)
            self.assertFalse(formal.json()["valid"])
            detail = client.get(
                f"/api/v2/master-data/brands/{created.json()['id']}"
            )
            self.assertEqual(created.json()["id"], detail.json()["id"])
            self.assertIn("catalog_content_hash", detail.json())
            self.assertEqual(
                f'"{detail.json()["catalog_content_hash"]}"',
                detail.headers["etag"],
            )
            legacy_values = client.get("/api/master-data").json()["brands"]
            self.assertIn("API Brand", {item["name"] for item in legacy_values})
            runtime_catalog = self.api.store.shared_master_data
            with runtime_catalog._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO legacy_job_rebind_intents(
                        id,entity_type,entity_id,old_name,new_name,status,
                        actor,created_at
                    ) VALUES(
                        'http-pending','brands',?,?,?,'pending',?,?
                    )
                    """,
                    (
                        created.json()["id"], "API Brand", "API Brand",
                        TEST_PROD_ACTOR, "2026-09-02T00:00:00+00:00",
                    ),
                )
                runtime_catalog._refresh_legacy_projection_seal(conn)
                conn.commit()
            pending = client.get(
                "/api/admin/master-data/integration-status",
                headers={"X-Master-Data-Actor": TEST_PROD_ACTOR, **proxy},
            )
            self.assertEqual(200, pending.status_code)
            self.assertEqual(1, pending.json()["pending_legacy_rebinds"])
            self.assertTrue(
                any(
                    "rebind recovery" in blocker
                    for blocker in pending.json()["deployment_blockers"]
                )
            )
            runtime_catalog.recover_legacy_rebinds(actor="test:http-recover")


if __name__ == "__main__":
    unittest.main(verbosity=2)
