PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS price_observations (
    id TEXT PRIMARY KEY,
    product_listing_id TEXT NOT NULL REFERENCES product_listings(id) ON DELETE CASCADE,
    price_kind TEXT NOT NULL DEFAULT 'retail' CHECK(price_kind = 'retail'),
    raw_value TEXT NOT NULL,
    normalized_amount REAL NOT NULL CHECK(normalized_amount >= 0),
    currency_code TEXT NOT NULL CHECK(
        length(currency_code) = 3 AND currency_code = upper(currency_code)
    ),
    market_region TEXT NOT NULL DEFAULT '',
    source_url TEXT,
    source_type TEXT,
    source_snapshot_hash TEXT,
    observed_at TEXT,
    review_status TEXT NOT NULL DEFAULT 'draft',
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_observations_listing_currency_time
ON price_observations(product_listing_id, currency_code, observed_at DESC, created_at DESC);
