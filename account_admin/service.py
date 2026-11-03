from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from .audit import build_audit_event
from .config import LocalDevConfig
from .contracts import (
    ContractSigner,
    format_utc,
    identity_context_claims,
    service_auth_claims,
    utc_now,
)
from .fixtures import load_principal_fixtures, load_service_fixtures
from .gateway import capabilities_for_roles
from .passwords import PasswordError, hash_password, verify_password
from .store import AccountAdminStore, StoreError


class AuthenticationError(PermissionError):
    pass


class AuthorizationError(PermissionError):
    pass


class RegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class SessionCredentials:
    token: str
    csrf_token: str
    session: dict[str, Any]
    principal: dict[str, Any]


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AccountAdminService:
    store_class = AccountAdminStore

    def __init__(self, config: LocalDevConfig):
        self.config = config
        self.store = self.store_class(config.data_dir)
        self.identity_signer = ContractSigner(
            config.identity_signing_keys,
            config.active_identity_key_id,
        )
        self.service_signer = ContractSigner(
            config.service_signing_keys,
            config.active_service_key_id,
        )
        self.store.initialize()
        if config.allow_fixture_login:
            fixtures = load_principal_fixtures()
            for principal in fixtures["principals"]:
                self.store.seed_principal(principal)
        self.service_fixtures = load_service_fixtures()
        self._dummy_credential = hash_password("account-admin-dummy-password")
        self._login_failures: dict[str, list[float]] = {}
        self._login_failure_lock = threading.Lock()

    def build_audit_event(self, **values):
        policy = getattr(self.config, "policy_version", None)
        return build_audit_event(**values, **({"policy_version": policy} if policy else {}))

    @staticmethod
    def _request_actor(principal: Mapping[str, Any] | None) -> dict[str, Any]:
        if principal:
            return {
                "principal_id": principal["principal_id"],
                "employee_id": principal["employee_id"],
                "display_name": principal["display_name"],
                "roles": list(principal.get("roles") or []),
            }
        return {
            "principal_id": "system_local_fixture",
            "employee_id": "LOCAL-SYSTEM",
            "display_name": "Synthetic Local System",
            "roles": [],
        }

    @staticmethod
    def _required_text(value: object, field: str, maximum: int = 128) -> str:
        if not isinstance(value, str) or not value.strip():
            raise RegistrationError(f"{field} must not be empty")
        normalized = value.strip()
        if len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
            raise RegistrationError(f"{field} is invalid")
        return normalized

    def _session_record(
        self,
        principal_id: str,
        *,
        remember_me: bool,
        rotated_from: str | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        created = utc_now()
        lifetime = (
            self.config.remember_session_lifetime_seconds
            if remember_me
            else self.config.standard_session_lifetime_seconds
        )
        return (
            {
                "session_id": f"ses_{secrets.token_hex(16)}",
                "principal_id": principal_id,
                "token_hash": _hash_token(token),
                "csrf_hash": _hash_token(csrf_token),
                "created_at": format_utc(created),
                "expires_at": format_utc(created + timedelta(seconds=lifetime)),
                "remember_me": bool(remember_me),
                "rotated_from_session_id": rotated_from,
            },
            token,
            csrf_token,
        )

    def login_fixture(
        self,
        fixture_id: str,
        *,
        request_id: str,
        client_ip: str,
        user_agent: str,
    ) -> SessionCredentials:
        if not self.config.allow_fixture_login:
            raise AuthenticationError("synthetic fixture login is disabled")
        principal = self.store.get_principal_by_fixture(fixture_id)
        if not principal or principal["status"] != "active":
            raise AuthenticationError("synthetic fixture is unavailable")
        session, token, csrf_token = self._session_record(principal["principal_id"], remember_me=True)
        event = self.build_audit_event(
            actor=self._request_actor(principal),
            session_id=session["session_id"],
            request_id=request_id,
            correlation_id=request_id,
            module="account_admin",
            action="session.login",
            resource_type="session",
            resource_id=session["session_id"],
            result="success",
            reason_code="local_fixture_login",
            client_ip=client_ip,
            user_agent=user_agent,
            metadata={"auth_source": "local_fixture", "non_production": True},
        )
        self.store.create_session(session, event)
        self.store.dispatch_audit_outbox()
        return SessionCredentials(token=token, csrf_token=csrf_token, session=session, principal=principal)

    def register_local_account(
        self,
        *,
        employee_id: object,
        display_name: object,
        password: object,
        remember_me: bool,
        request_id: str,
        client_ip: str,
        user_agent: str,
    ) -> SessionCredentials:
        if not self.config.allow_self_registration:
            raise AuthorizationError("self registration is disabled")
        employee = self._required_text(employee_id, "employee_id", 64)
        if employee.casefold() == "admin":
            raise RegistrationError("employee_id is reserved")
        name = self._required_text(display_name, "display_name", 128)
        try:
            credential = hash_password(password)
        except PasswordError as exc:
            raise RegistrationError(str(exc)) from exc
        created_at = format_utc(utc_now())
        principal = {
            "principal_id": f"prn_local_{secrets.token_hex(16)}",
            "username": employee,
            "employee_id": employee,
            "display_name": name,
            "roles": ["user"],
            "status": "active",
            "auth_source": "local_account",
            "non_production": True,
            "created_at": created_at,
        }
        event = self.build_audit_event(
            actor=self._request_actor(principal),
            session_id=None,
            request_id=request_id,
            correlation_id=request_id,
            module="account_admin",
            action="account.register",
            resource_type="principal",
            resource_id=principal["principal_id"],
            result="success",
            reason_code="self_registration",
            client_ip=client_ip,
            user_agent=user_agent,
            metadata={"auth_source": "local_account", "assigned_role": "user"},
        )
        try:
            self.store.create_local_principal(principal, credential, role="user", event=event)
        except StoreError as exc:
            raise RegistrationError(str(exc)) from exc
        self.store.dispatch_audit_outbox()
        return self._create_authenticated_session(
            principal,
            remember_me=bool(remember_me),
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
            reason_code="registration_auto_login",
        )

    def bootstrap_admin(
        self,
        *,
        employee_id: object,
        display_name: object,
        password: object,
        username: object = "admin",
    ) -> dict[str, Any]:
        login_name = self._required_text(username, "username", 64)
        employee = self._required_text(employee_id, "employee_id", 64)
        name = self._required_text(display_name, "display_name", 128)
        try:
            credential = hash_password(password)
        except PasswordError as exc:
            raise RegistrationError(str(exc)) from exc
        created_at = format_utc(utc_now())
        principal = {
            "principal_id": f"prn_local_{secrets.token_hex(16)}",
            "username": login_name,
            "employee_id": employee,
            "display_name": name,
            "roles": ["admin"],
            "status": "active",
            "auth_source": "local_account",
            "non_production": True,
            "created_at": created_at,
        }
        event = self.build_audit_event(
            actor=self._request_actor(None),
            session_id=None,
            request_id=f"req_bootstrap_{secrets.token_hex(12)}",
            correlation_id=f"corr_bootstrap_{secrets.token_hex(12)}",
            module="account_admin",
            action="account.bootstrap_admin",
            resource_type="principal",
            resource_id=principal["principal_id"],
            result="success",
            reason_code="local_operator_bootstrap",
            client_ip="local-cli",
            user_agent="account-admin-cli",
            metadata={"assigned_role": "admin"},
        )
        try:
            self.store.create_local_principal(principal, credential, role="admin", event=event)
        except StoreError as exc:
            raise RegistrationError(str(exc)) from exc
        self.store.dispatch_audit_outbox()
        return self.public_principal(principal)

    def _login_key(self, username: str, client_ip: str) -> str:
        return f"{client_ip.casefold()}|{username.casefold()}"

    def _check_login_rate(self, key: str) -> None:
        cutoff = time.monotonic() - 300
        with self._login_failure_lock:
            failures = [value for value in self._login_failures.get(key, []) if value >= cutoff]
            self._login_failures[key] = failures
            if len(failures) >= 5:
                raise AuthenticationError("invalid credentials")

    def _record_login_failure(self, key: str) -> None:
        with self._login_failure_lock:
            self._login_failures.setdefault(key, []).append(time.monotonic())

    def _audit_login_failure(
        self,
        username: str,
        *,
        reason_code: str,
        request_id: str,
        client_ip: str,
        user_agent: str,
    ) -> None:
        identifier_hash = hashlib.sha256(username.casefold().encode("utf-8")).hexdigest()
        self.record_access_event(
            None,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
            action="session.login",
            resource_type="login_identifier_hash",
            resource_id=identifier_hash,
            result="denied",
            reason_code=reason_code,
            metadata={"auth_source": "local_account"},
        )

    def login_local_account(
        self,
        *,
        username: object,
        password: object,
        remember_me: bool,
        request_id: str,
        client_ip: str,
        user_agent: str,
    ) -> SessionCredentials:
        try:
            login_name = self._required_text(username, "username", 64)
        except RegistrationError as exc:
            raise AuthenticationError("invalid credentials") from exc
        key = self._login_key(login_name, client_ip)
        try:
            self._check_login_rate(key)
        except AuthenticationError:
            self._audit_login_failure(
                login_name,
                reason_code="rate_limited",
                request_id=request_id,
                client_ip=client_ip,
                user_agent=user_agent,
            )
            raise
        principal = self.store.get_principal_by_username(login_name)
        credential = self.store.get_local_credential(principal["principal_id"]) if principal else None
        candidate = credential or self._dummy_credential
        authenticated = verify_password(password, candidate)
        if (
            not principal
            or principal.get("auth_source") != "local_account"
            or principal.get("status") != "active"
            or credential is None
            or not authenticated
        ):
            self._record_login_failure(key)
            self._audit_login_failure(
                login_name,
                reason_code="invalid_credentials",
                request_id=request_id,
                client_ip=client_ip,
                user_agent=user_agent,
            )
            raise AuthenticationError("invalid credentials")
        with self._login_failure_lock:
            self._login_failures.pop(key, None)
        return self._create_authenticated_session(
            principal,
            remember_me=bool(remember_me),
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
            reason_code="local_password_login",
        )

    def _create_authenticated_session(
        self,
        principal: Mapping[str, Any],
        *,
        remember_me: bool,
        request_id: str,
        client_ip: str,
        user_agent: str,
        reason_code: str,
    ) -> SessionCredentials:
        session, token, csrf_token = self._session_record(
            str(principal["principal_id"]),
            remember_me=remember_me,
        )
        event = self.build_audit_event(
            actor=self._request_actor(principal),
            session_id=session["session_id"],
            request_id=request_id,
            correlation_id=request_id,
            module="account_admin",
            action="session.login",
            resource_type="session",
            resource_id=session["session_id"],
            result="success",
            reason_code=reason_code,
            client_ip=client_ip,
            user_agent=user_agent,
            metadata={
                "auth_source": principal.get("auth_source"),
                "remember_me": remember_me,
            },
        )
        self.store.create_session(session, event)
        self.store.dispatch_audit_outbox()
        return SessionCredentials(
            token=token,
            csrf_token=csrf_token,
            session=session,
            principal=dict(principal),
        )

    def authenticate(self, token: str | None) -> dict[str, Any]:
        if not token:
            raise AuthenticationError("missing session")
        session = self.store.get_active_session(_hash_token(token), format_utc(utc_now()))
        if not session:
            raise AuthenticationError("invalid or expired session")
        return session

    @staticmethod
    def verify_csrf(session: Mapping[str, Any], supplied: str | None) -> None:
        if not supplied or not hmac.compare_digest(str(session["csrf_hash"]), _hash_token(supplied)):
            raise AuthorizationError("csrf validation failed")

    @staticmethod
    def require_capability(session: Mapping[str, Any], capability: str) -> None:
        if capability not in capabilities_for_roles(session.get("roles") or []):
            raise AuthorizationError("capability denied")

    def signed_identity_context(self, session: Mapping[str, Any], request_id: str) -> str:
        claims = identity_context_claims(
            auth_source=str(session["auth_source"]),
            principal_id=session["principal_id"],
            employee_id=session["employee_id"],
            display_name=session["display_name"],
            roles=list(session.get("roles") or []),
            session_id=session["session_id"],
            request_id=request_id,
            ttl_seconds=self.config.identity_context_ttl_seconds,
        )
        return self.identity_signer.sign(claims).token

    def rotate_session(
        self,
        current: Mapping[str, Any],
        *,
        request_id: str,
        client_ip: str,
        user_agent: str,
    ) -> SessionCredentials:
        principal = self.store.get_principal(current["principal_id"])
        if not principal:
            raise AuthenticationError("principal unavailable")
        new_session, token, csrf_token = self._session_record(
            principal["principal_id"],
            remember_me=bool(current.get("remember_me")),
            rotated_from=current["session_id"],
        )
        if current.get("auth_source") == "ad":
            new_session["expires_at"] = current["expires_at"]
            new_session["auth_verified_at"] = current["auth_verified_at"]
        actor = self._request_actor(principal)
        revoke_event = self.build_audit_event(
            actor=actor,
            session_id=current["session_id"],
            request_id=request_id,
            correlation_id=request_id,
            module="account_admin",
            action="session.revoke",
            resource_type="session",
            resource_id=current["session_id"],
            result="success",
            reason_code="rotated",
            client_ip=client_ip,
            user_agent=user_agent,
        )
        create_event = self.build_audit_event(
            actor=actor,
            session_id=new_session["session_id"],
            request_id=request_id,
            correlation_id=request_id,
            module="account_admin",
            action="session.rotate",
            resource_type="session",
            resource_id=new_session["session_id"],
            result="success",
            reason_code="rotation_completed",
            client_ip=client_ip,
            user_agent=user_agent,
            changes={"rotated_from_session_id": current["session_id"]},
        )
        self.store.rotate_session(
            old_session_id=current["session_id"],
            new_session=new_session,
            revoke_event=revoke_event,
            create_event=create_event,
        )
        self.store.dispatch_audit_outbox()
        return SessionCredentials(token=token, csrf_token=csrf_token, session=new_session, principal=principal)

    def revoke_session(
        self,
        current: Mapping[str, Any],
        target_session_id: str,
        *,
        reason: str,
        request_id: str,
        client_ip: str,
        user_agent: str,
    ) -> bool:
        actor = self._request_actor(current)
        event = self.build_audit_event(
            actor=actor,
            session_id=current["session_id"],
            request_id=request_id,
            correlation_id=request_id,
            module="account_admin",
            action="session.revoke",
            resource_type="session",
            resource_id=target_session_id,
            result="success",
            reason_code=reason,
            client_ip=client_ip,
            user_agent=user_agent,
            metadata={"authorization": "must_be_redacted"},
        )
        changed = self.store.revoke_session(
            target_session_id,
            revoked_at=format_utc(utc_now()),
            reason=reason,
            event=event,
        )
        self.store.dispatch_audit_outbox()
        return changed

    def set_principal_status(
        self,
        current: Mapping[str, Any],
        target_principal_id: str,
        status: str,
        *,
        request_id: str,
        client_ip: str,
        user_agent: str,
    ) -> bool:
        self.require_capability(current, "admin.manage_users")
        if current["principal_id"] == target_principal_id and status == "disabled":
            raise AuthorizationError("admin cannot disable the current account")
        target = self.store.get_principal(target_principal_id)
        if not target:
            return False
        changed_at = format_utc(utc_now())
        event = self.build_audit_event(
            actor=self._request_actor(current),
            session_id=current["session_id"],
            request_id=request_id,
            correlation_id=request_id,
            module="account_admin",
            action="account.status_change",
            resource_type="principal",
            resource_id=target_principal_id,
            result="success",
            reason_code="admin_status_change",
            client_ip=client_ip,
            user_agent=user_agent,
            changes={"before": target["status"], "after": status},
        )
        changed = self.store.set_principal_status(
            target_principal_id,
            status,
            updated_at=changed_at,
            event=event,
        )
        self.store.dispatch_audit_outbox()
        return changed

    def reset_principal_password(
        self,
        current: Mapping[str, Any],
        target_principal_id: str,
        password: object,
        *,
        request_id: str,
        client_ip: str,
        user_agent: str,
    ) -> bool:
        self.require_capability(current, "admin.manage_users")
        try:
            credential = hash_password(password)
        except PasswordError as exc:
            raise RegistrationError(str(exc)) from exc
        event = self.build_audit_event(
            actor=self._request_actor(current),
            session_id=current["session_id"],
            request_id=request_id,
            correlation_id=request_id,
            module="account_admin",
            action="account.password_reset",
            resource_type="principal",
            resource_id=target_principal_id,
            result="success",
            reason_code="admin_password_reset",
            client_ip=client_ip,
            user_agent=user_agent,
        )
        changed = self.store.reset_local_password(
            target_principal_id,
            credential,
            updated_at=format_utc(utc_now()),
            event=event,
        )
        self.store.dispatch_audit_outbox()
        return changed

    def issue_service_fixture(
        self,
        service_principal_id: str,
        *,
        request_id: str,
        correlation_id: str,
        ttl_seconds: int = 60,
    ) -> str:
        fixture = next(
            (
                item
                for item in self.service_fixtures["service_principals"]
                if item["service_principal_id"] == service_principal_id and item["status"] == "active"
            ),
            None,
        )
        if fixture is None:
            raise AuthenticationError("synthetic service fixture is unavailable")
        claims = service_auth_claims(
            service_principal_id=fixture["service_principal_id"],
            audience=fixture["audience"],
            scope=fixture["scope"],
            request_id=request_id,
            correlation_id=correlation_id,
            ttl_seconds=ttl_seconds,
        )
        return self.service_signer.sign(claims).token

    def record_access_event(
        self,
        current: Mapping[str, Any] | None,
        *,
        request_id: str,
        client_ip: str,
        user_agent: str,
        action: str,
        resource_type: str,
        resource_id: str,
        result: str,
        reason_code: str,
        changes: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        event = self.build_audit_event(
            actor=self._request_actor(current),
            session_id=str(current["session_id"]) if current else None,
            request_id=request_id,
            correlation_id=request_id,
            module="account_admin",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            reason_code=reason_code,
            client_ip=client_ip,
            user_agent=user_agent,
            changes=changes,
            metadata=metadata,
        )
        self.store.enqueue_audit_event(event)
        self.store.dispatch_audit_outbox()

    @staticmethod
    def public_principal(principal: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "principal_id": principal["principal_id"],
            "username": principal.get("username"),
            "employee_id": principal["employee_id"],
            "display_name": principal["display_name"],
            "roles": list(principal.get("roles") or []),
            "status": principal.get("status") or principal.get("principal_status"),
            "auth_source": principal.get("auth_source"),
            "non_production": bool(principal.get("non_production", True)),
            "capabilities": sorted(capabilities_for_roles(principal.get("roles") or [])),
        }
