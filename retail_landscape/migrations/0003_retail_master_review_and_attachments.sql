PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS market_regions (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    name_zh TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS retailers (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    domains_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    dimension TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE products ADD COLUMN energy_type TEXT;
ALTER TABLE products ADD COLUMN notes TEXT NOT NULL DEFAULT '';

ALTER TABLE product_listings ADD COLUMN retailer_id TEXT REFERENCES retailers(id);

ALTER TABLE field_definitions ADD COLUMN unit_id TEXT REFERENCES units(id);
ALTER TABLE field_definitions ADD COLUMN is_matrix INTEGER NOT NULL DEFAULT 0;
ALTER TABLE field_definitions ADD COLUMN active INTEGER NOT NULL DEFAULT 1;

ALTER TABLE spec_values ADD COLUMN numeric_value REAL;
ALTER TABLE spec_values ADD COLUMN unit_id TEXT REFERENCES units(id);

ALTER TABLE product_assets ADD COLUMN storage_path TEXT;
ALTER TABLE product_assets ADD COLUMN original_filename TEXT;
ALTER TABLE product_assets ADD COLUMN content_hash TEXT;
ALTER TABLE product_assets ADD COLUMN file_size INTEGER;

CREATE TABLE IF NOT EXISTS product_market_regions (
    product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    market_region_id TEXT NOT NULL REFERENCES market_regions(id),
    review_status TEXT NOT NULL DEFAULT 'pending_review',
    source_url TEXT,
    source_snapshot_hash TEXT,
    observed_at TEXT,
    PRIMARY KEY(product_id, market_region_id)
);

CREATE TABLE IF NOT EXISTS product_retailers (
    product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    retailer_id TEXT NOT NULL REFERENCES retailers(id),
    review_status TEXT NOT NULL DEFAULT 'pending_review',
    PRIMARY KEY(product_id, retailer_id)
);

CREATE TABLE IF NOT EXISTS review_decisions (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_market_regions_region
ON product_market_regions(market_region_id, product_id);

CREATE INDEX IF NOT EXISTS idx_product_retailers_retailer
ON product_retailers(retailer_id, product_id);

CREATE INDEX IF NOT EXISTS idx_review_decisions_product_time
ON review_decisions(product_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_product_assets_storage
ON product_assets(product_id, storage_path);
