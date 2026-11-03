CREATE TABLE IF NOT EXISTS catalog_consumer_scopes (
    scope TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('inactive', 'active')),
    reviewed_plan_sha256 TEXT,
    legacy_master_content_hash TEXT,
    activated_catalog_revision INTEGER,
    activated_catalog_content_hash TEXT,
    activated_at TEXT,
    activated_by TEXT,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO catalog_consumer_scopes(
    scope,status,updated_at
) VALUES('legacy_brand_category_projection','inactive','1970-01-01T00:00:00+00:00');

CREATE TABLE IF NOT EXISTS catalog_owned_legacy_projection (
    entity_id TEXT PRIMARY KEY REFERENCES master_entities(id)
);

CREATE TABLE IF NOT EXISTS legacy_projection_compat_snapshot (
    entity_id TEXT PRIMARY KEY REFERENCES master_entities(id),
    source_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    visible_when_inactive INTEGER NOT NULL DEFAULT 0
        CHECK(visible_when_inactive IN (0,1))
);

CREATE TABLE IF NOT EXISTS master_import_receipts (
    plan_sha256 TEXT PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL,
    legacy_copy_sha256 TEXT NOT NULL,
    legacy_master_content_hash TEXT NOT NULL,
    base_catalog_revision INTEGER NOT NULL,
    base_catalog_content_hash TEXT NOT NULL,
    resulting_catalog_revision INTEGER NOT NULL,
    resulting_catalog_content_hash TEXT NOT NULL,
    unresolved_json TEXT NOT NULL,
    reconcile_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS master_import_receipts_no_update
BEFORE UPDATE ON master_import_receipts
BEGIN
    SELECT RAISE(ABORT, 'master import receipts are append-only');
END;

CREATE TRIGGER IF NOT EXISTS master_import_receipts_no_delete
BEFORE DELETE ON master_import_receipts
BEGIN
    SELECT RAISE(ABORT, 'master import receipts are append-only');
END;

CREATE TABLE IF NOT EXISTS catalog_scope_events (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('activate', 'deactivate')),
    actor TEXT NOT NULL,
    plan_sha256 TEXT,
    legacy_master_content_hash TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_job_rebind_intents (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('brands', 'categories')),
    entity_id TEXT NOT NULL REFERENCES master_entities(id),
    old_name TEXT NOT NULL,
    new_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TRIGGER IF NOT EXISTS catalog_scope_events_no_update
BEFORE UPDATE ON catalog_scope_events
BEGIN
    SELECT RAISE(ABORT, 'catalog scope events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS catalog_scope_events_no_delete
BEFORE DELETE ON catalog_scope_events
BEGIN
    SELECT RAISE(ABORT, 'catalog scope events are append-only');
END;
