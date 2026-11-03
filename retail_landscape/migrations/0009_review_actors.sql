CREATE TABLE IF NOT EXISTS review_actors (
    principal_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    employee_id TEXT,
    updated_at TEXT NOT NULL
);
-- Retire the old built-in Fan alias only; no existing product is reclassified.
DELETE FROM category_aliases WHERE source_system='chengmu_field_catalog'
    AND category_id='cat_fan' AND alias='Misting Fan';
