PRAGMA foreign_keys = ON;

ALTER TABLE review_task_links ADD COLUMN unique_reviews INTEGER;
ALTER TABLE review_task_links ADD COLUMN source_total INTEGER;
ALTER TABLE review_task_links ADD COLUMN result_status TEXT;
ALTER TABLE review_task_links ADD COLUMN result_observed_at TEXT;
ALTER TABLE review_task_links ADD COLUMN result_snapshot_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_review_task_links_result
ON review_task_links(product_listing_id, result_status, result_observed_at DESC);
