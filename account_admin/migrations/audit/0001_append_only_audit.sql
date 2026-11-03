PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'audit-event/v1'),
    occurred_at TEXT NOT NULL,
    actor_principal_id TEXT NOT NULL,
    actor_employee_id TEXT NOT NULL,
    actor_display_name TEXT NOT NULL,
    actor_roles_json TEXT NOT NULL,
    session_id TEXT,
    request_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    module TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('success', 'denied', 'failure')),
    reason_code TEXT NOT NULL,
    client_ip TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_actor_time
ON audit_events(actor_principal_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_audit_events_resource_time
ON audit_events(resource_type, resource_id, occurred_at);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events are append-only');
END;
