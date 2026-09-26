from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from oauth_security import (
    OAuthConfiguration,
    OAuthConfigurationError,
    OAuthRequestError,
    validate_pkce_challenge,
    validate_pkce_verifier,
)
from oauth_store import OAuthStore
from server import (
    BearerAuthMiddleware,
    _oauth_authorize_submit,
    _oauth_authorization_metadata,
    _http_middleware,
    _oauth_state_path,
    _oauth_token_response,
    _validate_authorize_values,
    ServerConfig,
)


class OAuthSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.registry = Path(self.temporary_directory.name) / "clients.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_registry(
        self, redirects: list[str], *, client_id: str = "test-client"
    ) -> None:
        self.registry.write_text(
            json.dumps({"clients": {client_id: {"redirect_uris": redirects}}}),
            encoding="utf-8",
        )

    def test_registry_requires_exact_callbacks_and_uses_configured_resource_path(self) -> None:
        callback = "http://127.0.0.1:8765/callback"
        self.write_registry([callback])
        configuration = OAuthConfiguration.load(
            self.registry, "https://tutor.example", "/lingua"
        )

        self.assertTrue(configuration.allows_redirect("test-client", callback))
        self.assertFalse(configuration.allows_redirect("test-client", callback + "/extra"))
        self.assertFalse(configuration.allows_redirect("other-client", callback))
        self.assertEqual(configuration.resource_uri, "https://tutor.example/lingua")
        self.assertEqual(configuration.public_base_url, "https://tutor.example")

    def test_registry_rejects_unsafe_or_ambiguous_configuration(self) -> None:
        for callback in (
            "http://example.test/callback",
            "http://127.0.0.1.attacker.test/callback",
            "https://user:pass@example.test/callback",
            "https://example.test/callback#fragment",
        ):
            with self.subTest(callback=callback):
                self.write_registry([callback])
                with self.assertRaises(OAuthConfigurationError):
                    OAuthConfiguration.load(
                        self.registry, "https://tutor.example", "/mcp"
                    )

        self.write_registry(["https://client.example/callback", "https://client.example/callback"])
        with self.assertRaises(OAuthConfigurationError):
            OAuthConfiguration.load(self.registry, "https://tutor.example", "/mcp")
        self.write_registry(["https://client.example/callback"])
        with self.assertRaises(OAuthConfigurationError):
            OAuthConfiguration.load(self.registry, "http://tutor.example", "/mcp")

        for client_id in ("client with space", "bad\nclient", "x" * 2049):
            with self.subTest(client_id=client_id[:40]):
                self.write_registry(["https://client.example/callback"], client_id=client_id)
                with self.assertRaises(OAuthConfigurationError):
                    OAuthConfiguration.load(self.registry, "https://tutor.example", "/mcp")

    def test_http_oauth_state_must_be_outside_language_workspaces(self) -> None:
        self.write_registry(["http://127.0.0.1:8765/callback"])
        data_root = Path(self.temporary_directory.name) / "tutor_data"
        state_path = data_root / "german" / ".oauth" / "state.sqlite3"
        config = ServerConfig(
            transport="http",
            oauth_enabled=True,
            oauth_clients_file=self.registry,
            public_base_url="https://tutor.example",
            oauth_state_path=state_path,
        )

        with patch("server.TUTOR_DATA_ROOT", data_root), patch.dict(
            os.environ,
            {
                "LINGUAMCP_OAUTH_PASSWORD": "disposable-test-password",
                "LINGUAMCP_OAUTH_STATE_PATH": str(state_path),
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "outside every language workspace"):
                _http_middleware(config)

        self.assertFalse(state_path.exists())
        with patch("server.TUTOR_DATA_ROOT", data_root):
            self.assertEqual(
                _oauth_state_path(data_root / ".oauth" / "state.sqlite3"),
                (data_root / ".oauth" / "state.sqlite3").resolve(),
            )

    def test_pkce_requires_s256_and_valid_unreserved_verifier(self) -> None:
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        validate_pkce_challenge(challenge, "S256")
        validate_pkce_verifier(verifier)
        with self.assertRaises(OAuthRequestError):
            validate_pkce_challenge(challenge, "plain")
        with self.assertRaises(OAuthRequestError):
            validate_pkce_challenge("short", "S256")
        with self.assertRaises(OAuthRequestError):
            validate_pkce_verifier("a" * 42)

    def test_authorization_and_refresh_endpoints_use_the_configured_client(self) -> None:
        client_id = "https://client.example/oauth/test/client.json"
        callback = "https://client.example/connector/oauth/test"
        self.write_registry([callback], client_id=client_id)
        configuration = OAuthConfiguration.load(
            self.registry, "https://tutor.example", "/mcp"
        )
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        form = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": callback,
            "state": "state-123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": configuration.resource_uri,
            "scope": "linguamcp",
            "password": "test-only-approval-password",
        }
        values = {key: str(value) for key, value in form.items() if key != "password"}
        self.assertIsNone(_validate_authorize_values(values, configuration))
        metadata = _oauth_authorization_metadata(
            SimpleNamespace(base_url="https://internal.example/"), configuration
        )
        self.assertEqual(metadata["issuer"], "https://tutor.example")
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])

        store = OAuthStore(Path(self.temporary_directory.name) / "oauth" / "state.sqlite3")
        approved = _oauth_authorize_submit(
            SimpleNamespace(), form, "test-only-approval-password", configuration, store
        )
        self.assertEqual(approved.status_code, 302)
        self.assertEqual(approved.headers["cache-control"], "no-store")
        redirect = urlsplit(approved.headers["location"])
        self.assertEqual(redirect.scheme + "://" + redirect.netloc + redirect.path, callback)
        query = parse_qs(redirect.query)
        self.assertEqual(query["state"], ["state-123"])
        code = query["code"][0]

        rejected = _oauth_authorize_submit(
            SimpleNamespace(), {**form, "password": "wrong"},
            "test-only-approval-password", configuration, store,
        )
        self.assertEqual(rejected.status_code, 403)
        bad_callback = _oauth_authorize_submit(
            SimpleNamespace(), {**form, "redirect_uri": callback + "/attacker"},
            "test-only-approval-password", configuration, store,
        )
        self.assertEqual(bad_callback.status_code, 400)

        exchanged = _oauth_token_response(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": callback,
                "resource": configuration.resource_uri,
                "code": code,
                "code_verifier": verifier,
            },
            configuration,
            store,
        )
        tokens = json.loads(exchanged.body)
        self.assertEqual(exchanged.status_code, 200)
        self.assertEqual(exchanged.headers["cache-control"], "no-store")
        self.assertTrue(
            store.is_access_token_valid(
                tokens["access_token"], resource=configuration.resource_uri, now=time.time()
            )
        )
        middleware = BearerAuthMiddleware(
            lambda *_: None,
            token="static-test-bearer-token",
            oauth_password="test-only-approval-password",
            oauth_enabled=True,
            oauth_configuration=configuration,
            oauth_store=store,
        )
        self.assertTrue(middleware._is_authorized("Bearer static-test-bearer-token"))
        self.assertTrue(middleware._is_authorized("Bearer " + tokens["access_token"]))
        self.assertFalse(middleware._is_authorized("Bearer invalid-token"))

        refreshed = _oauth_token_response(
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "resource": configuration.resource_uri,
                "scope": "linguamcp",
                "refresh_token": tokens["refresh_token"],
            },
            configuration,
            store,
        )
        next_tokens = json.loads(refreshed.body)
        self.assertEqual(refreshed.status_code, 200)
        self.assertNotEqual(next_tokens["refresh_token"], tokens["refresh_token"])

        replay = _oauth_token_response(
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "resource": configuration.resource_uri,
                "scope": "linguamcp",
                "refresh_token": tokens["refresh_token"],
            },
            configuration,
            store,
        )
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(json.loads(replay.body)["error"], "invalid_grant")
        self.assertFalse(
            store.is_access_token_valid(
                next_tokens["access_token"], resource=configuration.resource_uri, now=time.time()
            )
        )


if __name__ == "__main__":
    unittest.main()
