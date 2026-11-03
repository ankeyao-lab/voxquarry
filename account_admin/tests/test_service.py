from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from account_admin.audit import REDACTED
from account_admin.backup import BackupError, create_backup, restore_backup
from account_admin.config import ConfigurationError, LocalDevConfig
from account_admin.service import AccountAdminService, AuthenticationError

from support import IDENTITY_TEST_KEY, SERVICE_TEST_KEY, config


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="account-admin-test-")
        self.root = Path(self.temp.name)
        self.service = AccountAdminService(config(self.root / "data"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def login(self, fixture_id: str = "fixture_user_alpha"):
        return self.service.login_fixture(
            fixture_id,
            request_id="req_" + "1" * 32,
            client_ip="127.0.0.1",
            user_agent="service-test",
        )

    def test_configuration_rejects_production_modes_and_non_loopback_native_bind(self) -> None:
        with self.assertRaises(ConfigurationError):
            LocalDevConfig(
                data_dir=self.root / "bad",
                identity_signing_keys={"local-identity-v1": IDENTITY_TEST_KEY},
                service_signing_keys={"local-service-v1": SERVICE_TEST_KEY},
                mode="production",
            )
        with self.assertRaisesRegex(ConfigurationError, "loopback"):
            LocalDevConfig(
                data_dir=self.root / "bad2",
                identity_signing_keys={"local-identity-v1": IDENTITY_TEST_KEY},
                service_signing_keys={"local-service-v1": SERVICE_TEST_KEY},
                native_bind_host="0.0.0.0",
            )

    def test_fixture_login_persists_only_hashes_and_survives_reopen(self) -> None:
        credentials = self.login()
        with self.service.store.auth_connection() as connection:
            row = connection.execute("SELECT token_hash, csrf_hash, expires_at FROM sessions").fetchone()
            columns = {item["name"] for item in connection.execute("PRAGMA table_info(sessions)").fetchall()}
        self.assertNotEqual(row["token_hash"], credentials.token)
        self.assertNotEqual(row["csrf_hash"], credentials.csrf_token)
        self.assertNotIn("token", columns)
        self.assertNotIn("csrf_token", columns)

        reopened = AccountAdminService(config(self.root / "data"))
        session = reopened.authenticate(credentials.token)
        self.assertEqual(session["principal_id"], "prn_local_user_alpha")
        self.assertTrue(session["expires_at"].endswith("Z"))

    def test_rotation_logout_and_admin_revocation_invalidate_sessions(self) -> None:
        user = self.login()
        current = self.service.authenticate(user.token)
        rotated = self.service.rotate_session(
            current,
            request_id="req_" + "2" * 32,
            client_ip="127.0.0.1",
            user_agent="service-test",
        )
        with self.assertRaises(AuthenticationError):
            self.service.authenticate(user.token)
        self.assertEqual(self.service.authenticate(rotated.token)["session_id"], rotated.session["session_id"])

        admin = self.login("fixture_admin")
        admin_session = self.service.authenticate(admin.token)
        changed = self.service.revoke_session(
            admin_session,
            rotated.session["session_id"],
            reason="admin_revoked",
            request_id="req_" + "3" * 32,
            client_ip="127.0.0.1",
            user_agent="service-test",
        )
        self.assertTrue(changed)
        with self.assertRaises(AuthenticationError):
            self.service.authenticate(rotated.token)

    def test_audit_outbox_is_idempotent_append_only_and_redacted(self) -> None:
        credentials = self.login("fixture_admin")
        current = self.service.authenticate(credentials.token)
        self.service.record_access_event(
            current,
            request_id="req_" + "4" * 32,
            client_ip="127.0.0.1",
            user_agent="service-test",
            action="security.redaction.test",
            resource_type="fixture",
            resource_id="fixture-1",
            result="denied",
            reason_code="test",
            changes={"password": "do-not-store", "profile": {"name": "safe"}},
            metadata={"authorization": "Bearer do-not-store", "cookie": "do-not-store"},
        )
        self.assertEqual(self.service.store.pending_outbox_count(), 0)
        before = self.service.store.list_audit_events(500)
        self.assertEqual(self.service.store.dispatch_audit_outbox(), 0)
        after = self.service.store.list_audit_events(500)
        self.assertEqual(len(before), len(after))
        event = next(item for item in after if item["action"] == "security.redaction.test")
        self.assertEqual(event["changes"]["password"], REDACTED)
        self.assertEqual(event["changes"]["profile"]["name"], "safe")
        self.assertEqual(event["metadata"]["authorization"], REDACTED)
        self.assertEqual(event["metadata"]["cookie"], REDACTED)

        with self.service.store.audit_connection() as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE audit_events SET reason_code='changed'")
            connection.rollback()
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM audit_events")

    def test_migrations_backup_and_empty_target_restore(self) -> None:
        credentials = self.login("fixture_admin")
        self.assertEqual(self.service.authenticate(credentials.token)["principal_id"], "prn_local_admin_0001")
        with self.service.store.auth_connection() as connection:
            auth_versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
        with self.service.store.audit_connection() as connection:
            audit_versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
        self.assertEqual(auth_versions, ["0001_local_identity.sql", "0002_ad_identities.sql"])
        self.assertEqual(audit_versions, ["0001_append_only_audit.sql"])

        backup_dir = self.root / "backup"
        manifest = create_backup(self.root / "data", backup_dir)
        self.assertEqual({item["integrity_check"] for item in manifest["files"]}, {"ok"})
        restored_dir = self.root / "restored"
        restore_backup(backup_dir, restored_dir)
        restored = AccountAdminService(config(restored_dir))
        self.assertEqual(restored.store.integrity(), {"auth": "ok", "audit": "ok"})
        self.assertEqual(restored.authenticate(credentials.token)["principal_id"], "prn_local_admin_0001")
        with self.assertRaisesRegex(BackupError, "must not contain"):
            restore_backup(backup_dir, restored_dir)

    def test_restore_rejects_modified_backup(self) -> None:
        self.login()
        backup_dir = self.root / "backup-corrupt"
        create_backup(self.root / "data", backup_dir)
        with (backup_dir / "auth.sqlite3").open("ab") as handle:
            handle.write(b"corruption")
        with self.assertRaisesRegex(BackupError, "hash mismatch"):
            restore_backup(backup_dir, self.root / "corrupt-restore")


if __name__ == "__main__":
    unittest.main()
