CREATE TABLE IF NOT EXISTS ad_identities (
    principal_id TEXT PRIMARY KEY REFERENCES principals(principal_id),
    account_name TEXT NOT NULL,
    password_version TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
