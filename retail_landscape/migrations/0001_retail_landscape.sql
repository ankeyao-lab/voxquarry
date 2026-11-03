PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    name_zh TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS field_definitions (
    id TEXT PRIMARY KEY,
    category_id TEXT NOT NULL REFERENCES categories(id),
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    field_group TEXT NOT NULL,
    value_type TEXT NOT NULL,
    requirement TEXT NOT NULL,
    canonical_unit TEXT,
    source_aliases_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(category_id, code)
);

CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    category_id TEXT NOT NULL REFERENCES categories(id),
    brand TEXT NOT NULL,
    model_number TEXT NOT NULL,
    product_name TEXT,
    product_family TEXT,
    platform_series TEXT,
    power_source_type TEXT,
    voltage_platform TEXT,
    review_status TEXT NOT NULL DEFAULT 'draft',
    entity_version INTEGER NOT NULL DEFAULT 1,
    source_type TEXT,
    source_snapshot_hash TEXT,
    source_url TEXT,
    observed_at TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(category_id, brand, model_number)
);

CREATE TABLE IF NOT EXISTS product_listings (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    market_region TEXT NOT NULL,
    retailer TEXT NOT NULL,
    channel TEXT,
    online_url TEXT NOT NULL,
    target_key TEXT,
    retail_status TEXT NOT NULL DEFAULT 'unknown',
    review_status TEXT NOT NULL DEFAULT 'draft',
    source_snapshot_hash TEXT,
    observed_at TEXT,
    UNIQUE(product_id, market_region, retailer, online_url)
);

CREATE TABLE IF NOT EXISTS spec_values (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    field_definition_id TEXT NOT NULL REFERENCES field_definitions(id),
    raw_value TEXT,
    normalized_value TEXT,
    raw_unit TEXT,
    normalized_unit TEXT,
    value_state TEXT NOT NULL DEFAULT 'unknown',
    review_status TEXT NOT NULL DEFAULT 'draft',
    source_url TEXT,
    source_snapshot_hash TEXT,
    observed_at TEXT,
    UNIQUE(product_id, field_definition_id)
);

CREATE TABLE IF NOT EXISTS review_task_links (
    product_listing_id TEXT NOT NULL REFERENCES product_listings(id),
    task_id TEXT NOT NULL,
    task_status TEXT NOT NULL,
    active_result_status TEXT,
    last_attempt_status TEXT,
    PRIMARY KEY(product_listing_id, task_id)
);

CREATE TABLE IF NOT EXISTS report_links (
    category_id TEXT NOT NULL REFERENCES categories(id),
    report_id TEXT NOT NULL,
    title TEXT NOT NULL,
    report_path TEXT NOT NULL,
    generated_at TEXT,
    market_region TEXT,
    PRIMARY KEY(category_id, report_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactional_outbox (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    entity_id TEXT NOT NULL,
    entity_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
