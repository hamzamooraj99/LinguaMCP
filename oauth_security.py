"""Strict configuration and URL validation for the small static-client OAuth flow."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
PKCE_CHALLENGE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
PKCE_VERIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class OAuthConfigurationError(ValueError):
    """The deployment-owned OAuth registry or canonical URL is invalid."""


class OAuthRequestError(ValueError):
    """An authorization or token request does not match configured policy."""


def _parse_url(value: Any, name: str):
    if not isinstance(value, str) or not value or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value) or "\\" in value:
        raise OAuthConfigurationError(f"{name} is not a valid absolute URL.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise OAuthConfigurationError(f"{name} is not a valid absolute URL.") from exc
    if not parsed.scheme or not parsed.netloc or not hostname:
        raise OAuthConfigurationError(f"{name} is not a valid absolute URL.")
    if parsed.username is not None or parsed.password is not None:
        raise OAuthConfigurationError(f"{name} must not contain user information.")
    if parsed.fragment:
        raise OAuthConfigurationError(f"{name} must not contain a fragment.")
    return parsed, hostname.lower()


def validate_redirect_uri(value: Any) -> str:
    parsed, hostname = _parse_url(value, "redirect URI")
    if parsed.scheme.lower() == "https":
        return value
    if parsed.scheme.lower() == "http" and hostname in LOOPBACK_HOSTS:
        return value
    raise OAuthConfigurationError("Redirect URI must use HTTPS or exact HTTP loopback.")


def validate_public_base_url(value: Any) -> str:
    parsed, _ = _parse_url(value, "LINGUAMCP_PUBLIC_BASE_URL")
    if parsed.scheme.lower() != "https":
        raise OAuthConfigurationError("LINGUAMCP_PUBLIC_BASE_URL must use HTTPS.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise OAuthConfigurationError("LINGUAMCP_PUBLIC_BASE_URL must contain only an HTTPS origin.")
    return value.rstrip("/")


@dataclass(frozen=True)
class OAuthConfiguration:
    clients: dict[str, tuple[str, ...]]
    public_base_url: str
    resource_uri: str
    mcp_path: str

    @classmethod
    def load(
        cls,
        clients_file: str | Path | None,
        public_base_url: str | None,
        mcp_path: str,
    ) -> "OAuthConfiguration":
        if not clients_file:
            raise OAuthConfigurationError("LINGUAMCP_OAUTH_CLIENTS_FILE is required when OAuth is enabled.")
        if not public_base_url:
            raise OAuthConfigurationError("LINGUAMCP_PUBLIC_BASE_URL is required when OAuth is enabled.")
        if not isinstance(mcp_path, str) or not mcp_path.startswith("/") or "?" in mcp_path or "#" in mcp_path or "\\" in mcp_path:
            raise OAuthConfigurationError("The configured MCP path is invalid.")
        try:
            raw = json.loads(Path(clients_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OAuthConfigurationError("The OAuth client registry cannot be read as JSON.") from exc
        if not isinstance(raw, dict) or set(raw) != {"clients"} or not isinstance(raw["clients"], dict) or not raw["clients"]:
            raise OAuthConfigurationError("The OAuth registry must contain a nonempty clients object.")
        clients: dict[str, tuple[str, ...]] = {}
        for client_id, record in raw["clients"].items():
            if not isinstance(client_id, str) or not CLIENT_ID_PATTERN.fullmatch(client_id):
                raise OAuthConfigurationError("The OAuth registry contains an invalid client ID.")
            if not isinstance(record, dict) or set(record) != {"redirect_uris"}:
                raise OAuthConfigurationError(f"The OAuth registry entry for {client_id} is invalid.")
            redirects = record["redirect_uris"]
            if (
                not isinstance(redirects, list)
                or not redirects
                or any(not isinstance(uri, str) for uri in redirects)
                or len(redirects) != len(set(redirects))
            ):
                raise OAuthConfigurationError(f"The OAuth registry entry for {client_id} needs unique redirect_uris.")
            clients[client_id] = tuple(validate_redirect_uri(uri) for uri in redirects)
        base = validate_public_base_url(public_base_url)
        path = mcp_path if mcp_path == "/" else mcp_path.rstrip("/")
        resource = base + path
        return cls(clients=clients, public_base_url=base, resource_uri=resource, mcp_path=path)

    def allows_redirect(self, client_id: str, redirect_uri: str) -> bool:
        return client_id in self.clients and redirect_uri in self.clients[client_id]


def validate_pkce_challenge(challenge: str, method: str) -> None:
    if method != "S256":
        raise OAuthRequestError("code_challenge_method must be S256.")
    if not isinstance(challenge, str) or not PKCE_CHALLENGE_PATTERN.fullmatch(challenge):
        raise OAuthRequestError("A valid S256 code_challenge is required.")


def validate_pkce_verifier(verifier: str) -> None:
    if not isinstance(verifier, str) or not PKCE_VERIFIER_PATTERN.fullmatch(verifier):
        raise OAuthRequestError("A valid code_verifier is required.")
