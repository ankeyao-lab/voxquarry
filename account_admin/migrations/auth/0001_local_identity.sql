PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS principals (
    principal_id TEXT PRIMARY KEY,
    fixture_id TEXT UNIQUE,
    auth_source TEXT NOT NULL CHECK (auth_source IN ('local_account', 'local_fixture', 'ad')),
    external_subject TEXT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    employee_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    non_production INTEGER NOT NULL CHECK (non_production = 1),
    directory_status_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(auth_source, external_subject)
);

CREATE TABLE IF NOT EXISTS local_credentials (
    principal_id TEXT PRIMARY KEY REFERENCES principals(principal_id) ON DELETE CASCADE,
    algorithm TEXT NOT NULL CHECK (algorithm = 'pbkdf2-sha256-v1'),
    salt_b64 TEXT NOT NULL,
    password_hash_b64 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_bindings (
    principal_id TEXT NOT NULL REFERENCES principals(principal_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, role)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    token_hash TEXT NOT NULL UNIQUE,
    csrf_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    remember_me INTEGER NOT NULL CHECK (remember_me IN (0, 1)),
    auth_verified_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    rotated_from_session_id TEXT REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_principal_status
ON sessions(principal_id, status, expires_at);

CREATE TABLE IF NOT EXISTS audit_outbox (
    event_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_outbox_pending
ON audit_outbox(delivered_at, created_at);
