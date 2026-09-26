from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oauth_store import InvalidGrantError, OAuthStore
from server import ServerConfig, configure_runtime_security, run_server


CLIENT = "test-client"
REDIRECT = "http://127.0.0.1:8765/callback"
RESOURCE = "https://tutor.example/mcp"
SCOPE = "linguamcp"
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode("ascii")).digest()).decode("ascii").rstrip("=")


class OAuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / ".oauth" / "state.sqlite3"
        self.store = OAuthStore(self.path)

    def tearDown(self) -> None:
        configure_runtime_security(ServerConfig())
        self.temporary_directory.cleanup()

    def store_code(self, code: str, *, expires_at: float = 2000) -> None:
        self.store.store_authorization_code(
            code,
            client_id=CLIENT,
            redirect_uri=REDIRECT,
            code_challenge=CHALLENGE,
            resource=RESOURCE,
            scope=SCOPE,
            expires_at=expires_at,
        )

    def redeem(self, code: str, *, now: float = 1000, client_id: str = CLIENT):
        return self.store.redeem_authorization_code(
            code,
            client_id=client_id,
            redirect_uri=REDIRECT,
            code_verifier=VERIFIER,
            resource=RESOURCE,
            now=now,
        )

    def test_code_is_bound_one_time_and_token_survives_store_reopen(self) -> None:
        self.store_code("one-time-code")
        with self.assertRaises(InvalidGrantError):
            self.redeem("one-time-code", client_id="wrong-client")

        pair = self.redeem("one-time-code")
        reopened = OAuthStore(self.path)
        self.assertTrue(
            reopened.is_access_token_valid(pair["access_token"], resource=RESOURCE, now=1001)
        )
        with self.assertRaises(InvalidGrantError):
            self.redeem("one-time-code")
        self.assertFalse(
            reopened.is_access_token_valid(pair["access_token"], resource="https://other.example/mcp", now=1001)
        )

    def test_expired_code_cannot_be_redeemed(self) -> None:
        self.store_code("expired-code", expires_at=999)
        with self.assertRaisesRegex(InvalidGrantError, "expired"):
            self.redeem("expired-code", now=1000)

    def test_refresh_rotation_replay_revokes_the_family(self) -> None:
        self.store_code("rotation-code")
        initial = self.redeem("rotation-code")
        rotated = self.store.rotate_refresh_token(
            initial["refresh_token"],
            client_id=CLIENT,
            resource=RESOURCE,
            requested_scope=SCOPE,
            now=1001,
        )
        self.assertNotEqual(rotated["refresh_token"], initial["refresh_token"])
        self.assertTrue(
            self.store.is_access_token_valid(rotated["access_token"], resource=RESOURCE, now=1002)
        )

        with self.assertRaisesRegex(InvalidGrantError, "replay"):
            self.store.rotate_refresh_token(
                initial["refresh_token"],
                client_id=CLIENT,
                resource=RESOURCE,
                requested_scope=SCOPE,
                now=1003,
            )
        self.assertFalse(
            self.store.is_access_token_valid(rotated["access_token"], resource=RESOURCE, now=1004)
        )

    def test_explicit_revocation_disables_persisted_access_token(self) -> None:
        self.store_code("revocation-code")
        pair = self.redeem("revocation-code")
        self.assertEqual(self.store.revoke_all(), 1)
        self.assertFalse(
            OAuthStore(self.path).is_access_token_valid(
                pair["access_token"], resource=RESOURCE, now=1001
            )
        )

    def test_admin_revoke_command_exits_without_starting_the_server(self) -> None:
        self.store_code("admin-revoke-code")
        pair = self.redeem("admin-revoke-code")

        with patch("server.mcp.run") as server_run:
            run_server(ServerConfig(revoke_oauth_grants=True, oauth_state_path=self.path))

        server_run.assert_not_called()
        self.assertFalse(
            OAuthStore(self.path).is_access_token_valid(
                pair["access_token"], resource=RESOURCE, now=1001
            )
        )


if __name__ == "__main__":
    unittest.main()
