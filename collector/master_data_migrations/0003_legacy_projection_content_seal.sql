PRAGMA journal_mode=DELETE;

ALTER TABLE catalog_consumer_scopes
ADD COLUMN projection_content_hash TEXT;
