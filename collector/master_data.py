"""Shared, versioned master-data catalog.

The catalog owns cross-product vocabulary.  Product/model bindings remain in
Chengmu.  This module deliberately uses a separate SQLite file so collection
queue lifecycle and shared dictionary lifecycle cannot accidentally share a
transaction or backup boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


CATALOG_SCHEMA_VERSION = "voxquarry.shared-master-data/v2"
MIGRATIONS = Path(__file__).resolve().parent / "master_data_migrations"
LEGACY_PROJECTION_SCOPE = "legacy_brand_category_projection"


class MasterDataError(ValueError):
    """A user-correctable catalog validation error."""


class MasterDataConflict(MasterDataError):
    """Optimistic concurrency or uniqueness conflict."""


class MasterDataNotFound(MasterDataError):
    """Requested catalog record does not exist."""


TYPE_SPECS: dict[str, dict[str, Any]] = {
    "brands": {
        "label": "品牌",
        "prefix": "brd",
        "fields": {"short_name", "official_domain"},
    },
    "categories": {
        "label": "品类",
        "prefix": "cat",
        "fields": set(),
        "hierarchy": True,
    },
    "energy-types": {
        "label": "能源类型",
        "prefix": "eng",
        "fields": set(),
    },
    "voltage-platforms": {
        "label": "电压平台",
        "prefix": "vpf",
        "fields": {"brand_id", "canonical_voltage", "unit_id"},
        "required": {"brand_id", "canonical_voltage", "unit_id"},
    },
    "market-regions": {
        "label": "市场地区",
        "prefix": "mkt",
        "fields": {"iso_code"},
        "hierarchy": True,
    },
    "retailers": {
        "label": "零售商",
        "prefix": "rtl",
        "fields": {"official_domain", "market_region_ids"},
    },
    "collection-channels": {
        "label": "采集渠道",
        "prefix": "chn",
        "fields": {
            "channel_type", "brand_id", "retailer_id", "market_region_ids",
            "supported_content",
        },
        "required": {"channel_type"},
    },
    "dimensions": {
        "label": "量纲",
        "prefix": "dim",
        "fields": {"symbol"},
    },
    "units": {
        "label": "单位",
        "prefix": "unt",
        "fields": {
            "dimension_id", "symbol", "is_base", "conversion_factor",
            "conversion_offset",
        },
        "required": {"dimension_id", "symbol"},
    },
    "product-lifecycle-statuses": {
        "label": "产品生命周期",
        "prefix": "plc",
        "fields": {"is_terminal"},
    },
    "currencies": {
        "label": "币种",
        "prefix": "cur",
        "fields": {"symbol", "decimal_places"},
    },
}

IMPORT_TYPE_ORDER = {
    entity_type: index
    for index, entity_type in enumerate(
        (
            "brands", "categories", "energy-types", "market-regions",
            "retailers", "dimensions", "units", "voltage-platforms",
            "product-lifecycle-statuses", "currencies",
            "collection-channels",
        )
    )
}

COMMON_FIELDS = {
    "code", "name", "name_zh", "definition", "active", "replacement_id",
    "parent_id", "sort_order", "aliases", "source_mappings",
}

COLLECTION_CHANNEL_TYPES = {
    "retailer_web", "brand_official", "historical_import", "file",
}
COLLECTION_SUPPORTED_CONTENT = {"review"}
LIST_ATTRIBUTE_FIELDS = {"market_region_ids", "supported_content"}
BOOLEAN_ATTRIBUTE_FIELDS = {"is_base", "is_terminal"}
REFERENCE_ATTRIBUTE_FIELDS = {
    "brand_id", "retailer_id", "dimension_id", "unit_id",
}
TEXT_ATTRIBUTE_FIELDS = {
    "short_name", "official_domain", "canonical_voltage", "iso_code",
    "channel_type", "symbol",
}
MAX_GOVERNED_TEXT_LENGTH = 2000


def catalog_required_by_runtime() -> bool:
    return os.getenv(
        "MASTER_DATA_CATALOG_REQUIRED", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}


def legacy_writes_protected_by_runtime() -> bool:
    return os.getenv(
        "MASTER_DATA_PROTECT_LEGACY_WRITES", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}


def admin_writes_enabled_by_runtime() -> bool:
    return os.getenv(
        "MASTER_DATA_ADMIN_WRITES_ENABLED", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}


def dev_mode_enabled_by_runtime() -> bool:
    return os.getenv(
        "MASTER_DATA_DEV_MODE", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}


def valid_admin_proxy_token_by_runtime() -> str | None:
    """Return the opaque proxy credential only when it is production strength."""
    token = os.getenv("MASTER_DATA_ADMIN_PROXY_TOKEN", "")
    if (
        not token
        or token != token.strip()
        or len(token.encode("utf-8")) < 32
    ):
        return None
    return token


def valid_production_trusted_actors() -> set[str]:
    """Production identities are injected by Basic-auth-aware trusted proxies."""
    actors: set[str] = set()
    for raw in os.getenv("MASTER_DATA_TRUSTED_ACTORS", "").split(","):
        actor = raw.strip()
        if (
            len(actor) <= 128
            and re.fullmatch(
                r"basic:[A-Za-z0-9][A-Za-z0-9._@-]{0,121}", actor
            )
        ):
            actors.add(actor)
    return actors


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_identity(value: Any) -> str:
    """NFKC + whitespace + casefold identity used by every unique index."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split()).strip().casefold()


def clean_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).strip()


