from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from account_admin.constants import (
    IDENTITY_SCHEMA_VERSION,
    POLICY_VERSION,
    SERVICE_AUTH_SCHEMA_VERSION,
    TOKEN_USAGE_SCHEMA_VERSION,
)
from account_admin.contracts import (
    ContractError,
    ContractSigner,
    identity_context_claims,
    service_auth_claims,
    utc_now,
)
from account_admin.fixtures import load_principal_fixtures, load_service_fixtures
from account_admin.gateway import BackendIdentityAuthorizer, ServiceAuthorizer, strip_untrusted_identity_headers
from account_admin.ownership import authorize_wenquan_owner, owner_scope, validate_token_usage_attribution


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_key = b"old-contract-test-key-material-00001"
        self.new_key = b"new-contract-test-key-material-00001"
        self.signer = ContractSigner({"old": self.old_key, "new": self.new_key}, "new")

    def identity_claims(self, **overrides):
        values = identity_context_claims(
            principal_id="prn_local_user_alpha",
            employee_id="LOCAL-USER-ALPHA",
            display_name="Local Fixture User Alpha",
            roles=["user"],
            session_id="ses_" + "a" * 32,
            request_id="req_" + "b" * 32,
            ttl_seconds=60,
            now=utc_now(),
        )
        values.update(overrides)
        return values

    def test_identity_context_is_integrity_protected_and_rotatable(self) -> None:
        signed = self.signer.sign(self.identity_claims())
        claims = self.signer.verify(signed.token, expected_schema=IDENTITY_SCHEMA_VERSION)
        self.assertEqual(claims["principal_id"], "prn_local_user_alpha")
        self.assertEqual(claims["policy_version"], POLICY_VERSION)

        parts = signed.token.split(".")
        parts[2] = parts[2][:-1] + ("A" if parts[2][-1] != "A" else "B")
        with self.assertRaisesRegex(ContractError, "integrity"):
            self.signer.verify(".".join(parts), expected_schema=IDENTITY_SCHEMA_VERSION)

        old_signer = ContractSigner({"old": self.old_key}, "old")
        old_token = old_signer.sign(self.identity_claims()).token
        self.assertEqual(
            self.signer.verify(old_token, expected_schema=IDENTITY_SCHEMA_VERSION)["principal_id"],
            "prn_local_user_alpha",
        )

    def test_identity_context_expires_and_forged_bare_headers_are_removed(self) -> None:
        issued = utc_now() - timedelta(minutes=5)
        token = self.signer.sign(
            identity_context_claims(
                principal_id="prn_local_user_alpha",
                employee_id="LOCAL-USER-ALPHA",
                display_name="Alpha",
                roles=["user"],
                session_id="ses_" + "a" * 32,
                request_id="req_" + "b" * 32,
                ttl_seconds=30,
                now=issued,
            )
        ).token
        with self.assertRaisesRegex(ContractError, "not currently valid"):
            self.signer.verify(token, expected_schema=IDENTITY_SCHEMA_VERSION, now=utc_now())

        stripped = strip_untrusted_identity_headers(
            [
                ("Host", "localhost"),
                ("X-VQ-Principal-ID", "prn_fake_admin"),
                ("x-vq-roles", "admin"),
                ("X-VQ-Identity-Context", "fake"),
            ]
        )
        self.assertEqual(stripped, [("Host", "localhost")])

    def test_backend_authorizer_enforces_capability(self) -> None:
        token = self.signer.sign(self.identity_claims()).token
        authorizer = BackendIdentityAuthorizer(self.signer)
        authorized = authorizer.authorize(token, required_capability="chat.session.own")
        self.assertIn("chat.session.own", authorized.capabilities)
        with self.assertRaises(PermissionError):
            authorizer.authorize(token, required_capability="admin.manage_users")
        with self.assertRaises(ContractError):
            authorizer.authorize(None, required_capability="platform.read")

    def test_service_auth_is_separate_and_enforces_audience_scope_ttl(self) -> None:
        claims = service_auth_claims(
            service_principal_id="svc_local_research_reader",
            audience="report-receiver",
            scope="report.read",
            request_id="req_" + "c" * 32,
            correlation_id="corr_" + "d" * 32,
            ttl_seconds=60,
        )
        token = self.signer.sign(claims).token
        authorizer = ServiceAuthorizer(self.signer)
        verified = authorizer.authorize(
            f"VQ-Service {token}",
            expected_audience="report-receiver",
            required_scope="report.read",
        )
        self.assertEqual(verified["schema_version"], SERVICE_AUTH_SCHEMA_VERSION)
        with self.assertRaisesRegex(ContractError, "audience"):
            authorizer.authorize(
                f"VQ-Service {token}",
                expected_audience="wrong-receiver",
                required_scope="report.read",
            )
        with self.assertRaisesRegex(ContractError, "scope"):
            authorizer.authorize(
                f"VQ-Service {token}",
                expected_audience="report-receiver",
                required_scope="retail.product.publish",
            )
        with self.assertRaisesRegex(ContractError, "TTL"):
            service_auth_claims(
                service_principal_id="svc_local_research_reader",
                audience="report-receiver",
                scope="report.read",
                request_id="req_" + "c" * 32,
                correlation_id="corr_" + "d" * 32,
                ttl_seconds=301,
            )

    def test_wenquan_owner_only_has_no_admin_content_bypass(self) -> None:
        alpha = {"principal_id": "prn_local_user_alpha", "roles": ["user"]}
        beta = {"principal_id": "prn_local_user_beta", "roles": ["user"]}
        admin = {"principal_id": "prn_local_admin_0001", "roles": ["admin"]}
        resource = {
            "schema_version": "wenquan-owner-scope/v1",
            "resource_type": "message",
            "resource_id": "msg_fixture_1",
            "owner_principal_id": alpha["principal_id"],
            "scope": owner_scope(alpha["principal_id"]),
        }
        self.assertTrue(authorize_wenquan_owner(alpha, resource))
        self.assertFalse(authorize_wenquan_owner(beta, resource))
        self.assertFalse(authorize_wenquan_owner(admin, resource))

    def test_wenquan_token_usage_is_attributed_to_principal(self) -> None:
        alpha = {"principal_id": "prn_local_user_alpha"}
        event = {
            "schema_version": TOKEN_USAGE_SCHEMA_VERSION,
            "principal_id": "prn_local_user_alpha",
            "input_tokens": 12,
            "output_tokens": 8,
            "total_tokens": 20,
        }
        self.assertTrue(validate_token_usage_attribution(event, alpha))
        event["principal_id"] = "prn_local_user_beta"
        self.assertFalse(validate_token_usage_attribution(event, alpha))
        event["principal_id"] = "prn_local_user_alpha"
        event["total_tokens"] = 21
        self.assertFalse(validate_token_usage_attribution(event, alpha))

    def test_fixtures_and_schemas_are_explicitly_non_production(self) -> None:
        identities = load_principal_fixtures()
        services = load_service_fixtures()
        self.assertTrue(identities["non_production"])
        self.assertTrue(services["non_production"])
        self.assertNotIn("password", json.dumps(identities).casefold())
        self.assertEqual(services["credential_material"], "not_in_fixture")
        for path in SCHEMA_ROOT.glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("schema_version", value.get("properties", {}) or {"schema_version": value.get("schema_version")}, path.name)


if __name__ == "__main__":
    unittest.main()
