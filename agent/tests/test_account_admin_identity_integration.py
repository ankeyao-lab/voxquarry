from __future__ import annotations

import unittest

from account_admin.contracts import ContractSigner, identity_context_claims
from agent.contracts import AgentError
from agent.identity import AccountAdminIdentityProvider


KEY = b"local-integration-identity-key-20260828"


def token(*, principal_id: str = "prn_local_user_alpha", roles: list[str] | None = None) -> str:
    claims = identity_context_claims(
        principal_id=principal_id,
        employee_id="E10001",
        display_name="Local User",
        roles=roles or ["user"],
        session_id="ses_" + "a" * 32,
        request_id="req_" + "b" * 24,
        ttl_seconds=60,
    )
    return ContractSigner({"local-identity-v1": KEY}, "local-identity-v1").sign(claims).token


class AccountAdminIdentityIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = AccountAdminIdentityProvider(KEY)

    def test_formal_account_identity_maps_to_owner_only_wenquan_scope(self) -> None:
        identity = self.provider.resolve({"x-vq-identity-context": token()})
        self.assertEqual(identity.actor_id, "prn_local_user_alpha")
        self.assertEqual(identity.owner_user_id, "prn_local_user_alpha")
        self.assertEqual(identity.scope_id, "principal:prn_local_user_alpha")
        self.assertEqual(identity.roles, ("user",))
        self.assertIsNone(identity.allowed_report_ids)
        self.assertFalse(identity.allow_archived_reports)
        self.assertEqual(identity.mode, "authenticated")

    def test_forged_legacy_headers_do_not_override_formal_identity(self) -> None:
        identity = self.provider.resolve(
            {
                "x-vq-identity-context": token(),
                "x-voxquarry-identity-context": "forged",
                "x-voxquarry-identity-signature": "sha256=" + "0" * 64,
            }
        )
        self.assertEqual(identity.owner_user_id, "prn_local_user_alpha")

    def test_invalid_or_unauthorized_formal_identity_fails_closed(self) -> None:
        with self.assertRaises(AgentError) as invalid:
            self.provider.resolve({"x-vq-identity-context": token() + "tampered"})
        self.assertEqual(invalid.exception.http_status, 401)

        with self.assertRaises(AgentError) as denied:
            self.provider.resolve({"x-vq-identity-context": token(roles=["unknown"])})
        self.assertEqual(denied.exception.http_status, 403)


if __name__ == "__main__":
    unittest.main()
