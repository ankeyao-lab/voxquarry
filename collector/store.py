import base64
import binascii
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import unicodedata
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from .product_form import family_category, observation_form, persist_form, form_columns
from .category_vocabulary import (
    DEFAULT_CATEGORY_NAMES,
    LEGACY_CATEGORY_NAMES,
    MASTER_CATEGORY_NAMES,
    POWER_CLEANER_IDENTITY_RULE_VERSION,
    canonical_category_name,
    reclassify_expansion_category,
    reclassify_legacy_non_mowing,
)
from .product_identity import observation_hash, validate_task_energy
from .source_product_identity import (
    pending_source_product_identity,
    resolved_source_product_identity,
    source_product_identity_hash,
    validate_source_product_identity,
)

NEW_JOB_PRIORITY = -100
_CATALOG_UNSET = object()
RETRY_JOB_PRIORITY = 10
EXTERNAL_IMPORT_ACTION = "external_import"
# Import eligibility is independent of automatic worker eligibility. These
# existing channels can publish a governed browser fallback under the same task.
EXTERNAL_IMPORT_CHANNELS = {
    "homedepot", "amazon_live", "walmart", "ace", "tractorsupply",
    "harborfreight", "husqvarna", "stihl",
}
EXTERNAL_IMPORT_PROGRESS = "cached for external collection; awaiting validated import"
EXTERNAL_IMPORT_DECLARED_INCOMPLETE_STOP_REASON = (
    "external_import_declared_incomplete"
)
EXTERNAL_IMPORT_VALIDATION_SCHEMA_VERSION = (
    "voxquarry.external-review-import-validation/v1"
)
# Existing published partial imports used this name before ADR-0039.  It is
# accepted only while reading historic attempt artifacts; new staging bundles
# must use EXTERNAL_IMPORT_DECLARED_INCOMPLETE_STOP_REASON.
LEGACY_EXTERNAL_EXPORT_INCOMPLETE_STOP_REASON = "external_export_incomplete"
REPORTABLE_VALIDATION_STATUSES = {
    "COMPLETE",
    "COMPLETE_FALLBACK_SOURCE",
    "PARTIAL_SUCCESS",
}
EXTERNAL_IMPORT_ARTIFACTS = {
    "reviews.csv",
    "reviews.raw.jsonl",
    "validation_report.json",
    "selection_manifest.json",
}
JOBS_PAGE_SCHEMA_VERSION = "voxquarry.jobs-page/v1"
JOBS_PAGE_DEFAULT_LIMIT = 200
JOBS_PAGE_MAX_LIMIT = 250
JOBS_PAGE_CURSOR_PREFIX = "gtjp1_"


