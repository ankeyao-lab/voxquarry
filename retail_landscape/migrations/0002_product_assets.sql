CREATE TABLE IF NOT EXISTS product_assets (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    asset_role TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'image',
    source_url TEXT NOT NULL,
    thumbnail_url TEXT,
    alt_text TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    source_type TEXT,
    source_snapshot_hash TEXT,
    observed_at TEXT,
    review_status TEXT NOT NULL DEFAULT 'draft',
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, asset_role, source_url)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_product_assets_one_hero
ON product_assets(product_id)
WHERE asset_role = 'hero';

CREATE INDEX IF NOT EXISTS idx_product_assets_product_order
ON product_assets(product_id, asset_role, sort_order);
