ALTER TABLE categories ADD COLUMN registry_status TEXT NOT NULL DEFAULT 'approved'
  CHECK(registry_status IN ('pending_review', 'approved', 'retired'));
ALTER TABLE categories ADD COLUMN registry_version TEXT NOT NULL DEFAULT 'category-registry/v1';
ALTER TABLE categories ADD COLUMN updated_at TEXT;

CREATE TABLE IF NOT EXISTS category_catalog_imports (
    manifest_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    source_snapshot_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    manifest_content_hash TEXT NOT NULL UNIQUE,
    expected_category_count INTEGER NOT NULL CHECK(expected_category_count > 0),
    imported_category_count INTEGER NOT NULL CHECK(imported_category_count > 0),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_source_mappings (
    source_system TEXT NOT NULL,
    source_category_id TEXT NOT NULL,
    category_id TEXT NOT NULL REFERENCES categories(id),
    source_name TEXT NOT NULL,
    source_name_key TEXT NOT NULL,
    source_created_at TEXT NOT NULL,
    source_updated_at TEXT NOT NULL,
    source_record_json TEXT NOT NULL,
    source_snapshot_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    manifest_id TEXT REFERENCES category_catalog_imports(manifest_id),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_system, source_category_id),
    UNIQUE(source_system, source_name_key)
);

CREATE INDEX IF NOT EXISTS idx_category_source_mappings_category
ON category_source_mappings(category_id, active);

CREATE TABLE IF NOT EXISTS category_aliases (
    id TEXT PRIMARY KEY,
    category_id TEXT NOT NULL REFERENCES categories(id),
    alias TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    source_system TEXT NOT NULL,
    manifest_id TEXT REFERENCES category_catalog_imports(manifest_id),
    created_at TEXT NOT NULL,
    UNIQUE(source_system, alias_key)
);

CREATE INDEX IF NOT EXISTS idx_category_aliases_category
ON category_aliases(category_id);
CREATE INDEX IF NOT EXISTS idx_category_aliases_key
ON category_aliases(alias_key);