def _encode_jobs_cursor(created_at, job_id, scope_token):
    payload = json.dumps(
        {
            "created_at": str(created_at),
            "id": str(job_id),
            "scope_token": str(scope_token),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return JOBS_PAGE_CURSOR_PREFIX + encoded


def _decode_jobs_cursor(value):
    raw = str(value or "")
    if not raw.startswith(JOBS_PAGE_CURSOR_PREFIX):
        raise ValueError("invalid stable task page cursor")
    encoded = raw[len(JOBS_PAGE_CURSOR_PREFIX):]
    if not encoded:
        raise ValueError("invalid stable task page cursor")
    try:
        payload = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        )
        decoded = json.loads(payload.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid stable task page cursor") from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"created_at", "id", "scope_token"}
        or not all(
            isinstance(decoded.get(field), str) and decoded.get(field)
            for field in ("created_at", "id", "scope_token")
        )
    ):
        raise ValueError("invalid stable task page cursor")
    return decoded


class CancelRequestedError(RuntimeError):
    """Cooperative cancellation requested by the queue API."""


def canonical_target_url(value):
    """Return a stable product key; preserve STIHL's reviewed variant aID."""
    raw = unquote(str(value or "").strip())
    if not raw:
        return ""
    parsed = urlsplit(raw)
    host = parsed.netloc.casefold().removeprefix("www.")
    path = re_sub_duplicate_slashes(parsed.path).rstrip("/") or "/"
    fragment = ""
    if host == "stihlusa.com" or host.endswith(".stihlusa.com"):
        selected = re.findall(r"(?:^|[?&])aID=(\d{8,15})(?:&|$)", parsed.fragment)
        if len(set(selected)) == 1:
            fragment = f"aID={selected[0]}"
    return urlunsplit(("https", host, path, "", fragment))


def re_sub_duplicate_slashes(path):
    while "//" in path:
        path = path.replace("//", "/")
    return path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, initialize=True):
        self.data_dir = Path(os.getenv("DATA_DIR", "data"))
        self.db_path = self.data_dir / "jobs.sqlite3"
        self.lock = threading.Lock()
        self.shared_master_data = None
        self.task_master_data_required = os.getenv(
            'MASTER_DATA_TASK_BINDINGS_REQUIRED', ''
        ).strip().casefold() in {'1', 'true', 'yes', 'on'}
        from .remote_master_data import RemoteMasterData
        authority_url = os.getenv("VOXQUARRY_MASTER_DATA_URL", "").strip()
        self.remote_master_data = RemoteMasterData(authority_url) if authority_url else None
        catalog_path = self.data_dir / "master_data.sqlite3"
        catalog_required = os.getenv(
            "MASTER_DATA_CATALOG_REQUIRED", ""
        ).strip().casefold() in {"1", "true", "yes", "on"}
        legacy_writes_protected = os.getenv(
            "MASTER_DATA_PROTECT_LEGACY_WRITES", ""
        ).strip().casefold() in {"1", "true", "yes", "on"}
        validated_catalog = None
        active_projection = False
        if initialize and self.remote_master_data is not None:
            # The copied catalog is a point-in-time archive, never a second
            # writable authority. Do not initialize, migrate or attach it.
            self.remote_master_data.list()
        elif initialize and catalog_path.exists():
            if not catalog_path.is_file() or catalog_path.is_symlink():
                raise ValueError(
                    "shared master-data catalog must be a regular, non-symlink file"
                )
            # Validate an explicit catalog before _init_db can migrate jobs,
            # requeue work, or perform any other legacy startup write.
            from .master_data import (
                MasterDataStore,
                admin_writes_enabled_by_runtime,
                dev_mode_enabled_by_runtime,
                valid_admin_proxy_token_by_runtime,
                valid_production_trusted_actors,
            )
            validated_catalog = MasterDataStore(
                self.data_dir,
                legacy_db_path=self.db_path,
                initialize=False,
            )
            if validated_catalog.legacy_projection_active():
                active_projection = True
                if not catalog_required:
                    raise ValueError(
                        "MASTER_DATA_CATALOG_REQUIRED=true is required for "
                        "an active legacy master-data projection"
                    )
                if not legacy_writes_protected:
                    raise ValueError(
                        "MASTER_DATA_PROTECT_LEGACY_WRITES=true is required "
                        "for an active legacy master-data projection"
                    )
                if not admin_writes_enabled_by_runtime():
                    raise ValueError(
                        "MASTER_DATA_ADMIN_WRITES_ENABLED=true is required "
                        "for an active legacy master-data projection"
                    )
                if dev_mode_enabled_by_runtime():
                    raise ValueError(
                        "MASTER_DATA_DEV_MODE=false is required for an active "
                        "legacy master-data projection"
                    )
                if valid_admin_proxy_token_by_runtime() is None:
                    raise ValueError(
                        "MASTER_DATA_ADMIN_PROXY_TOKEN must contain at least "
                        "32 bytes for an active legacy master-data projection"
                    )
                if not valid_production_trusted_actors():
                    raise ValueError(
                        "MASTER_DATA_TRUSTED_ACTORS must include at least one "
                        "basic:<htpasswd-user> identity for an active legacy "
                        "master-data projection"
                    )
                if not self.db_path.is_file() or self.db_path.is_symlink():
                    raise ValueError(
                        "active legacy master-data projection requires an "
                        "existing regular, non-symlink jobs.sqlite3"
                    )
                integration = validated_catalog.integration_status()
                active_scope = next(
                    scope for scope in integration["consumer_scopes"]
                    if scope["scope"]
                    == "legacy_brand_category_projection"
                )
                live_legacy, _source_hash, live_master_hash = (
                    MasterDataStore._read_legacy_snapshot(
                        self.db_path, immutable=False
                    )
                )
                if live_master_hash != active_scope[
                    "legacy_master_content_hash"
                ]:
                    raise ValueError(
                        "active legacy master-data projection jobs SQLite "
                        "does not match its frozen legacy master hash"
                    )
                validated_catalog.verify_active_legacy_projection(
                    live_legacy,
                    legacy_content_hash=live_master_hash,
                )
        elif initialize and catalog_required:
            raise ValueError(
                "shared master-data catalog is required but a regular "
                "master_data.sqlite3 file is missing"
            )
        self.shared_master_data = validated_catalog
        if initialize:
            self._verify_task_master_data_startup()
            self.data_dir.mkdir(parents=True, exist_ok=True)
        elif not self.data_dir.is_dir():
            raise ValueError("existing data directory is required")
        if initialize:
            if active_projection:
                # The legacy schema/data migrations and master-data seeding
                # are permanently frozen after consumer activation.  Runtime
                # recovery may only transition interrupted task states.
                self._recover_interrupted_jobs()
                live_legacy, _source_hash, live_master_hash = (
                    MasterDataStore._read_legacy_snapshot(
                        self.db_path, immutable=False
                    )
                )
                validated_catalog.verify_active_legacy_projection(
                    live_legacy,
                    legacy_content_hash=live_master_hash,
                )
            elif self.task_master_data_required:
                # The explicit, audited migration freezes the legacy startup
                # backfills. Runtime startup may only recover interrupted work.
                self._recover_interrupted_jobs()
            else:
                self._init_db()
            self._init_research_schema()
            self._init_product_form_schema()
            # Runtime startup never creates or migrates the independent catalog.
            # Until the explicit init/import CLI has completed, legacy collection
            # master data remains fully available and v2 APIs report unavailable.
            self.shared_master_data = validated_catalog
        elif not self.db_path.is_file():
            raise ValueError("existing jobs SQLite is required")

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_research_schema(self):
        """Add only research receipts; never re-run frozen legacy backfills."""
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE IF NOT EXISTS research_collection_operations (
                request_key TEXT PRIMARY KEY, research_run_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL, job_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                subscribed INTEGER NOT NULL DEFAULT 1, disposition TEXT NOT NULL,
                created_at TEXT NOT NULL, detached_at TEXT)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS research_attempt_policies (
                job_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, policy_version TEXT NOT NULL,
                firecrawl_allowed INTEGER NOT NULL CHECK(firecrawl_allowed=0),
                PRIMARY KEY(job_id, attempt_no))''')

    def research_schema_ready(self):
        try:
            with self._connect() as conn:
                conn.execute('SELECT request_key,research_run_id,payload_hash,job_id,attempt_no,subscribed,disposition,created_at,detached_at FROM research_collection_operations LIMIT 0')
                conn.execute('SELECT job_id,attempt_no,policy_version,firecrawl_allowed FROM research_attempt_policies LIMIT 0')
            return True
        except sqlite3.Error:
            return False

    @contextmanager
    def _task_catalog_context(self):
        if not self.task_master_data_required:
            yield None
            return
        from .task_master_data import TaskCatalog, TaskCatalogUnavailable
        if self.remote_master_data is not None:
            try:
                catalog = TaskCatalog(self.remote_master_data.snapshot())
            except (ValueError, OSError) as exc:
                raise TaskCatalogUnavailable('共享主数据目录暂不可用，请稍后重试') from exc
            yield catalog
        elif self.shared_master_data is not None:
            with ExitStack() as stack:
                try:
                    snapshot = stack.enter_context(self.shared_master_data.task_snapshot_context())
                    catalog = TaskCatalog(snapshot)
                except (ValueError, OSError, sqlite3.Error) as exc:
                    raise TaskCatalogUnavailable('共享主数据目录快照无效，请检查后台') from exc
                yield catalog
        else:
            raise TaskCatalogUnavailable('共享主数据目录暂不可用；禁止回退旧目录')

    def task_metadata_catalog(self):
        """One immutable preview snapshot; task creation always reads again."""
        with self._task_catalog_context() as catalog:
            return catalog

    def validate_task_metadata(self, action, brand, *, product_observation=None,
                               voltage_platform=None, task_energy=None, catalog=_CATALOG_UNSET):
        """Read-only admission check; create rechecks under its catalog lock."""
        platform = voltage_platform or {}
        energy = task_energy or {}
        context = self._task_catalog_context() if catalog is _CATALOG_UNSET else nullcontext(catalog)
        with context as catalog:
            if catalog is None:
                return None
            return catalog.bind({
                'action': action, 'brand': brand,
                'energy_type': energy.get('energy_type'),
                'energy_readiness_status': energy.get('readiness_status'),
                'voltage_platform_status': platform.get('status'),
                'voltage_platform_key': platform.get('platform_key'),
                'voltage_platform_brand': platform.get('brand'),
                'voltage_platform_voltage': platform.get('normalized_voltage'),
            })

    def _verify_task_master_data_startup(self):
        from .task_master_data import BINDING_COLUMNS
        if not self.db_path.is_file() or self.db_path.is_symlink():
            if self.task_master_data_required:
                raise ValueError('explicit task master-data migration required')
            return
        conn = sqlite3.connect(f'{self.db_path.resolve().as_uri()}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            state_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_master_data_binding_state'").fetchone()
            required = state_exists and conn.execute('SELECT required FROM task_master_data_binding_state WHERE id=1').fetchone()
            if required and required[0] and not self.task_master_data_required:
                raise ValueError('MASTER_DATA_TASK_BINDINGS_REQUIRED=true is required after task binding activation')
            if self.task_master_data_required:
                columns = {r[1] for r in conn.execute('PRAGMA table_info(jobs)')}
                if not set(BINDING_COLUMNS).issubset(columns) or not required or not required[0]:
                    raise ValueError('explicit task master-data migration and activation required')
                with self._task_catalog_context() as catalog:
                    for row in conn.execute('SELECT * FROM jobs'):
                        catalog.verify_existing(dict(row))
        finally:
            conn.close()

    def _init_product_form_schema(self):
        # Additive nullable observation fields only; no history backfill here.
        with self._connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            for name in form_columns(observation_form("")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")

    @staticmethod
    def _bind_task(conn, job_id, catalog, previous=None):
        if catalog is not None:
            from .task_master_data import write_binding
            return write_binding(conn, job_id, catalog, previous=previous)

    def task_master_data_status(self):
        with self._task_catalog_context() as catalog, self._connect() as conn:
            conn.execute('BEGIN')
            rows = conn.execute('SELECT * FROM jobs').fetchall()
            if catalog is not None:
                for row in rows:
                    catalog.verify_existing(dict(row))
            return {
                'schema_version': 'voxquarry.task-master-data-status/v1',
                'active': catalog is not None,
                'authority': 'production_remote' if self.remote_master_data else 'production_catalog',
                'task_count': len(rows),
                'energy_bindings': sum(bool(dict(row).get('energy_type_id')) for row in rows),
                'voltage_platform_bindings': sum(bool(dict(row).get('voltage_platform_id')) for row in rows),
                'catalog_revision': catalog.revision if catalog else None,
                'catalog_content_hash': catalog.content_hash if catalog else None,
            }

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, action TEXT NOT NULL, channel TEXT NOT NULL,
                status TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '',
                result_json TEXT, error TEXT, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY, author TEXT NOT NULL, tag TEXT NOT NULL,
                content TEXT NOT NULL, likes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS master_brands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS master_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                result_json TEXT,
                error TEXT,
                source_total_initial INTEGER,
                source_total_min INTEGER,
                source_total_max INTEGER,
                source_total_final INTEGER,
                drift_tolerance INTEGER,
                snapshot_fingerprint TEXT,
                legacy_job_id TEXT,
                UNIQUE(job_id, attempt_no)
            );
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                attempt_no INTEGER,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                detail_json TEXT
            );
            CREATE TABLE IF NOT EXISTS channel_cooldowns (
                channel TEXT PRIMARY KEY,
                blocked_until TEXT NOT NULL,
                failure_class TEXT NOT NULL,
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_collection_operations (
                request_key TEXT PRIMARY KEY,
                research_run_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                job_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                subscribed INTEGER NOT NULL DEFAULT 1,
                disposition TEXT NOT NULL,
                created_at TEXT NOT NULL,
                detached_at TEXT
            );
            CREATE TABLE IF NOT EXISTS research_attempt_policies (
                job_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                policy_version TEXT NOT NULL,
                firecrawl_allowed INTEGER NOT NULL CHECK(firecrawl_allowed=0),
                PRIMARY KEY(job_id, attempt_no)
            );
            """)
            for column, declaration in {
                "target_url": "TEXT",
                "target_key": "TEXT",
                "brand": "TEXT",
                "model": "TEXT",
                "source_product_id": "TEXT",
                "source_product_identity_status": "TEXT",
                "source_product_identity_json": "TEXT",
                "source_product_identity_hash": "TEXT",
                "source_product_identity_evidence_source": "TEXT",
                "category": "TEXT",
                "market_region": "TEXT NOT NULL DEFAULT 'Other'",
                "priority": "INTEGER NOT NULL DEFAULT 0",
                "retry_of": "TEXT",
                "superseded_by": "TEXT",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "active_attempt_no": "INTEGER",
                "active_result_status": "TEXT",
                "active_run_path": "TEXT",
                "last_attempt_status": "TEXT",
                "acquisition_batch_id": "TEXT",
                "product_master_schema_version": "TEXT",
                "product_master_snapshot_id": "TEXT",
                "product_master_model_id": "TEXT",
                "product_master_entity_version": "INTEGER",
                "product_master_record_snapshot_hash": "TEXT",
                "product_master_record_json": "TEXT",
                "product_observation_json": "TEXT",
                "product_observation_hash": "TEXT",
                "product_enrichment_status": "TEXT",
                "product_quality_flags_json": "TEXT",
                "voltage_platform_schema_version": "TEXT",
                "voltage_platform_status": "TEXT",
                "voltage_platform_key": "TEXT",
                "voltage_platform_brand": "TEXT",
                "voltage_platform_voltage": "TEXT",
                "voltage_platform_label": "TEXT",
                "voltage_platform_evidence_source": "TEXT",
                "voltage_platform_null_reason": "TEXT",
                "energy_schema_version": "TEXT",
                "energy_type": "TEXT",
                "energy_readiness_status": "TEXT",
                "energy_evidence_source": "TEXT",
                "energy_canonical_voltage": "TEXT",
                "energy_unknown_reason": "TEXT",
            }.items():
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute(
                    "ALTER TABLE job_attempts ADD COLUMN acquisition_batch_id TEXT"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_channel_queue ON jobs(channel, status, priority DESC, created_at ASC)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_created_id "
                "ON jobs(created_at DESC, id DESC)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_target_key ON jobs(target_key, status, finished_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_job_attempts_job ON job_attempts(job_id, attempt_no DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id DESC)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_acquisition_batch "
                "ON jobs(acquisition_batch_id, status)"
            )
            rows = conn.execute(
                "SELECT id, target_url FROM jobs WHERE COALESCE(target_key, '')=''"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE jobs SET target_key=? WHERE id=?",
                    (canonical_target_url(row["target_url"]), row["id"]),
                )
            self._migrate_category_names(conn)
            if self.remote_master_data is None:
                self._seed_master_data(conn)
            self._backfill_attempts(conn)
            self._merge_duplicate_targets(conn)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_channel_target_unique
                ON jobs(channel, target_key)
                WHERE action='scrape' AND COALESCE(target_key, '')<>''
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_channel_target_external_unique
                ON jobs(channel, target_key)
                WHERE action IN ('scrape','external_import')
                  AND COALESCE(target_key, '')<>''
                """
            )
            self._recover_interrupted_jobs_in(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS job_scope_revision (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    revision INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO job_scope_revision(id, revision)
                VALUES(1, 0);
                CREATE TRIGGER IF NOT EXISTS trg_jobs_scope_insert
                AFTER INSERT ON jobs
                BEGIN
                    UPDATE job_scope_revision
                    SET revision=revision+1 WHERE id=1;
                END;
                CREATE TRIGGER IF NOT EXISTS trg_jobs_scope_delete
                AFTER DELETE ON jobs
                BEGIN
                    UPDATE job_scope_revision
                    SET revision=revision+1 WHERE id=1;
                END;
                CREATE TRIGGER IF NOT EXISTS trg_jobs_scope_update
                AFTER UPDATE OF
                    action,channel,status,result_json,error,started_at,finished_at,
                    target_url,target_key,brand,model,source_product_id,category,
                    source_product_identity_status,source_product_identity_json,
                    source_product_identity_hash,source_product_identity_evidence_source,
                    market_region,priority,superseded_by,cancel_requested,
                    attempt_count,last_attempt_status,acquisition_batch_id,
                    active_attempt_no,active_result_status,active_run_path,
                    product_master_model_id,product_observation_json,
                    product_observation_hash,product_enrichment_status,
                    product_quality_flags_json,voltage_platform_schema_version,
                    voltage_platform_status,voltage_platform_key,
                    voltage_platform_brand,voltage_platform_voltage,
                    voltage_platform_label,voltage_platform_evidence_source,
                    voltage_platform_null_reason,energy_schema_version,
                    energy_type,energy_readiness_status,energy_evidence_source,
                    energy_canonical_voltage,energy_unknown_reason
                ON jobs
                BEGIN
                    UPDATE job_scope_revision
                    SET revision=revision+1 WHERE id=1;
                END;
                """
            )

    @staticmethod
    def _recover_interrupted_jobs_in(conn):
        # A container restart interrupts in-memory collection but does not
        # invalidate the task or its target. Requeue the same stable task ID.
        conn.execute(
            """
            UPDATE jobs
            SET status='queued', progress='requeued after service restart',
                error=NULL, started_at=NULL, finished_at=NULL
            WHERE status='running' AND cancel_requested=0
            """
        )
        conn.execute(
            """
            UPDATE jobs
            SET status='cancelled', progress='cancelled after service restart',
                finished_at=?
            WHERE status IN ('running', 'cancelling') AND cancel_requested=1
            """,
            (utc_now(),),
        )

    def _recover_interrupted_jobs(self):
        with self._connect() as conn:
            self._recover_interrupted_jobs_in(conn)

    @staticmethod
    def _master_table(kind):
        tables = {"brand": "master_brands", "category": "master_categories"}
        if kind not in tables:
            raise ValueError("master data kind must be brand or category")
        return tables[kind]

    @staticmethod
    def _clean_master_name(name):
        return " ".join(str(name or "").split()).strip()

    @staticmethod
    def _canonical_master_name(kind, name):
        cleaned = Store._clean_master_name(name)
        return canonical_category_name(cleaned) if kind == "category" else cleaned

    @staticmethod
    def _validate_master_name(kind, name):
        cleaned = Store._canonical_master_name(kind, name)
        if kind == "category" and any("\u3400" <= char <= "\u9fff" for char in cleaned):
            raise ValueError("品类名称请使用英文")
        return cleaned

    def _migrate_category_names(self, conn):
        """Atomically replace legacy Chinese category labels with English labels."""
        timestamp = utc_now()
        legacy_rows = conn.execute(
            """
            SELECT id,target_url,model
            FROM jobs
            WHERE category='Non-Mowing Equipment' COLLATE NOCASE
            """
        ).fetchall()
        for row in legacy_rows:
            conn.execute(
                "UPDATE jobs SET category=? WHERE id=?",
                (
                    reclassify_legacy_non_mowing(
                        row["target_url"],
                        row["model"],
                    ),
                    row["id"],
                ),
            )
        expansion_rows = conn.execute(
            """
            SELECT id,category,target_url,model,active_attempt_no
            FROM jobs
            WHERE category IN (
                'Unknown Category','Pump','Fan','Patio Cleaner','Pressure Washer'
            )
            """
        ).fetchall()
        for row in expansion_rows:
            category = reclassify_expansion_category(
                row["category"],
                row["target_url"],
                row["model"],
            )
            if category != row["category"]:
                conn.execute(
                    "UPDATE jobs SET category=? WHERE id=?",
                    (category, row["id"]),
                )
                if category == "Power Cleaner" and row["category"] == "Pressure Washer":
                    self._append_event(
                        conn,
                        row["id"],
                        row["active_attempt_no"],
                        "info",
                        "category_reclassified_power_cleaner",
                        "Task category reclassified from Pressure Washer to Power Cleaner",
                        {
                            "from_category": "Pressure Washer",
                            "to_category": "Power Cleaner",
                            "identity_rule_version": POWER_CLEANER_IDENTITY_RULE_VERSION,
                            "evidence_scope": "task product URL and model; review text excluded",
                        },
                    )
        for legacy, english in LEGACY_CATEGORY_NAMES.items():
            conn.execute(
                "UPDATE jobs SET category=? WHERE category=? COLLATE NOCASE",
                (english, legacy),
            )
            legacy_row = conn.execute(
                "SELECT id FROM master_categories WHERE name=? COLLATE NOCASE",
                (legacy,),
            ).fetchone()
            if not legacy_row:
                continue
            english_row = conn.execute(
                "SELECT id FROM master_categories WHERE name=? COLLATE NOCASE",
                (english,),
            ).fetchone()
            if english_row:
                conn.execute(
                    "DELETE FROM master_categories WHERE id=?",
                    (legacy_row["id"],),
                )
            else:
                conn.execute(
                    "UPDATE master_categories SET name=?, updated_at=? WHERE id=?",
                    (english, timestamp, legacy_row["id"]),
                )
        # The supplied OPE taxonomy is the initial authoritative master list.
        # Legacy labels which no longer bind a task are removed, while future
        # administration remains available through the normal CRUD API.
        placeholders = ",".join("?" for _ in MASTER_CATEGORY_NAMES)
        conn.execute(
            f"""
            DELETE FROM master_categories
            WHERE name NOT IN ({placeholders}) COLLATE NOCASE
              AND NOT EXISTS (
                SELECT 1 FROM jobs
                WHERE jobs.category=master_categories.name COLLATE NOCASE
              )
            """,
            tuple(MASTER_CATEGORY_NAMES),
        )

    def _seed_master_data(self, conn):
        """Keep legacy task metadata selectable after master-data migration."""
        defaults = {
            "brand": (
                "EGO", "RYOBI", "Milwaukee", "DEWALT", "Einhell", "Kobalt",
                "Greenworks", "Toro", "Craftsman", "WORX", "STIHL",
            ),
            "category": DEFAULT_CATEGORY_NAMES,
        }
        timestamp = utc_now()
        for kind, names in defaults.items():
            table = self._master_table(kind)
            column = "brand" if kind == "brand" else "category"
            existing = [row[0] for row in conn.execute(
                f"SELECT DISTINCT TRIM({column}) FROM jobs WHERE TRIM(COALESCE({column}, '')) <> ''"
            ).fetchall()]
            for name in (*names, *existing):
                cleaned = self._canonical_master_name(kind, name)
                if cleaned:
                    conn.execute(
                        f"INSERT OR IGNORE INTO {table}(name, created_at, updated_at) VALUES(?,?,?)",
                        (cleaned, timestamp, timestamp),
                    )

    def _append_event(self, conn, job_id, attempt_no, level, code, message, detail=None):
        conn.execute(
            """
            INSERT INTO job_events(
                job_id,attempt_no,created_at,level,code,message,detail_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                job_id,
                attempt_no,
                utc_now(),
                level,
                code,
                str(message),
                json.dumps(detail, ensure_ascii=False) if detail else None,
            ),
        )

    def _backfill_attempts(self, conn):
        """Represent every historic job row as its first immutable attempt."""
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at ASC, id ASC").fetchall()
        for row in rows:
            existing = conn.execute(
                "SELECT COUNT(*) AS value FROM job_attempts WHERE job_id=?",
                (row["id"],),
            ).fetchone()["value"]
            if existing:
                continue
            status = str(row["status"] or "failed")
            result_json = row["result_json"]
            active_status = (
                status if status in {"completed", "partial_success"} and result_json else None
            )
            conn.execute(
                """
                INSERT INTO job_attempts(
                    job_id,attempt_no,status,priority,created_at,started_at,
                    finished_at,result_json,error,legacy_job_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["id"],
                    1,
                    status,
                    int(row["priority"] or 0),
                    row["created_at"] or utc_now(),
                    row["started_at"],
                    row["finished_at"],
                    result_json,
                    row["error"],
                    row["id"],
                ),
            )
            active_path = (
                f"{row['id']}/{row['channel']}" if active_status else None
            )
            conn.execute(
                """
                UPDATE jobs
                SET attempt_count=1, active_attempt_no=?, active_result_status=?,
                    active_run_path=?, last_attempt_status=?
                WHERE id=?
                """,
                (1 if active_status else None, active_status, active_path, status, row["id"]),
            )
            self._append_event(
                conn,
                row["id"],
                1,
                "info",
                "legacy_attempt_imported",
                "Imported historic task state as attempt 1",
                {"legacy_job_id": row["id"], "status": status},
            )

    @staticmethod
    def _target_rank(row):
        status = str(row["status"] or "")
        usable = 0 if status == "completed" else 1 if status == "partial_success" else 2
        superseded = 1 if row["superseded_by"] else 0
        terminal = 0 if usable < 2 else 1
        return (terminal, usable, superseded, str(row["created_at"] or ""), str(row["id"]))

    def _archive_legacy_run(self, source_job_id, target_job_id, attempt_no, channel):
        source = self.data_dir / "runs" / source_job_id / channel
        target = self.data_dir / "runs" / target_job_id / "attempts" / str(attempt_no) / channel
        if not source.is_dir() or target.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return True

    def _merge_duplicate_targets(self, conn):
        """Collapse historical URL duplicates into one stable user-visible job."""
        groups = conn.execute(
            """
            SELECT channel,target_key
            FROM jobs
            WHERE action='scrape' AND COALESCE(target_key, '')<>''
            GROUP BY channel,target_key
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for group in groups:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE channel=? AND target_key=? ORDER BY created_at ASC, id ASC",
                (group["channel"], group["target_key"]),
            ).fetchall()
            canonical = sorted(rows, key=self._target_rank)[0]
            next_attempt = int(canonical["attempt_count"] or 0)
            for source in rows:
                if source["id"] == canonical["id"]:
                    continue
                attempts = conn.execute(
                    "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_no ASC, id ASC",
                    (source["id"],),
                ).fetchall()
                for attempt in attempts:
                    next_attempt += 1
                    archived = self._archive_legacy_run(
                        source["id"], canonical["id"], next_attempt, source["channel"]
                    )
                    conn.execute(
                        """
                        UPDATE job_attempts
                        SET job_id=?, attempt_no=?, legacy_job_id=?
                        WHERE id=?
                        """,
                        (canonical["id"], next_attempt, source["id"], attempt["id"]),
                    )
                    self._append_event(
                        conn,
                        canonical["id"],
                        next_attempt,
                        "info",
                        "duplicate_task_merged",
                        f"Merged historical task {source['id']} into stable task {canonical['id']}",
                        {"legacy_job_id": source["id"], "run_archived": archived},
                    )
                conn.execute("UPDATE job_events SET job_id=? WHERE job_id=?", (canonical["id"], source["id"]))
                conn.execute("DELETE FROM jobs WHERE id=?", (source["id"],))
            canonical_attempt = conn.execute(
                """
                SELECT attempt_no FROM job_attempts
                WHERE job_id=? AND status IN ('completed','partial_success')
                ORDER BY CASE status WHEN 'completed' THEN 0 ELSE 1 END,
                         COALESCE(finished_at, created_at) DESC, attempt_no DESC
                LIMIT 1
                """,
                (canonical["id"],),
            ).fetchone()
            active_attempt_no = canonical_attempt["attempt_no"] if canonical_attempt else None
            active_status = None
            active_path = None
            if canonical_attempt:
                attempt = conn.execute(
                    "SELECT status FROM job_attempts WHERE job_id=? AND attempt_no=?",
                    (canonical["id"], active_attempt_no),
                ).fetchone()
                active_status = attempt["status"]
                active_path = f"{canonical['id']}/attempts/{active_attempt_no}/{canonical['channel']}"
                root_path = self.data_dir / "runs" / canonical["id"] / canonical["channel"]
                if active_attempt_no == 1 and root_path.is_dir():
                    active_path = f"{canonical['id']}/{canonical['channel']}"
            conn.execute(
                """
                UPDATE jobs
                SET attempt_count=?, active_attempt_no=?, active_result_status=?,
                    active_run_path=?, superseded_by=NULL
                WHERE id=?
                """,
                (next_attempt, active_attempt_no, active_status, active_path, canonical["id"]),
            )

    def list_master_data(self):
        if self.remote_master_data is not None:
            return self.remote_master_data.list()
        if self.legacy_master_data_activated():
            return self.shared_master_data.legacy_list()
        with self._connect() as conn:
            brands = [dict(row) for row in conn.execute("SELECT * FROM master_brands ORDER BY name COLLATE NOCASE").fetchall()]
            categories = [dict(row) for row in conn.execute("SELECT * FROM master_categories ORDER BY name COLLATE NOCASE").fetchall()]
        return {"brands": brands, "categories": categories}

    def canonical_master_value(self, kind, name):
        cleaned = self._canonical_master_name(kind, name)
        if not cleaned:
            return ""
        if self.remote_master_data is not None:
            return self.remote_master_data.canonical(kind, cleaned)
        if self.legacy_master_data_activated():
            entity_type = {"brand": "brands", "category": "categories"}.get(kind)
            if not entity_type:
                raise ValueError("master data kind must be brand or category")
            try:
                resolved = self.shared_master_data.resolve(
                    entity_type, cleaned
                )
                return resolved["record"]["name"]
            except ValueError:
                label = "品牌" if kind == "brand" else "品类"
                raise ValueError(
                    f"{label}“{cleaned}”未在主数据中维护，请先在主数据管理中添加或选择已有项"
                )
        table = self._master_table(kind)
        with self._connect() as conn:
            return self._canonical_master_value_in_conn(
                conn, kind, cleaned
            )

    def _canonical_master_value_in_conn(self, conn, kind, name):
        cleaned = self._canonical_master_name(kind, name)
        if not cleaned:
            return ""
        if self.remote_master_data is not None:
            return self.remote_master_data.canonical(kind, cleaned)
        table = self._master_table(kind)
        row = conn.execute(
            f"SELECT name FROM {table} WHERE name=? COLLATE NOCASE",
            (cleaned,),
        ).fetchone()
        if not row:
            label = "品牌" if kind == "brand" else "品类"
            raise ValueError(f"{label}“{cleaned}”未在主数据中维护，请先在主数据管理中添加或选择已有项")
        return row["name"]

    def _active_binding_context(self, brand, category):
        if self.shared_master_data is not None:
            return self.shared_master_data.legacy_binding_snapshot(
                brand=brand, category=category
            )
        return nullcontext(None)

    def _binding_resolver_context(self):
        if self.shared_master_data is not None:
            return self.shared_master_data.legacy_binding_resolver()
        return nullcontext(lambda _brand, _category: None)

    def _legacy_scope_context(self):
        if self.remote_master_data is not None:
            raise ValueError("本地不维护第二套主数据，请通过生产共享后台修改")
        if self.shared_master_data is not None:
            return self.shared_master_data.legacy_scope_snapshot()
        return nullcontext(False)

    def create_master_data(self, kind, name, actor="legacy-service"):
        with self._legacy_scope_context() as active:
            if not active:
                return self._create_legacy_master_data(kind, name)
        return self.shared_master_data.legacy_create(kind, name, actor=actor)

    def _create_legacy_master_data(self, kind, name):
        cleaned = self._validate_master_name(kind, name)
        if not cleaned:
            raise ValueError("名称不能为空")
        table = self._master_table(kind)
        timestamp = utc_now()
        try:
            with self.lock, self._connect() as conn:
                conn.execute(f"INSERT INTO {table}(name, created_at, updated_at) VALUES(?,?,?)", (cleaned, timestamp, timestamp))
                row = conn.execute(f"SELECT * FROM {table} WHERE name=? COLLATE NOCASE", (cleaned,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("该名称已存在") from exc
        return dict(row)

    def update_master_data(self, kind, item_id, name, actor="legacy-service"):
        with self._legacy_scope_context() as active:
            if not active:
                return self._update_legacy_master_data(kind, item_id, name)
        return self.shared_master_data.legacy_update(
            kind, item_id, name, actor=actor
        )

    def _update_legacy_master_data(self, kind, item_id, name):
        cleaned = self._validate_master_name(kind, name)
        if not cleaned:
            raise ValueError("名称不能为空")
        table = self._master_table(kind)
        column = "brand" if kind == "brand" else "category"
        try:
            with self.lock, self._connect() as conn:
                row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
                if not row:
                    return None
                conn.execute(f"UPDATE {table} SET name=?, updated_at=? WHERE id=?", (cleaned, utc_now(), item_id))
                # A rename is a master-data correction, so retain bindings by
                # updating all historical and queued task values atomically.
                conn.execute(f"UPDATE jobs SET {column}=? WHERE {column}=? COLLATE NOCASE", (cleaned, row["name"]))
                updated = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("该名称已存在") from exc
        return dict(updated)

    def delete_master_data(self, kind, item_id, actor="legacy-service"):
        with self._legacy_scope_context() as active:
            if not active:
                return self._delete_legacy_master_data(kind, item_id)
        return self.shared_master_data.legacy_delete(
            kind, item_id, actor=actor
        )

    def _delete_legacy_master_data(self, kind, item_id):
        table = self._master_table(kind)
        column = "brand" if kind == "brand" else "category"
        with self.lock, self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
            if not row:
                return None
            binding_count = conn.execute(
                f"SELECT COUNT(*) AS value FROM jobs WHERE {column}=? COLLATE NOCASE", (row["name"],)
            ).fetchone()["value"]
            if binding_count:
                label = "品牌" if kind == "brand" else "品类"
                raise ValueError(f"无法删除“{row['name']}”：已有 {binding_count} 条采集任务绑定该{label}")
            conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        return {"id": item_id, "name": row["name"]}

    def legacy_master_data_activated(self):
        return (
            self.shared_master_data is not None
            and self.shared_master_data.legacy_projection_active()
        )

    def create_job(
        self,
        job_id,
        action,
        channel,
        target_url=None,
        brand=None,
        model=None,
        category=None,
        market_region="Other",
        priority=NEW_JOB_PRIORITY,
        retry_of=None,
        source_product_id=None,
        source_product_identity=None,
        acquisition_batch_id=None,
        product_master_binding=None,
        product_observation=None,
        product_enrichment_status=None,
        product_quality_flags=None,
        voltage_platform=None,
        task_energy=None,
        research_operation=None,
    ):
        binding = dict(product_master_binding or {})
        record = binding.get("record") if isinstance(binding.get("record"), dict) else None
        observation = dict(product_observation or {})
        form = observation_form(category, observation)
        category = family_category(category)
        platform = dict(voltage_platform or {})
        quality_flags = list(product_quality_flags or [])
        energy = validate_task_energy(task_energy) if task_energy else None
        if source_product_identity is None:
            source_product_identity = (
                resolved_source_product_identity(
                    source_product_id,
                    "governed_internal_import",
                    "prevalidated_internal_source_product_id",
                )
                if source_product_id
                else pending_source_product_identity()
            )
        source_product_identity = validate_source_product_identity(
            source_product_identity
        )
        source_product_id = source_product_identity.get("source_product_id")
        binding_context = self._active_binding_context(brand, category)
        with self.lock, binding_context as resolved, self._task_catalog_context() as task_catalog, self._connect() as conn:
            if research_operation is not None:
                conn.execute("BEGIN IMMEDIATE")
                replay = self._research_replay(conn, research_operation)
                if replay:
                    return replay
                existing = conn.execute(
                    "SELECT * FROM jobs WHERE channel=? AND target_key=?",
                    (channel, canonical_target_url(target_url)),
                ).fetchone()
                if existing:
                    # queue_retry performs its own atomic recheck. No writes
                    # have happened in this transaction.
                    return {"research_retry_job_id": existing["id"]}
                if research_operation.get("retry"):
                    raise ValueError("research retry target does not exist")
            if resolved is not None:
                brand = resolved["brand"]
                category = resolved["category"]
            target_key = canonical_target_url(target_url)
            is_external_cache = action == EXTERNAL_IMPORT_ACTION
            progress = (
                EXTERNAL_IMPORT_PROGRESS
                if is_external_cache
                else "metadata_incomplete: energy metadata required before collection"
                if energy and energy["readiness_status"] == "metadata_incomplete"
                else "queued"
            )
            columns = (
                "id", "action", "channel", "status", "progress", "result_json",
                "error", "created_at", "started_at", "finished_at", "target_url",
                "target_key", "brand", "model", "source_product_id", "category",
                "source_product_identity_status", "source_product_identity_json",
                "source_product_identity_hash",
                "source_product_identity_evidence_source",
                "market_region", "priority", "retry_of", "superseded_by",
                "cancel_requested", "attempt_count", "last_attempt_status",
                "acquisition_batch_id", "product_master_schema_version",
                "product_master_snapshot_id", "product_master_model_id",
                "product_master_entity_version", "product_master_record_snapshot_hash",
                "product_master_record_json", "product_observation_json",
                "product_observation_hash",
                "product_enrichment_status", "product_quality_flags_json",
                "voltage_platform_schema_version", "voltage_platform_status",
                "voltage_platform_key", "voltage_platform_brand",
                "voltage_platform_voltage", "voltage_platform_label",
                "voltage_platform_evidence_source", "voltage_platform_null_reason",
                "energy_schema_version", "energy_type",
                "energy_readiness_status", "energy_evidence_source",
                "energy_canonical_voltage", "energy_unknown_reason",
            )
            values = (
                    job_id, action, channel, "queued", progress, None, None,
                    utc_now(), None, None, target_url, target_key, brand, model,
                    source_product_id, category,
                    source_product_identity["status"],
                    json.dumps(
                        source_product_identity,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    source_product_identity_hash(source_product_identity),
                    source_product_identity["evidence_source"],
                    market_region, int(priority),
                    retry_of, None, 0, 1, "queued", acquisition_batch_id,
                    binding.get("schema_version"), binding.get("snapshot_id"),
                    binding.get("model_id"), binding.get("entity_version"),
                    binding.get("record_snapshot_hash"),
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) if record else None,
                    json.dumps(observation, ensure_ascii=False, separators=(",", ":")) if observation else None,
                    observation_hash(observation) if observation else None,
                    product_enrichment_status or ("enriched" if binding else "unknown"),
                    json.dumps(quality_flags, ensure_ascii=False, separators=(",", ":")),
                    platform.get("schema_version"), platform.get("status"),
                    platform.get("platform_key"), platform.get("brand"),
                    platform.get("normalized_voltage"), platform.get("display_name"),
                    platform.get("evidence_source"), platform.get("null_reason"),
                    energy.get("schema_version") if energy else None,
                    energy.get("energy_type") if energy else None,
                    energy.get("readiness_status") if energy else None,
                    energy.get("evidence_source") if energy else None,
                    energy.get("canonical_voltage") if energy else None,
                    energy.get("unknown_reason") if energy else None,
            )
            conn.execute(
                f"INSERT INTO jobs ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _column in columns)})",
                values,
            )
            persist_form(conn, job_id, form)
            self._bind_task(conn, job_id, task_catalog)
            conn.execute(
                """
                INSERT INTO job_attempts(
                    job_id,attempt_no,status,priority,created_at,legacy_job_id,
                    acquisition_batch_id
                ) VALUES(?,?,?,?,?,NULL,?)
                """,
                (
                    job_id,
                    1,
                    "queued",
                    int(priority),
                    utc_now(),
                    acquisition_batch_id,
                ),
            )
            self._append_event(
                conn,
                job_id,
                1,
                "info",
                "external_import_cached" if is_external_cache else "task_created",
                (
                    "Task cached for external collection; server execution is disabled"
                    if is_external_cache
                    else "Collection task queued"
                ),
                {"execution_policy": "external_import_only"}
                if is_external_cache
                else None,
            )
            if research_operation is not None:
                return self._research_bind(
                    conn, research_operation, job_id, 1, "created", new_attempt=True
                )

    def _research_replay(self, conn, operation):
        row = conn.execute(
            "SELECT * FROM research_collection_operations WHERE request_key=?",
            (operation["request_key"],),
        ).fetchone()
        if row and (row["payload_hash"] != operation["payload_hash"]
                    or row["research_run_id"] != operation["research_run_id"]):
            raise ValueError("research request key already bound to another request")
        return dict(row) if row else None

    def _research_bind(self, conn, operation, job_id, attempt_no, disposition,
                       *, new_attempt=False):
        if int(attempt_no or 0) < 1:
            raise ValueError("legacy task has no verifiable attempt; cannot subscribe")
        attempt = conn.execute(
            "SELECT 1 FROM job_attempts WHERE job_id=? AND attempt_no=?",
            (job_id, attempt_no),
        ).fetchone()
        if not attempt:
            raise ValueError("task attempt is missing; cannot subscribe")
        if new_attempt:
            conn.execute(
                "INSERT INTO research_attempt_policies VALUES(?,?,?,0)",
                (job_id, attempt_no, "voxquarry.research-attempt-policy/v1"),
            )
        conn.execute(
            """INSERT INTO research_collection_operations(
                request_key,research_run_id,payload_hash,job_id,attempt_no,
                disposition,created_at) VALUES(?,?,?,?,?,?,?)""",
            (operation["request_key"], operation["research_run_id"],
             operation["payload_hash"], job_id, attempt_no, disposition, utc_now()),
        )
        return self._research_replay(conn, operation)

    def research_operation(self, request_key, research_run_id):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM research_collection_operations
                WHERE request_key=? AND research_run_id=?""",
                (request_key, research_run_id),
            ).fetchone()
        return dict(row) if row else None

    def detach_research_subscription(self, request_key, research_run_id):
        # Deliberately does not call request_delete or change collector state.
        with self.lock, self._connect() as conn:
            conn.execute(
                """UPDATE research_collection_operations SET subscribed=0,
                detached_at=COALESCE(detached_at,?)
                WHERE request_key=? AND research_run_id=?""",
                (utc_now(), request_key, research_run_id),
            )
        return self.research_operation(request_key, research_run_id)

    def research_attempt_policy(self, job_id, attempt_no=None):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT p.* FROM research_attempt_policies p JOIN jobs j
                ON j.id=p.job_id WHERE p.job_id=?
                AND p.attempt_no=COALESCE(?,j.attempt_count)""",
                (job_id, attempt_no),
            ).fetchone()
        return dict(row) if row else None

    def research_attempt(self, job_id, attempt_no, *, metadata_only=False):
        with self._connect() as conn:
            projection = 'job_id,attempt_no,status,finished_at' if metadata_only else '*'
            row = conn.execute(
                "SELECT " + projection + " FROM job_attempts WHERE job_id=? AND attempt_no=?",
                (job_id, attempt_no),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if not metadata_only:
            item["result"] = json.loads(item.pop("result_json") or "null")
        return item

    def find_target(self, channel, target_url):
        target_key = canonical_target_url(target_url)
        if not target_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE channel=? AND target_key=? LIMIT 1",
                (channel, target_key),
            ).fetchone()
        return self._row(row)

    def list_external_import_queue(self, channel="homedepot"):
        """Return every cache-only task awaiting a governed external import."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE channel=? AND action=? AND status='queued'
                  AND superseded_by IS NULL
                ORDER BY created_at ASC, id ASC
                """,
                (channel, EXTERNAL_IMPORT_ACTION),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_external_import_tasks(self, channels, job_ids=None):
        """Read cache/import tasks for a bounded, channel-scoped export."""
        selected_channels = tuple(sorted(set(channels or ())))
        if not selected_channels or any(
            channel not in {"homedepot", "walmart"}
            for channel in selected_channels
        ):
            raise ValueError("external cache export channels must be homedepot/walmart")
        selected_ids = set(job_ids or ())
        if len(selected_ids) > 5000:
            raise ValueError("external cache export supports at most 5000 task IDs")
        placeholders = ",".join("?" for _channel in selected_channels)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE channel IN ({placeholders})
                  AND action=? AND superseded_by IS NULL
                ORDER BY channel ASC, created_at ASC, id ASC
                """,
                (*selected_channels, EXTERNAL_IMPORT_ACTION),
            ).fetchall()
        items = [self._row(row) for row in rows]
        if selected_ids:
            items = [item for item in items if item["id"] in selected_ids]
            found = {item["id"] for item in items}
            missing = sorted(selected_ids - found)
            if missing:
                raise ValueError(
                    "external cache export task is missing or outside channel scope: "
                    + missing[0]
                )
        return items

    def find_usable_target(self, target_url, exclude_job_id=None):
        target_key = canonical_target_url(target_url)
        if not target_key:
            return None
        params = [target_key]
        clause = ""
        if exclude_job_id:
            clause = " AND id<>?"
            params.append(exclude_job_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE target_key=?
                  AND status IN ('completed','partial_success')
                  AND superseded_by IS NULL
                  {clause}
                ORDER BY CASE status WHEN 'completed' THEN 0 ELSE 1 END,
                         COALESCE(finished_at, created_at) DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return self._row(row)

    def _preserved_legacy_root_evidence(
        self, conn, root, *, job_id, channel, target_key,
        source_product_id, active_attempt_no,
    ):
        """Prove an unused legacy root without moving or selecting its files."""
        conflict = "existing run root conflicts with active result artifacts"
        required = {"reviews.csv", "reviews.raw.jsonl", "validation_report.json", "selection_manifest.json"}
        if root.is_symlink() or not root.is_dir():
            raise ValueError(conflict)
        paths = sorted(root.iterdir())
        if {path.name for path in paths} != required or any(
            path.is_symlink() or not path.is_file() for path in paths
        ):
            raise ValueError(conflict)

        def comparable(report):
            if not isinstance(report, dict):
                return None
            artifacts = report.get("artifacts")
            expected = {"csv": f"{channel}/reviews.csv",
                        "raw_jsonl": f"{channel}/reviews.raw.jsonl"}
            # Legacy JSON files may omit the self-link that Store appended.
            if artifacts not in (expected, dict(expected, report=f"{channel}/validation_report.json")):
                return None
            return {key: value for key, value in report.items() if key != "artifacts"}

        try:
            report = json.loads((root / "validation_report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(conflict) from exc
        normalized = comparable(report)
        product = report.get("product") if isinstance(report, dict) else None
        if (normalized is None or report.get("status") != "COMPLETE"
                or report.get("channel") != channel or not isinstance(product, dict)
                or str(product.get("id")) != str(source_product_id)
                or canonical_target_url(product.get("url")) != target_key):
            raise ValueError(conflict)
        owners = []
        for attempt in conn.execute(
            "SELECT attempt_no,result_json FROM job_attempts WHERE job_id=? AND status='completed' AND attempt_no<>?",
            (job_id, active_attempt_no),
        ):
            try:
                prior = json.loads(attempt["result_json"] or "null")
            except ValueError:
                continue
            if isinstance(prior, dict) and comparable(prior.get(channel)) == normalized:
                owners.append(attempt["attempt_no"])
        if len(owners) != 1:
            raise ValueError(conflict)
        return {
            "owner_attempt_no": owners[0],
            "path": f"{job_id}/{channel}",
            "action": "preserved_in_place_not_selected",
            "files": [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                      for path in paths],
        }

    def publish_external_import(
        self,
        job_id,
        *,
        channel,
        target_url,
        brand,
        model,
        category,
        market_region,
        source_product_id,
        result,
        staged_run_dir,
        audit_detail=None,
        acquisition_batch_id=None,
        product_master_binding=None,
        product_observation=None,
        product_enrichment_status=None,
        product_quality_flags=None,
        voltage_platform=None,
        task_energy=None,
    ):
        """Publish one validated complete or partial external-import attempt.

        A partial attempt is publishable only with non-empty, internally
        reconciled artifacts.  It never replaces an already active completed
        result; that attempt is archived under its own attempt number while
        the completed root remains active.
        """
        target_key = canonical_target_url(target_url)
        binding = dict(product_master_binding or {})
        binding_record = binding.get("record") if isinstance(binding.get("record"), dict) else None
        observation = dict(product_observation or {})
        form = observation_form(category, observation)
        category = family_category(category)
        platform = dict(voltage_platform or {})
        quality_flags = list(product_quality_flags or [])
        energy = validate_task_energy(task_energy) if task_energy else None
        staged_run_dir = Path(staged_run_dir)
        required = {
            "reviews.csv",
            "reviews.raw.jsonl",
            "validation_report.json",
            "selection_manifest.json",
        }
        if not staged_run_dir.is_dir() or any(
            not (staged_run_dir / name).is_file() for name in required
        ):
            raise ValueError("external import staging artifacts are incomplete")
        if channel not in EXTERNAL_IMPORT_CHANNELS:
            raise ValueError(
                "channel is not supported for governed external import"
            )
        channel_result = result.get(channel) if isinstance(result, dict) else None
        report_status = str((channel_result or {}).get("status") or "")
        attempt_status = {
            "COMPLETE": "completed",
            "PARTIAL_SUCCESS": "partial_success",
        }.get(report_status)
        if not attempt_status:
            raise ValueError("external import report must be COMPLETE or PARTIAL_SUCCESS")
        try:
            artifact_report = json.loads(
                (staged_run_dir / "validation_report.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("external import validation report is unreadable") from exc
        if artifact_report.get("status") != report_status:
            raise ValueError("external import result and validation report status disagree")
        verified = self._verify_external_import_artifact_projection(
            staged_run_dir,
            expected_channel=channel,
            expected_target_key=target_key,
            expected_market_region=market_region,
            expected_source_product_id=source_product_id,
        )
        verified_form = verified["validated"]["task_product_metadata"]["product_form"]
        if form["value"] != "unknown" and verified_form["value"] != "unknown" and form["value"] != verified_form["value"]:
            raise ValueError("task form conflicts with verified imported product")
        if verified_form["value"] != "unknown":
            form = verified_form
        if channel_result != verified["report"]:
            raise ValueError(
                "external import result does not match validated artifact report"
            )
        partial_error = None
        if attempt_status == "partial_success":
            partial_error = (
                "external import declared incomplete: "
                f"{artifact_report['source_records_collected']} of "
                f"{artifact_report['source_total']} records collected; "
                f"{artifact_report['source_missing_records']} records missing"
            )

        root = self.data_dir / "runs" / job_id / channel
        moved_stage = False
        published_path = None
        archived_prior = None
        binding_context = self._active_binding_context(brand, category)
        with self.lock, binding_context as resolved, self._task_catalog_context() as task_catalog:
            if resolved is not None:
                brand = resolved["brand"]
                category = resolved["category"]
            try:
                with self._connect() as conn:
                    duplicate = conn.execute(
                        "SELECT * FROM jobs WHERE channel=? AND target_key=? LIMIT 1",
                        (channel, target_key),
                    ).fetchone()
                    if duplicate and duplicate["id"] != job_id:
                        raise ValueError(
                            f"target already belongs to stable task {duplicate['id']}"
                        )
                    existing = conn.execute(
                        "SELECT * FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if existing and (
                        existing["channel"] != channel
                        or existing["target_key"] != target_key
                    ):
                        raise ValueError(
                            "existing task channel/target does not match import"
                        )
                    completes_cached_attempt = bool(
                        existing
                        and existing["channel"] in EXTERNAL_IMPORT_CHANNELS
                        and existing["action"] == EXTERNAL_IMPORT_ACTION
                        and existing["status"] == "queued"
                    )
                    if existing and existing["status"] in {"running", "cancelling"}:
                        raise ValueError(
                            "active task cannot be replaced by an external import"
                        )
                    if (
                        existing
                        and existing["status"] == "queued"
                        and not completes_cached_attempt
                    ):
                        raise ValueError(
                            "active task cannot be replaced by an external import"
                        )
                    audit_detail = dict(audit_detail or {})
                    audit_detail.setdefault("publication_status", attempt_status)
                    prior_import_events = conn.execute(
                        """
                        SELECT detail_json FROM job_events
                        WHERE job_id=?
                          AND code IN ('external_import_completed','external_import_partial')
                        """,
                        (job_id,),
                    ).fetchall()
                    for event in prior_import_events:
                        try:
                            prior_detail = json.loads(event["detail_json"] or "{}")
                        except json.JSONDecodeError:
                            continue
                        same_import_id = (
                            audit_detail.get("import_id")
                            and prior_detail.get("import_id") == audit_detail.get("import_id")
                        )
                        same_upload = (
                            audit_detail.get("upload_sha256")
                            and prior_detail.get("upload_sha256")
                            == audit_detail.get("upload_sha256")
                        )
                        if same_import_id or same_upload:
                            raise ValueError("this external import file was already published")
                    if resolved is None and channel != "walmart":
                        for kind, value in (("brand", brand), ("category", category)):
                            table = self._master_table(kind)
                            if not conn.execute(
                                f"SELECT 1 FROM {table} WHERE name=? COLLATE NOCASE",
                                (value,),
                            ).fetchone():
                                raise ValueError(f"{kind} must exist in master data")

                    form = observation_form(category, {"product_form": form}, existing)
                    prior_attempt = int(existing["attempt_count"] or 0) if existing else 0
                    attempt_no = prior_attempt if completes_cached_attempt else prior_attempt + 1
                    batch_id = (
                        existing["acquisition_batch_id"]
                        if completes_cached_attempt and existing["acquisition_batch_id"]
                        else acquisition_batch_id
                    )
                    retain_completed = bool(
                        existing
                        and attempt_status == "partial_success"
                        and existing["active_result_status"] == "completed"
                    )
                    active_root = root
                    if existing and existing["active_result_status"]:
                        # A failed retry can leave the selected result in its
                        # attempt archive. Preserve it there when appending.
                        active_attempt_no = int(existing["active_attempt_no"] or 0)
                        active_archive_key = f"{job_id}/attempts/{active_attempt_no}/{channel}"
                        active_key = str(existing["active_run_path"] or f"{job_id}/{channel}")
                        if active_attempt_no < 1 or active_key not in {
                            f"{job_id}/{channel}", active_archive_key,
                        }:
                            raise ValueError("existing active result artifact path is unproven")
                        active_root = self.data_dir / "runs" / active_key
                        if active_root.is_symlink() or not active_root.is_dir():
                            raise ValueError("existing active result artifacts are missing")
                        if active_root != root and (root.exists() or root.is_symlink()):
                            if not retain_completed:
                                raise ValueError("existing run root conflicts with active result artifacts")
                            audit_detail["preserved_legacy_root"] = self._preserved_legacy_root_evidence(
                                conn, root, job_id=job_id, channel=channel,
                                target_key=target_key, source_product_id=source_product_id,
                                active_attempt_no=active_attempt_no,
                            )
                    if root.is_dir() and not retain_completed:
                        if completes_cached_attempt:
                            raise ValueError(
                                "cached external-import task unexpectedly has run artifacts"
                            )
                        if not existing or not prior_attempt:
                            raise ValueError(
                                "unexpected run artifacts exist for a new import task"
                            )
                        active_attempt_no = int(existing["active_attempt_no"] or 0)
                        if not active_attempt_no:
                            raise ValueError(
                                "existing run artifacts have no active attempt owner"
                            )
                        archived_prior = (
                            self.data_dir
                            / "runs"
                            / job_id
                            / "attempts"
                            / str(active_attempt_no)
                            / channel
                        )
                        if archived_prior.exists():
                            raise ValueError(
                                "prior attempt archive already exists; refusing overwrite"
                            )
                        archived_prior.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(root), str(archived_prior))

                    published_path = root
                    if retain_completed:
                        published_path = (
                            self.data_dir
                            / "runs"
                            / job_id
                            / "attempts"
                            / str(attempt_no)
                            / channel
                        )
                    if published_path.exists():
                        raise ValueError("external import attempt artifacts already exist")
                    published_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(staged_run_dir), str(published_path))
                    moved_stage = True

                    timestamp = utc_now()
                    result_json = json.dumps(result, ensure_ascii=False)
                    if existing:
                        if completes_cached_attempt:
                            cursor = conn.execute(
                                """
                                UPDATE job_attempts
                                SET status=?, started_at=?, finished_at=?,
                                    result_json=?, error=?, acquisition_batch_id=?
                                WHERE job_id=? AND attempt_no=? AND status='queued'
                                """,
                                (
                                    attempt_status,
                                    timestamp,
                                    timestamp,
                                    result_json,
                                    partial_error,
                                    batch_id,
                                    job_id,
                                    attempt_no,
                                ),
                            )
                            if cursor.rowcount != 1:
                                raise ValueError(
                                    "cached external-import attempt is no longer pending"
                                )
                        else:
                            conn.execute(
                                """
                                INSERT INTO job_attempts(
                                    job_id,attempt_no,status,priority,created_at,
                                    started_at,finished_at,result_json,error,
                                    acquisition_batch_id
                                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                                """,
                                (
                                    job_id,
                                    attempt_no,
                                    attempt_status,
                                    RETRY_JOB_PRIORITY,
                                    timestamp,
                                    timestamp,
                                    timestamp,
                                    result_json,
                                    partial_error,
                                    batch_id,
                                ),
                            )
                        if retain_completed:
                            conn.execute(
                                """
                                UPDATE jobs
                                SET action=?, status='completed',
                                    progress='external import partial; retained completed data',
                                    error=?, priority=?, cancel_requested=0,
                                    attempt_count=?, last_attempt_status='partial_success',
                                    acquisition_batch_id=?
                                WHERE id=?
                                """,
                                (
                                    EXTERNAL_IMPORT_ACTION,
                                    partial_error,
                                    RETRY_JOB_PRIORITY,
                                    attempt_no,
                                    batch_id,
                                    job_id,
                                ),
                            )
                        else:
                            progress = (
                                "external import completed"
                                if attempt_status == "completed"
                                else "external import partial success"
                            )
                            conn.execute(
                                """
                                UPDATE jobs
                                SET action=?, status=?, progress=?, result_json=?,
                                    error=?, started_at=?, finished_at=?, brand=?, model=?,
                                    source_product_id=?, category=?, market_region=?,
                                    priority=?, cancel_requested=0, attempt_count=?,
                                    active_attempt_no=?, active_result_status=?,
                                    active_run_path=?, last_attempt_status=?,
                                    acquisition_batch_id=?
                                WHERE id=?
                                """,
                                (
                                    EXTERNAL_IMPORT_ACTION,
                                    attempt_status,
                                    progress,
                                    result_json,
                                    partial_error,
                                    timestamp,
                                    timestamp,
                                    brand,
                                    model,
                                    source_product_id,
                                    category,
                                    market_region,
                                    RETRY_JOB_PRIORITY,
                                    attempt_no,
                                    attempt_no,
                                    attempt_status,
                                    f"{job_id}/{channel}",
                                    attempt_status,
                                    batch_id,
                                    job_id,
                                ),
                            )
                    else:
                        progress = (
                            "external import completed"
                            if attempt_status == "completed"
                            else "external import partial success"
                        )
                        conn.execute(
                            """
                            INSERT INTO jobs(
                                id,action,channel,status,progress,result_json,error,
                                created_at,started_at,finished_at,target_url,target_key,
                                brand,model,source_product_id,category,market_region,
                                priority,retry_of,superseded_by,cancel_requested,
                                attempt_count,active_attempt_no,active_result_status,
                                active_run_path,last_attempt_status,
                                acquisition_batch_id,product_master_schema_version,
                                product_master_snapshot_id,product_master_model_id,
                                product_master_entity_version,
                                product_master_record_snapshot_hash,
                                product_master_record_json,product_observation_json,
                                product_observation_hash,
                                product_enrichment_status,product_quality_flags_json,
                                voltage_platform_schema_version,voltage_platform_status,
                                voltage_platform_key,voltage_platform_brand,
                                voltage_platform_voltage,voltage_platform_label,
                                voltage_platform_evidence_source,voltage_platform_null_reason
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                job_id,
                                EXTERNAL_IMPORT_ACTION,
                                channel,
                                attempt_status,
                                progress,
                                result_json,
                                partial_error,
                                timestamp,
                                timestamp,
                                timestamp,
                                target_url,
                                target_key,
                                brand,
                                model,
                                source_product_id,
                                category,
                                market_region,
                                NEW_JOB_PRIORITY,
                                None,
                                None,
                                0,
                                1,
                                1,
                                attempt_status,
                                f"{job_id}/{channel}",
                                attempt_status,
                                batch_id,
                                binding.get("schema_version"),
                                binding.get("snapshot_id"),
                                binding.get("model_id"),
                                binding.get("entity_version"),
                                binding.get("record_snapshot_hash"),
                                json.dumps(binding_record, ensure_ascii=False, separators=(",", ":")) if binding_record else None,
                                json.dumps(observation, ensure_ascii=False, separators=(",", ":")) if observation else None,
                                observation_hash(observation) if observation else None,
                                product_enrichment_status or ("enriched" if binding else "unknown"),
                                json.dumps(quality_flags, ensure_ascii=False, separators=(",", ":")),
                                platform.get("schema_version"), platform.get("status"),
                                platform.get("platform_key"), platform.get("brand"),
                                platform.get("normalized_voltage"), platform.get("display_name"),
                                platform.get("evidence_source"), platform.get("null_reason"),
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO job_attempts(
                                job_id,attempt_no,status,priority,created_at,
                                started_at,finished_at,result_json,error,
                                acquisition_batch_id
                            ) VALUES(?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                job_id,
                                1,
                                attempt_status,
                                NEW_JOB_PRIORITY,
                                timestamp,
                                timestamp,
                                timestamp,
                                result_json,
                                partial_error,
                                batch_id,
                            ),
                        )
                    if existing and not retain_completed and observation:
                        conn.execute(
                            """
                            UPDATE jobs SET
                                product_master_schema_version=COALESCE(?, product_master_schema_version),
                                product_master_snapshot_id=COALESCE(?, product_master_snapshot_id),
                                product_master_model_id=COALESCE(?, product_master_model_id),
                                product_master_entity_version=COALESCE(?, product_master_entity_version),
                                product_master_record_snapshot_hash=COALESCE(?, product_master_record_snapshot_hash),
                                product_master_record_json=COALESCE(?, product_master_record_json),
                                product_observation_json=?,
                                product_observation_hash=?,
                                product_enrichment_status=?,
                                product_quality_flags_json=?,
                                voltage_platform_schema_version=?,
                                voltage_platform_status=?,
                                voltage_platform_key=?,
                                voltage_platform_brand=?,
                                voltage_platform_voltage=?,
                                voltage_platform_label=?,
                                voltage_platform_evidence_source=?,
                                voltage_platform_null_reason=?
                            WHERE id=?
                            """,
                            (
                                binding.get("schema_version"),
                                binding.get("snapshot_id"),
                                binding.get("model_id"),
                                binding.get("entity_version"),
                                binding.get("record_snapshot_hash"),
                                json.dumps(
                                    binding_record, ensure_ascii=False,
                                    separators=(",", ":"),
                                ) if binding_record else None,
                                json.dumps(
                                    observation, ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                observation_hash(observation),
                                product_enrichment_status or (
                                    "enriched" if binding else "unknown"
                                ),
                                json.dumps(
                                    quality_flags, ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                platform.get("schema_version"),
                                platform.get("status"),
                                platform.get("platform_key"),
                                platform.get("brand"),
                                platform.get("normalized_voltage"),
                                platform.get("display_name"),
                                platform.get("evidence_source"),
                                platform.get("null_reason"),
                                job_id,
                            ),
                        )
                    if energy:
                        conn.execute(
                            """
                            UPDATE jobs SET
                                energy_schema_version=?,energy_type=?,
                                energy_readiness_status=?,energy_evidence_source=?,
                                energy_canonical_voltage=?,energy_unknown_reason=?
                            WHERE id=?
                            """,
                            (
                                energy["schema_version"], energy["energy_type"],
                                energy["readiness_status"], energy["evidence_source"],
                                energy["canonical_voltage"], energy["unknown_reason"],
                                job_id,
                            ),
                        )
                    if not retain_completed:
                        persist_form(conn, job_id, form)
                    self._bind_task(conn, job_id, task_catalog, existing)
                    event_code = (
                        "external_import_completed"
                        if attempt_status == "completed"
                        else "external_import_partial"
                    )
                    self._append_event(
                        conn,
                        job_id,
                        attempt_no,
                        "info" if attempt_status == "completed" else "warning",
                        event_code,
                        f"Published validated external import attempt {attempt_no} as {attempt_status}",
                        audit_detail,
                    )
                    updated = conn.execute(
                        "SELECT * FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                return self._row(updated)
            except Exception:
                if moved_stage and published_path and published_path.exists():
                    staged_run_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(published_path), str(staged_run_dir))
                if archived_prior and archived_prior.exists() and not root.exists():
                    root.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(archived_prior), str(root))
                raise

    def find_external_import_publication(self, import_id, upload_sha256):
        """Recover an idempotent publication after an orchestration crash.

        The authoritative proof is the append-only Store event written in the
        same SQLite transaction as the job/attempt result.  No event means the
        generic orchestrator must not claim publication.
        """
        import_id = str(import_id or "")
        upload_sha256 = str(upload_sha256 or "")
        if not import_id or not re.fullmatch(r"[0-9a-f]{64}", upload_sha256):
            return None
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT e.job_id,e.attempt_no,e.detail_json,
                       j.channel,j.status,j.active_result_status,
                       j.acquisition_batch_id
                FROM job_events e
                JOIN jobs j ON j.id=e.job_id
                WHERE e.code IN ('external_import_completed','external_import_partial')
                ORDER BY e.id DESC
                """
            ).fetchall()
        matches = []
        for row in rows:
            try:
                detail = json.loads(row["detail_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if (
                detail.get("import_id") == import_id
                and detail.get("upload_sha256") == upload_sha256
            ):
                matches.append(row)
        if len(matches) != 1:
            return None
        row = matches[0]
        return {
            "channel": row["channel"],
            "job_id": row["job_id"],
            "attempt_no": row["attempt_no"],
            "job_status": row["status"],
            "active_result_status": row["active_result_status"],
            "acquisition_batch_id": row["acquisition_batch_id"],
        }

    @staticmethod
    def _read_json_artifact(path, label):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} is unreadable") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be a JSON object")
        return value

    @staticmethod
    def _verify_external_import_artifact_projection(
        run_dir,
        *,
        expected_channel,
        expected_target_key,
        expected_market_region,
        expected_source_product_id,
        allow_legacy_declared_incomplete=False,
    ):
        """Rebuild a new import from manifest+raw before Store publication."""
        # Import lazily: external_import imports canonical_target_url from this
        # module, so a module-level import would create an import cycle.
        from .external_import import ExternalImportError, verify_import_artifacts

        try:
            artifacts = verify_import_artifacts(
                run_dir,
                allow_legacy_declared_incomplete=allow_legacy_declared_incomplete,
            )
        except ExternalImportError as exc:
            raise ValueError(str(exc)) from exc
        validated = artifacts["validated"]
        if (
            validated["channel"] != expected_channel
            or validated["target_key"] != expected_target_key
            or validated["market_region"] != expected_market_region
            or validated["product"]["id"] != expected_source_product_id
        ):
            raise ValueError("rebuilt external import identity does not match publication")
        return artifacts

    @staticmethod
    def _external_import_partial_artifact_counts(
        run_dir,
        *,
        require_declared_incomplete_reason=False,
        require_current_validation_schema=False,
        expected_channel=None,
        expected_target_key=None,
        expected_market_region=None,
        expected_source_product_id=None,
    ):
        missing = sorted(
            name for name in EXTERNAL_IMPORT_ARTIFACTS
            if not (run_dir / name).is_file()
        )
        if missing:
            raise ValueError(
                "partial attempt artifacts are incomplete: " + ", ".join(missing)
            )
        report = Store._read_json_artifact(
            run_dir / "validation_report.json",
            "partial validation report",
        )
        manifest = Store._read_json_artifact(
            run_dir / "selection_manifest.json",
            "partial selection manifest",
        )
        try:
            declared = int(report.get("source_total"))
            collected = int(report.get("source_records_collected"))
            missing_records = int(report.get("source_missing_records"))
            source_unique = int(report.get("source_unique_reviews"))
            eligible = int(report.get("eligible_reviews"))
            unique = int(report.get("unique_reviews"))
            excluded = int(report.get("excluded_reviews"))
        except (TypeError, ValueError) as exc:
            raise ValueError("partial artifact counts are not integers") from exc
        allowed_stop_reasons = {EXTERNAL_IMPORT_DECLARED_INCOMPLETE_STOP_REASON}
        if not require_declared_incomplete_reason:
            allowed_stop_reasons.add(LEGACY_EXTERNAL_EXPORT_INCOMPLETE_STOP_REASON)
        if (
            (require_current_validation_schema and report.get("validation_schema_version")
             != EXTERNAL_IMPORT_VALIDATION_SCHEMA_VERSION)
            or
            report.get("status") != "PARTIAL_SUCCESS"
            or report.get("source_reconciled") is not False
            or report.get("stop_reason") not in allowed_stop_reasons
            or collected <= 0
            or declared <= collected
            or missing_records != declared - collected
            or source_unique != collected
            or unique != eligible
            or excluded != collected - eligible
        ):
            raise ValueError("partial artifact counts are not reconciled")
        raw_count = 0
        raw_rows = []
        try:
            with (run_dir / "reviews.raw.jsonl").open(
                "r", encoding="utf-8"
            ) as handle:
                for line in handle:
                    if not line.strip():
                        raise ValueError("partial raw JSONL contains an empty line")
                    raw = json.loads(line)
                    if not isinstance(raw, dict):
                        raise ValueError("partial raw JSONL contains a non-object record")
                    raw_rows.append(raw)
                    raw_count += 1
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("partial raw JSONL is unreadable") from exc
        try:
            with (run_dir / "reviews.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                reader = csv.DictReader(handle)
                fields = set(reader.fieldnames or [])
                if not {"review_id", "source_product_id", "source_site"}.issubset(
                    fields
                ):
                    raise ValueError("partial CSV is missing identity fields")
                csv_rows = list(reader)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise ValueError("partial CSV is unreadable") from exc
        if raw_count != collected or len(csv_rows) != eligible:
            raise ValueError("partial artifact counts do not match CSV/raw rows")
        product = report.get("product")
        if not isinstance(product, dict):
            raise ValueError("partial validation report product is missing")
        default_source_site = str(product.get("source_site") or "").strip().casefold()
        raw_keys = []
        for raw in raw_rows:
            source_site = str(raw.get("origin_source_site") or default_source_site).strip().casefold()
            key = (
                source_site,
                str(raw.get("source_product_id") or "").strip(),
                str(raw.get("review_id") or "").strip(),
            )
            if not all(key):
                raise ValueError("partial raw JSONL contains an incomplete business key")
            raw_keys.append(key)
            if expected_source_product_id and str(raw.get("product_id") or "").strip() != str(
                expected_source_product_id
            ):
                raise ValueError("partial raw JSONL product identity does not match import")
            if expected_target_key and canonical_target_url(raw.get("product_url")) != expected_target_key:
                raise ValueError("partial raw JSONL target URL does not match import")
        if len(set(raw_keys)) != len(raw_keys):
            raise ValueError("partial raw JSONL business keys are duplicated")
        if any(not str(row.get("review_id") or "").strip() for row in csv_rows):
            raise ValueError("partial CSV contains an empty review_id")
        csv_keys = [
            (
                str(row.get("source_site") or "").strip().casefold(),
                str(row.get("source_product_id") or "").strip(),
                str(row.get("review_id") or "").strip(),
            )
            for row in csv_rows
        ]
        if any(not all(value) for value in csv_keys) or len(set(csv_keys)) != len(
            csv_keys
        ):
            raise ValueError("partial CSV business keys are missing or duplicated")
        if not set(csv_keys).issubset(set(raw_keys)):
            raise ValueError("partial CSV business keys are absent from raw JSONL")
        raw_by_key = dict(zip(raw_keys, raw_rows))
        for csv_row, csv_key in zip(csv_rows, csv_keys):
            raw = raw_by_key[csv_key]
            if (
                (expected_channel and csv_row.get("channel") != expected_channel)
                or csv_row.get("product_id") != str(raw.get("product_id") or "")
                or canonical_target_url(csv_row.get("product_url"))
                != canonical_target_url(raw.get("product_url"))
                or csv_row.get("source_site", "").strip().casefold() != csv_key[0]
                or (
                    expected_market_region
                    and csv_row.get("market_region") != expected_market_region
                )
                or csv_row.get("market_region_evidence") != "source_market"
                or str(csv_row.get("is_eligible") or "").strip().casefold()
                != "true"
                or str(csv_row.get("exclusion_reason") or "").strip()
                or csv_row.get("review_text") != str(raw.get("review_text") or "")
            ):
                raise ValueError("partial CSV row does not match raw import evidence")
            try:
                csv_rating = float(csv_row.get("rating"))
                raw_rating = float(raw.get("rating"))
            except (TypeError, ValueError) as exc:
                raise ValueError("partial CSV rating is invalid") from exc
            if csv_rating != raw_rating:
                raise ValueError("partial CSV rating does not match raw import evidence")
        manifest_completeness = manifest.get("completeness")
        if not isinstance(manifest_completeness, dict):
            raise ValueError("partial selection manifest completeness is missing")
        try:
            manifest_counts = (
                int(manifest.get("review_count")),
                int(manifest.get("source_records_collected")),
                int(manifest.get("source_missing_records")),
                int(manifest_completeness.get("source_total")),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("partial selection manifest counts are invalid") from exc
        if (
            manifest.get("publication_status") != "partial_success"
            or manifest.get("stop_reason") not in allowed_stop_reasons
            or manifest_completeness.get("is_complete") is not False
            or manifest_counts
            != (collected, collected, missing_records, declared)
        ):
            raise ValueError("partial selection manifest counts are not reconciled")
        if expected_channel and (
            report.get("channel") != expected_channel
            or manifest.get("channel") != expected_channel
        ):
            raise ValueError("partial artifacts channel does not match import")
        if expected_market_region and (
            report.get("market_region") != expected_market_region
            or manifest.get("market_region") != expected_market_region
        ):
            raise ValueError("partial artifacts market region does not match import")
        if expected_target_key and canonical_target_url(manifest.get("target_url")) != expected_target_key:
            raise ValueError("partial selection manifest target URL does not match import")
        if expected_source_product_id and (
            str(product.get("id") or "").strip() != str(expected_source_product_id)
            or str((manifest.get("product") or {}).get("id") or "").strip()
            != str(expected_source_product_id)
        ):
            raise ValueError("partial artifacts product identity does not match import")
        return {
            "report": report,
            "manifest": manifest,
            "source_total": declared,
            "source_records_collected": collected,
            "source_missing_records": missing_records,
            "eligible_reviews": eligible,
            "raw_records": raw_count,
            "csv_records": len(csv_rows),
        }

    def _external_import_partial_promotion_plan(
        self,
        conn,
        job_id,
        attempt_no,
        *,
        expected_import_id,
        expected_upload_sha256,
    ):
        expected_import_id = str(expected_import_id or "").strip()
        expected_upload_sha256 = str(expected_upload_sha256 or "").strip().casefold()
        if not expected_import_id:
            raise ValueError("expected import_id is required")
        if (
            len(expected_upload_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_upload_sha256)
        ):
            raise ValueError("expected upload_sha256 must be 64 lowercase hex characters")
        try:
            attempt_no = int(attempt_no)
        except (TypeError, ValueError) as exc:
            raise ValueError("attempt number must be an integer") from exc
        if attempt_no < 1:
            raise ValueError("attempt number must be positive")
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise ValueError("stable task does not exist")
        if job["action"] != EXTERNAL_IMPORT_ACTION or job["channel"] not in {
            "homedepot",
            "amazon_live",
            "walmart",
        }:
            raise ValueError("task is not a governed external import")
        root = self.data_dir / "runs" / job_id / job["channel"]
        target_archive = (
            self.data_dir
            / "runs"
            / job_id
            / "attempts"
            / str(attempt_no)
            / job["channel"]
        )
        prior_attempt_no = int(job["active_attempt_no"] or 0)
        prior_archive = (
            self.data_dir
            / "runs"
            / job_id
            / "attempts"
            / str(prior_attempt_no)
            / job["channel"]
        )
        promotion_events = conn.execute(
            """
            SELECT * FROM job_events
            WHERE job_id=? AND attempt_no=?
              AND code='external_import_partial_promoted'
            ORDER BY id ASC
            """,
            (job_id, attempt_no),
        ).fetchall()
        if (
            int(job["active_attempt_no"] or 0) == attempt_no
            and job["active_result_status"] == "partial_success"
        ):
            if len(promotion_events) != 1:
                raise ValueError("promoted partial active result has no unique audit event")
            try:
                detail = json.loads(promotion_events[0]["detail_json"] or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("partial promotion audit event is unreadable") from exc
            if (
                detail.get("import_id") != expected_import_id
                or str(detail.get("upload_sha256") or "").casefold()
                != expected_upload_sha256
            ):
                raise ValueError("partial promotion audit identity does not match")
            counts = self._verify_external_import_artifact_projection(
                root,
                expected_channel=job["channel"],
                expected_target_key=job["target_key"],
                expected_market_region=job["market_region"],
                expected_source_product_id=job["source_product_id"],
                allow_legacy_declared_incomplete=True,
            )
            return {
                "promotion_status": "already_promoted",
                "job": job,
                "attempt_no": attempt_no,
                "prior_attempt_no": int(detail.get("from_active_attempt_no") or 0),
                "prior_validation_status": detail.get("prior_validation_status"),
                "root": root,
                "target_archive": target_archive,
                "prior_archive": (
                    self.data_dir
                    / "runs"
                    / job_id
                    / "attempts"
                    / str(int(detail.get("from_active_attempt_no") or 0))
                    / job["channel"]
                ),
                "counts": counts,
                "audit_detail": detail,
            }
        if attempt_no != int(job["attempt_count"] or 0):
            raise ValueError("only the latest external import attempt can be promoted")
        if job["last_attempt_status"] != "partial_success":
            raise ValueError("latest external import attempt is not partial_success")
        if (
            job["active_result_status"] != "completed"
            or prior_attempt_no < 1
            or prior_attempt_no == attempt_no
        ):
            raise ValueError("task has no distinct completed active result to replace")
        attempt = conn.execute(
            """
            SELECT * FROM job_attempts
            WHERE job_id=? AND attempt_no=?
            """,
            (job_id, attempt_no),
        ).fetchone()
        if not attempt or attempt["status"] != "partial_success":
            raise ValueError("latest external import attempt is not partial_success")
        try:
            attempt_result = json.loads(attempt["result_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("partial attempt result is unreadable") from exc
        channel_result = attempt_result.get(job["channel"])
        if (
            not isinstance(channel_result, dict)
            or channel_result.get("status") != "PARTIAL_SUCCESS"
        ):
            raise ValueError("partial attempt result status is not PARTIAL_SUCCESS")
        import_events = conn.execute(
            """
            SELECT * FROM job_events
            WHERE job_id=? AND attempt_no=? AND code='external_import_partial'
            ORDER BY id ASC
            """,
            (job_id, attempt_no),
        ).fetchall()
        if len(import_events) != 1:
            raise ValueError("partial attempt has no unique external import audit event")
        try:
            import_detail = json.loads(import_events[0]["detail_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("partial external import audit event is unreadable") from exc
        if (
            import_detail.get("import_id") != expected_import_id
            or str(import_detail.get("upload_sha256") or "").casefold()
            != expected_upload_sha256
        ):
            raise ValueError("partial promotion audit identity does not match")
        if import_detail.get("publication_status") != "partial_success":
            raise ValueError("partial import audit publication status is invalid")
        if not root.is_dir():
            raise ValueError("completed active result artifacts are missing")
        prior_report = self._read_json_artifact(
            root / "validation_report.json",
            "active validation report",
        )
        prior_validation_status = str(prior_report.get("status") or "")
        if prior_validation_status in REPORTABLE_VALIDATION_STATUSES:
            raise ValueError("active validation is already reportable")
        if not prior_validation_status:
            raise ValueError("active validation status is missing")
        if prior_archive.exists():
            raise ValueError("prior active attempt archive already exists")
        if not target_archive.is_dir():
            raise ValueError("latest partial attempt archive is missing")
        counts = self._verify_external_import_artifact_projection(
            target_archive,
            expected_channel=job["channel"],
            expected_target_key=job["target_key"],
            expected_market_region=job["market_region"],
            expected_source_product_id=job["source_product_id"],
            allow_legacy_declared_incomplete=True,
        )
        manifest = counts["manifest"]
        report = counts["report"]
        if (
            manifest.get("import_id") != expected_import_id
            or manifest.get("channel") != job["channel"]
            or canonical_target_url(manifest.get("target_url")) != job["target_key"]
            or manifest.get("market_region") != job["market_region"]
            or report.get("channel") != job["channel"]
            or report.get("market_region") != job["market_region"]
            or (report.get("notes") or {}).get("import_id") != expected_import_id
        ):
            raise ValueError("partial artifacts do not match the audited import identity")
        if (
            counts["source_total"]
            != int(import_detail.get("source_total") or -1)
            or counts["source_records_collected"]
            != int(import_detail.get("source_records_collected") or -1)
            or counts["source_missing_records"]
            != int(import_detail.get("source_missing_records") or -1)
            or counts["eligible_reviews"]
            != int(import_detail.get("eligible_reviews") or -1)
        ):
            raise ValueError("partial artifacts do not match the external import audit counts")
        audit_detail = {
            "from_active_attempt_no": prior_attempt_no,
            "to_active_attempt_no": attempt_no,
            "prior_validation_status": prior_validation_status,
            "new_validation_status": "PARTIAL_SUCCESS",
            "reason": "prior_active_validation_not_reportable",
            "import_id": expected_import_id,
            "upload_sha256": expected_upload_sha256,
            "source_total": counts["source_total"],
            "source_records_collected": counts["source_records_collected"],
            "source_missing_records": counts["source_missing_records"],
            "eligible_reviews": counts["eligible_reviews"],
        }
        return {
            "promotion_status": "ready",
            "job": job,
            "attempt": attempt,
            "attempt_no": attempt_no,
            "prior_attempt_no": prior_attempt_no,
            "prior_validation_status": prior_validation_status,
            "root": root,
            "target_archive": target_archive,
            "prior_archive": prior_archive,
            "counts": counts,
            "audit_detail": audit_detail,
        }

    @staticmethod
    def _public_external_partial_promotion_plan(plan):
        return {
            "promotion_status": plan["promotion_status"],
            "job_id": plan["job"]["id"],
            "channel": plan["job"]["channel"],
            "attempt_no": plan["attempt_no"],
            "prior_attempt_no": plan["prior_attempt_no"],
            "prior_validation_status": plan["prior_validation_status"],
            "new_validation_status": "PARTIAL_SUCCESS",
            **{
                key: plan["counts"][key]
                for key in (
                    "source_total",
                    "source_records_collected",
                    "source_missing_records",
                    "eligible_reviews",
                )
            },
        }

    def inspect_external_import_partial_promotion(
        self,
        job_id,
        attempt_no,
        *,
        expected_import_id,
        expected_upload_sha256,
    ):
        """Fail-closed dry-run for a single audited partial promotion."""
        with self.lock, self._connect() as conn:
            plan = self._external_import_partial_promotion_plan(
                conn,
                job_id,
                attempt_no,
                expected_import_id=expected_import_id,
                expected_upload_sha256=expected_upload_sha256,
            )
        return self._public_external_partial_promotion_plan(plan)

    def promote_external_import_partial(
        self,
        job_id,
        attempt_no,
        *,
        expected_import_id,
        expected_upload_sha256,
    ):
        """Promote one latest audited partial when the old active is invalid.

        The operation never selects an arbitrary historic attempt: the caller
        must name the latest attempt and match both immutable import audit
        identifiers.  Filesystem moves and the SQLite update are rolled back
        together on ordinary failures, while a durable event records success.
        """
        moved_prior = False
        moved_partial = False
        plan = None
        with self.lock:
            try:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    plan = self._external_import_partial_promotion_plan(
                        conn,
                        job_id,
                        attempt_no,
                        expected_import_id=expected_import_id,
                        expected_upload_sha256=expected_upload_sha256,
                    )
                    if plan["promotion_status"] == "already_promoted":
                        public = self._row(plan["job"])
                        public["promotion_status"] = "already_promoted"
                        return public
                    plan["prior_archive"].parent.mkdir(
                        parents=True, exist_ok=True
                    )
                    shutil.move(str(plan["root"]), str(plan["prior_archive"]))
                    moved_prior = True
                    shutil.move(str(plan["target_archive"]), str(plan["root"]))
                    moved_partial = True
                    finished_at = plan["attempt"]["finished_at"] or utc_now()
                    cursor = conn.execute(
                        """
                        UPDATE jobs
                        SET status='partial_success',
                            progress='promoted validated external import partial',
                            result_json=?, error=?, finished_at=?,
                            active_attempt_no=?,
                            active_result_status='partial_success',
                            active_run_path=?,
                            last_attempt_status='partial_success'
                        WHERE id=? AND active_attempt_no=?
                          AND active_result_status='completed'
                          AND attempt_count=?
                          AND last_attempt_status='partial_success'
                        """,
                        (
                            plan["attempt"]["result_json"],
                            plan["attempt"]["error"],
                            finished_at,
                            plan["attempt_no"],
                            f"{job_id}/{plan['job']['channel']}",
                            job_id,
                            plan["prior_attempt_no"],
                            plan["attempt_no"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("task state changed during partial promotion")
                    self._append_event(
                        conn,
                        job_id,
                        plan["attempt_no"],
                        "warning",
                        "external_import_partial_promoted",
                        (
                            "Promoted validated partial external import because "
                            "the prior active validation was not reportable"
                        ),
                        plan["audit_detail"],
                    )
                    updated = conn.execute(
                        "SELECT * FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                public = self._row(updated)
                public["promotion_status"] = "promoted"
                return public
            except Exception:
                if plan:
                    if (
                        moved_partial
                        and plan["root"].exists()
                        and not plan["target_archive"].exists()
                    ):
                        plan["target_archive"].parent.mkdir(
                            parents=True, exist_ok=True
                        )
                        shutil.move(
                            str(plan["root"]), str(plan["target_archive"])
                        )
                    if (
                        moved_prior
                        and plan["prior_archive"].exists()
                        and not plan["root"].exists()
                    ):
                        plan["root"].parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(
                            str(plan["prior_archive"]), str(plan["root"])
                        )
                raise

    def queue_retry(
        self,
        job_id,
        priority=RETRY_JOB_PRIORITY,
        action=None,
        acquisition_batch_id=None,
        research_operation=None,
    ):
        """Start a new execution attempt without changing the stable task ID."""
        moved_root = None
        moved_archive = None
        with self.lock:
            try:
                with self._connect() as conn:
                    if research_operation is not None:
                        conn.execute("BEGIN IMMEDIATE")
                        replay = self._research_replay(conn, research_operation)
                        if replay:
                            return replay
                    row = conn.execute(
                        "SELECT * FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if not row:
                        return None
                    if research_operation is not None:
                        if row["status"] in {"queued", "running", "cancelling"}:
                            if research_operation.get("retry") and int(research_operation.get("expected_attempt_no") or 0) + 1 != int(row["attempt_count"] or 0):
                                raise ValueError("retry expected attempt does not match active retry")
                            return self._research_bind(
                                conn, research_operation, job_id,
                                row["attempt_count"], "subscribed_active",
                            )
                        if not research_operation.get("retry"):
                            return self._research_bind(
                                conn, research_operation, job_id,
                                row["active_attempt_no"] or row["attempt_count"],
                                "reused_terminal",
                            )
                        if int(research_operation.get("expected_attempt_no") or 0) != int(row["attempt_count"] or 0):
                            raise ValueError("retry expected attempt does not match latest attempt")
                        if row["last_attempt_status"] not in {"failed", "partial_success", "cancelled"}:
                            raise ValueError("research retry requires a failed or partial attempt")
                    if row["status"] in {"queued", "running", "cancelling"}:
                        raise ValueError("任务已在队列中或正在采集")
                    next_action = action or row["action"]
                    prior_attempt = int(row["attempt_count"] or 0)
                    root = self.data_dir / "runs" / job_id / row["channel"]
                    if root.exists() and (
                        root.is_symlink() or not root.is_dir()
                    ):
                        raise ValueError(
                            "run artifact root is not a governed directory"
                        )
                    if root.is_dir() and prior_attempt:
                        active_attempt_no = int(row["active_attempt_no"] or 0)
                        owner_attempt_no = active_attempt_no or prior_attempt
                        archived = (
                            self.data_dir
                            / "runs"
                            / job_id
                            / "attempts"
                            / str(owner_attempt_no)
                            / row["channel"]
                        )
                        if archived.exists():
                            raise ValueError(
                                "prior attempt archive already exists; refusing overwrite"
                            )
                        recovery_detail = None
                        if not active_attempt_no:
                            latest_attempt = conn.execute(
                                """
                                SELECT attempt_no,status,result_json
                                FROM job_attempts
                                WHERE job_id=? AND attempt_no=?
                                """,
                                (job_id, prior_attempt),
                            ).fetchone()
                            if (
                                row["status"] not in {"failed", "cancelled"}
                                or row["last_attempt_status"]
                                not in {"failed", "cancelled"}
                                or not latest_attempt
                                or latest_attempt["status"]
                                != row["last_attempt_status"]
                                or latest_attempt["result_json"] is not None
                            ):
                                raise ValueError(
                                    "existing run artifacts have no provable failed attempt owner"
                                )
                            files = []
                            total_bytes = 0
                            for path in sorted(root.rglob("*")):
                                if path.is_symlink() or (
                                    not path.is_dir() and not path.is_file()
                                ):
                                    raise ValueError(
                                        "failed attempt artifacts contain an unsupported path"
                                    )
                                if not path.is_file():
                                    continue
                                relative = path.relative_to(root).as_posix()
                                digest = hashlib.sha256()
                                with path.open("rb") as handle:
                                    for chunk in iter(
                                        lambda: handle.read(1024 * 1024), b""
                                    ):
                                        digest.update(chunk)
                                size = path.stat().st_size
                                total_bytes += size
                                files.append(
                                    {
                                        "path": relative,
                                        "bytes": size,
                                        "sha256": "sha256:" + digest.hexdigest(),
                                    }
                                )
                            manifest_bytes = json.dumps(
                                files,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                            recovery_detail = {
                                "reason": "terminal_failed_attempt_owned_active_root",
                                "artifact_count": len(files),
                                "artifact_bytes": total_bytes,
                                "artifact_manifest_sha256": (
                                    "sha256:"
                                    + hashlib.sha256(manifest_bytes).hexdigest()
                                ),
                                "archive_path": (
                                    f"{job_id}/attempts/{prior_attempt}/"
                                    f"{row['channel']}"
                                ),
                            }
                        archived.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(root), str(archived))
                        moved_root, moved_archive = root, archived
                        if active_attempt_no:
                            conn.execute(
                                "UPDATE jobs SET active_run_path=? WHERE id=?",
                                (
                                    f"{job_id}/attempts/{active_attempt_no}/"
                                    f"{row['channel']}",
                                    job_id,
                                ),
                            )
                        else:
                            self._append_event(
                                conn,
                                job_id,
                                prior_attempt,
                                "warning",
                                "failed_attempt_artifacts_archived",
                                (
                                    "Archived terminal failed-attempt artifacts "
                                    "before retry"
                                ),
                                recovery_detail,
                            )
                    attempt_no = prior_attempt + 1
                    timestamp = utc_now()
                    is_external_cache = next_action == EXTERNAL_IMPORT_ACTION
                    progress = (
                        EXTERNAL_IMPORT_PROGRESS
                        if is_external_cache
                        else f"queued retry attempt {attempt_no}"
                    )
                    conn.execute(
                        """
                        INSERT INTO job_attempts(
                            job_id,attempt_no,status,priority,created_at,
                            acquisition_batch_id
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            job_id,
                            attempt_no,
                            "queued",
                            int(priority),
                            timestamp,
                            acquisition_batch_id,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE jobs
                        SET action=?, status='queued', progress=?, error=NULL, priority=?,
                            started_at=NULL, finished_at=NULL, cancel_requested=0,
                            attempt_count=?, last_attempt_status='queued', retry_of=NULL,
                            acquisition_batch_id=?
                        WHERE id=?
                        """,
                        (
                            next_action,
                            progress,
                            int(priority),
                            attempt_no,
                            acquisition_batch_id,
                            job_id,
                        ),
                    )
                    self._append_event(
                        conn,
                        job_id,
                        attempt_no,
                        "info",
                        (
                            "external_import_cached"
                            if is_external_cache
                            else "retry_queued"
                        ),
                        (
                            "Task cached for external collection; server execution is disabled"
                            if is_external_cache
                            else f"Queued retry attempt {attempt_no}"
                        ),
                        {
                            "priority": int(priority),
                            **(
                                {"execution_policy": "external_import_only"}
                                if is_external_cache
                                else {}
                            ),
                        },
                    )
                    updated = conn.execute(
                        "SELECT * FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if research_operation is not None:
                        updated = self._research_bind(
                            conn, research_operation, job_id, attempt_no,
                            "retried", new_attempt=True,
                        )
            except Exception:
                if (
                    moved_root is not None
                    and moved_archive is not None
                    and moved_archive.exists()
                    and not moved_root.exists()
                ):
                    moved_root.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(moved_archive), str(moved_root))
                raise
        return dict(updated) if research_operation is not None else self._row(updated)

    def next_job(self, channel):
        with self.lock, self._connect() as conn:
            cooldown = conn.execute(
                "SELECT blocked_until FROM channel_cooldowns WHERE channel=?",
                (channel,),
            ).fetchone()
            if cooldown:
                try:
                    blocked_until = datetime.fromisoformat(cooldown["blocked_until"])
                except (TypeError, ValueError):
                    return None
                if blocked_until > datetime.now(timezone.utc):
                    return None
                conn.execute(
                    "DELETE FROM channel_cooldowns WHERE channel=?", (channel,)
                )
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status='queued' AND action='scrape' AND channel=?
                  AND COALESCE(energy_readiness_status, 'ready')='ready'
                ORDER BY priority DESC, created_at ASC LIMIT 1
                """,
                (channel,),
            ).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status='running', started_at=?, progress=?, error=NULL,
                    finished_at=NULL, last_attempt_status='running'
                WHERE id=? AND action='scrape' AND cancel_requested=0
                """,
                (utc_now(), f"{channel}: starting", row["id"]),
            )
            if cursor.rowcount == 0:
                return None
            item = dict(row)
            item["status"] = "running"
            item["progress"] = f"{channel}: starting"
            item["started_at"] = utc_now()
            conn.execute(
                """
                UPDATE job_attempts SET status='running', started_at=?, error=NULL
                WHERE job_id=? AND attempt_no=?
                """,
                (item["started_at"], row["id"], int(row["attempt_count"] or 1)),
            )
            self._append_event(
                conn,
                row["id"],
                int(row["attempt_count"] or 1),
                "info",
                "attempt_started",
                f"Started attempt {int(row['attempt_count'] or 1)}",
            )
            return item

    def update(self, job_id, progress):
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status, cancel_requested FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row or row["cancel_requested"] or row["status"] == "cancelling":
                raise CancelRequestedError(f"job {job_id} cancellation requested")
            conn.execute("UPDATE jobs SET progress=? WHERE id=?", (progress, job_id))

    def retire_queued_channel(self, channel, reason):
        """Fail queued attempts when a source worker is formally retired."""
        reason = str(reason or "channel collection retired")
        retired = []
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id,attempt_count,active_result_status
                FROM jobs
                WHERE channel=? AND status='queued' AND action='scrape'
                """,
                (channel,),
            ).fetchall()
            for row in rows:
                attempt_no = int(row["attempt_count"] or 1)
                finished_at = utc_now()
                if row["active_result_status"] in {"completed", "partial_success"}:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status=?, progress=?, error=?, finished_at=?,
                            last_attempt_status='failed'
                        WHERE id=? AND status='queued'
                        """,
                        (
                            row["active_result_status"],
                            (
                                "retired retry; retained "
                                f"{row['active_result_status']} data"
                            ),
                            reason,
                            finished_at,
                            row["id"],
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status='failed', progress='channel collection retired',
                            error=?, finished_at=?, last_attempt_status='failed'
                        WHERE id=? AND status='queued'
                        """,
                        (reason, finished_at, row["id"]),
                    )
                conn.execute(
                    """
                    UPDATE job_attempts
                    SET status='failed', error=?, finished_at=?
                    WHERE job_id=? AND attempt_no=?
                    """,
                    (reason, finished_at, row["id"], attempt_no),
                )
                self._append_event(
                    conn,
                    row["id"],
                    attempt_no,
                    "warning",
                    "channel_collection_retired",
                    reason,
                    {"channel": channel},
                )
                retired.append(row["id"])
        return retired

    def pause_channel(self, channel, retry_after, failure_class, reason):
        """Persist a channel circuit breaker so no worker coroutine sleeps."""
        seconds = max(1, int(retry_after))
        blocked_until = (
            datetime.now(timezone.utc) + timedelta(seconds=seconds)
        ).isoformat()
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_cooldowns(
                    channel,blocked_until,failure_class,reason,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(channel) DO UPDATE SET
                    blocked_until=excluded.blocked_until,
                    failure_class=excluded.failure_class,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (
                    str(channel), blocked_until,
                    str(failure_class or "transport_error"),
                    str(reason or "source access paused"), utc_now(),
                ),
            )
        return blocked_until

    def channel_cooldown(self, channel):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_cooldowns WHERE channel=?", (channel,)
            ).fetchone()
        return dict(row) if row else None

    def requeue(
        self,
        job_id,
        progress,
        error=None,
        *,
        retry_after=None,
        failure_class=None,
    ):
        """Return one running job to its channel queue after a retryable outage."""
        with self.lock, self._connect() as conn:
            job_row = conn.execute(
                "SELECT channel FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            blocked_until = None
            if job_row and retry_after is not None:
                blocked_until = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=max(1, int(retry_after)))
                ).isoformat()
                conn.execute(
                    """
                    INSERT INTO channel_cooldowns(
                        channel,blocked_until,failure_class,reason,updated_at
                    ) VALUES(?,?,?,?,?)
                    ON CONFLICT(channel) DO UPDATE SET
                        blocked_until=excluded.blocked_until,
                        failure_class=excluded.failure_class,
                        reason=excluded.reason,
                        updated_at=excluded.updated_at
                    """,
                    (
                        job_row["channel"], blocked_until,
                        str(failure_class or "transport_error"),
                        str(error or progress), utc_now(),
                    ),
                )
            conn.execute(
                """
                UPDATE jobs
                SET status='queued', progress=?, error=?, started_at=NULL,
                    finished_at=NULL, last_attempt_status='queued'
                WHERE id=? AND status='running'
                """,
                (progress, error, job_id),
            )
            row = conn.execute("SELECT attempt_count FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE job_attempts SET status='queued', error=?
                    WHERE job_id=? AND attempt_no=?
                    """,
                    (error, job_id, int(row["attempt_count"] or 1)),
                )
                self._append_event(
                    conn,
                    job_id,
                    int(row["attempt_count"] or 1),
                    "warning",
                    "source_requeued",
                    progress,
                    {
                        **({"error": error} if error else {}),
                        **(
                            {
                                "failure_class": str(
                                    failure_class or "transport_error"
                                ),
                                "next_retry_at": blocked_until,
                            }
                            if blocked_until
                            else {}
                        ),
                    }
                    if error or blocked_until
                    else None,
                )

    def enrich_metadata(
        self, job_id, brand=None, model=None, category=None, *,
        product_observation=None, voltage_platform=None, task_energy=None,
        source_verified=False, market_region=None,
    ):
        """Fill source metadata and its governed projections during collection."""
        values = {"brand": brand, "model": model, "category": category}
        observation = dict(product_observation or {}) if product_observation else None
        platform = dict(voltage_platform or {}) if voltage_platform else None
        energy = validate_task_energy(task_energy) if task_energy else None
        resolver_context = self._binding_resolver_context()
        with self.lock, resolver_context as resolve_binding, self._task_catalog_context() as task_catalog, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            previous_observation = json.loads(row['product_observation_json'] or '{}')
            protected = set(previous_observation.get('manual_fields') or [])
            if row['product_master_model_id'] or previous_observation.get('evidence_source') in {
                'user_confirmed', 'request', 'manual_edit', 'task_metadata_edit',
                'governed_external_import', 'product_master_exact_match',
            }:
                protected.update({'brand', 'model', 'category', 'energy_type', 'voltage'})

            def replace_from_source(field):
                return source_verified and field not in protected
            unknown_placeholders = {
                "unknown", "unbound", "pending", "待补充", "待确认", "未知",
            }

            def candidate(field, value):
                if not value:
                    return None
                placeholder = " ".join(str(value).split()).strip().casefold()
                field_unknown = set(unknown_placeholders)
                if field == "category":
                    field_unknown.add("unknown category")
                elif field == "brand":
                    field_unknown.update({"unbranded", "unknown brand"})
                if placeholder in field_unknown:
                    return None
                replace_legacy_category = (
                    field == "category"
                    and str(row[field] or "").casefold() in {
                        "gas pressure washer", "electric pressure washer",
                        "cordless pressure washer", "unknown category",
                    }
                )
                if (row[field] or "").strip() and not replace_legacy_category and not replace_from_source(field):
                    return None
                return value

            candidate_brand = candidate("brand", brand)
            candidate_category = candidate("category", category)
            resolved = resolve_binding(candidate_brand, candidate_category)
            if resolved is not None:
                if candidate_brand is not None:
                    values["brand"] = resolved["brand"]
                if candidate_category is not None:
                    values["category"] = resolved["category"]
            updates = {}
            if (replace_from_source('model') and model is None
                    and 'model' in ((observation or {}).get('source_metadata_evidence') or {}).get('gaps', [])):
                updates['model'] = None
            for key, value in values.items():
                replace_legacy_category = (
                    key == "category"
                    and str(row[key] or "").casefold() in {
                        "gas pressure washer", "electric pressure washer",
                        "cordless pressure washer", "unknown category",
                    }
                )
                if not value or ((row[key] or "").strip() and not replace_legacy_category and not replace_from_source(key)):
                    continue
                placeholder = " ".join(str(value).split()).strip().casefold()
                field_unknown_placeholders = set(unknown_placeholders)
                if key == "category":
                    field_unknown_placeholders.add("unknown category")
                elif key == "brand":
                    field_unknown_placeholders.update(
                        {"unbranded", "unknown brand"}
                    )
                elif key == "model":
                    field_unknown_placeholders.add("unknown model")
                if placeholder in field_unknown_placeholders:
                    continue
                if key in {"brand", "category"}:
                    if resolved is None:
                        table = self._master_table(key)
                        canonical = conn.execute(
                            f"SELECT name FROM {table} WHERE name=? COLLATE NOCASE", (self._clean_master_name(value),)
                        ).fetchone()
                        # Keep values governed by master data; a newly recognised
                        # name is surfaced to the user as unselected rather than
                        # silently creating a second vocabulary.
                        if not canonical:
                            continue
                        value = canonical["name"]
                updates[key] = value
            if market_region and market_region != row['market_region']:
                if row['result_json']:
                    raise ValueError('已有评论的来源市场不能在元数据回填时改写')
                updates['market_region'] = market_region
            if energy and protected.intersection({'energy_type', 'voltage'}):
                if (energy.get('energy_type') != row['energy_type'] or
                        energy.get('canonical_voltage') != row['energy_canonical_voltage']):
                    raise ValueError('自动采集不得覆盖已确认的能源或电压平台')
            if energy and energy["readiness_status"] == "ready":
                updates.update({
                    "product_observation_json": json.dumps(
                        observation, ensure_ascii=False, separators=(",", ":")
                    ),
                    "product_observation_hash": observation_hash(observation),
                    "voltage_platform_schema_version": platform.get("schema_version"),
                    "voltage_platform_status": platform.get("status"),
                    "voltage_platform_key": platform.get("platform_key"),
                    "voltage_platform_brand": platform.get("brand"),
                    "voltage_platform_voltage": platform.get("normalized_voltage"),
                    "voltage_platform_label": platform.get("display_name"),
                    "voltage_platform_evidence_source": platform.get("evidence_source"),
                    "voltage_platform_null_reason": platform.get("null_reason"),
                    "energy_schema_version": energy.get("schema_version"),
                    "energy_type": energy.get("energy_type"),
                    "energy_readiness_status": energy.get("readiness_status"),
                    "energy_evidence_source": energy.get("evidence_source"),
                    "energy_canonical_voltage": energy.get("canonical_voltage"),
                    "energy_unknown_reason": energy.get("unknown_reason"),
                })
            if updates:
                clause = ", ".join(f"{key}=?" for key in updates)
                conn.execute(f"UPDATE jobs SET {clause} WHERE id=?", (*updates.values(), job_id))
                self._bind_task(conn, job_id, task_catalog, row)
            updated = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(updated)

    def finish(self, job_id, result):
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT target_key, channel, attempt_count, status, cancel_requested FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if (
                not row
                or row["cancel_requested"]
                or row["status"] == "cancelling"
            ):
                raise CancelRequestedError(
                    f"job {job_id} cancellation requested before completion"
                )
            conn.execute(
                """
                UPDATE jobs
                SET status='completed', progress='completed', result_json=?,
                    error=NULL, finished_at=?, active_attempt_no=?,
                    active_result_status='completed', active_run_path=?,
                    last_attempt_status='completed'
                WHERE id=?
                """,
                (
                    json.dumps(result, ensure_ascii=False), utc_now(),
                    int(row["attempt_count"] or 1),
                    f"{job_id}/{row['channel']}", job_id,
                ),
            )
            finished_at = utc_now()
            conn.execute(
                """
                UPDATE job_attempts
                SET status='completed', result_json=?, error=NULL, finished_at=?
                WHERE job_id=? AND attempt_no=?
                """,
                (json.dumps(result, ensure_ascii=False), finished_at, job_id, int(row["attempt_count"] or 1)),
            )
            self._append_event(conn, job_id, int(row["attempt_count"] or 1), "info", "attempt_completed", "Collection completed")

    def finish_partial(self, job_id, result, reason=None):
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT channel, attempt_count, active_result_status, status, cancel_requested FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if (
                not row
                or row["cancel_requested"]
                or row["status"] == "cancelling"
            ):
                raise CancelRequestedError(
                    f"job {job_id} cancellation requested before partial publication"
                )
            error = str(reason or "source access stopped before full reconciliation")
            attempt_no = int(row["attempt_count"] or 1)
            finished_at = utc_now()
            conn.execute(
                """
                UPDATE job_attempts
                SET status='partial_success', result_json=?, error=?, finished_at=?
                WHERE job_id=? AND attempt_no=?
                """,
                (json.dumps(result, ensure_ascii=False), error, finished_at, job_id, attempt_no),
            )
            if row["active_result_status"] == "completed":
                conn.execute(
                    """
                    UPDATE jobs
                    SET status='completed', progress='retry partial; retained completed data',
                        error=?, last_attempt_status='partial_success'
                    WHERE id=?
                    """,
                    (error, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status='partial_success', progress='partial success',
                        result_json=?, error=?, finished_at=?, active_attempt_no=?,
                        active_result_status='partial_success', active_run_path=?,
                        last_attempt_status='partial_success'
                    WHERE id=?
                    """,
                    (
                        json.dumps(result, ensure_ascii=False), error, finished_at,
                        attempt_no, f"{job_id}/{row['channel']}", job_id,
                    ),
                )
            self._append_event(conn, job_id, attempt_no, "warning", "attempt_partial", error)

    def fail(self, job_id, error):
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT attempt_count, active_result_status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                return
            attempt_no = int(row["attempt_count"] or 1)
            finished_at = utc_now()
            message = str(error)
            failure_class = getattr(error, "failure_class", None)
            conn.execute(
                """
                UPDATE job_attempts SET status='failed', error=?, finished_at=?
                WHERE job_id=? AND attempt_no=?
                """,
                (message, finished_at, job_id, attempt_no),
            )
            if row["active_result_status"] in {"completed", "partial_success"}:
                conn.execute(
                    """
                    UPDATE jobs SET status=?, progress=?, error=?,
                        last_attempt_status='failed'
                    WHERE id=?
                    """,
                    (
                        row["active_result_status"],
                        f"retry failed; retained {row['active_result_status']} data",
                        message,
                        job_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs SET status='failed', progress='failed', error=?,
                        finished_at=?, last_attempt_status='failed'
                    WHERE id=?
                    """,
                    (message, finished_at, job_id),
                )
            self._append_event(
                conn,
                job_id,
                attempt_no,
                "error",
                "attempt_failed",
                message,
                (
                    {"failure_class": str(failure_class)}
                    if failure_class
                    else None
                ),
            )

    def delete_failed_jobs(self):
        """Remove only terminal failed jobs and return their IDs for artifact cleanup."""
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT id FROM jobs WHERE status='failed'").fetchall()
            job_ids = [row["id"] for row in rows]
            if job_ids:
                conn.execute("DELETE FROM jobs WHERE status='failed'")
        return job_ids

    def request_delete(self, job_id):
        """Delete queued work immediately or request cooperative running cancel."""
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            if row["status"] in {"queued", "paused", "cancelled"}:
                if row["active_result_status"] in {"completed", "partial_success"}:
                    conn.execute(
                        """
                        UPDATE job_attempts SET status='cancelled', finished_at=?
                        WHERE job_id=? AND attempt_no=?
                        """,
                        (utc_now(), job_id, int(row["attempt_count"] or 1)),
                    )
                    conn.execute(
                        """
                        UPDATE jobs SET status=?, progress=?, cancel_requested=0,
                            last_attempt_status='cancelled'
                        WHERE id=?
                        """,
                        (
                            row["active_result_status"],
                            f"retry cancelled; retained {row['active_result_status']} data",
                            job_id,
                        ),
                    )
                    self._append_event(
                        conn, job_id, int(row["attempt_count"] or 1), "info",
                        "retry_cancelled", "Queued retry cancelled; retained prior data"
                    )
                    return {"id": job_id, "deleted": False, "cancelling": False, "retained_data": True}
                conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                return {"id": job_id, "deleted": True, "cancelling": False}
            if row["status"] in {"running", "cancelling"}:
                conn.execute(
                    """
                    UPDATE jobs SET status='cancelling', cancel_requested=1,
                        progress='cancellation requested'
                    WHERE id=?
                    """,
                    (job_id,),
                )
                return {"id": job_id, "deleted": False, "cancelling": True}
            raise ValueError("only queued or running jobs can be removed")

    def is_cancel_requested(self, job_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested, status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return bool(row and (row["cancel_requested"] or row["status"] == "cancelling"))

    def delete_cancelled(self, job_id):
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=? AND status IN ('cancelling','cancelled')",
                (job_id,),
            ).fetchone()
            if row:
                attempt_no = int(row["attempt_count"] or 1)
                if row["active_result_status"] in {"completed", "partial_success"}:
                    conn.execute(
                        """
                        UPDATE job_attempts SET status='cancelled', finished_at=?
                        WHERE job_id=? AND attempt_no=?
                        """,
                        (utc_now(), job_id, attempt_no),
                    )
                    conn.execute(
                        """
                        UPDATE jobs SET status=?, progress=?, cancel_requested=0,
                            last_attempt_status='cancelled'
                        WHERE id=?
                        """,
                        (
                            row["active_result_status"],
                            f"retry cancelled; retained {row['active_result_status']} data",
                            job_id,
                        ),
                    )
                    self._append_event(conn, job_id, attempt_no, "info", "retry_cancelled", "Running retry cancelled; retained prior data")
                    return False
                conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return bool(row)

    def delete_stale_cancelled(self):
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status='cancelled' OR cancel_requested=1"
            ).fetchall()
            job_ids = []
            for row in rows:
                if row["active_result_status"] in {"completed", "partial_success"}:
                    conn.execute(
                        """
                        UPDATE jobs SET status=?, progress=?, cancel_requested=0,
                            last_attempt_status='cancelled'
                        WHERE id=?
                        """,
                        (
                            row["active_result_status"],
                            f"retry cancelled; retained {row['active_result_status']} data",
                            row["id"],
                        ),
                    )
                else:
                    job_ids.append(row["id"])
                    conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
        return job_ids

    def requeue_paused(self, channel=None):
        params = []
        channel_clause = ""
        if channel:
            channel_clause = " AND channel=?"
            params.append(channel)
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE jobs
                SET status='queued', progress='queued after bounded connector upgrade',
                    error=NULL, started_at=NULL, finished_at=NULL,
                    cancel_requested=0
                WHERE status='paused' {channel_clause}
                """,
                params,
            )
        return cursor.rowcount

    def update_job(
        self,
        job_id,
        brand=None,
        model=None,
        category=None,
        priority=None,
        market_region=None,
        source_product_id=None,
        product_observation=None,
        voltage_platform=None,
        task_energy=None,
    ):
        fields = {
            "brand": brand,
            "model": model,
            "category": category,
            "priority": priority,
            "market_region": market_region,
            "source_product_id": source_product_id,
        }
        requested = {key: value for key, value in fields.items() if value is not None}
        observation = dict(product_observation or {}) if product_observation is not None else None
        platform = dict(voltage_platform or {}) if voltage_platform is not None else None
        energy = validate_task_energy(task_energy) if task_energy is not None else None
        binding_context = self._active_binding_context(
            requested.get("brand"), requested.get("category")
        )
        with self.lock, binding_context as resolved, self._task_catalog_context() as task_catalog, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            if "brand" in requested:
                if resolved is not None:
                    requested["brand"] = resolved["brand"]
            if "category" in requested:
                if resolved is not None:
                    requested["category"] = resolved["category"]
            if "priority" in requested and row["status"] != "queued":
                raise ValueError("only queued jobs can be reprioritized")
            if (
                "source_product_id" in requested
                and row["status"] != "queued"
            ):
                raise ValueError(
                    "only queued jobs can change source_product_id"
                )
            if observation is not None or "category" in requested:
                form = observation_form(requested.get("category", row["category"]), observation, row)
                requested.update(form_columns(form))
                if "category" in requested:
                    requested["category"] = family_category(requested["category"])
            metadata_changed = observation is not None or energy is not None or any(
                key != "priority" and row[key] != value for key, value in requested.items()
            )
            if metadata_changed and row["status"] in {"running", "cancelling"}:
                raise ValueError("任务正在采集或取消中，请结束后再编辑产品信息")
            if (
                "market_region" in requested
                and requested["market_region"] != row["market_region"]
                and row["active_result_status"] in {"completed", "partial_success"}
            ):
                raise ValueError("已有评论结果的市场地区不能单独修改，请使用经过验证的数据修正流程")
            if observation is not None:
                requested.update({
                    "product_observation_json": json.dumps(
                        observation, ensure_ascii=False, separators=(",", ":")
                    ),
                    "product_observation_hash": observation_hash(observation),
                })
            if platform is not None:
                requested.update({
                    "voltage_platform_schema_version": platform.get("schema_version"),
                    "voltage_platform_status": platform.get("status"),
                    "voltage_platform_key": platform.get("platform_key"),
                    "voltage_platform_brand": platform.get("brand"),
                    "voltage_platform_voltage": platform.get("normalized_voltage"),
                    "voltage_platform_label": platform.get("display_name"),
                    "voltage_platform_evidence_source": platform.get("evidence_source"),
                    "voltage_platform_null_reason": platform.get("null_reason"),
                })
            if energy is not None:
                requested.update({
                    "energy_schema_version": energy.get("schema_version"),
                    "energy_type": energy.get("energy_type"),
                    "energy_readiness_status": energy.get("readiness_status"),
                    "energy_evidence_source": energy.get("evidence_source"),
                    "energy_canonical_voltage": energy.get("canonical_voltage"),
                    "energy_unknown_reason": energy.get("unknown_reason"),
                })
                if row["status"] == "queued" and row["action"] == "scrape":
                    requested["progress"] = (
                        "queued" if energy["readiness_status"] == "ready"
                        else "metadata_incomplete: energy metadata required before collection"
                    )
            requested = {key: value for key, value in requested.items() if row[key] != value}
            if requested:
                set_clause = ", ".join(f"{name}=?" for name in requested)
                conn.execute(f"UPDATE jobs SET {set_clause} WHERE id=?", (*requested.values(), job_id))
                self._bind_task(conn, job_id, task_catalog, row)
                changed = {
                    key: {"from": row[key], "to": value}
                    for key, value in requested.items()
                    if key != "priority" and row[key] != value
                }
                if changed:
                    self._append_event(
                        conn,
                        job_id,
                        int(row["attempt_count"] or 1),
                        "info",
                        "task_metadata_updated",
                        "Task metadata updated",
                        {"changes": changed},
                    )
            updated = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(updated)

    def record_source_product_identity(self, job_id, identity):
        """Persist worker-proved identity without changing the task target.

        A resolved identity is immutable. Replaying the same official evidence
        is a no-op; a different ID fails closed before collection can mix two
        products under one stable task.
        """
        identity = validate_source_product_identity(identity)
        if identity["status"] != "resolved":
            raise ValueError("worker source product identity must be resolved")
        encoded = json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        identity_hash = source_product_identity_hash(identity)
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                return None
            existing_id = str(row["source_product_id"] or "").strip()
            if existing_id and existing_id != identity["source_product_id"]:
                raise ValueError(
                    "source product identity conflict: stable task already "
                    "belongs to a different source product"
                )
            if (
                existing_id == identity["source_product_id"]
                and row["source_product_identity_hash"] == identity_hash
            ):
                return self._row(row)
            conn.execute(
                """
                UPDATE jobs SET
                    source_product_id=?, source_product_identity_status=?,
                    source_product_identity_json=?,
                    source_product_identity_hash=?,
                    source_product_identity_evidence_source=?
                WHERE id=?
                """,
                (
                    identity["source_product_id"], identity["status"], encoded,
                    identity_hash, identity["evidence_source"], job_id,
                ),
            )
            self._append_event(
                conn,
                job_id,
                int(row["attempt_count"] or 1),
                "info",
                "source_product_identity_resolved",
                "Source product identity resolved from official evidence",
                {
                    "schema_version": identity["schema_version"],
                    "source_product_id": identity["source_product_id"],
                    "evidence_source": identity["evidence_source"],
                    "identity_hash": identity_hash,
                },
            )
            updated = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return self._row(updated)

    def acquisition_batch_readiness(self, settle_seconds=600, now=None):
        """Expose auditable, read-only readiness for structured-data refresh.

        Only explicitly batched work is returned. Historic tasks are not
        silently adopted, and an external-import cache remains pending until
        its validated import completes under the same stable task ID.
        """
        settle_seconds = max(600, int(settle_seconds))
        current = now or datetime.now(timezone.utc)
        if isinstance(current, str):
            current = datetime.fromisoformat(current.replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT j.id, j.acquisition_batch_id, j.status,
                       j.active_result_status, j.last_attempt_status, j.action,
                       j.created_at, j.finished_at, j.started_at, j.channel,
                       j.target_key, j.brand, j.model, j.category, j.market_region,
                       j.energy_schema_version, j.energy_type,
                       j.energy_readiness_status, j.energy_canonical_voltage,
                       j.voltage_platform_key, j.active_attempt_no,
                       j.active_run_path, j.result_json,
                       COALESCE(MAX(e.created_at), j.finished_at,
                                j.started_at, j.created_at) AS last_activity_at
                FROM jobs j
                LEFT JOIN job_events e ON e.job_id=j.id
                WHERE COALESCE(j.acquisition_batch_id, '')<>''
                  AND j.superseded_by IS NULL
                GROUP BY j.id
                ORDER BY j.acquisition_batch_id, j.created_at, j.id
                """
            ).fetchall()
        grouped = {}
        terminal = {"completed", "partial_success", "failed", "cancelled"}
        usable = {"completed", "partial_success"}
        for row in rows:
            item = dict(row)
            grouped.setdefault(item["acquisition_batch_id"], []).append(item)
        batches = []
        for batch_id, jobs in grouped.items():
            pending_jobs = [job for job in jobs if job["status"] not in terminal]
            # Retries keep the previous usable result for existing readers.
            # Refresh readiness counts only terminal work; otherwise a queued
            # retry can invalidate the complete batch-readiness response.
            usable_jobs = [
                job for job in jobs
                if job["status"] in terminal and job["active_result_status"] in usable
            ]
            activity_values = [
                datetime.fromisoformat(
                    str(job["last_activity_at"]).replace("Z", "+00:00")
                )
                for job in jobs
            ]
            last_activity = max(activity_values)
            ready_at = last_activity + timedelta(seconds=settle_seconds)
            if pending_jobs:
                status = "pending"
            elif current < ready_at:
                status = "settling"
            elif usable_jobs:
                status = "ready"
            else:
                status = "terminal_no_usable_results"
            fingerprint_rows = []
            for job in sorted(usable_jobs, key=lambda value: value["id"]):
                fingerprint_rows.append(
                    {
                        "id": job["id"],
                        "channel": job["channel"],
                        "target_key": job["target_key"],
                        "brand": job["brand"],
                        "model": job["model"],
                        "category": job["category"],
                        "market_region": job["market_region"],
                        "energy_schema_version": job["energy_schema_version"],
                        "energy_type": job["energy_type"],
                        "energy_readiness_status": job["energy_readiness_status"],
                        "energy_canonical_voltage": job["energy_canonical_voltage"],
                        "voltage_platform_key": job["voltage_platform_key"],
                        "active_attempt_no": job["active_attempt_no"],
                        "active_result_status": job["active_result_status"],
                        "active_run_path": job["active_run_path"],
                        "result_sha256": hashlib.sha256(
                            (job["result_json"] or "").encode("utf-8")
                        ).hexdigest(),
                    }
                )
            fingerprint = (
                hashlib.sha256(
                    json.dumps(
                        fingerprint_rows,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if fingerprint_rows
                else None
            )
            batches.append(
                {
                    "batch_id": batch_id,
                    "created_at": min(job["created_at"] for job in jobs),
                    "last_activity_at": last_activity.isoformat(),
                    "ready_at": ready_at.isoformat(),
                    "settle_seconds": settle_seconds,
                    "status": status,
                    "job_ids": sorted(job["id"] for job in jobs),
                    "usable_job_ids": sorted(job["id"] for job in usable_jobs),
                    "job_count": len(jobs),
                    "pending_count": len(pending_jobs),
                    "external_import_pending_count": sum(
                        1
                        for job in pending_jobs
                        if job["action"] == EXTERNAL_IMPORT_ACTION
                    ),
                    "terminal_count": len(jobs) - len(pending_jobs),
                    "usable_result_count": len(usable_jobs),
                    "failed_count": sum(
                        1 for job in jobs if job["last_attempt_status"] == "failed"
                    ),
                    "cancelled_count": sum(
                        1 for job in jobs if job["status"] == "cancelled"
                    ),
                    "result_fingerprint": fingerprint,
                }
            )
        return sorted(
            batches,
            key=lambda batch: (batch["last_activity_at"], batch["batch_id"]),
        )

    def promote(self, job_id):
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            if row["status"] != "queued":
                raise ValueError("only queued jobs can be promoted")
            highest = conn.execute(
                "SELECT COALESCE(MAX(priority), 0) AS value FROM jobs WHERE channel=? AND status='queued'",
                (row["channel"],),
            ).fetchone()["value"]
            conn.execute("UPDATE jobs SET priority=? WHERE id=?", (highest + 1, job_id))
            updated = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(updated)

    def stats(self, by):
        if by not in {"channel", "brand", "category"}:
            raise ValueError("by must be channel, brand or category")
        with self._connect() as conn:
            # A product may be recollected after a channel adapter improvement.
            # Keep only its latest completed run in aggregate statistics so the
            # overview never inflates real review coverage by counting retries.
            rows = conn.execute(
                """
                SELECT id, target_url, brand, category, result_json FROM jobs
                WHERE status IN ('completed','partial_success')
                  AND result_json IS NOT NULL
                  AND superseded_by IS NULL
                ORDER BY CASE status WHEN 'completed' THEN 0 ELSE 1 END,
                         COALESCE(finished_at, created_at) DESC
                """
            ).fetchall()
        groups = {}
        seen_products = set()
        for row in rows:
            # These aggregates need result counts and product identity only;
            # decoding unrelated task metadata amplifies concurrent read cost.
            item = dict(row)
            result = (json.loads(item["result_json"]) if item["result_json"] else {}) or {}
            for report_channel, report in result.items():
                if not isinstance(report, dict):
                    continue
                reviews = int(report.get("unique_reviews") or 0)
                if not reviews:
                    continue
                product = report.get("product") or {}
                product_key = f"{report_channel}:{product.get('id') or item.get('target_url') or item['id']}"
                if product_key in seen_products:
                    continue
                seen_products.add(product_key)
                if by == "channel":
                    name = report_channel
                else:
                    name = item.get(by) or self._infer_dimension(by, report) or "待补充"
                group = groups.setdefault(name, {"name": name, "review_count": 0, "product_ids": set()})
                group["review_count"] += reviews
                group["product_ids"].add(product_key)
        return [
            {"name": group["name"], "review_count": group["review_count"], "product_count": len(group["product_ids"])}
            for group in sorted(groups.values(), key=lambda value: value["review_count"], reverse=True)
        ]

    @staticmethod
    def _summary_identity_token(value):
        return "".join(character for character in str(value or "").casefold() if character.isalnum())

    def _summary_product_identity(self, job, report):
        """Return the strongest stable product/model identity available.

        The connector task list predates the shared Chengmu Model ID.  Current
        source product IDs remain stronger than human-readable model strings;
        legacy model fallback is explicitly brand-scoped and reported to the
        caller so it cannot be mistaken for a fully governed identity.
        """
        master_model_id = str(job.get("product_master_model_id") or "").strip()
        if master_model_id:
            return f"chengmu-model:{master_model_id}", False
        product = report.get("product") if isinstance(report.get("product"), dict) else {}
        product_id = self._summary_identity_token(product.get("id"))
        if product_id and product_id not in {"unknown", "none", "null"}:
            return f"source-product:{product_id}", False
        source_product_id = self._summary_identity_token(job.get("source_product_id"))
        if source_product_id:
            return f"source-product:{source_product_id}", False
        brand = self._summary_identity_token(job.get("brand")) or "unknown-brand"
        model = self._summary_identity_token(product.get("model") or job.get("model"))
        if model and model != "unknown":
            return f"brand-model:{brand}:{model}", True
        target = canonical_target_url(job.get("target_key") or job.get("target_url"))
        return f"target:{target or job['id']}", True

    def _summary_active_validation(self, job):
        """Read and reconcile one current validation artifact, fail closed.

        SQLite fields are pointers and display caches, not the source of truth
        for reportability or counts.  Only the stable task's published root is
        accepted; archived attempts and paths outside DATA_DIR/runs are never
        eligible for a filtered summary.
        """
        active_status = str(job.get("active_result_status") or "")
        if active_status not in {"completed", "partial_success"}:
            return None, "no_active_result"
        try:
            active_attempt_no = int(job.get("active_attempt_no"))
        except (TypeError, ValueError):
            return None, "active_path_invalid"
        if active_attempt_no < 1:
            return None, "active_path_invalid"

        expected_run_path = f"{job['id']}/{job['channel']}"
        active_run_path = str(job.get("active_run_path") or "").strip()
        if active_run_path != expected_run_path:
            return None, "active_path_invalid"
        try:
            runs_root = (self.data_dir / "runs").resolve()
            validation_path = (
                runs_root / active_run_path / "validation_report.json"
            ).resolve()
            validation_path.relative_to(runs_root)
        except (OSError, RuntimeError, ValueError):
            return None, "active_path_invalid"
        if not validation_path.is_file():
            return None, "validation_missing"
        try:
            if validation_path.stat().st_size > 5_000_000:
                return None, "validation_unreadable"
            physical = json.loads(validation_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, "validation_unreadable"
        if not isinstance(physical, dict):
            return None, "validation_unreadable"

        physical_status = str(physical.get("status") or "")
        tier_by_status = {
            "COMPLETE": "complete",
            "COMPLETE_FALLBACK_SOURCE": "complete",
            "PARTIAL_SUCCESS": "partial",
        }
        tier = tier_by_status.get(physical_status)
        if not tier:
            return None, "validation_unreportable"
        reviews = physical.get("unique_reviews")
        if isinstance(reviews, bool) or not isinstance(reviews, int):
            return None, "validation_unreadable"
        if reviews < 0:
            return None, "validation_unreadable"

        expected_active_status = "completed" if tier == "complete" else "partial_success"
        if active_status != expected_active_status:
            return None, "validation_mismatch"
        try:
            cached_result = json.loads(job.get("result_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return None, "validation_mismatch"
        physical_channel = str(physical.get("channel") or job.get("channel") or "").strip()
        if physical_channel != str(job.get("channel") or "") or not isinstance(cached_result, dict):
            return None, "validation_mismatch"
        cached_report = cached_result.get(physical_channel)
        if not isinstance(cached_report, dict):
            return None, "validation_mismatch"
        cached_reviews = cached_report.get("unique_reviews")
        if isinstance(cached_reviews, bool) or not isinstance(cached_reviews, int):
            return None, "validation_mismatch"
        if (
            str(cached_report.get("status") or "") != physical_status
            or cached_reviews != reviews
        ):
            return None, "validation_mismatch"
        return {
            "report": physical,
            "report_channel": physical_channel,
            "tier": tier,
            "reviews": reviews,
        }, None

    def summarize_job_scope(self, job_ids):
        """Summarize the current effective review population for stable jobs.

        The caller supplies the stable IDs selected by the task filters.  This
        method owns all result selection and counting: superseded tasks are
        excluded, current reportable results are grouped by report channel and
        product/model identity, complete results outrank partial results, and a
        newer result only wins within the same completeness tier.
        """
        requested_ids = {
            str(value).strip()
            for value in (job_ids or [])
            if str(value or "").strip()
        }
        if len(requested_ids) > 5000:
            raise ValueError("job summary accepts at most 5000 stable task IDs")
        with self._connect() as conn:
            all_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC, id DESC"
                ).fetchall()
                if row["id"] in requested_ids
            ]
        existing_ids = {row["id"] for row in all_rows}
        superseded_rows = [row for row in all_rows if row.get("superseded_by")]
        current_rows = [row for row in all_rows if not row.get("superseded_by")]
        selected = {}
        valid_result_tasks = set()
        candidate_result_count = 0
        identity_fallback_count = 0
        audit_counts = {
            "no_active_result": 0,
            "active_path_invalid": 0,
            "validation_missing": 0,
            "validation_unreadable": 0,
            "validation_unreportable": 0,
            "validation_mismatch": 0,
        }
        for job in current_rows:
            verified, failure = self._summary_active_validation(job)
            if failure:
                audit_counts[failure] += 1
                continue
            report = verified["report"]
            report_channel = verified["report_channel"]
            tier = verified["tier"]
            reviews = verified["reviews"]
            identity, used_fallback = self._summary_product_identity(job, report)
            key = (str(report_channel).casefold(), identity)
            candidate_result_count += 1
            identity_fallback_count += int(used_fallback)
            valid_result_tasks.add(job["id"])
            candidate = {
                "job_id": job["id"],
                "report_channel": report_channel,
                "identity": identity,
                "tier": tier,
                "reviews": reviews,
                "finished_at": str(job.get("finished_at") or job.get("created_at") or ""),
            }
            previous = selected.get(key)
            if previous is None:
                selected[key] = candidate
                continue
            candidate_rank = 0 if tier == "complete" else 1
            previous_rank = 0 if previous["tier"] == "complete" else 1
            if candidate_rank < previous_rank or (
                candidate_rank == previous_rank
                and (candidate["finished_at"], candidate["job_id"])
                > (previous["finished_at"], previous["job_id"])
            ):
                selected[key] = candidate
        selected_rows = list(selected.values())
        unusable_result_task_count = len(current_rows) - len(valid_result_tasks)
        has_partial_fallback = any(
            row["tier"] == "partial" for row in selected_rows
        )
        if not current_rows:
            summary_status = "empty_scope"
        elif not selected_rows:
            summary_status = "unavailable"
        elif unusable_result_task_count or has_partial_fallback:
            summary_status = "partial"
        else:
            summary_status = "available"
        effective_review_count = (
            None
            if summary_status == "unavailable"
            else sum(row["reviews"] for row in selected_rows)
        )
        return {
            "schema_version": "voxquarry.job-filter-summary/v1",
            "summary_status": summary_status,
            "task_count": len(current_rows),
            "requested_job_count": len(requested_ids),
            "missing_job_count": len(requested_ids - existing_ids),
            "superseded_task_count": len(superseded_rows),
            "effective_review_count": effective_review_count,
            "selected_result_count": len(selected_rows),
            "complete_result_count": sum(
                1 for row in selected_rows if row["tier"] == "complete"
            ),
            "partial_fallback_result_count": sum(
                1 for row in selected_rows if row["tier"] == "partial"
            ),
            "duplicate_result_count": candidate_result_count - len(selected_rows),
            "unusable_result_task_count": unusable_result_task_count,
            "no_active_result_task_count": audit_counts["no_active_result"],
            "active_path_invalid_task_count": audit_counts["active_path_invalid"],
            "validation_missing_task_count": audit_counts["validation_missing"],
            "validation_unreadable_task_count": audit_counts["validation_unreadable"],
            "validation_unreportable_task_count": audit_counts["validation_unreportable"],
            "validation_mismatch_task_count": audit_counts["validation_mismatch"],
            "identity_fallback_count": identity_fallback_count,
            "selection_policy": "complete-first-partial-fallback/1.2.0",
            "identity_policy": "report-channel-product-model-identity/1.0.0",
        }

    @staticmethod
    def _infer_dimension(by, report):
        product = report.get("product") or {}
        title = f"{product.get('title', '')} {product.get('url', '')}".lower()
        if by == "brand":
            for brand in (
                "ego", "ryobi", "milwaukee", "dewalt", "greenworks",
                "kobalt", "toro", "craftsman",
            ):
                if brand in title:
                    if brand in {"ego", "dewalt"}:
                        return brand.upper()
                    return brand.title()
        if by == "category":
            mapping = (
                ("风扇", ("jobsite fan", "jobsite-fan", "brushless fan", "brushless-fan")),
                ("割草机", ("mower", "lawn-mower", "lawn mower")),
                ("吹风机", ("blower",)),
                ("打草机", ("trimmer",)),
                ("链锯", ("chainsaw", "chain-saw")),
                ("充气泵", ("inflator", "air-inflator", "air inflator", "compressor")),
            )
            for category, markers in mapping:
                if any(marker in title for marker in markers):
                    return category
        return None

    def list_feedback(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM feedback ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(row) for row in rows]

    def create_feedback(self, feedback_id, author, tag, content):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO feedback (id,author,tag,content,likes,created_at) VALUES(?,?,?,?,?,?)",
                (feedback_id, author, tag, content, 0, utc_now()),
            )
            row = conn.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,)).fetchone()
        return dict(row)

    def like_feedback(self, feedback_id):
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE feedback SET likes=likes+1 WHERE id=?", (feedback_id,))
            updated = conn.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,)).fetchone()
        return dict(updated)

    @staticmethod
    def _jobs_scope_token(conn):
        jobs = conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(MAX(created_at), '') AS newest FROM jobs"
        ).fetchone()
        revision = conn.execute(
            "SELECT revision FROM job_scope_revision WHERE id=1"
        ).fetchone()
        payload = json.dumps(
            {
                "job_count": int(jobs["count"]),
                "job_scope_revision": int(revision["revision"]),
                "newest_job_created_at": str(jobs["newest"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def list_jobs_page(self, limit=JOBS_PAGE_DEFAULT_LIMIT, cursor=None, filters=None, *, metadata_only=False):
        """Return one bounded keyset page from a mutation-guarded task scope."""
        fields = {'brands': 'brand', 'models': 'model', 'channels': 'channel',
                  'categories': 'category', 'job_ids': 'id', 'market_regions': 'market_region'}
        filters = filters or {}
        if (not isinstance(filters, dict) or set(filters) - fields.keys()
                or any(not isinstance(v, list) or len(v) > 200 or any(not isinstance(x, str) or not x or len(x) > 160 for x in v) for v in filters.values())):
            raise ValueError('invalid task catalog filters')
        normalize = lambda value: ' '.join(unicodedata.normalize('NFKC', str(value or '')).split()).casefold()
        filters = {k: sorted({normalize(v) if k == 'categories' else v if k == 'job_ids' else v.casefold()
                             for v in values}) for k, values in filters.items() if values}
        try:
            page_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("stable task page limit must be an integer") from exc
        if page_limit < 1 or page_limit > JOBS_PAGE_MAX_LIMIT:
            raise ValueError(
                f"stable task page limit must be between 1 and {JOBS_PAGE_MAX_LIMIT}"
            )
        decoded_cursor = _decode_jobs_cursor(cursor) if cursor else None
        with self._connect() as conn:
            conn.create_function('research_category', 1, normalize, deterministic=True)
            scope_token = self._jobs_scope_token(conn)
            if filters:
                scope_token = 'sha256:' + hashlib.sha256(json.dumps([scope_token, filters], sort_keys=True).encode()).hexdigest()
            if (
                decoded_cursor
                and decoded_cursor["scope_token"] != scope_token
            ):
                raise ValueError("stable task scope changed; restart pagination")
            parameters = []
            clauses = []
            for key, values in sorted(filters.items()):
                expression = fields[key] if key == 'job_ids' else (
                    'research_category(category)' if key == 'categories' else 'LOWER(' + fields[key] + ')')
                clauses.append(expression + ' IN (' + ','.join('?' for _ in values) + ')')
                parameters.extend(values)
            where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
            total_count = int(conn.execute('SELECT COUNT(*) AS count FROM jobs ' + where, parameters).fetchone()['count'])
            if decoded_cursor:
                clauses.append('(created_at < ? OR (created_at = ? AND id < ?))')
                parameters.extend(
                    [
                        decoded_cursor["created_at"],
                        decoded_cursor["created_at"],
                        decoded_cursor["id"],
                    ]
                )
            where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
            parameters.append(page_limit + 1)
            projection = '*'
            if metadata_only:
                projection = ','.join('"' + row[1].replace('"', '""') + '"' for row in conn.execute('PRAGMA table_info(jobs)')
                                      if row[1] != 'result_json')
            rows = conn.execute(
                f"""
                SELECT {projection} FROM jobs
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        has_more = len(rows) > page_limit
        selected_rows = rows[:page_limit]
        next_cursor = None
        if has_more and selected_rows:
            last = selected_rows[-1]
            next_cursor = _encode_jobs_cursor(
                last["created_at"], last["id"], scope_token
            )
        items = [self._row(row) for row in selected_rows]
        return {
            "schema_version": JOBS_PAGE_SCHEMA_VERSION,
            "items": items,
            "page_size": page_limit,
            "returned_count": len(items),
            "total_count": total_count,
            "scope_token": scope_token,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    def iter_jobs(self, page_size=JOBS_PAGE_MAX_LIMIT):
        """Iterate the complete stable scope without an unbounded result set."""
        cursor = None
        while True:
            page = self.list_jobs_page(limit=page_size, cursor=cursor)
            yield from page["items"]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]

    def list_all_jobs(self):
        """Return every task for bounded startup migrations, not UI paging."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_attempts(self, job_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_no DESC",
                (job_id,),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            if item.get("result_json"):
                item["result"] = json.loads(item.pop("result_json"))
            else:
                item.pop("result_json", None)
            values.append(item)
        return values

    def list_events(self, job_id, limit=200):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM job_events WHERE job_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (job_id, int(limit)),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            if item.get("detail_json"):
                item["detail"] = json.loads(item.pop("detail_json"))
            else:
                item.pop("detail_json", None)
            values.append(item)
        return values

    def count_events(self, job_id, code):
        """Count durable job events used by bounded retry policies."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM job_events
                WHERE job_id=? AND code=?
                """,
                (job_id, str(code)),
            ).fetchone()
        return int(row["count"] if row else 0)

    def list_failed_jobs(self):
        """Return every failed job for one-time artifact recovery and audits."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status='failed' ORDER BY created_at DESC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def get_job(self, job_id, *, metadata_only=False):
        with self._connect() as conn:
            projection = '*'
            if metadata_only:
                projection = ','.join('"' + row[1].replace('"', '""') + '"' for row in conn.execute('PRAGMA table_info(jobs)')
                                      if row[1] != 'result_json')
            row = conn.execute("SELECT " + projection + " FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row):
        if not row:
            return None
        item = dict(row)
        if item.get("result_json"):
            item["result"] = json.loads(item.pop("result_json"))
        else:
            item.pop("result_json", None)
        if item.get("product_master_record_json"):
            item["product_master_record"] = json.loads(item.pop("product_master_record_json"))
        else:
            item.pop("product_master_record_json", None)
        if item.get("product_observation_json"):
            item["product_observation"] = json.loads(
                item.pop("product_observation_json")
            )
        else:
            item.pop("product_observation_json", None)
        if item.get("source_product_identity_json"):
            item["source_product_identity"] = json.loads(
                item.pop("source_product_identity_json")
            )
        else:
            item.pop("source_product_identity_json", None)
            item["source_product_identity"] = None
        item["source_product_identity_status"] = (
            item.get("source_product_identity_status")
            or ("resolved" if item.get("source_product_id") else "pending")
        )
        if item.get("product_quality_flags_json"):
            item["product_quality_flags"] = json.loads(
                item.pop("product_quality_flags_json")
            )
        else:
            item.pop("product_quality_flags_json", None)
            item["product_quality_flags"] = []
        item["product_master_binding_status"] = (
            "bound"
            if item.get("product_master_model_id")
            else "unbound"
            if item.get("product_observation")
            else "legacy_unbound"
        )
        item["product_enrichment_status"] = (
            item.get("product_enrichment_status")
            or ("enriched" if item.get("product_master_model_id") else "unknown")
        )
        item["voltage_platform_status"] = (
            item.get("voltage_platform_status") or "unknown"
        )
        return item
