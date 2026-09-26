"""Private SQLite-backed OAuth code and rotating token storage."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from oauth_security import validate_pkce_verifier


ACCESS_TOKEN_TTL_SECONDS = 24 * 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


class OAuthStoreError(RuntimeError):
    """Persistent OAuth state is unavailable or invalid."""


class InvalidGrantError(ValueError):
    """A code or token is expired, invalid, replayed, or incorrectly bound."""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class OAuthStore:
    def __init__(self, path: str | Path) -> None:
        candidate = Path(path).expanduser()
        if candidate.is_symlink():
            raise OAuthStoreError("The OAuth state path is unsafe.")
        self.path = candidate.resolve(strict=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(self.path.parent, 0o700)
            self._initialize()
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error) as exc:
            raise OAuthStoreError("Persistent OAuth state could not be initialized.") from exc

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise OAuthStoreError("The OAuth state path is unsafe.")
        try:
            connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            raise OAuthStoreError("Persistent OAuth state is unavailable.") from exc

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS authorization_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grant_families (
                    family_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS access_tokens (
                    token_hash TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL REFERENCES grant_families(family_id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL REFERENCES grant_families(family_id) ON DELETE CASCADE,
                    consumed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS access_tokens_family_idx ON access_tokens(family_id);
                CREATE INDEX IF NOT EXISTS refresh_tokens_family_idx ON refresh_tokens(family_id);
                """
            )
        except sqlite3.Error as exc:
            raise OAuthStoreError("Persistent OAuth state could not be initialized.") from exc
        finally:
            connection.close()

    def store_authorization_code(
        self,
        code: str,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        resource: str,
        scope: str,
        expires_at: float,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO authorization_codes VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_token_hash(code), client_id, redirect_uri, code_challenge, resource, scope, expires_at),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise OAuthStoreError("Authorization could not be saved.") from exc
        finally:
            connection.close()

    def redeem_authorization_code(
        self,
        code: str,
        *,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
        resource: str,
        now: float,
    ) -> dict[str, Any]:
        try:
            validate_pkce_verifier(code_verifier)
        except ValueError as exc:
            raise InvalidGrantError("PKCE verification failed.") from exc
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM authorization_codes WHERE code_hash = ?",
                (_token_hash(code),),
            ).fetchone()
            if row is None:
                raise InvalidGrantError("Authorization code is invalid.")
            if float(row["expires_at"]) <= now:
                connection.execute("DELETE FROM authorization_codes WHERE code_hash = ?", (_token_hash(code),))
                connection.commit()
                raise InvalidGrantError("Authorization code has expired.")
            if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
                raise InvalidGrantError("Authorization code client or redirect URI does not match.")
            if row["resource"] != resource:
                raise InvalidGrantError("Authorization code resource does not match.")
            import base64

            digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
            calculated = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
            if not hmac.compare_digest(row["code_challenge"], calculated):
                raise InvalidGrantError("PKCE verification failed.")

            connection.execute("DELETE FROM authorization_codes WHERE code_hash = ?", (_token_hash(code),))
            family_id = str(uuid.uuid4())
            family_expiry = now + REFRESH_TOKEN_TTL_SECONDS
            connection.execute(
                "INSERT INTO grant_families VALUES (?, ?, ?, ?, ?, 0)",
                (family_id, row["client_id"], row["resource"], row["scope"], family_expiry),
            )
            response = self._issue_pair(connection, family_id, now, family_expiry, row["scope"])
            connection.commit()
            try:
                self.cleanup(now=now)
            except OAuthStoreError:
                pass
            return response
        except InvalidGrantError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise OAuthStoreError("Authorization state could not be read.") from exc
        finally:
            connection.close()

    def _issue_pair(
        self,
        connection: sqlite3.Connection,
        family_id: str,
        now: float,
        family_expiry: float,
        scope: str,
    ) -> dict[str, Any]:
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        access_expiry = min(now + ACCESS_TOKEN_TTL_SECONDS, family_expiry)
        connection.execute(
            "INSERT INTO access_tokens VALUES (?, ?, ?)",
            (_token_hash(access_token), family_id, access_expiry),
        )
        connection.execute(
            "INSERT INTO refresh_tokens VALUES (?, ?, 0)",
            (_token_hash(refresh_token), family_id),
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": max(0, int(access_expiry - now)),
            "scope": scope or "linguamcp",
        }

    def is_access_token_valid(self, token: str, *, resource: str, now: float) -> bool:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            row = connection.execute(
                """SELECT a.expires_at AS access_expiry, f.expires_at AS family_expiry,
                          f.revoked, f.resource, f.scope
                   FROM access_tokens a JOIN grant_families f ON f.family_id = a.family_id
                   WHERE a.token_hash = ?""",
                (_token_hash(token),),
            ).fetchone()
        except (OAuthStoreError, sqlite3.Error):
            return False
        finally:
            if connection is not None:
                connection.close()
        return bool(
            row
            and not row["revoked"]
            and float(row["access_expiry"]) > now
            and float(row["family_expiry"]) > now
            and row["resource"] == resource
            and row["scope"] == "linguamcp"
        )

    def rotate_refresh_token(
        self,
        refresh_token: str,
        *,
        client_id: str,
        resource: str,
        requested_scope: str,
        now: float,
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT r.consumed, r.family_id, f.client_id, f.resource, f.scope,
                          f.expires_at, f.revoked
                   FROM refresh_tokens r JOIN grant_families f ON f.family_id = r.family_id
                   WHERE r.token_hash = ?""",
                (_token_hash(refresh_token),),
            ).fetchone()
            if row is None:
                raise InvalidGrantError("Refresh token is invalid.")
            if row["consumed"]:
                connection.execute("UPDATE grant_families SET revoked = 1 WHERE family_id = ?", (row["family_id"],))
                connection.commit()
                raise InvalidGrantError("Refresh token replay revoked this grant.")
            if row["revoked"] or float(row["expires_at"]) <= now:
                raise InvalidGrantError("Refresh token is expired or revoked.")
            if row["client_id"] != client_id or row["resource"] != resource:
                raise InvalidGrantError("Refresh token client or resource does not match.")
            requested = requested_scope or row["scope"]
            if set(requested.split()) - set(row["scope"].split()):
                raise InvalidGrantError("The requested scope is wider than the original grant.")
            connection.execute(
                "UPDATE refresh_tokens SET consumed = 1 WHERE token_hash = ?",
                (_token_hash(refresh_token),),
            )
            response = self._issue_pair(
                connection,
                row["family_id"],
                now,
                float(row["expires_at"]),
                row["scope"],
            )
            connection.commit()
            try:
                self.cleanup(now=now)
            except OAuthStoreError:
                pass
            return response
        except InvalidGrantError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise OAuthStoreError("Refresh state could not be updated.") from exc
        finally:
            connection.close()

    def revoke_all(self) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("UPDATE grant_families SET revoked = 1 WHERE revoked = 0")
            count = cursor.rowcount
            connection.commit()
            return count
        except sqlite3.Error as exc:
            connection.rollback()
            raise OAuthStoreError("OAuth grants could not be revoked.") from exc
        finally:
            connection.close()

    def cleanup(self, *, now: float) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            codes = connection.execute(
                "SELECT code_hash FROM authorization_codes WHERE expires_at <= ? LIMIT 100", (now,)
            ).fetchall()
            for row in codes:
                connection.execute("DELETE FROM authorization_codes WHERE code_hash = ?", (row["code_hash"],))
            access = connection.execute(
                "SELECT token_hash FROM access_tokens WHERE expires_at <= ? LIMIT 100", (now,)
            ).fetchall()
            for row in access:
                connection.execute("DELETE FROM access_tokens WHERE token_hash = ?", (row["token_hash"],))
            families = connection.execute(
                "SELECT family_id FROM grant_families WHERE expires_at <= ? LIMIT 100", (now,)
            ).fetchall()
            for row in families:
                connection.execute("DELETE FROM grant_families WHERE family_id = ?", (row["family_id"],))
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise OAuthStoreError("Expired OAuth state could not be cleaned up.") from exc
        finally:
            connection.close()
