"""Revalidate local principal authority before and during autonomous research."""
import hashlib
import hmac
import json
from .contracts import format_utc, utc_now
from .gateway import capabilities_for_roles

PATH = "/account-admin/internal/research-authorize"
SCHEMA = "voxquarry.research-authority/v1"
MODE = "local_dev_account_admin"


class ResearchAuthority:
    def __init__(self, service, **_):
        self.service = service

    def authorize(self, principal_id, required_capability):
        principal = self.service.store.get_principal(principal_id)
        roles = sorted(set((principal or {}).get("roles") or []))
        value = {"schema_version": SCHEMA, "principal_id": principal_id,
                 "required_capability": required_capability, "mode": MODE,
                 "roles": roles, "allowed": False, "ttl_seconds": 30,
                 "checked_at": format_utc(utc_now())}
        if not principal or principal.get("status") != "active":
            return 403, {**value, "reason": "principal_inactive"}
        if required_capability not in {"chat.session.own", "collect.command"} or required_capability not in capabilities_for_roles(roles):
            return 403, {**value, "reason": "capability_denied"}
        credential = self.service.store.get_local_credential(principal_id)
        state = {"id": principal_id, "roles": roles, "status": principal["status"],
                 "credential": vars(credential) if credential else None}
        key = self.service.config.identity_signing_keys[self.service.config.active_identity_key_id]
        version = hmac.new(key, json.dumps(state, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        return 200, {**value, "allowed": True, "owner_scope": "principal:" + principal_id,
                     "authorization_version": "authv_" + version, "reason": "authorized"}
