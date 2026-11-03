PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS catalog_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    content_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS master_entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    code TEXT NOT NULL,
    normalized_code TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    name_zh TEXT,
    definition TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    replacement_id TEXT REFERENCES master_entities(id),
    parent_id TEXT REFERENCES master_entities(id),
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    UNIQUE(entity_type, normalized_code),
    UNIQUE(entity_type, normalized_name),
    CHECK (replacement_id IS NULL OR replacement_id != id),
    CHECK (parent_id IS NULL OR parent_id != id)
);

CREATE INDEX IF NOT EXISTS idx_master_entities_type_active_name
ON master_entities(entity_type, active, normalized_name);

CREATE INDEX IF NOT EXISTS idx_master_entities_parent
ON master_entities(entity_type, parent_id, sort_order, normalized_name);

CREATE TABLE IF NOT EXISTS master_aliases (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES master_entities(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(entity_type, normalized_value)
);

CREATE INDEX IF NOT EXISTS idx_master_aliases_entity
ON master_aliases(entity_id, normalized_value);

CREATE TABLE IF NOT EXISTS master_source_mappings (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES master_entities(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_value TEXT NOT NULL,
    normalized_source_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(entity_type, source_system, normalized_source_value)
);

CREATE INDEX IF NOT EXISTS idx_master_source_mappings_entity
ON master_source_mappings(entity_id, source_system);

CREATE TABLE IF NOT EXISTS legacy_projection (
    entity_id TEXT PRIMARY KEY REFERENCES master_entities(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('brands', 'categories')),
    legacy_integer_id INTEGER NOT NULL,
    UNIQUE(entity_type, legacy_integer_id)
);

CREATE TABLE IF NOT EXISTS catalog_revisions (
    revision INTEGER PRIMARY KEY CHECK (revision >= 0),
    content_hash TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS master_audit_events (
    id TEXT PRIMARY KEY,
    catalog_revision INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_revision INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_master_audit_entity_time
ON master_audit_events(entity_type, entity_id, catalog_revision DESC);

CREATE INDEX IF NOT EXISTS idx_master_audit_time
ON master_audit_events(catalog_revision DESC);

CREATE TRIGGER IF NOT EXISTS master_audit_events_no_update
BEFORE UPDATE ON master_audit_events
BEGIN
    SELECT RAISE(ABORT, 'master audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS master_audit_events_no_delete
BEFORE DELETE ON master_audit_events
BEGIN
    SELECT RAISE(ABORT, 'master audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS catalog_revisions_no_update
BEFORE UPDATE ON catalog_revisions
BEGIN
    SELECT RAISE(ABORT, 'catalog revisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS catalog_revisions_no_delete
BEFORE DELETE ON catalog_revisions
BEGIN
    SELECT RAISE(ABORT, 'catalog revisions are append-only');
END;
