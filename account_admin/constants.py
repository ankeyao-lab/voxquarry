from __future__ import annotations

AUTH_SOURCE = "local_account"
SERVICE_AUTH_SOURCE = "local_fixture"
ALLOWED_USER_AUTH_SOURCES = frozenset({"local_account", "local_fixture", "ad"})
MODE = "local_dev_account_admin"
POLICY_VERSION = "account-admin-local-dev/1.1.0"
IDENTITY_SCHEMA_VERSION = "identity-context/v1"
SERVICE_AUTH_SCHEMA_VERSION = "service-auth/v1"
AUDIT_SCHEMA_VERSION = "audit-event/v1"
OWNER_SCOPE_SCHEMA_VERSION = "wenquan-owner-scope/v1"
TOKEN_USAGE_SCHEMA_VERSION = "wenquan-token-usage/v1"

SESSION_COOKIE = "vq_session"
CSRF_COOKIE = "vq_csrf"
STANDARD_SESSION_LIFETIME_SECONDS = 12 * 60 * 60
REMEMBER_SESSION_LIFETIME_SECONDS = 90 * 24 * 60 * 60
IDENTITY_CONTEXT_TTL_SECONDS = 60
SERVICE_AUTH_MAX_TTL_SECONDS = 300

USER_CAPABILITIES = frozenset(
    {
        "platform.read",
        "collect.command",
        "report.command",
        "retail.command",
        "retail.review",
        "chat.session.own",
        "chat.usage.own",
    }
)

ADMIN_CAPABILITIES = frozenset(
    USER_CAPABILITIES
    | {
        "master_data.manage",
        "report_manage",
        "admin.manage_users",
        "admin.read_audit",
        "admin.revoke_sessions",
        "chat.governance.read",
    }
)

ROLE_CAPABILITIES = {
    "user": USER_CAPABILITIES,
    "admin": ADMIN_CAPABILITIES,
}

SENSITIVE_AUDIT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "csrf",
        "csrf_token",
        "ldap_bind_secret",
        "password_hash",
        "salt",
        "password",
        "secret",
        "session_cookie",
        "session_token",
        "token",
    }
)