def normalize_code(value: Any) -> str:
    code = clean_text(value)
    if not code or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", code):
        raise MasterDataError("code must use 1-128 ASCII letters, digits, dot, underscore, or hyphen")
    return code.casefold()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(entity_type: str, code: str) -> str:
    prefix = TYPE_SPECS[entity_type]["prefix"]
    digest = hashlib.sha256(f"{entity_type}\x1f{normalize_code(code)}".encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _seed_code(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", clean_text(name)).casefold()
    token = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if token:
        return token[:96]
    return "value_" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


class MasterDataStore:
    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        legacy_db_path: str | Path | None = None,
        initialize: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir or os.getenv("DATA_DIR", "data"))
        self.db_path = self.data_dir / "master_data.sqlite3"
        self.legacy_db_path = Path(legacy_db_path) if legacy_db_path else None
        self.lock = threading.RLock()
        if self.db_path.is_symlink():
            raise ValueError(
                "shared master-data SQLite must be a regular, non-symlink file"
            )
        if self.db_path.exists() and not self.db_path.is_file():
            raise ValueError(
                "shared master-data SQLite must be a regular, non-symlink file"
            )
        if initialize:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._apply_migrations()
            self._bootstrap()
            self._verify_initialized()
        elif not self.db_path.is_file():
            raise ValueError("existing shared master-data SQLite is required")
        else:
            self._verify_initialized()

    def _verify_initialized(self) -> None:
        try:
            with self._connect() as conn:
                migration_files = sorted(MIGRATIONS.glob("*.sql"))
                versions = {
                    row["version"]: row["checksum"]
                    for row in conn.execute(
                        "SELECT version,checksum FROM schema_migrations"
                    )
                }
                expected_versions = {migration.stem for migration in migration_files}
                if set(versions) != expected_versions:
                    unknown = sorted(set(versions) - expected_versions)
                    missing = sorted(expected_versions - set(versions))
                    raise ValueError(
                        "shared master-data migration set does not match this "
                        f"runtime (unknown={unknown}, missing={missing})"
                    )
                for migration in migration_files:
                    checksum = hashlib.sha256(
                        migration.read_bytes()
                    ).hexdigest()
                    if versions.get(migration.stem) != checksum:
                        raise ValueError(
                            f"shared master-data migration missing or drifted: {migration.stem}"
                        )
                meta = conn.execute(
                    "SELECT 1 FROM catalog_meta WHERE id=1"
                ).fetchone()
                if not meta:
                    raise ValueError("shared master-data catalog metadata is missing")
                integrity_rows = [
                    row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()
                ]
                if integrity_rows != ["ok"]:
                    raise ValueError(
                        "shared master-data SQLite integrity check failed: "
                        + "; ".join(integrity_rows)
                    )
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(journal_mode).casefold() != "delete":
                    raise ValueError(
                        "shared master-data SQLite must use DELETE journal mode "
                        "for cross-database binding serialization"
                    )
                foreign_key_rows = conn.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if foreign_key_rows:
                    raise ValueError(
                        "shared master-data SQLite foreign-key check failed"
                    )
                meta = conn.execute(
                    "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
                ).fetchone()
                latest_revision = conn.execute(
                    """
                    SELECT revision,content_hash FROM catalog_revisions
                    ORDER BY revision DESC LIMIT 1
                    """
                ).fetchone()
                if (
                    not latest_revision
                    or int(latest_revision["revision"]) != int(meta["revision"])
                    or latest_revision["content_hash"] != meta["content_hash"]
                ):
                    raise ValueError(
                        "shared master-data revision metadata is inconsistent"
                    )
                computed_hash = hashlib.sha256(
                    canonical_json(self._catalog_content(conn)).encode()
                ).hexdigest()
                if computed_hash != meta["content_hash"]:
                    raise ValueError(
                        "shared master-data catalog content hash mismatch"
                    )
                self._verify_active_scope_seal(conn)
        except sqlite3.Error as exc:
            raise ValueError("shared master-data SQLite is not initialized") from exc

    @staticmethod
    def _legacy_projection_content_hash(conn: sqlite3.Connection) -> str:
        content = {
            "projections": [
                dict(row) for row in conn.execute(
                    """
                    SELECT entity_id,entity_type,legacy_integer_id
                    FROM legacy_projection
                    ORDER BY entity_type,legacy_integer_id,entity_id
                    """
                ).fetchall()
            ],
            "catalog_owned": [
                row["entity_id"] for row in conn.execute(
                    """
                    SELECT entity_id FROM catalog_owned_legacy_projection
                    ORDER BY entity_id
                    """
                ).fetchall()
            ],
            "compat_snapshots": [
                dict(row) for row in conn.execute(
                    """
                    SELECT entity_id,source_name,created_at,updated_at,
                           visible_when_inactive
                    FROM legacy_projection_compat_snapshot
                    ORDER BY entity_id
                    """
                ).fetchall()
            ],
            "rebind_intents": [
                dict(row) for row in conn.execute(
                    """
                    SELECT id,entity_type,entity_id,old_name,new_name,status,
                           actor,created_at,completed_at,completed_by
                    FROM legacy_job_rebind_intents
                    ORDER BY created_at,id
                    """
                ).fetchall()
            ],
        }
        return hashlib.sha256(canonical_json(content).encode()).hexdigest()

    @classmethod
    def _refresh_legacy_projection_seal(
        cls, conn: sqlite3.Connection
    ) -> str | None:
        scope = conn.execute(
            "SELECT status,activated_at FROM catalog_consumer_scopes WHERE scope=?",
            (LEGACY_PROJECTION_SCOPE,),
        ).fetchone()
        if not scope or (
            scope["status"] == "inactive" and not scope["activated_at"]
        ):
            return None
        content_hash = cls._legacy_projection_content_hash(conn)
        conn.execute(
            """
            UPDATE catalog_consumer_scopes SET projection_content_hash=?
            WHERE scope=?
            """,
            (content_hash, LEGACY_PROJECTION_SCOPE),
        )
        return content_hash

    @staticmethod
    def _verify_active_scope_seal(conn: sqlite3.Connection) -> None:
        scope = conn.execute(
            "SELECT * FROM catalog_consumer_scopes WHERE scope=?",
            (LEGACY_PROJECTION_SCOPE,),
        ).fetchone()
        if not scope:
            raise ValueError(
                "legacy master-data consumer scope lifecycle row is missing"
            )
        if scope["status"] not in {"active", "inactive"}:
            raise ValueError(
                "legacy master-data consumer scope status is invalid"
            )
        required_fields = (
            "reviewed_plan_sha256", "legacy_master_content_hash",
            "activated_catalog_revision", "activated_catalog_content_hash",
            "activated_at", "activated_by", "projection_content_hash",
        )
        if scope["status"] == "inactive" and all(
            scope[field] in {None, ""} for field in required_fields
        ):
            event_count = int(conn.execute(
                "SELECT COUNT(*) FROM catalog_scope_events WHERE scope=?",
                (LEGACY_PROJECTION_SCOPE,),
            ).fetchone()[0])
            if (
                event_count
                or scope["updated_at"] != "1970-01-01T00:00:00+00:00"
            ):
                raise ValueError(
                    "inactive legacy master-data consumer scope has no valid "
                    "initial lifecycle evidence"
                )
            return
        missing = [field for field in required_fields if scope[field] in {None, ""}]
        if missing:
            raise ValueError(
                "active legacy master-data projection is not sealed; missing "
                + ", ".join(missing)
            )
        for field in (
            "reviewed_plan_sha256", "legacy_master_content_hash",
            "activated_catalog_content_hash", "projection_content_hash",
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", str(scope[field])):
                raise ValueError(
                    "active legacy master-data projection has an invalid "
                    f"{field}"
                )
        try:
            activated_revision = int(scope["activated_catalog_revision"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "active legacy master-data projection has an invalid revision"
            ) from exc
        if activated_revision < 0:
            raise ValueError(
                "active legacy master-data projection has an invalid revision"
            )
        receipt = conn.execute(
            "SELECT * FROM master_import_receipts WHERE plan_sha256=?",
            (scope["reviewed_plan_sha256"],),
        ).fetchone()
        if not receipt:
            raise ValueError(
                "active legacy master-data projection has no reviewed import receipt"
            )
        if (
            receipt["legacy_master_content_hash"]
            != scope["legacy_master_content_hash"]
            or int(receipt["resulting_catalog_revision"])
            != activated_revision
            or receipt["resulting_catalog_content_hash"]
            != scope["activated_catalog_content_hash"]
        ):
            raise ValueError(
                "active legacy master-data projection does not match its "
                "reviewed import receipt"
            )
        try:
            unresolved = json.loads(receipt["unresolved_json"])
            reconcile = json.loads(receipt["reconcile_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "active legacy master-data projection receipt is invalid"
            ) from exc
        if not isinstance(unresolved, list) or any(
            not isinstance(item, dict) for item in unresolved
        ):
            raise ValueError(
                "active legacy master-data projection receipt is invalid"
            )
        if any(
            item.get("scope") in {"legacy.brands", "legacy.categories"}
            for item in unresolved
        ):
            raise ValueError(
                "active legacy master-data projection receipt has unresolved "
                "legacy values"
            )
        if (
            not isinstance(reconcile, dict)
            or reconcile.get("ok") is not True
            or reconcile.get("legacy_master_content_hash")
            != scope["legacy_master_content_hash"]
        ):
            raise ValueError(
                "active legacy master-data projection receipt did not reconcile"
            )
        revision = conn.execute(
            """
            SELECT 1 FROM catalog_revisions
            WHERE revision=? AND content_hash=?
            """,
            (activated_revision, scope["activated_catalog_content_hash"]),
        ).fetchone()
        if not revision:
            raise ValueError(
                "active legacy master-data projection revision evidence is missing"
            )
        latest_event = conn.execute(
            """
            SELECT action,actor,plan_sha256,legacy_master_content_hash,created_at
            FROM catalog_scope_events WHERE scope=?
            ORDER BY rowid DESC LIMIT 1
            """,
            (LEGACY_PROJECTION_SCOPE,),
        ).fetchone()
        if scope["status"] == "active":
            if (
                not latest_event
                or latest_event["action"] != "activate"
                or latest_event["actor"] != scope["activated_by"]
                or latest_event["plan_sha256"]
                != scope["reviewed_plan_sha256"]
                or latest_event["legacy_master_content_hash"]
                != scope["legacy_master_content_hash"]
                or latest_event["created_at"] != scope["activated_at"]
            ):
                raise ValueError(
                    "active legacy master-data projection activation event is invalid"
                )
        else:
            activation_event = conn.execute(
                """
                SELECT action,actor,plan_sha256,legacy_master_content_hash,created_at
                FROM catalog_scope_events
                WHERE scope=? AND action='activate'
                ORDER BY rowid DESC LIMIT 1
                """,
                (LEGACY_PROJECTION_SCOPE,),
            ).fetchone()
            if (
                not latest_event
                or latest_event["action"] != "deactivate"
                or latest_event["plan_sha256"] is not None
                or latest_event["legacy_master_content_hash"] is not None
                or latest_event["created_at"] != scope["updated_at"]
                or not latest_event["actor"]
                or not activation_event
                or activation_event["actor"] != scope["activated_by"]
                or activation_event["plan_sha256"]
                != scope["reviewed_plan_sha256"]
                or activation_event["legacy_master_content_hash"]
                != scope["legacy_master_content_hash"]
                or activation_event["created_at"] != scope["activated_at"]
            ):
                raise ValueError(
                    "inactive legacy master-data projection deactivation "
                    "evidence is invalid"
                )
        current_projection_hash = (
            MasterDataStore._legacy_projection_content_hash(conn)
        )
        if current_projection_hash != scope["projection_content_hash"]:
            raise ValueError(
                "active legacy master-data projection content seal mismatch"
            )
        invalid_intent = conn.execute(
            """
            SELECT id FROM legacy_job_rebind_intents
            WHERE (status='pending' AND (
                       completed_at IS NOT NULL OR completed_by IS NOT NULL
                   ))
               OR (status='completed' AND (
                       completed_at IS NULL OR completed_by IS NULL
                   ))
            LIMIT 1
            """
        ).fetchone()
        if invalid_intent:
            raise ValueError(
                "legacy master-data rebind completion evidence is invalid"
            )

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        if create:
            conn = sqlite3.connect(
                self.db_path, timeout=30, check_same_thread=False
            )
        else:
            conn = sqlite3.connect(
                f"file:{self.db_path.resolve()}?mode=rw",
                uri=True,
                timeout=30,
                check_same_thread=False,
            )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _legacy_projection_active_in(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT status FROM catalog_consumer_scopes WHERE scope=?",
            (LEGACY_PROJECTION_SCOPE,),
        ).fetchone()
        if not row or row["status"] != "active":
            return False
        pending = conn.execute(
            "SELECT COUNT(*) AS value FROM legacy_job_rebind_intents WHERE status='pending'"
        ).fetchone()["value"]
        if pending:
            raise MasterDataConflict(
                "legacy master-data projection requires explicit rebind recovery"
            )
        return True

    def legacy_projection_active(self) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN")
            self._verify_active_scope_seal(conn)
            active = self._legacy_projection_active_in(conn)
            return active

    @contextmanager
    def legacy_scope_snapshot(self):
        """Serialize inactive legacy writes with explicit scope activation."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            self._verify_active_scope_seal(conn)
            yield self._legacy_projection_active_in(conn)
        finally:
            conn.rollback()
            conn.close()

    def verify_active_legacy_projection(
        self,
        legacy: dict[str, list[dict[str, Any]]],
        *,
        legacy_content_hash: str,
    ) -> None:
        """Verify the live compatibility projection without mutating either DB."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            if not self._legacy_projection_active_in(conn):
                raise ValueError(
                    "legacy master-data projection is not active"
                )
            self._verify_active_scope_seal(conn)
            scope = conn.execute(
                "SELECT * FROM catalog_consumer_scopes WHERE scope=?",
                (LEGACY_PROJECTION_SCOPE,),
            ).fetchone()
            if legacy_content_hash != scope["legacy_master_content_hash"]:
                raise ValueError(
                    "active legacy master-data projection live content hash "
                    "does not match its activation seal"
                )

            issues: list[dict[str, Any]] = []
            for entity_type in ("brands", "categories"):
                source_by_id = {
                    int(row["id"]): row for row in legacy[entity_type]
                }
                projection_rows = conn.execute(
                    """
                    SELECT p.entity_id,p.entity_type,p.legacy_integer_id,
                           e.entity_type AS master_entity_type,e.active,
                           CASE WHEN o.entity_id IS NULL THEN 0 ELSE 1 END
                             AS catalog_owned,
                           s.entity_id AS snapshot_entity_id,
                           s.source_name,s.created_at AS source_created_at,
                           s.updated_at AS source_updated_at,
                           s.visible_when_inactive
                    FROM legacy_projection p
                    JOIN master_entities e ON e.id=p.entity_id
                    LEFT JOIN catalog_owned_legacy_projection o
                      ON o.entity_id=p.entity_id
                    LEFT JOIN legacy_projection_compat_snapshot s
                      ON s.entity_id=p.entity_id
                    WHERE p.entity_type=?
                    ORDER BY p.legacy_integer_id,p.entity_id
                    """,
                    (entity_type,),
                ).fetchall()
                projection_by_id = {
                    int(row["legacy_integer_id"]): row
                    for row in projection_rows
                }
                for legacy_id, source in source_by_id.items():
                    projection = projection_by_id.get(legacy_id)
                    if not projection:
                        issues.append(
                            {
                                "kind": "missing_source_projection",
                                "type": entity_type,
                                "legacy_id": legacy_id,
                            }
                        )
                        continue
                    if projection["catalog_owned"]:
                        issues.append(
                            {
                                "kind": "source_marked_catalog_owned",
                                "type": entity_type,
                                "legacy_id": legacy_id,
                                "entity_id": projection["entity_id"],
                            }
                        )
                    if not projection["snapshot_entity_id"]:
                        issues.append(
                            {
                                "kind": "missing_compat_snapshot",
                                "type": entity_type,
                                "legacy_id": legacy_id,
                                "entity_id": projection["entity_id"],
                            }
                        )
                    elif (
                        projection["source_name"] != source["name"]
                        or projection["source_created_at"]
                        != source["created_at"]
                        or projection["source_updated_at"]
                        != source["updated_at"]
                        or int(projection["visible_when_inactive"] or 0) != 0
                    ):
                        issues.append(
                            {
                                "kind": "compat_snapshot_mismatch",
                                "type": entity_type,
                                "legacy_id": legacy_id,
                                "entity_id": projection["entity_id"],
                            }
                        )
                for projection in projection_rows:
                    legacy_id = int(projection["legacy_integer_id"])
                    if projection["master_entity_type"] != entity_type:
                        issues.append(
                            {
                                "kind": "projection_type_mismatch",
                                "type": entity_type,
                                "legacy_id": legacy_id,
                                "entity_id": projection["entity_id"],
                            }
                        )
                    if projection["catalog_owned"]:
                        if legacy_id in source_by_id:
                            continue
                        if projection["snapshot_entity_id"]:
                            issues.append(
                                {
                                    "kind": "catalog_owned_has_snapshot",
                                    "type": entity_type,
                                    "legacy_id": legacy_id,
                                    "entity_id": projection["entity_id"],
                                }
                            )
                    elif legacy_id not in source_by_id:
                        issues.append(
                            {
                                "kind": "projection_has_no_source",
                                "type": entity_type,
                                "legacy_id": legacy_id,
                                "entity_id": projection["entity_id"],
                            }
                        )

                missing_active = conn.execute(
                    """
                    SELECT e.id,e.code FROM master_entities e
                    LEFT JOIN legacy_projection p ON p.entity_id=e.id
                    WHERE e.entity_type=? AND e.active=1 AND p.entity_id IS NULL
                    ORDER BY e.id
                    """,
                    (entity_type,),
                ).fetchall()
                issues.extend(
                    {
                        "kind": "active_entity_unprojected",
                        "type": entity_type,
                        "entity_id": row["id"],
                        "code": row["code"],
                    }
                    for row in missing_active
                )

            orphan_owned = conn.execute(
                """
                SELECT o.entity_id FROM catalog_owned_legacy_projection o
                LEFT JOIN legacy_projection p ON p.entity_id=o.entity_id
                WHERE p.entity_id IS NULL ORDER BY o.entity_id
                """
            ).fetchall()
            issues.extend(
                {"kind": "orphan_catalog_owned", "entity_id": row["entity_id"]}
                for row in orphan_owned
            )
            orphan_snapshots = conn.execute(
                """
                SELECT s.entity_id FROM legacy_projection_compat_snapshot s
                LEFT JOIN legacy_projection p ON p.entity_id=s.entity_id
                WHERE p.entity_id IS NULL ORDER BY s.entity_id
                """
            ).fetchall()
            issues.extend(
                {"kind": "orphan_compat_snapshot", "entity_id": row["entity_id"]}
                for row in orphan_snapshots
            )
            if issues:
                raise ValueError(
                    "active legacy master-data projection integrity failed: "
                    + canonical_json(issues)
                )

    @staticmethod
    def _scope_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT scope,status,reviewed_plan_sha256,
                       legacy_master_content_hash,activated_catalog_revision,
                       activated_catalog_content_hash,activated_at,activated_by,
                       updated_at
                FROM catalog_consumer_scopes ORDER BY scope
                """
            ).fetchall()
        ]

    def _apply_migrations(self) -> None:
        migration_files = sorted(MIGRATIONS.glob("*.sql"))
        if not migration_files:
            raise RuntimeError("shared master-data migrations are missing")
        with self._connect(create=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]: row["checksum"]
                for row in conn.execute("SELECT version,checksum FROM schema_migrations")
            }
            known_versions = {migration.stem for migration in migration_files}
            unknown_versions = sorted(set(applied) - known_versions)
            if unknown_versions:
                raise RuntimeError(
                    "shared master-data contains unsupported future migrations: "
                    + ", ".join(unknown_versions)
                )
            for migration in migration_files:
                version = migration.stem
                sql = migration.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if version in applied:
                    if applied[version] != checksum:
                        raise RuntimeError(f"migration checksum drift: {version}")
                    continue
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version,checksum,applied_at) VALUES(?,?,?)",
                    (version, checksum, utc_now()),
                )

    @staticmethod
    def _require_type(entity_type: str) -> dict[str, Any]:
        if entity_type not in TYPE_SPECS:
            raise MasterDataError(
                "master data type must be one of: " + ", ".join(TYPE_SPECS)
            )
        return TYPE_SPECS[entity_type]

    def _bootstrap(self) -> None:
        now = utc_now()
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            empty_hash = hashlib.sha256(canonical_json([]).encode()).hexdigest()
            conn.execute(
                """
                INSERT OR IGNORE INTO catalog_meta(
                    id,revision,content_hash,updated_at
                ) VALUES(1,0,?,?)
                """,
                (empty_hash, now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO catalog_revisions(
                    revision,content_hash,entity_type,entity_id,action,actor,created_at
                ) VALUES(0,?,NULL,NULL,'initialize','system:schema',?)
                """,
                (empty_hash, now),
            )
            conn.commit()

    def _unique_seed_code(self, conn: sqlite3.Connection, entity_type: str, name: str) -> str:
        base = _seed_code(name)
        code = base
        suffix = hashlib.sha256(normalize_identity(name).encode()).hexdigest()[:8]
        attempt = 0
        while True:
            row = conn.execute(
                "SELECT name FROM master_entities WHERE entity_type=? AND normalized_code=?",
                (entity_type, normalize_code(code)),
            ).fetchone()
            if not row or normalize_identity(row["name"]) == normalize_identity(name):
                return code
            attempt += 1
            code = f"{base[:80]}_{suffix}" if attempt == 1 else f"{base[:76]}_{suffix}_{attempt}"

    def _normalize_payload(
        self,
        conn: sqlite3.Connection,
        entity_type: str,
        payload: dict[str, Any],
        *,
        creating: bool,
        existing: dict[str, Any] | None = None,
        bootstrap: bool = False,
    ) -> dict[str, Any]:
        spec = self._require_type(entity_type)
        if not isinstance(payload, dict):
            raise MasterDataError("payload must be an object")
        unknown = set(payload) - COMMON_FIELDS - set(spec.get("fields", ()))
        if unknown:
            raise MasterDataError("unsupported fields: " + ", ".join(sorted(unknown)))

        # The HTTP layer intentionally accepts a dynamic object because the
        # eleven resource types share one endpoint.  Enforce JSON types here
        # instead of relying on Python coercion (for example bool("false")).
        for field in {"code", "name"} & set(payload):
            if not isinstance(payload[field], str):
                raise MasterDataError(f"{field} must be a string")
        for field in {"name_zh", "definition", "parent_id", "replacement_id"} & set(payload):
            if payload[field] is not None and not isinstance(payload[field], str):
                raise MasterDataError(f"{field} must be a string or null")
        for field in TEXT_ATTRIBUTE_FIELDS & set(payload):
            if payload[field] is not None and not isinstance(payload[field], str):
                raise MasterDataError(f"{field} must be a string or null")
        for field in REFERENCE_ATTRIBUTE_FIELDS & set(payload):
            if payload[field] is not None and not isinstance(payload[field], str):
                raise MasterDataError(f"{field} must be a string or null")
        if "active" in payload and type(payload["active"]) is not bool:
            raise MasterDataError("active must be a boolean")
        for field in BOOLEAN_ATTRIBUTE_FIELDS & set(payload):
            if type(payload[field]) is not bool:
                raise MasterDataError(f"{field} must be a boolean")
        if "sort_order" in payload and (
            type(payload["sort_order"]) is not int or payload["sort_order"] < 0
        ):
            raise MasterDataError("sort_order must be a non-negative integer")
        if "decimal_places" in payload and type(payload["decimal_places"]) is not int:
            raise MasterDataError("decimal_places must be an integer")
        for field in {"conversion_factor", "conversion_offset"} & set(payload):
            if type(payload[field]) not in {int, float}:
                raise MasterDataError(f"{field} must be a JSON number")

        result = dict(existing or {})
        result.update(payload)
        for field in TEXT_ATTRIBUTE_FIELDS & set(spec.get("fields", ())):
            value = result.get(field)
            if value is not None and len(clean_text(value)) > MAX_GOVERNED_TEXT_LENGTH:
                raise MasterDataError(
                    f"{field} must be at most {MAX_GOVERNED_TEXT_LENGTH} characters"
                )
        if creating:
            for field in ("code", "name"):
                if not clean_text(result.get(field)):
                    raise MasterDataError(f"{field} is required")
        if "code" in result:
            result["code"] = normalize_code(result["code"])
            if (
                existing
                and normalize_code(result["code"])
                != normalize_code(existing["code"])
            ):
                raise MasterDataError("published code is immutable")
        if "name" in result:
            result["name"] = clean_text(result["name"])
            if not result["name"]:
                raise MasterDataError("name is required")
            if len(result["name"]) > MAX_GOVERNED_TEXT_LENGTH:
                raise MasterDataError(
                    f"name must be at most {MAX_GOVERNED_TEXT_LENGTH} characters"
                )
        for field in ("name_zh", "definition"):
            if field in result:
                result[field] = clean_text(result.get(field)) or None
                if result[field] and len(result[field]) > MAX_GOVERNED_TEXT_LENGTH:
                    raise MasterDataError(
                        f"{field} must be at most {MAX_GOVERNED_TEXT_LENGTH} characters"
                    )
        result["active"] = result.get("active", True)
        result["sort_order"] = result.get("sort_order", 0)
        if type(result["sort_order"]) is not int or result["sort_order"] < 0:
            raise MasterDataError("sort_order must be a non-negative integer")
        result["parent_id"] = clean_text(result.get("parent_id")) or None
        result["replacement_id"] = clean_text(result.get("replacement_id")) or None
        if result["active"] and result["replacement_id"]:
            raise MasterDataError("active records cannot have replacement_id")

        aliases = result.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(item, str) for item in aliases):
            raise MasterDataError("aliases must be an array of strings")
        alias_map: dict[str, str] = {}
        for alias in aliases:
            cleaned = clean_text(alias)
            if not cleaned:
                continue
            if len(cleaned) > MAX_GOVERNED_TEXT_LENGTH:
                raise MasterDataError(
                    f"alias must be at most {MAX_GOVERNED_TEXT_LENGTH} characters"
                )
            key = normalize_identity(cleaned)
            if key == normalize_identity(result.get("name")):
                if "aliases" in payload:
                    raise MasterDataError("alias must differ from the canonical name")
                continue
            alias_map.setdefault(key, cleaned)
        if (
            existing
            and normalize_identity(existing.get("name"))
            != normalize_identity(result.get("name"))
        ):
            previous_name = clean_text(existing.get("name"))
            alias_map.setdefault(normalize_identity(previous_name), previous_name)
        result["aliases"] = list(alias_map.values())

        mappings = result.get("source_mappings", [])
        if not isinstance(mappings, list):
            raise MasterDataError("source_mappings must be an array")
        mapping_map: dict[tuple[str, str], dict[str, str]] = {}
        for item in mappings:
            if not isinstance(item, dict) or set(item) != {"system", "source_value"}:
                raise MasterDataError("each source mapping requires only system and source_value")
            if not isinstance(item["system"], str) or not isinstance(
                item["source_value"], str
            ):
                raise MasterDataError(
                    "source mapping system and source_value must be strings"
                )
            system = normalize_code(item["system"])
            source_value = clean_text(item["source_value"])
            if not source_value:
                raise MasterDataError("source mapping value is required")
            if len(source_value) > MAX_GOVERNED_TEXT_LENGTH:
                raise MasterDataError(
                    "source_value must be at most "
                    f"{MAX_GOVERNED_TEXT_LENGTH} characters"
                )
            mapping_map[(system, normalize_identity(source_value))] = {
                "system": system, "source_value": source_value,
            }
        result["source_mappings"] = list(mapping_map.values())

        for field in spec.get("required", set()):
            value = result.get(field)
            if value is None or value == "" or value == []:
                raise MasterDataError(f"{field} is required for {entity_type}")

        if result["parent_id"]:
            if not spec.get("hierarchy"):
                raise MasterDataError(f"{entity_type} does not support parent_id")
            result["parent_id"] = self._require_reference(
                conn, result["parent_id"], entity_type,
                require_active=bool(result["active"]),
            )["id"]
        if result["replacement_id"]:
            replacement = self._require_reference(conn, result["replacement_id"], entity_type)
            if existing and replacement["id"] == existing.get("id"):
                raise MasterDataError("replacement cannot reference the same record")
            if not replacement["active"]:
                raise MasterDataError("replacement must be active")
            result["replacement_id"] = replacement["id"]

        list_fields = {"market_region_ids", "supported_content"}
        for field in list_fields & set(spec.get("fields", ())):
            values = result.get(field, [])
            if not isinstance(values, list):
                raise MasterDataError(f"{field} must be an array")
            result[field] = sorted(
                {
                    clean_text(value)
                    for value in values
                    if isinstance(value, str) and clean_text(value)
                },
                key=normalize_identity,
            )
            if any(not isinstance(value, str) for value in values):
                raise MasterDataError(f"{field} must be an array of strings")

        if entity_type == "voltage-platforms":
            result["brand_id"] = self._require_reference(
                conn, result["brand_id"], "brands",
                require_active=bool(result["active"]),
            )["id"]
            result["canonical_voltage"] = self._canonical_voltage(result["canonical_voltage"])
            unit = self._require_reference(
                conn, result["unit_id"], "units",
                require_active=bool(result["active"]),
            )
            result["unit_id"] = unit["id"]
            if unit.get("symbol") != "V":
                raise MasterDataError("voltage platform unit must have symbol V")
            dimension = self._require_reference(
                conn, unit.get("dimension_id"), "dimensions",
                require_active=bool(result["active"]),
            )
            if normalize_code(dimension.get("code")) != "voltage":
                raise MasterDataError(
                    "voltage platform unit must belong to the voltage dimension"
                )
        elif entity_type == "retailers":
            result["market_region_ids"] = self._validate_references(
                conn, result.get("market_region_ids", []), "market-regions",
                require_active=bool(result["active"]),
            )
            if result.get("official_domain"):
                result["official_domain"] = self._domain(result["official_domain"])
        elif entity_type == "collection-channels":
            result["channel_type"] = normalize_code(result["channel_type"])
            if result["channel_type"] not in COLLECTION_CHANNEL_TYPES:
                raise MasterDataError(
                    "channel_type must be one of: "
                    + ", ".join(sorted(COLLECTION_CHANNEL_TYPES))
                )
            if result["channel_type"] == "brand_official" and not result.get("brand_id"):
                raise MasterDataError("brand_id is required for brand_official channels")
            if result["channel_type"] == "retailer_web" and not result.get("retailer_id"):
                raise MasterDataError("retailer_id is required for retailer_web channels")
            if result.get("brand_id"):
                result["brand_id"] = self._require_reference(
                    conn, result["brand_id"], "brands",
                    require_active=bool(result["active"]),
                )["id"]
            if result.get("retailer_id"):
                result["retailer_id"] = self._require_reference(
                    conn, result["retailer_id"], "retailers",
                    require_active=bool(result["active"]),
                )["id"]
            result["market_region_ids"] = self._validate_references(
                conn, result.get("market_region_ids", []), "market-regions",
                require_active=bool(result["active"]),
            )
            result["supported_content"] = sorted({
                normalize_code(value)
                for value in result.get("supported_content", [])
            })
            unsupported = set(result["supported_content"]) - COLLECTION_SUPPORTED_CONTENT
            if unsupported:
                raise MasterDataError(
                    "supported_content must use: "
                    + ", ".join(sorted(COLLECTION_SUPPORTED_CONTENT))
                )
        elif entity_type == "units":
            result["dimension_id"] = self._require_reference(
                conn, result["dimension_id"], "dimensions",
                require_active=bool(result["active"]),
            )["id"]
            result["symbol"] = clean_text(result["symbol"])
            if not result["symbol"]:
                raise MasterDataError("unit symbol is required")
            result["is_base"] = result.get("is_base", False)
            factor = result.get("conversion_factor")
            offset = result.get("conversion_offset", 0)
            if factor is None:
                raise MasterDataError("conversion_factor is required for units")
            try:
                number = Decimal(str(factor))
            except InvalidOperation as exc:
                raise MasterDataError("conversion_factor must be numeric") from exc
            if not number.is_finite() or number <= 0:
                raise MasterDataError("conversion_factor must be positive")
            result["conversion_factor"] = float(number)
            try:
                number = Decimal(str(offset))
            except InvalidOperation as exc:
                raise MasterDataError("conversion_offset must be numeric") from exc
            if not number.is_finite():
                raise MasterDataError("conversion_offset must be finite")
            result["conversion_offset"] = float(number)
            if result["is_base"]:
                if result.get("conversion_factor") not in {None, 1, 1.0} or result.get("conversion_offset") not in {None, 0, 0.0}:
                    raise MasterDataError("base unit conversion must be factor 1 and offset 0")
                result["conversion_factor"] = 1.0
                result["conversion_offset"] = 0.0
            elif result["active"]:
                base_exists = False
                for row in conn.execute(
                    "SELECT attributes_json FROM master_entities WHERE entity_type='units' AND active=1"
                ).fetchall():
                    attributes = json.loads(row["attributes_json"])
                    if (
                        attributes.get("dimension_id") == result["dimension_id"]
                        and attributes.get("is_base")
                    ):
                        base_exists = True
                        break
                if not base_exists:
                    raise MasterDataError(
                        "an active base unit must exist before adding a non-base unit"
                    )
            self._validate_unit_transition(conn, result, existing)
        elif entity_type == "product-lifecycle-statuses":
            result["is_terminal"] = result.get("is_terminal", False)
        elif entity_type == "currencies":
            result["code"] = result["code"].upper()
            if not re.fullmatch(r"[A-Z]{3}", result["code"]):
                raise MasterDataError("currency code must be a three-letter ISO code")
            result["symbol"] = clean_text(result.get("symbol")) or None
            if result["symbol"] and len(result["symbol"]) > 32:
                raise MasterDataError(
                    "currency symbol must be at most 32 characters"
                )
            decimals = result.get("decimal_places", 2)
            if type(decimals) is not int:
                raise MasterDataError("decimal_places must be an integer")
            if decimals < 0 or decimals > 6:
                raise MasterDataError("currency decimal_places must be between 0 and 6")
            result["decimal_places"] = decimals
        elif entity_type == "market-regions" and result.get("iso_code"):
            result["iso_code"] = clean_text(result["iso_code"]).upper()
            if not re.fullmatch(r"[A-Z0-9-]{2,12}", result["iso_code"]):
                raise MasterDataError("market region iso_code is invalid")
        elif entity_type == "brands":
            result["short_name"] = clean_text(result.get("short_name")) or None
            if result.get("official_domain"):
                result["official_domain"] = self._domain(result["official_domain"])
            else:
                result["official_domain"] = None
        elif entity_type == "dimensions":
            result["symbol"] = clean_text(result.get("symbol")) or None

        if entity_type == "market-regions" and not result.get("iso_code"):
            result["iso_code"] = None
        if entity_type == "retailers" and not result.get("official_domain"):
            result["official_domain"] = None

        if not bootstrap:
            self._validate_unique(conn, entity_type, result, existing_id=(existing or {}).get("id"))
        return result

    def _validate_unit_transition(
        self,
        conn: sqlite3.Connection,
        result: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> None:
        existing_id = (existing or {}).get("id")
        if existing:
            referenced = False
            for row in conn.execute(
                """
                SELECT attributes_json FROM master_entities
                WHERE entity_type='voltage-platforms' AND active=1
                """
            ).fetchall():
                if json.loads(row["attributes_json"]).get("unit_id") == existing_id:
                    referenced = True
                    break
            if referenced:
                immutable_fields = {
                    "dimension_id", "symbol", "is_base", "conversion_factor",
                    "conversion_offset",
                }
                if any(result.get(field) != existing.get(field) for field in immutable_fields):
                    raise MasterDataConflict(
                        "unit conversion identity cannot change while an active voltage platform references it"
                    )

        projected: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT id,attributes_json FROM master_entities WHERE entity_type='units' AND active=1"
        ).fetchall():
            if row["id"] == existing_id:
                continue
            projected.append(json.loads(row["attributes_json"]))
        if result.get("active", True):
            projected.append(result)
        affected_dimensions = {
            value
            for value in (
                (existing or {}).get("dimension_id"), result.get("dimension_id")
            )
            if value
        }
        for dimension_id in affected_dimensions:
            units = [
                item for item in projected
                if item.get("dimension_id") == dimension_id
            ]
            if units and sum(bool(item.get("is_base")) for item in units) != 1:
                raise MasterDataConflict(
                    "each dimension with active units must have exactly one active base unit"
                )

    @staticmethod
    def _domain(value: Any) -> str:
        domain = clean_text(value).casefold().removeprefix("https://").removeprefix("http://")
        domain = domain.split("/", 1)[0].removeprefix("www.").rstrip(".")
        labels = domain.split(".")
        if (
            len(domain) > 253
            or len(labels) < 2
            or not re.fullmatch(r"[a-z]{2,63}", labels[-1] if labels else "")
            or any(
                len(label) > 63
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels
            )
        ):
            raise MasterDataError("official_domain must be a hostname")
        return domain

    @staticmethod
    def _canonical_voltage(value: Any) -> str:
        text = clean_text(value)
        if '/' in text:
            parts = text.split('/')
            if len(parts) != 2:
                raise MasterDataError('canonical_voltage accepts at most two explicit voltages')
            values = [MasterDataStore._canonical_voltage(part) for part in parts]
            return '/'.join(dict.fromkeys(values))
        match = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)\s*(?:V|volt|volts)?", text)
        if not match:
            raise MasterDataError("canonical_voltage must be a positive voltage, for example 56V")
        number = Decimal(match.group(1))
        if not number.is_finite() or number <= 0 or number > 1000:
            raise MasterDataError("canonical_voltage must be between 0 and 1000V")
        rendered = format(number.normalize(), "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return f"{rendered}V"

    def _require_reference(
        self,
        conn: sqlite3.Connection,
        entity_id: str,
        entity_type: str,
        *,
        require_active: bool = False,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM master_entities WHERE id=? AND entity_type=?",
            (clean_text(entity_id), entity_type),
        ).fetchone()
        if not row:
            raise MasterDataError(f"referenced {entity_type} record does not exist: {entity_id}")
        item = self._entity(conn, row["id"])
        if require_active and not item["active"]:
            raise MasterDataError(f"referenced {entity_type} record is inactive: {entity_id}")
        return item

    def _validate_references(
        self,
        conn: sqlite3.Connection,
        ids: list[str],
        entity_type: str,
        *,
        require_active: bool = True,
    ) -> list[str]:
        return sorted({
            self._require_reference(
                conn, entity_id, entity_type,
                require_active=require_active,
            )["id"]
            for entity_id in ids
        })

    def _validate_unique(
        self,
        conn: sqlite3.Connection,
        entity_type: str,
        payload: dict[str, Any],
        *,
        existing_id: str | None,
    ) -> None:
        identity_keys = {
            normalize_identity(payload["code"]),
            normalize_identity(payload["name"]),
            *(normalize_identity(value) for value in payload.get("aliases", [])),
        }
        identity_keys.discard("")
        placeholders = ",".join("?" for _ in identity_keys)
        ordered_keys = sorted(identity_keys)
        rows = conn.execute(
            f"""
            SELECT id,code,name FROM master_entities
            WHERE entity_type=?
              AND (
                normalized_code IN ({placeholders})
                OR normalized_name IN ({placeholders})
              )
            """,
            [entity_type, *ordered_keys, *ordered_keys],
        ).fetchall()
        if any(row["id"] != existing_id for row in rows):
            raise MasterDataConflict(
                "code, NFKC-normalized name, or alias resolves to another record"
            )
        alias_rows = conn.execute(
            f"""
            SELECT entity_id FROM master_aliases
            WHERE entity_type=? AND normalized_value IN ({placeholders})
            """,
            [entity_type, *ordered_keys],
        ).fetchall()
        if any(row["entity_id"] != existing_id for row in alias_rows):
            raise MasterDataConflict(
                "code, NFKC-normalized name, or alias resolves to another record"
            )

        params = [entity_type, normalize_code(payload["code"]), normalize_identity(payload["name"])]
        sql = (
            "SELECT id,code,name FROM master_entities WHERE entity_type=? "
            "AND (normalized_code=? OR normalized_name=?)"
        )
        if existing_id:
            sql += " AND id!=?"
            params.append(existing_id)
        if conn.execute(sql, params).fetchone():
            raise MasterDataConflict("code or NFKC-normalized name already exists")
        for value in [payload["name"], *payload.get("aliases", [])]:
            key = normalize_identity(value)
            params = [entity_type, key]
            sql = "SELECT entity_id FROM master_aliases WHERE entity_type=? AND normalized_value=?"
            row = conn.execute(sql, params).fetchone()
            if row and row["entity_id"] != existing_id:
                raise MasterDataConflict(f"alias conflicts with another record: {value}")
            params = [entity_type, key]
            sql = "SELECT id FROM master_entities WHERE entity_type=? AND normalized_name=?"
            row = conn.execute(sql, params).fetchone()
            if row and row["id"] != existing_id:
                raise MasterDataConflict(f"alias conflicts with another canonical name: {value}")
        for mapping in payload.get("source_mappings", []):
            row = conn.execute(
                """
                SELECT entity_id FROM master_source_mappings
                WHERE entity_type=? AND source_system=? AND normalized_source_value=?
                """,
                (entity_type, mapping["system"], normalize_identity(mapping["source_value"])),
            ).fetchone()
            if row and row["entity_id"] != existing_id:
                raise MasterDataConflict(
                    f"source mapping already belongs to another record: {mapping['system']}:{mapping['source_value']}"
                )
        if entity_type == "voltage-platforms":
            rows = conn.execute(
                "SELECT id,attributes_json FROM master_entities WHERE entity_type='voltage-platforms'"
            ).fetchall()
            for row in rows:
                if row["id"] == existing_id:
                    continue
                attributes = json.loads(row["attributes_json"])
                if (
                    attributes.get("brand_id") == payload.get("brand_id")
                    and attributes.get("canonical_voltage") == payload.get("canonical_voltage")
                ):
                    raise MasterDataConflict("brand_id + canonical_voltage must be unique")
        if entity_type == "units":
            rows = conn.execute(
                "SELECT id,attributes_json FROM master_entities WHERE entity_type='units' AND active=1"
            ).fetchall()
            for row in rows:
                if row["id"] == existing_id:
                    continue
                attributes = json.loads(row["attributes_json"])
                if payload.get("is_base") and attributes.get("is_base") and attributes.get("dimension_id") == payload.get("dimension_id"):
                    raise MasterDataConflict("a dimension can have only one active base unit")

    def _insert_entity(
        self,
        conn: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
        actor: str,
        now: str,
    ) -> None:
        spec_fields = set(TYPE_SPECS[entity_type].get("fields", ()))
        attributes = {field: payload.get(field) for field in spec_fields if field in payload}
        conn.execute(
            """
            INSERT INTO master_entities(
                id,entity_type,code,normalized_code,name,normalized_name,name_zh,
                definition,active,replacement_id,parent_id,sort_order,revision,
                attributes_json,created_at,updated_at,created_by,updated_by
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)
            """,
            (
                entity_id, entity_type, payload["code"], normalize_code(payload["code"]),
                payload["name"], normalize_identity(payload["name"]), payload.get("name_zh"),
                payload.get("definition"), int(payload.get("active", True)),
                payload.get("replacement_id"), payload.get("parent_id"), payload.get("sort_order", 0),
                canonical_json(attributes), now, now, actor, actor,
            ),
        )
        self._replace_aliases(conn, entity_type, entity_id, payload.get("aliases", []), actor, now)
        self._replace_mappings(conn, entity_type, entity_id, payload.get("source_mappings", []), actor, now)

    def _replace_aliases(
        self, conn: sqlite3.Connection, entity_type: str, entity_id: str,
        aliases: list[str], actor: str, now: str,
    ) -> None:
        conn.execute("DELETE FROM master_aliases WHERE entity_id=?", (entity_id,))
        for alias in aliases:
            conn.execute(
                """
                INSERT INTO master_aliases(
                    id,entity_id,entity_type,value,normalized_value,created_at,created_by
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex, entity_id, entity_type, alias,
                    normalize_identity(alias), now, actor,
                ),
            )

    def _replace_mappings(
        self, conn: sqlite3.Connection, entity_type: str, entity_id: str,
        mappings: list[dict[str, str]], actor: str, now: str,
    ) -> None:
        conn.execute("DELETE FROM master_source_mappings WHERE entity_id=?", (entity_id,))
        for mapping in mappings:
            conn.execute(
                """
                INSERT INTO master_source_mappings(
                    id,entity_id,entity_type,source_system,source_value,
                    normalized_source_value,created_at,created_by
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex, entity_id, entity_type, mapping["system"],
                    mapping["source_value"], normalize_identity(mapping["source_value"]),
                    now, actor,
                ),
            )

    def _entity(self, conn: sqlite3.Connection, entity_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM master_entities WHERE id=?", (entity_id,)).fetchone()
        if not row:
            raise MasterDataNotFound("master data record not found")
        item = {
            "id": row["id"], "type": row["entity_type"], "code": row["code"],
            "name": row["name"], "name_zh": row["name_zh"],
            "definition": row["definition"], "active": bool(row["active"]),
            "replacement_id": row["replacement_id"], "parent_id": row["parent_id"],
            "sort_order": row["sort_order"], "revision": row["revision"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "created_by": row["created_by"], "updated_by": row["updated_by"],
        }
        item.update(json.loads(row["attributes_json"] or "{}"))
        for field in TYPE_SPECS[row["entity_type"]].get("fields", set()):
            if field in LIST_ATTRIBUTE_FIELDS:
                item.setdefault(field, [])
            elif field in BOOLEAN_ATTRIBUTE_FIELDS:
                item.setdefault(field, False)
            elif field == "conversion_offset":
                item.setdefault(field, 0.0)
            else:
                item.setdefault(field, None)
        item["aliases"] = [
            alias["value"] for alias in conn.execute(
                "SELECT value FROM master_aliases WHERE entity_id=? ORDER BY normalized_value",
                (entity_id,),
            ).fetchall()
        ]
        item["source_mappings"] = [
            {"system": mapping["source_system"], "source_value": mapping["source_value"]}
            for mapping in conn.execute(
                """
                SELECT source_system,source_value FROM master_source_mappings
                WHERE entity_id=? ORDER BY source_system,normalized_source_value
                """,
                (entity_id,),
            ).fetchall()
        ]
        return item

    def _catalog_content(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [
            self._entity(conn, row["id"])
            for row in conn.execute(
                "SELECT id FROM master_entities ORDER BY entity_type,normalized_code"
            ).fetchall()
        ]

    def _advance_catalog(
        self,
        conn: sqlite3.Connection,
        entity_type: str | None,
        entity_id: str | None,
        action: str,
        actor: str,
        now: str,
    ) -> tuple[int, str]:
        meta = conn.execute("SELECT revision FROM catalog_meta WHERE id=1").fetchone()
        revision = int(meta["revision"]) + 1
        content_hash = hashlib.sha256(canonical_json(self._catalog_content(conn)).encode()).hexdigest()
        conn.execute(
            "UPDATE catalog_meta SET revision=?,content_hash=?,updated_at=? WHERE id=1",
            (revision, content_hash, now),
        )
        conn.execute(
            """
            INSERT INTO catalog_revisions(
                revision,content_hash,entity_type,entity_id,action,actor,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (revision, content_hash, entity_type, entity_id, action, actor, now),
        )
        return revision, content_hash

    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        catalog_revision: int,
        entity_type: str,
        entity_id: str,
        entity_revision: int,
        action: str,
        actor: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO master_audit_events(
                id,catalog_revision,entity_type,entity_id,entity_revision,
                action,actor,before_json,after_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                uuid.uuid4().hex, catalog_revision, entity_type, entity_id,
                entity_revision, action, actor,
                canonical_json(before) if before else None,
                canonical_json(after) if after else None,
                now,
            ),
        )

    def etag(self) -> str:
        with self._connect() as conn:
            conn.execute("BEGIN")
            meta = conn.execute("SELECT revision,content_hash FROM catalog_meta WHERE id=1").fetchone()
        return f'"{meta["content_hash"]}"'

    def summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            meta = conn.execute("SELECT * FROM catalog_meta WHERE id=1").fetchone()
            counts = {
                row["entity_type"]: row
                for row in conn.execute(
                    """
                    SELECT entity_type,COUNT(*) AS total,SUM(active) AS active
                    FROM master_entities GROUP BY entity_type
                    """
                ).fetchall()
            }
        types = []
        for entity_type, spec in TYPE_SPECS.items():
            row = counts.get(entity_type)
            total = int(row["total"]) if row else 0
            active = int(row["active"] or 0) if row else 0
            types.append(
                {
                    "type": entity_type, "label": spec["label"], "total": total,
                    "active": active, "inactive": total - active,
                }
            )
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_revision": int(meta["revision"]),
            "catalog_content_hash": meta["content_hash"],
            "updated_at": meta["updated_at"],
            "types": types,
        }

    @contextmanager
    def task_snapshot_context(self):
        """Pin catalog values until a caller's task transaction is committed."""
        with self.lock, self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield self._snapshot_in(conn)
            finally:
                conn.rollback()

    def _snapshot_in(self, conn):
        meta = conn.execute('SELECT revision,content_hash FROM catalog_meta WHERE id=1').fetchone()
        records = self._catalog_content(conn)
        if hashlib.sha256(canonical_json(records).encode()).hexdigest() != meta['content_hash']:
            raise MasterDataConflict('catalog snapshot hash mismatch')
        return {'schema_version': 'voxquarry.shared-master-data-snapshot/v1',
                'catalog_revision': int(meta['revision']),
                'catalog_content_hash': meta['content_hash'], 'records': records}

    def snapshot(self):
        with self._connect() as conn:
            conn.execute('BEGIN')
            return self._snapshot_in(conn)

    def integration_status(self) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            meta = conn.execute(
                "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
            ).fetchone()
            scopes = self._scope_rows(conn)
            pending = int(conn.execute(
                """
                SELECT COUNT(*) AS value FROM legacy_job_rebind_intents
                WHERE status='pending'
                """
            ).fetchone()["value"])
        catalog_required = catalog_required_by_runtime()
        legacy_writes_protected = legacy_writes_protected_by_runtime()
        admin_writes_enabled = admin_writes_enabled_by_runtime()
        dev_mode_enabled = dev_mode_enabled_by_runtime()
        proxy_configured = valid_admin_proxy_token_by_runtime() is not None
        trusted_actor_count = len(valid_production_trusted_actors())
        deployment_blockers = []
        if not catalog_required:
            deployment_blockers.append(
                "MASTER_DATA_CATALOG_REQUIRED must be true for every runtime "
                "after legacy_brand_category_projection activation"
            )
        if not legacy_writes_protected:
            deployment_blockers.append(
                "MASTER_DATA_PROTECT_LEGACY_WRITES must be true before "
                "legacy_brand_category_projection activation"
            )
        if not admin_writes_enabled:
            deployment_blockers.append(
                "MASTER_DATA_ADMIN_WRITES_ENABLED must be true before activation"
            )
        if not proxy_configured:
            deployment_blockers.append(
                "MASTER_DATA_ADMIN_PROXY_TOKEN must contain at least 32 bytes "
                "before activation"
            )
        if not trusted_actor_count:
            deployment_blockers.append(
                "MASTER_DATA_TRUSTED_ACTORS must include at least one "
                "basic:<htpasswd-user> identity before activation"
            )
        if dev_mode_enabled:
            deployment_blockers.append(
                "MASTER_DATA_DEV_MODE must be false before production activation"
            )
        if pending:
            deployment_blockers.append(
                "pending legacy master-data rebind recovery must complete"
            )
        return {
            "schema_version": "voxquarry.shared-master-data-integration-status/v1",
            "catalog_revision": int(meta["revision"]),
            "catalog_content_hash": meta["content_hash"],
            "consumer_scopes": scopes,
            "pending_legacy_rebinds": pending,
            "catalog_required": catalog_required,
            "catalog_required_after_activation": True,
            "legacy_writes_protected": legacy_writes_protected,
            "legacy_write_protection_required_after_activation": True,
            "admin_writes_enabled": admin_writes_enabled,
            "admin_proxy_configured": proxy_configured,
            "trusted_actor_count": trusted_actor_count,
            "dev_mode_enabled": dev_mode_enabled,
            "deployment_blockers": deployment_blockers,
        }

    def list(
        self,
        entity_type: str,
        *,
        q: str = "",
        status: str = "active",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        self._require_type(entity_type)
        if status not in {"active", "inactive", "all"}:
            raise MasterDataError("status must be active, inactive, or all")
        page = int(page)
        page_size = int(page_size)
        if page < 1 or page_size < 1 or page_size > 250:
            raise MasterDataError("page must be >= 1 and page_size must be 1-250")
        clauses = ["e.entity_type=?"]
        params: list[Any] = [entity_type]
        if status != "all":
            clauses.append("e.active=?")
            params.append(1 if status == "active" else 0)
        if clean_text(q):
            token = f"%{normalize_identity(q)}%"
            clauses.append(
                """(
                    e.normalized_code LIKE ? OR e.normalized_name LIKE ? OR
                    EXISTS(SELECT 1 FROM master_aliases a WHERE a.entity_id=e.id AND a.normalized_value LIKE ?)
                )"""
            )
            params.extend([token, token, token])
        where = " AND ".join(clauses)
        with self._connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute(
                f"SELECT COUNT(*) AS value FROM master_entities e WHERE {where}", params
            ).fetchone()["value"]
            rows = conn.execute(
                f"""
                SELECT e.id FROM master_entities e WHERE {where}
                ORDER BY e.sort_order,e.normalized_name,e.id LIMIT ? OFFSET ?
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            items = [self._entity(conn, row["id"]) for row in rows]
            meta = conn.execute("SELECT revision,content_hash FROM catalog_meta WHERE id=1").fetchone()
        return {
            "type": entity_type, "items": items, "page": page,
            "page_size": page_size, "total": int(total),
            "catalog_revision": int(meta["revision"]),
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_content_hash": meta["content_hash"],
        }

    def detail(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        self._require_type(entity_type)
        with self._connect() as conn:
            conn.execute("BEGIN")
            item = self._entity(conn, entity_id)
            meta = conn.execute(
                "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
            ).fetchone()
        if item["type"] != entity_type:
            raise MasterDataNotFound("master data record not found")
        item["schema_version"] = CATALOG_SCHEMA_VERSION
        item["catalog_revision"] = int(meta["revision"])
        item["catalog_content_hash"] = meta["content_hash"]
        return item

    def validate_candidate(
        self,
        entity_type: str,
        *,
        code: str | None = None,
        name: str | None = None,
        exclude_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_type(entity_type)
        if not clean_text(code) and not clean_text(name):
            raise MasterDataError("code or name is required")
        conflicts: list[dict[str, Any]] = []
        with self._connect() as conn:
            conn.execute("BEGIN")
            candidates: list[tuple[str, str]] = []
            if clean_text(code):
                candidates.append(("code", normalize_identity(normalize_code(code))))
            if clean_text(name):
                candidates.append(("name", normalize_identity(name)))
            for input_field, normalized in candidates:
                rows = conn.execute(
                    """
                    SELECT id,code,name FROM master_entities
                    WHERE entity_type=?
                      AND (normalized_code=? OR normalized_name=?)
                    """,
                    (entity_type, normalized, normalized),
                ).fetchall()
                conflicts.extend(
                    dict(row) | {"field": input_field}
                    for row in rows if row["id"] != exclude_id
                )
                aliases = conn.execute(
                    """
                    SELECT e.id,e.code,e.name FROM master_aliases a
                    JOIN master_entities e ON e.id=a.entity_id
                    WHERE a.entity_type=? AND a.normalized_value=?
                    """,
                    (entity_type, normalized),
                ).fetchall()
                conflicts.extend(
                    dict(row) | {"field": input_field}
                    for row in aliases if row["id"] != exclude_id
                )
            meta = conn.execute(
                "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
            ).fetchone()
        deduped = list({(item["id"], item["field"]): item for item in conflicts}.values())
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_revision": int(meta["revision"]),
            "catalog_content_hash": meta["content_hash"],
            "valid": not deduped,
            "conflicts": deduped,
            "errors": [],
        }

    def resolve(
        self,
        entity_type: str,
        value: str,
        *,
        source_system: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a governed code/name/alias or an explicit source mapping."""
        self._require_type(entity_type)
        cleaned = clean_text(value)
        if not cleaned:
            raise MasterDataError("value is required")
        identity = normalize_identity(cleaned)
        with self._connect() as conn:
            conn.execute("BEGIN")
            match_kind = None
            entity_id = None
            if source_system:
                system = normalize_code(source_system)
                row = conn.execute(
                    """
                    SELECT m.entity_id FROM master_source_mappings m
                    JOIN master_entities e ON e.id=m.entity_id
                    WHERE m.entity_type=? AND m.source_system=?
                      AND m.normalized_source_value=? AND e.active=1
                    """,
                    (entity_type, system, identity),
                ).fetchone()
                if row:
                    entity_id = row["entity_id"]
                    match_kind = "source_mapping"
            else:
                try:
                    code = normalize_code(cleaned)
                except MasterDataError:
                    code = None
                if code:
                    row = conn.execute(
                        """
                        SELECT id FROM master_entities
                        WHERE entity_type=? AND normalized_code=? AND active=1
                        """,
                        (entity_type, code),
                    ).fetchone()
                    if row:
                        entity_id = row["id"]
                        match_kind = "code"
                if not entity_id:
                    row = conn.execute(
                        """
                        SELECT id FROM master_entities
                        WHERE entity_type=? AND normalized_name=? AND active=1
                        """,
                        (entity_type, identity),
                    ).fetchone()
                    if row:
                        entity_id = row["id"]
                        match_kind = "name"
                if not entity_id:
                    row = conn.execute(
                        """
                        SELECT a.entity_id FROM master_aliases a
                        JOIN master_entities e ON e.id=a.entity_id
                        WHERE a.entity_type=? AND a.normalized_value=? AND e.active=1
                        """,
                        (entity_type, identity),
                    ).fetchone()
                    if row:
                        entity_id = row["entity_id"]
                        match_kind = "alias"
            if not entity_id:
                raise MasterDataNotFound("no active master-data value matched")
            record = self._entity(conn, entity_id)
            meta = conn.execute(
                "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
            ).fetchone()
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_revision": int(meta["revision"]),
            "catalog_content_hash": meta["content_hash"],
            "match_kind": match_kind,
            "record": record,
        }

    def _resolve_active_record_in(
        self, conn: sqlite3.Connection, entity_type: str, value: str
    ) -> dict[str, Any]:
        cleaned = clean_text(value)
        if not cleaned:
            raise MasterDataError("value is required")
        identity = normalize_identity(cleaned)
        try:
            code = normalize_code(cleaned)
        except MasterDataError:
            code = None
        row = None
        if code:
            row = conn.execute(
                """
                SELECT id FROM master_entities
                WHERE entity_type=? AND normalized_code=? AND active=1
                """,
                (entity_type, code),
            ).fetchone()
        if not row:
            row = conn.execute(
                """
                SELECT id FROM master_entities
                WHERE entity_type=? AND normalized_name=? AND active=1
                """,
                (entity_type, identity),
            ).fetchone()
        if not row:
            row = conn.execute(
                """
                SELECT a.entity_id AS id FROM master_aliases a
                JOIN master_entities e ON e.id=a.entity_id
                WHERE a.entity_type=? AND a.normalized_value=? AND e.active=1
                """,
                (entity_type, identity),
            ).fetchone()
        if not row:
            raise MasterDataNotFound("no active master-data value matched")
        return self._entity(conn, row["id"])

    @contextmanager
    def legacy_binding_resolver(self):
        """Yield a same-snapshot resolver while the caller commits jobs."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            self._verify_active_scope_seal(conn)
            active = self._legacy_projection_active_in(conn)

            def resolve(brand: str | None, category: str | None):
                if not active:
                    return None
                resolved: dict[str, str | None] = {
                    "brand": brand,
                    "category": category,
                }
                for field, entity_type in (
                    ("brand", "brands"), ("category", "categories")
                ):
                    value = resolved[field]
                    if value is None:
                        continue
                    if not clean_text(value):
                        resolved[field] = ""
                        continue
                    try:
                        record = self._resolve_active_record_in(
                            conn, entity_type, value
                        )
                    except MasterDataNotFound as exc:
                        label = "品牌" if field == "brand" else "品类"
                        raise MasterDataError(
                            f"{label}“{clean_text(value)}”未在主数据中维护，"
                            "请先在主数据管理中添加或选择已有项"
                        ) from exc
                    resolved[field] = record["name"]
                return resolved

            yield resolve
        finally:
            conn.rollback()
            conn.close()

    @contextmanager
    def legacy_binding_snapshot(
        self, *, brand: str | None, category: str | None
    ):
        """Hold scope/values stable until the caller commits its jobs write."""
        with self.legacy_binding_resolver() as resolve:
            yield resolve(brand, category)

    def create(
        self,
        entity_type: str,
        payload: dict[str, Any],
        *,
        actor: str,
        expected_catalog_hash: str | None = None,
        _require_legacy_scope: bool = False,
    ) -> dict[str, Any]:
        actor = self._actor(actor)
        now = utc_now()
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_catalog_hash is not None:
                self._check_catalog_hash(conn, expected_catalog_hash)
            legacy_projection_active = self._legacy_projection_active_in(conn)
            if legacy_projection_active:
                self._verify_active_scope_seal(conn)
            if _require_legacy_scope and not legacy_projection_active:
                raise MasterDataConflict(
                    "legacy catalog consumer scope is inactive"
                )
            normalized = self._normalize_payload(conn, entity_type, payload, creating=True)
            entity_id = stable_id(entity_type, normalized["code"])
            if conn.execute("SELECT 1 FROM master_entities WHERE id=?", (entity_id,)).fetchone():
                raise MasterDataConflict("stable ID already exists")
            try:
                self._insert_entity(conn, entity_type, entity_id, normalized, actor, now)
                if (
                    legacy_projection_active
                    and entity_type in {"brands", "categories"}
                    and normalized["active"]
                ):
                    self._ensure_catalog_legacy_projection(
                        conn, entity_type, entity_id
                    )
                if legacy_projection_active:
                    self._refresh_legacy_projection_seal(conn)
                after = self._entity(conn, entity_id)
                revision, content_hash = self._advance_catalog(
                    conn, entity_type, entity_id, "create", actor, now
                )
                self._audit(
                    conn, catalog_revision=revision, entity_type=entity_type,
                    entity_id=entity_id, entity_revision=after["revision"], action="create",
                    actor=actor, before=None, after=after, now=now,
                )
                after["catalog_revision"] = revision
                after["catalog_content_hash"] = content_hash
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise MasterDataConflict(str(exc)) from exc
        return after

    def update(
        self,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
        *,
        actor: str,
        expected_revision: int | None = None,
        expected_catalog_hash: str | None = None,
        _action: str = "update",
        _legacy_rebind: bool = False,
        _require_legacy_scope: bool = False,
    ) -> dict[str, Any]:
        actor = self._actor(actor)
        now = utc_now()
        legacy_rebind_intent_id: str | None = None
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_catalog_hash is not None:
                self._check_catalog_hash(conn, expected_catalog_hash)
            legacy_projection_active = self._legacy_projection_active_in(conn)
            if legacy_projection_active:
                self._verify_active_scope_seal(conn)
            if _require_legacy_scope and not legacy_projection_active:
                raise MasterDataConflict(
                    "legacy catalog consumer scope is inactive"
                )
            before = self._entity(conn, entity_id)
            if before["type"] != entity_type:
                raise MasterDataNotFound("master data record not found")
            if expected_catalog_hash is None:
                if expected_revision is None:
                    raise MasterDataConflict("expected revision or catalog hash is required")
                self._check_revision(before, expected_revision)
            normalized = self._normalize_payload(
                conn, entity_type, payload, creating=False, existing=before
            )
            if entity_type == 'voltage-platforms' and any(
                normalized.get(field) != before.get(field)
                for field in ('brand_id', 'canonical_voltage', 'unit_id')
            ):
                raise MasterDataError('voltage platform identity is immutable; create a replacement record')
            if before["active"] and not normalized["active"]:
                self._check_disable_allowed(conn, before)
            spec_fields = set(TYPE_SPECS[entity_type].get("fields", ()))
            attributes = {field: normalized.get(field) for field in spec_fields if field in normalized}
            try:
                conn.execute(
                    """
                    UPDATE master_entities SET
                        name=?,normalized_name=?,name_zh=?,definition=?,active=?,
                        replacement_id=?,parent_id=?,sort_order=?,revision=revision+1,
                        attributes_json=?,updated_at=?,updated_by=?
                    WHERE id=? AND revision=?
                    """,
                    (
                        normalized["name"], normalize_identity(normalized["name"]),
                        normalized.get("name_zh"), normalized.get("definition"),
                        int(normalized["active"]), normalized.get("replacement_id"),
                        normalized.get("parent_id"), normalized.get("sort_order", 0),
                        canonical_json(attributes), now, actor, entity_id, before["revision"],
                    ),
                )
                self._replace_aliases(conn, entity_type, entity_id, normalized.get("aliases", []), actor, now)
                self._replace_mappings(conn, entity_type, entity_id, normalized.get("source_mappings", []), actor, now)
                self._validate_hierarchy(conn, entity_type, entity_id, normalized.get("parent_id"))
                after = self._entity(conn, entity_id)
                if (
                    legacy_projection_active
                    and entity_type in {"brands", "categories"}
                    and after["active"]
                ):
                    self._ensure_catalog_legacy_projection(
                        conn, entity_type, entity_id
                    )
                    conn.execute(
                        """
                        UPDATE legacy_projection_compat_snapshot
                        SET visible_when_inactive=0 WHERE entity_id=?
                        """,
                        (entity_id,),
                    )
                if (
                    legacy_projection_active
                    and _legacy_rebind
                    and before["name"] != after["name"]
                ):
                    legacy_rebind_intent_id = uuid.uuid4().hex
                    conn.execute(
                        """
                        INSERT INTO legacy_job_rebind_intents(
                            id,entity_type,entity_id,old_name,new_name,status,
                            actor,created_at
                        ) VALUES(?,?,?,?,?,'pending',?,?)
                        """,
                        (
                            legacy_rebind_intent_id, entity_type, entity_id,
                            before["name"], after["name"], actor, now,
                        ),
                    )
                if legacy_projection_active:
                    self._refresh_legacy_projection_seal(conn)
                revision, content_hash = self._advance_catalog(
                    conn, entity_type, entity_id, _action, actor, now
                )
                self._audit(
                    conn, catalog_revision=revision, entity_type=entity_type,
                    entity_id=entity_id, entity_revision=after["revision"], action=_action,
                    actor=actor, before=before, after=after, now=now,
                )
                after["catalog_revision"] = revision
                after["catalog_content_hash"] = content_hash
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise MasterDataConflict(str(exc)) from exc
        if legacy_rebind_intent_id:
            self._apply_legacy_rebind_intent(
                legacy_rebind_intent_id, actor=actor
            )
        return after

    def set_active(
        self,
        entity_type: str,
        entity_id: str,
        active: bool,
        *,
        actor: str,
        expected_revision: int | None = None,
        expected_catalog_hash: str | None = None,
        replacement_id: str | None = None,
        _require_legacy_scope: bool = False,
    ) -> dict[str, Any]:
        if type(active) is not bool:
            raise MasterDataError("active must be a boolean")
        payload: dict[str, Any] = {
            "active": active,
            "replacement_id": None if active else replacement_id,
        }
        if not active and replacement_id is not None:
            payload["replacement_id"] = replacement_id
        result = self.update(
            entity_type, entity_id, payload, actor=actor,
            expected_revision=expected_revision,
            expected_catalog_hash=expected_catalog_hash,
            _action="enable" if active else "disable",
            _require_legacy_scope=_require_legacy_scope,
        )
        return result

    @staticmethod
    def _actor(actor: str) -> str:
        value = clean_text(actor)
        if not value or value.casefold() in {"anonymous", "匿名"}:
            raise MasterDataError("an explicit master-data actor is required")
        if len(value) > 128:
            raise MasterDataError("actor is too long")
        return value

    @staticmethod
    def _check_revision(item: dict[str, Any], expected_revision: int) -> None:
        if int(expected_revision) != int(item["revision"]):
            raise MasterDataConflict(
                f"revision conflict: expected {expected_revision}, current {item['revision']}"
            )

    @staticmethod
    def _check_catalog_hash(conn: sqlite3.Connection, expected_hash: str) -> None:
        expected = clean_text(expected_hash).strip('"')
        row = conn.execute("SELECT content_hash FROM catalog_meta WHERE id=1").fetchone()
        if not row or expected != row["content_hash"]:
            current = row["content_hash"] if row else "unavailable"
            raise MasterDataConflict(
                f"catalog ETag conflict: expected {expected}, current {current}"
            )

    def _validate_hierarchy(
        self, conn: sqlite3.Connection, entity_type: str, entity_id: str, parent_id: str | None
    ) -> None:
        if not parent_id:
            return
        current = parent_id
        seen = {entity_id}
        while current:
            if current in seen:
                raise MasterDataConflict("hierarchy cannot contain a cycle")
            seen.add(current)
            row = conn.execute(
                "SELECT parent_id,entity_type FROM master_entities WHERE id=?", (current,)
            ).fetchone()
            if not row or row["entity_type"] != entity_type:
                raise MasterDataError("parent must belong to the same master-data type")
            current = row["parent_id"]

    def _check_disable_allowed(self, conn: sqlite3.Connection, item: dict[str, Any]) -> None:
        references = self._active_references(conn, item)
        if references:
            preview = ", ".join(
                f"{ref['type']}:{ref['code']}" for ref in references[:5]
            )
            raise MasterDataConflict(
                f"cannot disable {item['name']}: active master-data references exist ({preview})"
            )

    def _active_references(
        self, conn: sqlite3.Connection, item: dict[str, Any]
    ) -> list[dict[str, str]]:
        references: list[dict[str, str]] = []
        for row in conn.execute(
            """
            SELECT id,entity_type,code,parent_id,replacement_id,attributes_json
            FROM master_entities WHERE id!=? AND active=1
            """,
            (item["id"],),
        ).fetchall():
            attributes = json.loads(row["attributes_json"] or "{}")
            matched = row["parent_id"] == item["id"]
            if item["type"] == "brands":
                matched = matched or attributes.get("brand_id") == item["id"]
            elif item["type"] == "market-regions":
                matched = matched or item["id"] in attributes.get("market_region_ids", [])
            elif item["type"] == "retailers":
                matched = matched or attributes.get("retailer_id") == item["id"]
            elif item["type"] == "dimensions":
                matched = matched or attributes.get("dimension_id") == item["id"]
            elif item["type"] == "units":
                matched = matched or attributes.get("unit_id") == item["id"]
            if matched:
                references.append(
                    {"id": row["id"], "type": row["entity_type"], "code": row["code"]}
                )
        # A replacement target is still a live governance pointer even though
        # its source record is inactive.
        for row in conn.execute(
            """
            SELECT id,entity_type,code FROM master_entities
            WHERE id!=? AND replacement_id=?
            """,
            (item["id"], item["id"]),
        ).fetchall():
            candidate = {"id": row["id"], "type": row["entity_type"], "code": row["code"]}
            if candidate not in references:
                references.append(candidate)
        return references

    def _ensure_catalog_legacy_projection(
        self, conn: sqlite3.Connection, entity_type: str, entity_id: str
    ) -> int:
        projection = conn.execute(
            "SELECT legacy_integer_id FROM legacy_projection WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if projection:
            return int(projection["legacy_integer_id"])
        legacy_id = int(conn.execute(
            """
            SELECT COALESCE(MAX(legacy_integer_id),0)+1 AS value
            FROM legacy_projection WHERE entity_type=?
            """,
            (entity_type,),
        ).fetchone()["value"])
        conn.execute(
            """
            INSERT INTO legacy_projection(entity_id,entity_type,legacy_integer_id)
            VALUES(?,?,?)
            """,
            (entity_id, entity_type, legacy_id),
        )
        conn.execute(
            "INSERT INTO catalog_owned_legacy_projection(entity_id) VALUES(?)",
            (entity_id,),
        )
        return legacy_id

    def _legacy_projection_rows(
        self, conn: sqlite3.Connection, entity_type: str
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": int(row["legacy_integer_id"]),
                "name": row["name"],
                "created_at": row["projected_created_at"],
                "updated_at": row["projected_updated_at"],
            }
            for row in conn.execute(
                """
                SELECT p.legacy_integer_id,e.name,
                       COALESCE(s.created_at,e.created_at) AS projected_created_at,
                       CASE
                         WHEN s.entity_id IS NOT NULL AND e.name=s.source_name
                           THEN s.updated_at
                         ELSE e.updated_at
                       END AS projected_updated_at
                FROM legacy_projection p
                JOIN master_entities e ON e.id=p.entity_id
                LEFT JOIN legacy_projection_compat_snapshot s ON s.entity_id=e.id
                WHERE p.entity_type=?
                  AND (e.active=1 OR COALESCE(s.visible_when_inactive,0)=1)
                ORDER BY e.normalized_name,e.id
                """,
                (entity_type,),
            ).fetchall()
        ]

    def _apply_legacy_rebind_intent(
        self, intent_id: str, *, actor: str | None = None
    ) -> None:
        if (
            not self.legacy_db_path
            or not self.legacy_db_path.is_file()
            or self.legacy_db_path.is_symlink()
        ):
            raise MasterDataConflict(
                "live jobs SQLite is required to complete legacy rename recovery"
            )
        with self._connect() as conn:
            conn.execute("BEGIN")
            scope = conn.execute(
                "SELECT status FROM catalog_consumer_scopes WHERE scope=?",
                (LEGACY_PROJECTION_SCOPE,),
            ).fetchone()
            if not scope or scope["status"] != "active":
                raise MasterDataConflict(
                    "legacy catalog consumer scope is inactive"
                )
            self._verify_active_scope_seal(conn)
            intent = conn.execute(
                "SELECT * FROM legacy_job_rebind_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
            binding_names = []
            if intent:
                binding_names = sorted(
                    {
                        intent["old_name"],
                        *[
                            row["value"] for row in conn.execute(
                                """
                                SELECT value FROM master_aliases
                                WHERE entity_id=? ORDER BY normalized_value
                                """,
                                (intent["entity_id"],),
                            ).fetchall()
                        ],
                    },
                    key=normalize_identity,
                )
        if not intent or intent["status"] == "completed":
            return
        completion_actor = self._actor(actor or intent["actor"])
        column = "brand" if intent["entity_type"] == "brands" else "category"
        with sqlite3.connect(self.legacy_db_path, timeout=30) as legacy:
            legacy.execute("PRAGMA busy_timeout=30000")
            legacy.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _name in binding_names)
            legacy.execute(
                f"UPDATE jobs SET {column}=? WHERE {column} COLLATE NOCASE "
                f"IN ({placeholders})",
                (intent["new_name"], *binding_names),
            )
            legacy.commit()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._verify_active_scope_seal(conn)
            conn.execute(
                """
                UPDATE legacy_job_rebind_intents
                SET status='completed',completed_at=?,completed_by=?
                WHERE id=? AND status='pending'
                """,
                (utc_now(), completion_actor, intent_id),
            )
            self._refresh_legacy_projection_seal(conn)
            conn.commit()

    def recover_legacy_rebinds(self, *, actor: str) -> dict[str, Any]:
        self._actor(actor)
        with self._connect() as conn:
            pending = [
                row["id"] for row in conn.execute(
                    """
                    SELECT id FROM legacy_job_rebind_intents
                    WHERE status='pending' ORDER BY created_at,id
                    """
                ).fetchall()
            ]
        for intent_id in pending:
            self._apply_legacy_rebind_intent(intent_id, actor=actor)
        return {"recovered": len(pending), "pending": 0}

    @staticmethod
    def _governed_import_state(
        entity_type: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        fields = {
            "code", "name", "name_zh", "definition", "active",
            "replacement_id", "parent_id", "sort_order", "aliases",
            "source_mappings", *TYPE_SPECS[entity_type].get("fields", set()),
        }
        state = {field: item.get(field) for field in sorted(fields)}
        state["aliases"] = sorted(
            state.get("aliases") or [], key=normalize_identity
        )
        state["source_mappings"] = sorted(
            state.get("source_mappings") or [],
            key=lambda mapping: (
                normalize_code(mapping["system"]),
                normalize_identity(mapping["source_value"]),
            ),
        )
        return state

    @staticmethod
    def _merge_import_source_mappings(
        existing: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        merged_payload = dict(payload)
        mappings = list(existing.get("source_mappings", []))
        identities = {
            (
                normalize_code(mapping["system"]),
                normalize_identity(mapping["source_value"]),
            )
            for mapping in mappings
        }
        for mapping in payload.get("source_mappings", []):
            identity = (
                normalize_code(mapping["system"]),
                normalize_identity(mapping["source_value"]),
            )
            if identity not in identities:
                mappings.append(mapping)
                identities.add(identity)
        merged_payload["source_mappings"] = mappings
        return merged_payload

    def _write_import_existing(
        self,
        conn: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        before: dict[str, Any],
        normalized: dict[str, Any],
        actor: str,
        now: str,
    ) -> dict[str, Any]:
        if before["active"] and not normalized["active"]:
            self._check_disable_allowed(conn, before)
        spec_fields = set(TYPE_SPECS[entity_type].get("fields", ()))
        attributes = {
            field: normalized.get(field)
            for field in spec_fields
            if field in normalized
        }
        cursor = conn.execute(
            """
            UPDATE master_entities SET
                name=?,normalized_name=?,name_zh=?,definition=?,active=?,
                replacement_id=?,parent_id=?,sort_order=?,revision=revision+1,
                attributes_json=?,updated_at=?,updated_by=?
            WHERE id=? AND revision=?
            """,
            (
                normalized["name"], normalize_identity(normalized["name"]),
                normalized.get("name_zh"), normalized.get("definition"),
                int(normalized["active"]), normalized.get("replacement_id"),
                normalized.get("parent_id"), normalized.get("sort_order", 0),
                canonical_json(attributes), now, actor, entity_id,
                before["revision"],
            ),
        )
        if cursor.rowcount != 1:
            raise MasterDataConflict("reviewed import record revision changed")
        self._replace_aliases(
            conn, entity_type, entity_id, normalized.get("aliases", []),
            actor, now,
        )
        self._replace_mappings(
            conn, entity_type, entity_id,
            normalized.get("source_mappings", []), actor, now,
        )
        self._validate_hierarchy(
            conn, entity_type, entity_id, normalized.get("parent_id")
        )
        return self._entity(conn, entity_id)

    @staticmethod
    def _ordered_import_records(
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        def item_id(item: dict[str, Any]) -> str:
            return item.get("existing_id") or stable_id(
                item["type"], item["payload"]["code"]
            )

        def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
            return (
                IMPORT_TYPE_ORDER[item["type"]],
                normalize_code(item["payload"]["code"]),
                int(item.get("legacy_id") or 0),
            )

        grouped_actionable: dict[str, list[dict[str, Any]]] = {}
        for item in records:
            if item["action"] in {"conflict", "unresolved"}:
                continue
            grouped_actionable.setdefault(item_id(item), []).append(item)
        for entity_id, items in grouped_actionable.items():
            if len(items) <= 1:
                continue
            for item in items:
                item["action"] = "conflict"
                item["error"] = (
                    "multiple reviewed import records target the same stable "
                    f"entity ID: {entity_id}"
                )
        actionable_ids = {
            entity_id: items[0]
            for entity_id, items in grouped_actionable.items()
            if len(items) == 1
        }
        reference_fields = {
            "parent_id", "replacement_id", "brand_id", "retailer_id",
            "dimension_id", "unit_id",
        }
        dependencies: dict[str, set[str]] = {}
        for entity_id, item in actionable_ids.items():
            payload = item["payload"]
            referenced = {
                payload[field]
                for field in reference_fields
                if isinstance(payload.get(field), str) and payload[field]
            }
            referenced.update(
                value
                for field in ("market_region_ids",)
                for value in payload.get(field, [])
                if isinstance(value, str)
            )
            dependencies[entity_id] = (
                referenced & set(actionable_ids)
            ) - {entity_id}

        ordered: list[dict[str, Any]] = []
        emitted: set[str] = set()
        while len(emitted) < len(actionable_ids):
            ready = sorted(
                (
                    item for entity_id, item in actionable_ids.items()
                    if entity_id not in emitted
                    and dependencies[entity_id] <= emitted
                ),
                key=sort_key,
            )
            if not ready:
                # Preserve deterministic output; the exact API hierarchy/
                # reference validator will report the cycle as a conflict.
                ready = sorted(
                    (
                        item for entity_id, item in actionable_ids.items()
                        if entity_id not in emitted
                    ),
                    key=sort_key,
                )[:1]
            for item in ready:
                entity_id = item_id(item)
                if entity_id not in emitted:
                    ordered.append(item)
                    emitted.add(entity_id)
        ordered.extend(
            sorted(
                (
                    item for item in records
                    if item["action"] in {"conflict", "unresolved"}
                ),
                key=sort_key,
            )
        )
        return ordered

    @staticmethod
    def _load_manifest(path: str | Path) -> tuple[dict[str, Any], str]:
        manifest_path = Path(path)
        try:
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MasterDataError(f"invalid bootstrap manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise MasterDataError("bootstrap manifest must be an object")
        required_keys = {
            "schema_version", "manifest_id", "evidence", "records",
            "legacy_overrides", "unresolved",
        }
        if set(manifest) != required_keys:
            raise MasterDataError(
                "bootstrap manifest keys must be exactly: "
                + ", ".join(sorted(required_keys))
            )
        if manifest.get("schema_version") != "voxquarry.shared-master-data-bootstrap/v1":
            raise MasterDataError("unsupported bootstrap manifest schema_version")
        if not isinstance(manifest.get("manifest_id"), str) or not clean_text(
            manifest["manifest_id"]
        ):
            raise MasterDataError("bootstrap manifest_id is required")
        evidence = manifest["evidence"]
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(
                not isinstance(item, str)
                or not clean_text(item)
                or len(clean_text(item)) > MAX_GOVERNED_TEXT_LENGTH
                for item in evidence
            )
        ):
            raise MasterDataError(
                "bootstrap manifest evidence must be a non-empty array of strings"
            )
        records = manifest.get("records")
        if not isinstance(records, dict):
            raise MasterDataError("bootstrap manifest records must be an object")
        for entity_type, items in records.items():
            spec = MasterDataStore._require_type(entity_type)
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise MasterDataError(f"manifest records.{entity_type} must be an array of objects")
            allowed = COMMON_FIELDS | set(spec.get("fields", ()))
            normalized_codes: set[str] = set()
            normalized_names: set[str] = set()
            for item in items:
                unknown = set(item) - allowed
                if unknown:
                    raise MasterDataError(
                        f"manifest records.{entity_type} has unsupported fields: "
                        + ", ".join(sorted(unknown))
                    )
                if isinstance(item.get("code"), str):
                    code_key = normalize_code(item["code"])
                    if code_key in normalized_codes:
                        raise MasterDataError(
                            f"manifest records.{entity_type} codes must be "
                            "NFKC-normalized unique"
                        )
                    normalized_codes.add(code_key)
                if isinstance(item.get("name"), str):
                    name_key = normalize_identity(item["name"])
                    if name_key in normalized_names:
                        raise MasterDataError(
                            f"manifest records.{entity_type} names must be "
                            "NFKC-normalized unique"
                        )
                    normalized_names.add(name_key)
        overrides = manifest.get("legacy_overrides", {})
        if not isinstance(overrides, dict):
            raise MasterDataError("legacy_overrides must be an object")
        for entity_type, values in overrides.items():
            if entity_type not in {"brands", "categories"} or not isinstance(values, dict):
                raise MasterDataError("legacy_overrides supports only brands/categories objects")
            normalized_override_names: set[str] = set()
            for source_name, rule in values.items():
                if (
                    not isinstance(source_name, str)
                    or not clean_text(source_name)
                    or not isinstance(rule, dict)
                ):
                    raise MasterDataError("legacy override names and rules are required")
                normalized_source_name = normalize_identity(source_name)
                if normalized_source_name in normalized_override_names:
                    raise MasterDataError(
                        "legacy override names must be NFKC-normalized unique"
                    )
                normalized_override_names.add(normalized_source_name)
                if set(rule) != {"action", "reason"}:
                    raise MasterDataError(
                        "legacy override rules require exactly action and reason"
                    )
                if rule.get("action") not in {"import_inactive", "unresolved"}:
                    raise MasterDataError("legacy override action must be import_inactive or unresolved")
                if (
                    not isinstance(rule.get("reason"), str)
                    or not clean_text(rule["reason"])
                    or len(clean_text(rule["reason"])) > MAX_GOVERNED_TEXT_LENGTH
                ):
                    raise MasterDataError("legacy override reason is required")
        unresolved = manifest["unresolved"]
        if not isinstance(unresolved, list):
            raise MasterDataError("bootstrap manifest unresolved must be an array")
        for item in unresolved:
            if not isinstance(item, dict) or set(item) != {
                "scope", "values", "reason",
            }:
                raise MasterDataError(
                    "each unresolved item requires exactly scope, values, and reason"
                )
            if (
                not isinstance(item["scope"], str)
                or not clean_text(item["scope"])
                or len(clean_text(item["scope"])) > MAX_GOVERNED_TEXT_LENGTH
            ):
                raise MasterDataError("unresolved scope must be a non-empty string")
            if (
                not isinstance(item["reason"], str)
                or not clean_text(item["reason"])
                or len(clean_text(item["reason"])) > MAX_GOVERNED_TEXT_LENGTH
            ):
                raise MasterDataError("unresolved reason must be a non-empty string")
            if (
                not isinstance(item["values"], list)
                or any(
                    not isinstance(value, str)
                    or not clean_text(value)
                    or len(clean_text(value)) > MAX_GOVERNED_TEXT_LENGTH
                    for value in item["values"]
                )
            ):
                raise MasterDataError(
                    "unresolved values must be an array of non-empty strings"
                )
        return manifest, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _legacy_master_content_hash(
        legacy: dict[str, list[dict[str, Any]]]
    ) -> str:
        governed = {
            entity_type: [
                {"id": int(row["id"]), "name": str(row["name"])}
                for row in sorted(rows, key=lambda item: int(item["id"]))
            ]
            for entity_type, rows in sorted(legacy.items())
        }
        return hashlib.sha256(canonical_json(governed).encode()).hexdigest()

    @staticmethod
    def _read_legacy_snapshot(
        path: str | Path, *, immutable: bool
    ) -> tuple[dict[str, list[dict[str, Any]]], str | None, str]:
        db_path = Path(path)
        if not db_path.is_file() or db_path.is_symlink():
            raise MasterDataError(
                "jobs SQLite must be a regular, non-symlink file"
            )
        source_hash = file_sha256(db_path) if immutable else None
        uri = f"file:{db_path.resolve()}?mode=ro"
        if immutable:
            uri += "&immutable=1"
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise MasterDataError(f"jobs SQLite integrity_check={integrity}")
            foreign_key_rows = conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_rows:
                raise MasterDataError(
                    "jobs SQLite foreign_key_check found violations"
                )
            result = {
                "brands": [
                    dict(row) for row in conn.execute(
                        "SELECT id,name,created_at,updated_at FROM master_brands ORDER BY id"
                    )
                ],
                "categories": [
                    dict(row) for row in conn.execute(
                        "SELECT id,name,created_at,updated_at FROM master_categories ORDER BY id"
                    )
                ],
            }
            conn.commit()
        except sqlite3.Error as exc:
            raise MasterDataError(f"invalid jobs SQLite: {exc}") from exc
        finally:
            if "conn" in locals():
                conn.close()
        return result, source_hash, MasterDataStore._legacy_master_content_hash(result)

    @staticmethod
    def _read_legacy_copy(
        path: str | Path,
    ) -> tuple[dict[str, list[dict[str, Any]]], str, str]:
        legacy, source_hash, content_hash = MasterDataStore._read_legacy_snapshot(
            path, immutable=True
        )
        assert source_hash is not None
        return legacy, source_hash, content_hash

    def import_plan(
        self,
        *,
        legacy_db_copy: str | Path,
        manifest_path: str | Path,
    ) -> dict[str, Any]:
        """Build a zero-write, content-addressed import plan from reviewed inputs."""
        legacy, source_hash, legacy_content_hash = self._read_legacy_copy(
            legacy_db_copy
        )
        manifest, manifest_hash = self._load_manifest(manifest_path)
        proposed: list[dict[str, Any]] = []
        unresolved = list(manifest.get("unresolved") or [])
        with self._connect() as conn:
            conn.execute("BEGIN")
            base_meta = conn.execute(
                "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
            ).fetchone()
            used_codes: dict[str, set[str]] = {
                entity_type: {
                    row["normalized_code"] for row in conn.execute(
                        "SELECT normalized_code FROM master_entities WHERE entity_type=?",
                        (entity_type,),
                    )
                }
                for entity_type in TYPE_SPECS
            }
            existing_by_name = {
                entity_type: {
                    row["normalized_name"]: dict(row)
                    for row in conn.execute(
                        "SELECT id,code,name,normalized_name FROM master_entities WHERE entity_type=?",
                        (entity_type,),
                    )
                }
                for entity_type in TYPE_SPECS
            }

            for entity_type in ("brands", "categories"):
                override_entries = {
                    normalize_identity(name): (name, rule)
                    for name, rule in (
                        manifest.get("legacy_overrides", {}).get(entity_type, {})
                    ).items()
                }
                matched_overrides: set[str] = set()
                for legacy_row in legacy[entity_type]:
                    name_key = normalize_identity(legacy_row["name"])
                    override_entry = override_entries.get(name_key)
                    override = override_entry[1] if override_entry else None
                    if override_entry:
                        matched_overrides.add(name_key)
                    if override and override["action"] == "unresolved":
                        unresolved.append(
                            {
                                "scope": f"legacy.{entity_type}",
                                "values": [legacy_row["name"]],
                                "legacy_id": int(legacy_row["id"]),
                                "reason": override["reason"],
                            }
                        )
                        continue
                    existing = existing_by_name[entity_type].get(name_key)
                    if existing:
                        code = existing["code"]
                        projection = conn.execute(
                            """
                            SELECT entity_id FROM legacy_projection
                            WHERE entity_type=? AND legacy_integer_id=?
                            """,
                            (entity_type, int(legacy_row["id"])),
                        ).fetchone()
                        action = (
                            "already_present"
                            if projection and projection["entity_id"] == existing["id"]
                            else "map_existing"
                        )
                    else:
                        base = _seed_code(legacy_row["name"])
                        code = base
                        suffix = hashlib.sha256(name_key.encode()).hexdigest()[:8]
                        attempt = 0
                        while normalize_code(code) in used_codes[entity_type]:
                            attempt += 1
                            code = (
                                f"{base[:80]}_{suffix}"
                                if attempt == 1
                                else f"{base[:76]}_{suffix}_{attempt}"
                            )
                        used_codes[entity_type].add(normalize_code(code))
                        action = "create"
                    payload = {
                        "code": code,
                        "name": clean_text(legacy_row["name"]),
                        "active": not (
                            override
                            and override["action"] == "import_inactive"
                        ),
                        "source_mappings": [
                            {"system": "wotu-v1", "source_value": str(legacy_row["id"])},
                            {"system": "wotu", "source_value": clean_text(legacy_row["name"])},
                        ],
                    }
                    if override:
                        payload["definition"] = override["reason"]
                    proposed.append(
                        {
                            "type": entity_type,
                            "action": action,
                            "existing_id": existing["id"] if existing else None,
                            "legacy_id": int(legacy_row["id"]),
                            "payload": payload,
                        }
                    )
                for name_key, (source_name, rule) in override_entries.items():
                    if name_key in matched_overrides:
                        continue
                    unresolved.append(
                        {
                            "scope": f"legacy.{entity_type}",
                            "values": [source_name],
                            "reason": (
                                "legacy override did not match the reviewed "
                                f"source snapshot: {rule['reason']}"
                            ),
                        }
                    )

            for entity_type, records in manifest["records"].items():
                for payload in records:
                    code_key = normalize_code(payload.get("code"))
                    row = conn.execute(
                        "SELECT id,name FROM master_entities WHERE entity_type=? AND normalized_code=?",
                        (entity_type, code_key),
                    ).fetchone()
                    if row and normalize_identity(row["name"]) != normalize_identity(payload.get("name")):
                        action = "conflict"
                    elif row:
                        action = "already_present"
                    elif code_key in used_codes[entity_type]:
                        action = "conflict"
                    else:
                        action = "create"
                        used_codes[entity_type].add(code_key)
                    proposed.append(
                        {
                            "type": entity_type,
                            "action": action,
                            "existing_id": row["id"] if row else None,
                            "legacy_id": None,
                            "payload": payload,
                        }
                    )

            active_ids: dict[str, set[str]] = {
                entity_type: {
                    row["id"] for row in conn.execute(
                        """
                        SELECT id FROM master_entities
                        WHERE entity_type=? AND active=1
                        """,
                        (entity_type,),
                    ).fetchall()
                }
                for entity_type in TYPE_SPECS
            }
            for item in proposed:
                entity_id = item.get("existing_id") or stable_id(
                    item["type"], item["payload"]["code"]
                )
                if item["payload"].get("active", True):
                    if item["action"] == "create":
                        active_ids[item["type"]].add(entity_id)
                else:
                    active_ids[item["type"]].discard(entity_id)

            # An active channel whose governed subject is absent is not a
            # usable authoritative value. Keep it visible in the review plan,
            # but never silently import it or infer the subject from its name.
            for item in proposed:
                if item["type"] != "collection-channels" or item["action"] != "create":
                    continue
                payload = item["payload"]
                required: tuple[str, str] | None = None
                if payload.get("channel_type") == "brand_official":
                    required = ("brand_id", "brands")
                elif payload.get("channel_type") == "retailer_web":
                    required = ("retailer_id", "retailers")
                if not required:
                    continue
                field, target_type = required
                reference = clean_text(payload.get(field))
                if reference and reference in active_ids[target_type]:
                    continue
                reason = (
                    f"{payload.get('channel_type')} requires an active explicit "
                    f"{field}; name-based inference is prohibited"
                )
                item["action"] = "unresolved"
                item["error"] = reason
                unresolved.append(
                    {
                        "scope": "manifest.collection-channels",
                        "values": [payload.get("code")],
                        "reason": reason,
                    }
                )

            proposed = self._ordered_import_records(proposed)

            # Validate every proposed create/update/no-op with the exact API
            # validator against an in-memory snapshot. A SAVEPOINT on the
            # real catalog is logically reversible but still changes SQLite's
            # physical file/mtime; a dry-run must leave the evidence artifact
            # byte-for-byte unchanged.
            validation_conn = sqlite3.connect(":memory:")
            validation_conn.row_factory = sqlite3.Row
            validation_conn.execute("PRAGMA foreign_keys=ON")
            conn.backup(validation_conn)
            validation_conn.execute("BEGIN")
            try:
                for index, proposed_item in enumerate(proposed):
                    if proposed_item["action"] in {"conflict", "unresolved"}:
                        continue
                    item_savepoint = f"import_plan_item_{index}"
                    validation_conn.execute(f"SAVEPOINT {item_savepoint}")
                    try:
                        entity_type = proposed_item["type"]
                        payload = dict(proposed_item["payload"])
                        if proposed_item["action"] == "create":
                            normalized = self._normalize_payload(
                                validation_conn, entity_type, payload,
                                creating=True,
                            )
                            entity_id = stable_id(
                                entity_type, normalized["code"]
                            )
                            self._insert_entity(
                                validation_conn, entity_type, entity_id,
                                normalized, "dry-run:validator",
                                "1970-01-01T00:00:00+00:00",
                            )
                        else:
                            entity_id = proposed_item.get("existing_id")
                            if not entity_id:
                                raise MasterDataConflict(
                                    "existing import record is missing stable ID"
                                )
                            existing = self._entity(
                                validation_conn, entity_id
                            )
                            update_payload = payload
                            if proposed_item.get("legacy_id") is not None:
                                update_payload = self._merge_import_source_mappings(
                                    existing, payload
                                )
                            normalized = self._normalize_payload(
                                validation_conn, entity_type, update_payload,
                                creating=False, existing=existing,
                            )
                            before_state = self._governed_import_state(
                                entity_type, existing
                            )
                            after_state = self._governed_import_state(
                                entity_type, normalized
                            )
                            difference_fields = sorted(
                                field for field in before_state
                                if before_state[field] != after_state[field]
                            )
                            proposed_item["difference_fields"] = difference_fields
                            if difference_fields:
                                if proposed_item.get("legacy_id") is not None:
                                    allowed = (
                                        set(difference_fields)
                                        <= {"active", "definition", "source_mappings"}
                                        and not (
                                            not existing["active"]
                                            and normalized["active"]
                                        )
                                    )
                                else:
                                    allowed = (
                                        payload.get("active") is False
                                        and existing["active"]
                                        and not normalized["active"]
                                    )
                                if not allowed:
                                    raise MasterDataConflict(
                                        "existing record differs from reviewed "
                                        "manifest: "
                                        + ", ".join(difference_fields)
                                    )
                                if proposed_item["action"] != "map_existing":
                                    proposed_item["action"] = "update_existing"
                                self._write_import_existing(
                                    validation_conn, entity_type, entity_id,
                                    existing, normalized, "dry-run:validator",
                                    "1970-01-01T00:00:00+00:00",
                                )
                            elif proposed_item["action"] != "map_existing":
                                proposed_item["action"] = "already_present"
                        legacy_id = proposed_item.get("legacy_id")
                        if legacy_id is not None:
                            by_legacy_id = validation_conn.execute(
                                """
                                SELECT entity_id FROM legacy_projection
                                WHERE entity_type=? AND legacy_integer_id=?
                                """,
                                (entity_type, int(legacy_id)),
                            ).fetchone()
                            by_entity = validation_conn.execute(
                                """
                                SELECT entity_type,legacy_integer_id
                                FROM legacy_projection WHERE entity_id=?
                                """,
                                (entity_id,),
                            ).fetchone()
                            if (
                                by_legacy_id
                                and by_legacy_id["entity_id"] != entity_id
                            ) or (
                                by_entity
                                and (
                                    by_entity["entity_type"] != entity_type
                                    or int(by_entity["legacy_integer_id"])
                                    != int(legacy_id)
                                )
                            ):
                                raise MasterDataConflict(
                                    "legacy integer ID projection conflict"
                                )
                            if not by_legacy_id and not by_entity:
                                validation_conn.execute(
                                    """
                                    INSERT INTO legacy_projection(
                                        entity_id,entity_type,legacy_integer_id
                                    ) VALUES(?,?,?)
                                    """,
                                    (entity_id, entity_type, int(legacy_id)),
                                )
                        validation_conn.execute(f"RELEASE {item_savepoint}")
                    except (MasterDataError, sqlite3.IntegrityError) as exc:
                        validation_conn.execute(
                            f"ROLLBACK TO {item_savepoint}"
                        )
                        validation_conn.execute(f"RELEASE {item_savepoint}")
                        proposed_item["action"] = "conflict"
                        proposed_item["error"] = str(exc)
            finally:
                validation_conn.rollback()
                validation_conn.close()

        plan_core = {
            "schema_version": "voxquarry.shared-master-data-import-plan/v1",
            "manifest_id": manifest["manifest_id"],
            "manifest_sha256": manifest_hash,
            "legacy_copy_sha256": source_hash,
            "legacy_master_content_hash": legacy_content_hash,
            "base_catalog_revision": int(base_meta["revision"]),
            "base_catalog_content_hash": base_meta["content_hash"],
            "records": proposed,
            "unresolved": unresolved,
            "counts": {
                "brands": len(legacy["brands"]),
                "categories": len(legacy["categories"]),
                "create": sum(item["action"] == "create" for item in proposed),
                "map_existing": sum(item["action"] == "map_existing" for item in proposed),
                "update_existing": sum(
                    item["action"] == "update_existing" for item in proposed
                ),
                "already_present": sum(item["action"] == "already_present" for item in proposed),
                "conflicts": sum(item["action"] == "conflict" for item in proposed),
                "unresolved_records": sum(
                    item["action"] == "unresolved" for item in proposed
                ),
                "unresolved_groups": len(unresolved),
            },
        }
        return {
            **plan_core,
            "plan_sha256": hashlib.sha256(canonical_json(plan_core).encode()).hexdigest(),
        }

    def apply_import(
        self,
        *,
        legacy_db_copy: str | Path,
        manifest_path: str | Path,
        actor: str,
        expected_plan_sha256: str,
        expected_legacy_copy_sha256: str,
    ) -> dict[str, Any]:
        """Apply exactly one previously reviewed plan; never infer new mappings."""
        actor = self._actor(actor)
        if (
            not self.legacy_db_path
            or not self.legacy_db_path.is_file()
            or self.legacy_db_path.is_symlink()
        ):
            raise MasterDataConflict(
                "--legacy-db-live is required to bind import to live governed tables"
            )
        copy_path = Path(legacy_db_copy)
        if not copy_path.is_file() or copy_path.is_symlink():
            raise MasterDataConflict(
                "reviewed legacy copy must be a regular, non-symlink file"
            )
        try:
            copy_stat = copy_path.stat()
            live_stat = self.legacy_db_path.stat()
        except OSError as exc:
            raise MasterDataConflict(
                "legacy copy/live SQLite path cannot be inspected"
            ) from exc
        if (
            copy_path.resolve() == self.legacy_db_path.resolve()
            or (copy_stat.st_dev, copy_stat.st_ino)
            == (live_stat.st_dev, live_stat.st_ino)
        ):
            raise MasterDataConflict(
                "reviewed legacy copy must be a distinct file from live jobs SQLite"
            )
        copy_sidecars = [
            Path(f"{copy_path}-wal"), Path(f"{copy_path}-shm"),
        ]
        if any(path.exists() for path in copy_sidecars):
            raise MasterDataConflict(
                "reviewed legacy copy must not have WAL/SHM sidecars"
            )
        plan = self.import_plan(
            legacy_db_copy=legacy_db_copy, manifest_path=manifest_path
        )
        if plan["plan_sha256"] != clean_text(expected_plan_sha256):
            raise MasterDataConflict("import plan hash changed; review a fresh dry-run")
        if plan["legacy_copy_sha256"] != clean_text(expected_legacy_copy_sha256):
            raise MasterDataConflict("copied jobs SQLite hash changed")
        if plan["counts"]["conflicts"]:
            raise MasterDataConflict("import plan contains conflicts")

        now = utc_now()
        changed: list[
            tuple[str, str, str, dict[str, Any] | None]
        ] = []
        with self.lock, self._connect() as conn:
                conn.execute(
                    "ATTACH DATABASE ? AS legacy_live",
                    (
                        f"file:{self.legacy_db_path.resolve()}?mode=rw",
                    ),
                )
                # Reserve catalog then jobs in the same order used by every
                # legacy/task binding path.  The attached jobs DB is read-only
                # in this transaction; only the independent catalog changes.
                conn.execute("BEGIN EXCLUSIVE")
                live_legacy = {
                    "brands": [
                        dict(row) for row in conn.execute(
                            "SELECT id,name,created_at,updated_at "
                            "FROM legacy_live.master_brands ORDER BY id"
                        )
                    ],
                    "categories": [
                        dict(row) for row in conn.execute(
                            "SELECT id,name,created_at,updated_at "
                            "FROM legacy_live.master_categories ORDER BY id"
                        )
                    ],
                }
                live_content_hash = self._legacy_master_content_hash(
                    live_legacy
                )
                if live_content_hash != plan["legacy_master_content_hash"]:
                    raise MasterDataConflict(
                        "live legacy brand/category content changed since "
                        "reviewed copy"
                    )
                meta = conn.execute(
                    "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
                ).fetchone()
                if (
                    int(meta["revision"]) != plan["base_catalog_revision"]
                    or meta["content_hash"] != plan["base_catalog_content_hash"]
                ):
                    raise MasterDataConflict(
                        "catalog changed after dry-run; review a fresh import plan"
                    )
                self._verify_active_scope_seal(conn)
                if self._legacy_projection_active_in(conn):
                    raise MasterDataConflict(
                        "explicit import is disabled while legacy projection is active"
                    )
                for proposed in plan["records"]:
                    entity_type = proposed["type"]
                    payload = dict(proposed["payload"])
                    action = proposed["action"]
                    legacy_id = proposed.get("legacy_id")
                    if action == "unresolved":
                        continue
                    if action == "create":
                        normalized = self._normalize_payload(
                            conn, entity_type, payload, creating=True
                        )
                        entity_id = stable_id(entity_type, normalized["code"])
                        self._insert_entity(
                            conn, entity_type, entity_id, normalized, actor, now
                        )
                        changed.append(
                            (entity_type, entity_id, "import_create", None)
                        )
                    else:
                        entity_id = proposed.get("existing_id")
                        if not entity_id:
                            raise MasterDataConflict(
                                "reviewed existing import record has no stable ID"
                            )
                        existing = self._entity(conn, entity_id)
                        if existing["type"] != entity_type:
                            raise MasterDataConflict(
                                "reviewed import record type changed"
                            )
                        update_payload = payload
                        if legacy_id is not None:
                            update_payload = self._merge_import_source_mappings(
                                existing, payload
                            )
                        normalized = self._normalize_payload(
                            conn, entity_type, update_payload, creating=False,
                            existing=existing,
                        )
                        before_state = self._governed_import_state(
                            entity_type, existing
                        )
                        after_state = self._governed_import_state(
                            entity_type, normalized
                        )
                        differs = before_state != after_state
                        if differs and action not in {
                            "map_existing", "update_existing",
                        }:
                            raise MasterDataConflict(
                                "reviewed existing record is no longer a no-op"
                            )
                        if differs:
                            self._write_import_existing(
                                conn, entity_type, entity_id, existing,
                                normalized, actor, now,
                            )
                            changed.append(
                                (
                                    entity_type, entity_id,
                                    "import_map" if action == "map_existing"
                                    else "import_update",
                                    existing,
                                )
                            )
                    if legacy_id is not None:
                        collision = conn.execute(
                            """
                            SELECT entity_id FROM legacy_projection
                            WHERE entity_type=? AND legacy_integer_id=?
                            """,
                            (entity_type, int(legacy_id)),
                        ).fetchone()
                        if collision and collision["entity_id"] != entity_id:
                            raise MasterDataConflict(
                                "legacy integer ID projection conflict"
                            )
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO legacy_projection(
                                entity_id,entity_type,legacy_integer_id
                            ) VALUES(?,?,?)
                            """,
                            (entity_id, entity_type, int(legacy_id)),
                        )
                        source = next(
                            row for row in live_legacy[entity_type]
                            if int(row["id"]) == int(legacy_id)
                        )
                        projected_item = self._entity(conn, entity_id)
                        conn.execute(
                            """
                            INSERT INTO legacy_projection_compat_snapshot(
                                entity_id,source_name,created_at,updated_at,
                                visible_when_inactive
                            ) VALUES(?,?,?,?,?)
                            ON CONFLICT(entity_id) DO UPDATE SET
                                source_name=excluded.source_name,
                                created_at=excluded.created_at,
                                updated_at=excluded.updated_at,
                                visible_when_inactive=excluded.visible_when_inactive
                            """,
                            (
                                entity_id, source["name"], source["created_at"],
                                source["updated_at"],
                                int(not projected_item["active"]),
                            ),
                        )
                if changed:
                    revision, content_hash = self._advance_catalog(
                        conn, None, None, "explicit_import", actor, now
                    )
                    for entity_type, entity_id, action, before in changed:
                        after = self._entity(conn, entity_id)
                        self._audit(
                            conn, catalog_revision=revision,
                            entity_type=entity_type, entity_id=entity_id,
                            entity_revision=after["revision"], action=action,
                            actor=actor, before=before, after=after, now=now,
                        )
                else:
                    revision = int(meta["revision"])
                    content_hash = meta["content_hash"]
                ignored = {
                    (item["scope"].split(".", 1)[1], int(item["legacy_id"]))
                    for item in plan["unresolved"]
                    if item.get("scope") in {"legacy.brands", "legacy.categories"}
                    and item.get("legacy_id") is not None
                }
                reconcile = self._reconcile_legacy_data(
                    conn, live_legacy, ignored_legacy=ignored,
                    legacy_content_hash=live_content_hash,
                )
                if not reconcile["ok"]:
                    raise MasterDataConflict(
                        "post-import legacy reconciliation failed"
                    )
                self._refresh_legacy_projection_seal(conn)
                conn.execute(
                    """
                    INSERT INTO master_import_receipts(
                        plan_sha256,manifest_sha256,legacy_copy_sha256,
                        legacy_master_content_hash,base_catalog_revision,
                        base_catalog_content_hash,resulting_catalog_revision,
                        resulting_catalog_content_hash,unresolved_json,
                        reconcile_json,actor,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        plan["plan_sha256"], plan["manifest_sha256"],
                        plan["legacy_copy_sha256"], live_content_hash,
                        plan["base_catalog_revision"],
                        plan["base_catalog_content_hash"], revision, content_hash,
                        canonical_json(plan["unresolved"]),
                        canonical_json(reconcile), actor, now,
                    ),
                )
                conn.commit()
        return {
            "applied": len(changed),
            "plan_sha256": plan["plan_sha256"],
            "legacy_copy_sha256": plan["legacy_copy_sha256"],
            "legacy_master_content_hash": live_content_hash,
            "catalog": self.summary(),
            "integration_status": self.integration_status(),
            "unresolved": plan["unresolved"],
            "reconcile": reconcile,
        }

    def _reconcile_legacy_data(
        self,
        conn: sqlite3.Connection,
        legacy: dict[str, list[dict[str, Any]]],
        *,
        ignored_legacy: set[tuple[str, int]] | None = None,
        legacy_content_hash: str,
    ) -> dict[str, Any]:
        ignored_legacy = ignored_legacy or set()
        missing_projection: list[dict[str, Any]] = []
        missing_source: list[dict[str, Any]] = []
        name_mismatch: list[dict[str, Any]] = []
        inactive_projection: list[dict[str, Any]] = []
        reviewed_unresolved: list[dict[str, Any]] = []
        for entity_type in ("brands", "categories"):
            source_by_id = {int(row["id"]): row for row in legacy[entity_type]}
            projections = conn.execute(
                """
                SELECT p.legacy_integer_id,e.id,e.name,e.active,
                       CASE WHEN o.entity_id IS NULL THEN 0 ELSE 1 END AS catalog_owned,
                       COALESCE(s.visible_when_inactive,0) AS visible_when_inactive
                FROM legacy_projection p JOIN master_entities e ON e.id=p.entity_id
                LEFT JOIN catalog_owned_legacy_projection o ON o.entity_id=e.id
                LEFT JOIN legacy_projection_compat_snapshot s ON s.entity_id=e.id
                WHERE p.entity_type=?
                """,
                (entity_type,),
            ).fetchall()
            projection_ids = {int(row["legacy_integer_id"]) for row in projections}
            for legacy_id, source in source_by_id.items():
                if (entity_type, legacy_id) in ignored_legacy:
                    reviewed_unresolved.append(
                        {"type": entity_type, "legacy_id": legacy_id, "name": source["name"]}
                    )
                elif legacy_id not in projection_ids:
                    missing_projection.append(
                        {"type": entity_type, "legacy_id": legacy_id, "name": source["name"]}
                    )
            for row in projections:
                legacy_id = int(row["legacy_integer_id"])
                source = source_by_id.get(legacy_id)
                if not source:
                    if not row["catalog_owned"]:
                        missing_source.append(
                            {"type": entity_type, "legacy_id": legacy_id, "id": row["id"]}
                        )
                elif normalize_identity(source["name"]) != normalize_identity(row["name"]):
                    name_mismatch.append(
                        {
                            "type": entity_type, "legacy_id": legacy_id,
                            "catalog_name": row["name"], "source_name": source["name"],
                        }
                    )
                elif not row["active"] and not row["visible_when_inactive"]:
                    inactive_projection.append(
                        {
                            "type": entity_type,
                            "legacy_id": legacy_id,
                            "id": row["id"],
                            "name": row["name"],
                        }
                    )
        return {
            "schema_version": "voxquarry.shared-master-data-reconcile/v1",
            "legacy_master_content_hash": legacy_content_hash,
            "source_counts": {
                "brands": len(legacy["brands"]),
                "categories": len(legacy["categories"]),
            },
            "missing_projection": missing_projection,
            "missing_source": missing_source,
            "name_mismatch": name_mismatch,
            "inactive_projection": inactive_projection,
            "reviewed_unresolved": reviewed_unresolved,
            "ok": not (
                missing_projection or missing_source or name_mismatch
                or inactive_projection
            ),
        }

    def reconcile_legacy_copy(
        self, legacy_db_copy: str | Path, *, manifest_path: str | Path | None = None
    ) -> dict[str, Any]:
        """Read-only reconciliation; it never repairs either database."""
        legacy, source_hash, content_hash = self._read_legacy_copy(legacy_db_copy)
        ignored: set[tuple[str, int]] = set()
        if manifest_path:
            manifest, _ = self._load_manifest(manifest_path)
            for entity_type in ("brands", "categories"):
                rules = {
                    normalize_identity(name): rule
                    for name, rule in manifest.get("legacy_overrides", {}).get(
                        entity_type, {}
                    ).items()
                }
                for row in legacy[entity_type]:
                    rule = rules.get(normalize_identity(row["name"]))
                    if rule and rule.get("action") == "unresolved":
                        ignored.add((entity_type, int(row["id"])))
        with self._connect() as conn:
            conn.execute("BEGIN")
            result = self._reconcile_legacy_data(
                conn, legacy, ignored_legacy=ignored,
                legacy_content_hash=content_hash,
            )
        result["legacy_copy_sha256"] = source_hash
        return result

    def activate_legacy_projection(
        self,
        *,
        actor: str,
        expected_plan_sha256: str,
        expected_legacy_master_content_hash: str,
    ) -> dict[str, Any]:
        """Explicitly switch only legacy brand/category consumers to catalog."""
        actor = self._actor(actor)
        if not catalog_required_by_runtime():
            raise MasterDataError(
                "MASTER_DATA_CATALOG_REQUIRED=true is required before "
                "legacy brand/category projection activation"
            )
        if not legacy_writes_protected_by_runtime():
            raise MasterDataError(
                "MASTER_DATA_PROTECT_LEGACY_WRITES=true is required before "
                "legacy brand/category projection activation"
            )
        if not admin_writes_enabled_by_runtime():
            raise MasterDataError(
                "MASTER_DATA_ADMIN_WRITES_ENABLED=true is required before "
                "legacy brand/category projection activation"
            )
        if dev_mode_enabled_by_runtime():
            raise MasterDataError(
                "MASTER_DATA_DEV_MODE must be false before legacy "
                "brand/category projection activation"
            )
        if valid_admin_proxy_token_by_runtime() is None:
            raise MasterDataError(
                "MASTER_DATA_ADMIN_PROXY_TOKEN must contain at least 32 bytes "
                "before legacy brand/category projection activation"
            )
        if not valid_production_trusted_actors():
            raise MasterDataError(
                "MASTER_DATA_TRUSTED_ACTORS must include at least one "
                "basic:<htpasswd-user> identity before legacy brand/category "
                "projection activation"
            )
        if (
            not self.legacy_db_path
            or not self.legacy_db_path.is_file()
            or self.legacy_db_path.is_symlink()
        ):
            raise MasterDataConflict("live jobs SQLite is required for activation")
        now = utc_now()
        with self.lock, self._connect() as conn:
                conn.execute(
                    "ATTACH DATABASE ? AS legacy_live",
                    (
                        f"file:{self.legacy_db_path.resolve()}?mode=rw",
                    ),
                )
                # The catalog is SQLite's main database, so this reserves the
                # catalog before the attached jobs DB and matches binding
                # operations' catalog -> jobs lock order.
                conn.execute("BEGIN EXCLUSIVE")
                live_legacy = {
                    "brands": [
                        dict(row) for row in conn.execute(
                            "SELECT id,name,created_at,updated_at "
                            "FROM legacy_live.master_brands ORDER BY id"
                        )
                    ],
                    "categories": [
                        dict(row) for row in conn.execute(
                            "SELECT id,name,created_at,updated_at "
                            "FROM legacy_live.master_categories ORDER BY id"
                        )
                    ],
                }
                live_hash = self._legacy_master_content_hash(live_legacy)
                expected_live_hash = clean_text(
                    expected_legacy_master_content_hash
                )
                if live_hash != expected_live_hash:
                    raise MasterDataConflict(
                        "live legacy brand/category content hash does not "
                        "match activation receipt"
                    )
                plan_sha = clean_text(expected_plan_sha256)
                receipt = conn.execute(
                    "SELECT * FROM master_import_receipts WHERE plan_sha256=?",
                    (plan_sha,),
                ).fetchone()
                if not receipt:
                    raise MasterDataConflict(
                        "reviewed applied import receipt was not found"
                    )
                if receipt["legacy_master_content_hash"] != live_hash:
                    raise MasterDataConflict(
                        "live legacy content differs from applied import receipt"
                    )
                meta = conn.execute(
                    "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
                ).fetchone()
                if (
                    int(meta["revision"]) != int(receipt["resulting_catalog_revision"])
                    or meta["content_hash"]
                    != receipt["resulting_catalog_content_hash"]
                ):
                    raise MasterDataConflict(
                        "catalog changed after reviewed import; create a fresh plan"
                    )
                legacy_unresolved = [
                    item for item in json.loads(receipt["unresolved_json"])
                    if item.get("scope") in {
                        "legacy.brands", "legacy.categories"
                    }
                ]
                if legacy_unresolved:
                    raise MasterDataConflict(
                        "legacy brand/category scope still has unresolved values"
                    )
                pending = conn.execute(
                    """
                    SELECT COUNT(*) AS value FROM legacy_job_rebind_intents
                    WHERE status='pending'
                    """
                ).fetchone()["value"]
                if pending:
                    raise MasterDataConflict(
                        "pending legacy rename recovery must complete before activation"
                    )
                reconcile = self._reconcile_legacy_data(
                    conn, live_legacy, ignored_legacy=set(),
                    legacy_content_hash=live_hash,
                )
                if not reconcile["ok"]:
                    raise MasterDataConflict(
                        "legacy reconciliation is not complete; activation refused"
                    )
                inactive_binding_blockers = [
                    dict(row) for row in conn.execute(
                        """
                        SELECT e.id,e.entity_type,e.code,e.name,
                               p.legacy_integer_id
                        FROM legacy_projection_compat_snapshot s
                        JOIN master_entities e ON e.id=s.entity_id
                        JOIN legacy_projection p ON p.entity_id=e.id
                        WHERE s.visible_when_inactive=1
                          AND e.entity_type IN ('brands','categories')
                        ORDER BY e.entity_type,p.legacy_integer_id
                        """
                    ).fetchall()
                ]
                if inactive_binding_blockers:
                    raise MasterDataConflict(
                        "legacy binding parity failed: inactive projected values "
                        "would reject previously valid task bindings: "
                        + canonical_json(inactive_binding_blockers)
                    )
                unprojected = [
                    dict(row) for row in conn.execute(
                    """
                    SELECT e.id,e.entity_type,e.code,e.name
                    FROM master_entities e
                    LEFT JOIN legacy_projection p ON p.entity_id=e.id
                    WHERE e.entity_type IN ('brands','categories')
                      AND e.active=1 AND p.entity_id IS NULL
                    ORDER BY e.entity_type,e.normalized_code
                    """
                    ).fetchall()
                ]
                if unprojected:
                    raise MasterDataConflict(
                        "legacy projection response parity failed: "
                        + canonical_json({"unprojected_catalog_values": unprojected})
                    )
                parity_differences: dict[str, Any] = {}
                for entity_type in ("brands", "categories"):
                    expected = sorted(
                        [
                            {
                                "id": int(row["id"]),
                                "name": row["name"],
                                "created_at": row["created_at"],
                                "updated_at": row["updated_at"],
                            }
                            for row in live_legacy[entity_type]
                        ],
                        key=lambda row: (normalize_identity(row["name"]), row["id"]),
                    )
                    candidate = self._legacy_projection_rows(conn, entity_type)
                    if candidate != expected:
                        expected_by_id = {row["id"]: row for row in expected}
                        candidate_by_id = {row["id"]: row for row in candidate}
                        parity_differences[entity_type] = {
                            "missing_ids": sorted(set(expected_by_id) - set(candidate_by_id)),
                            "extra_ids": sorted(set(candidate_by_id) - set(expected_by_id)),
                            "changed": [
                                {
                                    "id": entity_id,
                                    "expected": expected_by_id[entity_id],
                                    "candidate": candidate_by_id[entity_id],
                                }
                                for entity_id in sorted(
                                    set(expected_by_id) & set(candidate_by_id)
                                )
                                if expected_by_id[entity_id]
                                != candidate_by_id[entity_id]
                            ],
                        }
                if parity_differences:
                    raise MasterDataConflict(
                        "legacy projection response parity failed: "
                        + canonical_json(parity_differences)
                    )
                projection_content_hash = (
                    self._legacy_projection_content_hash(conn)
                )
                conn.execute(
                    """
                    UPDATE catalog_consumer_scopes
                    SET status='active',reviewed_plan_sha256=?,
                        legacy_master_content_hash=?,activated_catalog_revision=?,
                        activated_catalog_content_hash=?,activated_at=?,
                        activated_by=?,projection_content_hash=?,updated_at=?
                    WHERE scope=?
                    """,
                    (
                        plan_sha, live_hash, int(meta["revision"]),
                        meta["content_hash"], now, actor,
                        projection_content_hash, now,
                        LEGACY_PROJECTION_SCOPE,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO catalog_scope_events(
                        id,scope,action,actor,plan_sha256,
                        legacy_master_content_hash,created_at
                    ) VALUES(?,?,'activate',?,?,?,?)
                    """,
                    (
                        uuid.uuid4().hex, LEGACY_PROJECTION_SCOPE, actor,
                        plan_sha, live_hash, now,
                    ),
                )
                conn.commit()
        return {
            "activated": True,
            "scope": LEGACY_PROJECTION_SCOPE,
            "plan_sha256": plan_sha,
            "legacy_master_content_hash": live_hash,
            "reconcile": reconcile,
            "integration_status": self.integration_status(),
        }

    def deactivate_legacy_projection(
        self, *, actor: str, expected_catalog_hash: str
    ) -> dict[str, Any]:
        """Rollback consumer routing without mutating either authority store."""
        actor = self._actor(actor)
        if (
            not self.legacy_db_path
            or not self.legacy_db_path.is_file()
            or self.legacy_db_path.is_symlink()
        ):
            raise MasterDataConflict("live jobs SQLite is required for rollback")
        now = utc_now()
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN EXCLUSIVE")
            live_conn = None
            try:
                self._verify_active_scope_seal(conn)
                self._check_catalog_hash(conn, expected_catalog_hash)
                scope = conn.execute(
                    "SELECT * FROM catalog_consumer_scopes WHERE scope=?",
                    (LEGACY_PROJECTION_SCOPE,),
                ).fetchone()
                if not scope or scope["status"] != "active":
                    raise MasterDataConflict(
                        "legacy projection is not active; rollback refused"
                    )
                meta = conn.execute(
                    "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
                ).fetchone()
                if scope and scope["status"] == "active" and (
                    int(meta["revision"]) != int(scope["activated_catalog_revision"])
                    or meta["content_hash"]
                    != scope["activated_catalog_content_hash"]
                ):
                    raise MasterDataConflict(
                        "legacy projection cannot be deactivated after post-activation catalog writes"
                    )
                live_conn = sqlite3.connect(
                    self.legacy_db_path, timeout=30
                )
                live_conn.row_factory = sqlite3.Row
                live_conn.execute("PRAGMA busy_timeout=30000")
                live_conn.execute("BEGIN IMMEDIATE")
                live_legacy = {
                    "brands": [
                        dict(row) for row in live_conn.execute(
                            "SELECT id,name,created_at,updated_at "
                            "FROM master_brands ORDER BY id"
                        )
                    ],
                    "categories": [
                        dict(row) for row in live_conn.execute(
                            "SELECT id,name,created_at,updated_at "
                            "FROM master_categories ORDER BY id"
                        )
                    ],
                }
                live_hash = self._legacy_master_content_hash(live_legacy)
                if (
                    scope and scope["status"] == "active"
                    and live_hash != scope["legacy_master_content_hash"]
                ):
                    raise MasterDataConflict(
                        "legacy projection cannot be deactivated after frozen legacy table drift"
                    )
                conn.execute(
                    """
                    UPDATE catalog_consumer_scopes
                    SET status='inactive',updated_at=? WHERE scope=?
                    """,
                    (now, LEGACY_PROJECTION_SCOPE),
                )
                conn.execute(
                    """
                    INSERT INTO catalog_scope_events(
                        id,scope,action,actor,plan_sha256,
                        legacy_master_content_hash,created_at
                    ) VALUES(?,?,'deactivate',?,NULL,NULL,?)
                    """,
                    (uuid.uuid4().hex, LEGACY_PROJECTION_SCOPE, actor, now),
                )
                conn.commit()
                live_conn.commit()
            except Exception:
                conn.rollback()
                if live_conn is not None:
                    live_conn.rollback()
                raise
            finally:
                if live_conn is not None:
                    live_conn.close()
        return {
            "activated": False,
            "scope": LEGACY_PROJECTION_SCOPE,
            "integration_status": self.integration_status(),
        }

    def legacy_list(self) -> dict[str, list[dict[str, Any]]]:
        if not self.legacy_projection_active():
            raise MasterDataConflict("legacy catalog consumer scope is inactive")
        result: dict[str, list[dict[str, Any]]] = {}
        with self._connect() as conn:
            conn.execute("BEGIN")
            for entity_type in ("brands", "categories"):
                result[entity_type] = self._legacy_projection_rows(
                    conn, entity_type
                )
        return result

    def legacy_create(self, kind: str, name: str, *, actor: str = "legacy-service") -> dict[str, Any]:
        entity_type = {"brand": "brands", "category": "categories"}.get(kind)
        if not entity_type:
            raise MasterDataError("master data kind must be brand or category")
        cleaned = clean_text(name)
        if kind == "category" and any("\u3400" <= char <= "\u9fff" for char in cleaned):
            raise MasterDataError("品类名称请使用英文")
        item = self.create(
            entity_type, {"code": self._next_available_code(entity_type, cleaned), "name": cleaned},
            actor=actor,
            _require_legacy_scope=True,
        )
        return self._legacy_row(item)

    def _next_available_code(self, entity_type: str, name: str) -> str:
        with self._connect() as conn:
            return self._unique_seed_code(conn, entity_type, name)

    def _legacy_row(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            projection = conn.execute(
                "SELECT legacy_integer_id FROM legacy_projection WHERE entity_id=?", (item["id"],)
            ).fetchone()
        if not projection:
            raise MasterDataError("legacy projection is unavailable")
        if not self.legacy_projection_active():
            raise MasterDataConflict("legacy catalog consumer scope is inactive")
        with self._connect() as conn:
            conn.execute("BEGIN")
            rows = self._legacy_projection_rows(conn, item["type"])
        return next(
            row for row in rows
            if row["id"] == int(projection["legacy_integer_id"])
        )

    def _item_for_legacy_id(self, entity_type: str, legacy_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                """
                SELECT p.entity_id FROM legacy_projection p
                JOIN master_entities e ON e.id=p.entity_id
                LEFT JOIN legacy_projection_compat_snapshot s
                  ON s.entity_id=p.entity_id
                WHERE p.entity_type=? AND p.legacy_integer_id=?
                  AND (e.active=1 OR COALESCE(s.visible_when_inactive,0)=1)
                """,
                (entity_type, int(legacy_id)),
            ).fetchone()
            return self._entity(conn, row["entity_id"]) if row else None

    def legacy_update(
        self, kind: str, legacy_id: int, name: str, *, actor: str = "legacy-service"
    ) -> dict[str, Any] | None:
        entity_type = {"brand": "brands", "category": "categories"}.get(kind)
        if not entity_type:
            raise MasterDataError("master data kind must be brand or category")
        item = self._item_for_legacy_id(entity_type, legacy_id)
        if not item:
            return None
        cleaned = clean_text(name)
        if kind == "category" and any("\u3400" <= char <= "\u9fff" for char in cleaned):
            raise MasterDataError("品类名称请使用英文")
        updated = self.update(
            entity_type, item["id"], {"name": cleaned}, actor=actor,
            expected_revision=item["revision"],
            _legacy_rebind=True,
            _require_legacy_scope=True,
        )
        return self._legacy_row(updated)

    def legacy_delete(
        self, kind: str, legacy_id: int, *, actor: str = "legacy-service"
    ) -> dict[str, Any] | None:
        actor = self._actor(actor)
        entity_type = {"brand": "brands", "category": "categories"}.get(kind)
        if not entity_type:
            raise MasterDataError("master data kind must be brand or category")
        if (
            not self.legacy_db_path
            or not self.legacy_db_path.is_file()
            or self.legacy_db_path.is_symlink()
        ):
            raise MasterDataConflict("live jobs SQLite is required for legacy delete")
        column = "brand" if entity_type == "brands" else "category"
        item: dict[str, Any] | None = None
        with self.lock, self._connect() as conn:
            # Cross-database lock order is always catalog -> jobs.  EXCLUSIVE
            # waits for task binding read snapshots before jobs is reserved,
            # preventing the catalog-read/jobs-write inverse deadlock.
            conn.execute("BEGIN EXCLUSIVE")
            legacy_guard = None
            try:
                if not self._legacy_projection_active_in(conn):
                    raise MasterDataConflict(
                        "legacy catalog consumer scope is inactive"
                    )
                self._verify_active_scope_seal(conn)
                projection = conn.execute(
                    """
                    SELECT p.entity_id FROM legacy_projection p
                    JOIN master_entities e ON e.id=p.entity_id
                    LEFT JOIN legacy_projection_compat_snapshot s
                      ON s.entity_id=p.entity_id
                    WHERE p.entity_type=? AND p.legacy_integer_id=?
                      AND (e.active=1 OR COALESCE(s.visible_when_inactive,0)=1)
                    """,
                    (entity_type, int(legacy_id)),
                ).fetchone()
                if not projection:
                    conn.rollback()
                    return None
                item = self._entity(conn, projection["entity_id"])
                legacy_guard = sqlite3.connect(
                    self.legacy_db_path, timeout=30
                )
                legacy_guard.execute("PRAGMA busy_timeout=30000")
                legacy_guard.execute("BEGIN IMMEDIATE")
                binding_names = sorted(
                    {item["name"], *item.get("aliases", [])},
                    key=normalize_identity,
                )
                placeholders = ",".join("?" for _name in binding_names)
                binding_count = legacy_guard.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE {column} COLLATE NOCASE "
                    f"IN ({placeholders})",
                    binding_names,
                ).fetchone()[0]
                if binding_count:
                    label = "品牌" if entity_type == "brands" else "品类"
                    raise MasterDataConflict(
                        f"无法删除“{item['name']}”：已有 {binding_count} 条采集任务绑定该{label}"
                    )
                now = utc_now()
                before = item
                normalized = self._normalize_payload(
                    conn, entity_type,
                    {"active": False, "replacement_id": None},
                    creating=False, existing=before,
                )
                after = self._write_import_existing(
                    conn, entity_type, item["id"], before, normalized,
                    actor, now,
                )
                conn.execute(
                    """
                    UPDATE legacy_projection_compat_snapshot
                    SET visible_when_inactive=0 WHERE entity_id=?
                    """,
                    (item["id"],),
                )
                self._refresh_legacy_projection_seal(conn)
                revision, _content_hash = self._advance_catalog(
                    conn, entity_type, item["id"], "disable", actor, now
                )
                self._audit(
                    conn, catalog_revision=revision, entity_type=entity_type,
                    entity_id=item["id"], entity_revision=after["revision"],
                    action="disable", actor=actor, before=before,
                    after=after, now=now,
                )
                conn.commit()
                legacy_guard.commit()
            except Exception:
                conn.rollback()
                if legacy_guard is not None:
                    legacy_guard.rollback()
                raise
            finally:
                if legacy_guard is not None:
                    legacy_guard.close()
        assert item is not None
        return {"id": int(legacy_id), "name": item["name"]}

    def audit(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if entity_type:
            self._require_type(entity_type)
        if page < 1 or page_size < 1 or page_size > 250:
            raise MasterDataError("page must be >= 1 and page_size must be 1-250")
        clauses = ["1=1"]
        params: list[Any] = []
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id=?")
            params.append(entity_id)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute(
                f"SELECT COUNT(*) AS value FROM master_audit_events WHERE {where}", params
            ).fetchone()["value"]
            rows = conn.execute(
                f"""
                SELECT * FROM master_audit_events WHERE {where}
                ORDER BY catalog_revision DESC,id DESC LIMIT ? OFFSET ?
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            meta = conn.execute(
                "SELECT revision,content_hash FROM catalog_meta WHERE id=1"
            ).fetchone()
        items = []
        for row in rows:
            item = dict(row)
            item["before"] = json.loads(item.pop("before_json")) if item["before_json"] else None
            item["after"] = json.loads(item.pop("after_json")) if item["after_json"] else None
            items.append(item)
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_revision": int(meta["revision"]),
            "catalog_content_hash": meta["content_hash"],
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": int(total),
        }

    def migration_versions(self) -> list[str]:
        with self._connect() as conn:
            return [
                row["version"] for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]

    def integrity(self) -> dict[str, Any]:
        with self._connect() as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
        return {"integrity_check": integrity, "foreign_key_issues": foreign_keys}
