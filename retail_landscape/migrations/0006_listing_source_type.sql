ALTER TABLE product_listings ADD COLUMN source_type TEXT;
ALTER TABLE spec_values ADD COLUMN source_type TEXT;

-- Existing rows are already typed as online listings by their table and canonical URL.
-- This migration records that known provenance class; it does not infer a retailer,
-- market, price, observation timestamp, or page content.
UPDATE product_listings
SET source_type = 'online_listing_page'
WHERE source_type IS NULL AND online_url IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_product_listings_source_type
ON product_listings(source_type);

CREATE INDEX IF NOT EXISTS idx_spec_values_source_type
ON spec_values(source_type);
