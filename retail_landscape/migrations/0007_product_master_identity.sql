PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS brands (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_key TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brand_aliases (
    alias_key TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL REFERENCES brands(id),
    alias_display TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voltage_platforms (
    id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL REFERENCES brands(id),
    standard_voltage TEXT NOT NULL,
    voltage_value REAL NOT NULL CHECK(voltage_value > 0),
    voltage_unit TEXT NOT NULL DEFAULT 'V' CHECK(voltage_unit = 'V'),
    identity_version TEXT NOT NULL DEFAULT 'brand-voltage/v1',
    created_at TEXT NOT NULL,
    UNIQUE(brand_id, standard_voltage)
);

ALTER TABLE products ADD COLUMN brand_id TEXT REFERENCES brands(id);
ALTER TABLE products ADD COLUMN voltage_platform_id TEXT REFERENCES voltage_platforms(id);
ALTER TABLE products ADD COLUMN product_kind TEXT NOT NULL DEFAULT 'single_product'
    CHECK(product_kind IN ('single_product', 'bundle'));

CREATE TABLE IF NOT EXISTS product_bundles (
    product_id TEXT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK(scope IN ('same_category', 'cross_category')),
    primary_model_id TEXT REFERENCES products(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(primary_model_id IS NULL OR primary_model_id != product_id)
);

CREATE TABLE IF NOT EXISTS product_bundle_members (
    id TEXT PRIMARY KEY,
    bundle_product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    member_role TEXT NOT NULL CHECK(member_role IN (
        'primary_product', 'included_product', 'battery', 'charger', 'accessory', 'other'
    )),
    member_product_id TEXT REFERENCES products(id),
    member_label TEXT,
    resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved', 'unresolved')),
    quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity > 0),
    sort_order INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    source_type TEXT,
    source_snapshot_hash TEXT,
    observed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(bundle_product_id, member_role, member_product_id, member_label),
    CHECK(member_product_id IS NULL OR bundle_product_id != member_product_id),
    CHECK(
        (resolution_status = 'resolved' AND member_product_id IS NOT NULL)
        OR
        (resolution_status = 'unresolved' AND member_product_id IS NULL AND member_label IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_products_brand_id
ON products(brand_id);

CREATE INDEX IF NOT EXISTS idx_products_voltage_platform_id
ON products(voltage_platform_id);

CREATE INDEX IF NOT EXISTS idx_bundle_members_member
ON product_bundle_members(member_product_id, bundle_product_id);
