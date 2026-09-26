"""FastMCP server for local, Markdown-backed language tutor memory."""

from __future__ import annotations

import re
import base64
import hashlib
import html
import json
import logging
import os
import secrets
import sys
import time
import uuid
from functools import wraps
from urllib.parse import urlencode
from hmac import compare_digest
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from lesson_contract import (
    CONTRACT_START,
    ContractValidationError,
    apply_assessment,
    apply_state_updates,
    create_metadata,
    mark_completed,
    parse_contract_document,
    readiness,
    render_mastery_record,
    serialize_contract_document,
)
from memory_storage import (
    ABSENT_VERSION,
    CONTEXT_LIMIT_CHARS,
    CUMULATIVE_FILES,
    DELIVERY_FILES as STORAGE_DELIVERY_FILES,
    LESSON_CONTRACT_FILE,
    MAX_READ_BYTES,
    MEMORY_LOCK,
    READ_PAGE_LIMIT_CHARS,
    VOLATILE_FILES,
    OversizedFileError,
    RecoveryRequiredError,
    StorageError,
    VersionConflictError,
    WriterLock,
    atomic_write,
    check_version,
    check_operation,
    commit_write_group,
    file_version,
    normalize_operation_id,
    paged_read,
    recover_data_root,
    recover_pending,
    read_limited_bytes,
    request_fingerprint,
    save_single_file,
    validate_content,
    version_bytes,
)
from oauth_security import (
    OAuthConfiguration,
    OAuthConfigurationError,
    OAuthRequestError,
    validate_pkce_challenge,
    validate_pkce_verifier,
)
from oauth_store import (
    ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_TOKEN_TTL_SECONDS,
    InvalidGrantError,
    OAuthStore,
    OAuthStoreError,
)

try:
    from fastmcp import FastMCP
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
except ModuleNotFoundError:
    class FastMCP:  # type: ignore[no-redef]
        """Small import-time shim so storage verification can run before setup."""

        def __init__(
            self, name: str, instructions: str | None = None, **_: Any
        ) -> None:
            self.name = name
            self.instructions = instructions

        def tool(self, function: Any) -> Any:
            return function

        def run(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "FastMCP is not installed. Run: python -m pip install -r requirements.txt"
            )

    Middleware = None  # type: ignore[assignment]
    BaseHTTPMiddleware = object  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[assignment]
    Response = Any  # type: ignore[assignment]
    HTMLResponse = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    RedirectResponse = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parent
TUTOR_DATA_ROOT = PROJECT_ROOT / "tutor_data"
TEMPLATE_ROOT = PROJECT_ROOT / "templates"

CONTEXT_FILES = (
    "00-profile.md",
    "01-lesson-plan.md",
    "02-progress.md",
    "03-vocabulary.md",
    "04-mistakes.md",
    "05-scenarios.md",
    "latest-summary.md",
    "active-session.md",
    "latest-homework.md",
)

DELIVERY_FILES = (
    "delivery/latest-email.md",
    "delivery/latest-whatsapp.md",
)

ALLOWED_FILES = CONTEXT_FILES + DELIVERY_FILES
OPTIONAL_CONTEXT_FILES = (LESSON_CONTRACT_FILE,)
READABLE_FILES = ALLOWED_FILES + OPTIONAL_CONTEXT_FILES

COMPACTABLE_FILES = (
    "00-profile.md",
    "01-lesson-plan.md",
    "02-progress.md",
    "03-vocabulary.md",
    "04-mistakes.md",
    "05-scenarios.md",
)

CONTEXT_COMPACTION_THRESHOLD_CHARS = 12_000
TOTAL_CONTEXT_COMPACTION_THRESHOLD_CHARS = 36_000

MCP_SERVER_INSTRUCTIONS = (
    "Before teaching, call read_language_context and "
    "get_language_context_status. Treat returned learner files as "
    "authoritative. During long lessons, checkpoint after major topics or "
    "roleplays and about every 20-30 turns. Before ending, update applicable "
    "00 profile, 01 plan, 02 progress, 03 vocabulary, 04 mistakes, and 05 "
    "scenarios. Save homework before the session summary. Never claim memory "
    "or a saved change without a successful write. Follow the protocol's "
    "versions, lesson contract, and completion rules."
)

MEMORY_PROTOCOL = """# LinguaMCP Memory Protocol

Read all returned context and `get_language_context_status` before teaching. The profile governs teaching method; the lesson plan governs curriculum; an active lesson contract governs this lesson's scope and completion. Resume its checkpoint. A completed contract is reference only; create a new one before starting another lesson.

The six cumulative files are `00-profile.md`, `01-lesson-plan.md`, `02-progress.md`, `03-vocabulary.md`, `04-mistakes.md`, and `05-scenarios.md`. For every protected replacement, retain the file hash returned with the content and pass it as `expected_version`. On conflict, reread and deliberately merge; do not resubmit a stale replacement with a newer hash. Use bounded pages for large files and keep the same version on every page. If full context reports `context_too_large`, read status and retrieve needed files in pages; no lesson context was silently shortened.

Before new material, create a finite contract from the plan, profile, progress, vocabulary, mistakes, scenarios, homework, summary, and checkpoint. Include required competencies, evidence thresholds, boundaries, deferred topics, critical gaps, and a completion rule. Digressions can be answered, then return to the active contract; optional enrichment does not become a requirement. Replace scope only intentionally, using the current contract token. State updates record meaningful changes and clear prior assessment. Checkpoints hold immediate evidence and next actions, not requirements or formal assessment.

Assess every required competency using actual learner evidence. Distinguish introduction, recognition, supported production, independent performance, and delayed or mixed retrieval. A supported answer does not meet an independent threshold. The server checks structure and readiness but cannot verify that reported evidence is truthful. An assessment is not permanent progress.

At lesson end, update or explicitly leave unchanged all six cumulative files. If a contract exists, use the latest passing assessment and token, and include its exact returned mastery block once in the complete progress replacement. Use `finalize_lesson`, with all returned file versions and a fresh UUID operation ID. Retrying the same intended save reuses the same ID and exact payload. A different payload needs a new ID. Finalization saves the mastery record, cumulative changes, homework, session log, summary, checkpoint reset, and contract completion through one recoverable write group. Do not use generic writes or standalone logging to declare a contract complete.

Standalone session logging requires a fresh operation ID and the latest-summary version; it preserves the checkpoint because pausing does not finish a lesson. Save a checkpoint with its current file version when stopping unfinished work. Only successful finalization clears it. Compaction applies only to the six cumulative files and must preserve mastery criteria and evidence needed later. Shorten an oversized contract through deliberate replacement, never ordinary compaction.

Session logs are excluded from normal context. Use `list_language_sessions` and bounded, version-checked `read_language_session` pages when older evidence is needed. A conversational promise is not stored memory until a write succeeds.
"""

TEMPLATE_FILES = {
    "00-profile.md": "00-profile.md",
    "01-lesson-plan.md": "01-lesson-plan.md",
    "02-progress.md": "02-progress.md",
    "03-vocabulary.md": "03-vocabulary.md",
    "04-mistakes.md": "04-mistakes.md",
    "05-scenarios.md": "05-scenarios.md",
    "latest-summary.md": "latest-summary.md",
    "active-session.md": "active-session.md",
    "latest-homework.md": "latest-homework.md",
    "delivery/latest-email.md": "latest-email.md",
    "delivery/latest-whatsapp.md": "latest-whatsapp.md",
}

LANGUAGE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,49}$")
TIMESTAMPED_MARKDOWN_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{6}Z\.md$"
)
NOTE_FILENAME_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9_-]{0,98}\.md$", re.IGNORECASE
)

mcp = FastMCP(
    "Local Language Tutor Memory",
    instructions=MCP_SERVER_INSTRUCTIONS,
)


@dataclass(frozen=True)
class ServerConfig:
    """Runtime transport settings for the MCP server."""

    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    log_level: str = "info"
    allow_writes: bool = True
    require_auth: bool = False
    auth_token_env: str = "LINGUAMCP_AUTH_TOKEN"
    oauth_enabled: bool = False
    oauth_password_env: str = "LINGUAMCP_OAUTH_PASSWORD"
    oauth_clients_file: Path | None = None
    public_base_url: str | None = None
    oauth_state_path: Path = TUTOR_DATA_ROOT / ".oauth" / "state.sqlite3"
    revoke_oauth_grants: bool = False
    audit_enabled: bool = True
    audit_log: Path = TUTOR_DATA_ROOT / "audit-log.jsonl"


@dataclass(frozen=True)
class RuntimeSecurity:
    """Mutable runtime security policy used by MCP tool wrappers."""

    allow_writes: bool = True
    audit_enabled: bool = True
    audit_log: Path = TUTOR_DATA_ROOT / "audit-log.jsonl"


CURRENT_SECURITY = RuntimeSecurity()
OAUTH_CODE_TTL_SECONDS = 300
OAUTH_LOGGER = logging.getLogger("uvicorn.error")


class BearerAuthMiddleware(BaseHTTPMiddleware):  # type: ignore[misc,valid-type]
    """Require a static bearer or persistent OAuth token, with OAuth opt-in."""

    def __init__(
        self,
        app: Any,
        token: str | None = None,
        oauth_password: str | None = None,
        oauth_enabled: bool = False,
        oauth_configuration: OAuthConfiguration | None = None,
        oauth_store: OAuthStore | None = None,
    ) -> None:
        super().__init__(app)
        self._expected_header = f"Bearer {token}" if token else None
        self._oauth_password = oauth_password
        self._oauth_enabled = oauth_enabled
        self._oauth_configuration = oauth_configuration
        self._oauth_store = oauth_store

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        oauth_response = (
            await self._handle_oauth_request(request) if self._oauth_enabled else None
        )
        if oauth_response is not None:
            return oauth_response

        authorization = request.headers.get("authorization", "")
        if not self._is_authorized(authorization):
            challenge = "Bearer"
            if self._oauth_enabled and self._oauth_configuration is not None:
                resource_metadata = self._oauth_configuration.public_base_url + "/.well-known/oauth-protected-resource"
                challenge = f'Bearer resource_metadata="{resource_metadata}"'
            return JSONResponse(  # type: ignore[misc]
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": challenge},
            )
        return await call_next(request)

    def _is_authorized(self, authorization: str) -> bool:
        if self._expected_header and compare_digest(authorization, self._expected_header):
            return True
        if not authorization.startswith("Bearer "):
            return False
        token = authorization.removeprefix("Bearer ").strip()
        if self._oauth_store is None or self._oauth_configuration is None:
            return False
        return self._oauth_store.is_access_token_valid(
            token,
            resource=self._oauth_configuration.resource_uri,
            now=time.time(),
        )

    async def _handle_oauth_request(self, request: Request) -> Response | None:
        path = request.url.path.rstrip("/") or "/"
        if path == "/.well-known/oauth-authorization-server":
            return JSONResponse(_oauth_authorization_metadata(request, self._oauth_configuration), headers={"Cache-Control": "no-store"})  # type: ignore[misc]
        if path == "/.well-known/oauth-protected-resource":
            return JSONResponse(_oauth_protected_resource_metadata(request, self._oauth_configuration), headers={"Cache-Control": "no-store"})  # type: ignore[misc]
        if path == "/oauth/authorize" and request.method == "GET":
            return _oauth_authorize_form(request, self._oauth_configuration)
        if path == "/oauth/authorize" and request.method == "POST":
            form = await request.form()
            return _oauth_authorize_submit(
                request,
                dict(form),
                self._oauth_password,
                self._oauth_configuration,
                self._oauth_store,
            )
        if path == "/oauth/token" and request.method == "POST":
            form = await request.form()
            return _oauth_token_response(
                dict(form), self._oauth_configuration, self._oauth_store
            )
        return None


def _oauth_base_url(
    request: Request, configuration: OAuthConfiguration | None = None
) -> str:
    if configuration is not None:
        return configuration.public_base_url
    return str(request.base_url).rstrip("/")


def _oauth_authorization_metadata(
    request: Request, configuration: OAuthConfiguration | None = None
) -> dict[str, Any]:
    base_url = _oauth_base_url(request, configuration)
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "client_id_metadata_document_supported": False,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["linguamcp"],
    }


def _oauth_protected_resource_metadata(
    request: Request, configuration: OAuthConfiguration | None = None
) -> dict[str, Any]:
    base_url = _oauth_base_url(request, configuration)
    return {
        "resource": configuration.resource_uri if configuration is not None else f"{base_url}/mcp",
        "authorization_servers": [base_url],
        "scopes_supported": ["linguamcp"],
        "bearer_methods_supported": ["header"],
    }


def _oauth_authorize_form(
    request: Request, configuration: OAuthConfiguration | None = None
) -> Response:
    query = request.query_params
    values = {
        "response_type": query.get("response_type", ""),
        "client_id": query.get("client_id", ""),
        "redirect_uri": query.get("redirect_uri", ""),
        "state": query.get("state", ""),
        "code_challenge": query.get("code_challenge", ""),
        "code_challenge_method": query.get("code_challenge_method", ""),
        "resource": query.get("resource", ""),
        "scope": query.get("scope", ""),
    }
    error = _validate_authorize_values(values, configuration)
    if error:
        return JSONResponse({"error": "invalid_request", "error_description": error}, status_code=400, headers={"Cache-Control": "no-store"})  # type: ignore[misc]

    hidden_inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
        for key, value in values.items()
    )
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorize LinguaMCP</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 42rem; margin: 4rem auto; padding: 0 1rem; line-height: 1.5; }}
    label, input, button {{ display: block; width: 100%; box-sizing: border-box; }}
    input {{ margin: .5rem 0 1rem; padding: .65rem; }}
    button {{ padding: .75rem; }}
    code {{ background: #f2f2f2; padding: .1rem .25rem; }}
  </style>
</head>
<body>
  <h1>Authorize LinguaMCP</h1>
  <p>Approve access for client <code>{html.escape(values["client_id"])}</code>.</p>
  <p>This grants access to the local tutor memory MCP tools exposed by this server.</p>
  <form method="post" action="/oauth/authorize">
    {hidden_inputs}
    <label for="password">OAuth approval password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Authorize</button>
  </form>
</body>
</html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})  # type: ignore[misc]


def _oauth_authorize_submit(
    request: Request,
    form: dict[str, Any],
    oauth_password: str | None,
    configuration: OAuthConfiguration | None = None,
    oauth_store: OAuthStore | None = None,
) -> Response:
    values = {
        "response_type": str(form.get("response_type", "")),
        "client_id": str(form.get("client_id", "")),
        "redirect_uri": str(form.get("redirect_uri", "")),
        "state": str(form.get("state", "")),
        "code_challenge": str(form.get("code_challenge", "")),
        "code_challenge_method": str(form.get("code_challenge_method", "")),
        "resource": str(form.get("resource", "")),
        "scope": str(form.get("scope", "")),
    }
    error = _validate_authorize_values(values, configuration)
    if error:
        return JSONResponse({"error": "invalid_request", "error_description": error}, status_code=400, headers={"Cache-Control": "no-store"})  # type: ignore[misc]
    if not oauth_password or oauth_store is None:
        return JSONResponse({"error": "server_error"}, status_code=500, headers={"Cache-Control": "no-store"})  # type: ignore[misc]
    submitted_password = str(form.get("password", ""))
    if not compare_digest(submitted_password, oauth_password):
        return JSONResponse({"error": "access_denied"}, status_code=403, headers={"Cache-Control": "no-store"})  # type: ignore[misc]

    code = secrets.token_urlsafe(32)
    try:
        oauth_store.store_authorization_code(
            code,
            client_id=values["client_id"],
            redirect_uri=values["redirect_uri"],
            code_challenge=values["code_challenge"],
            resource=values["resource"],
            scope=values["scope"],
            expires_at=time.time() + OAUTH_CODE_TTL_SECONDS,
        )
    except OAuthStoreError:
        return JSONResponse({"error": "server_error"}, status_code=500, headers={"Cache-Control": "no-store"})  # type: ignore[misc]
    redirect_params = {"code": code}
    if values["state"]:
        redirect_params["state"] = values["state"]
    separator = "&" if "?" in values["redirect_uri"] else "?"
    redirect_url = values["redirect_uri"] + separator + urlencode(redirect_params)
    response = RedirectResponse(redirect_url, status_code=302)  # type: ignore[misc]
    response.headers["Cache-Control"] = "no-store"
    return response


def _validate_authorize_values(
    values: dict[str, str], configuration: OAuthConfiguration | None
) -> str | None:
    if values["response_type"] != "code":
        return "response_type must be code."
    if configuration is None:
        return "OAuth client configuration is unavailable."
    client_id = values["client_id"]
    redirect_uri = values["redirect_uri"]
    if not client_id or client_id not in configuration.clients:
        return "client_id is not configured."
    if not configuration.allows_redirect(client_id, redirect_uri):
        return "redirect_uri is not configured for this client."
    if len(values["state"]) > 2048 or any(ord(char) < 0x20 for char in values["state"]):
        return "state is invalid."
    try:
        validate_pkce_challenge(values["code_challenge"], values["code_challenge_method"])
    except OAuthRequestError as exc:
        return str(exc)
    if values["resource"] != configuration.resource_uri:
        return "resource must identify this configured MCP endpoint."
    if values["scope"] != "linguamcp":
        return "scope must be linguamcp."
    return None


def _oauth_token_response(
    form: dict[str, Any],
    configuration: OAuthConfiguration | None = None,
    oauth_store: OAuthStore | None = None,
) -> Response:
    if configuration is None or oauth_store is None:
        return _oauth_error("server_error", "OAuth is not configured.")
    grant_type = str(form.get("grant_type", ""))
    client_id = str(form.get("client_id", ""))
    resource = str(form.get("resource", ""))
    if grant_type == "authorization_code":
        try:
            tokens = oauth_store.redeem_authorization_code(
                str(form.get("code", "")),
                client_id=client_id,
                redirect_uri=str(form.get("redirect_uri", "")),
                code_verifier=str(form.get("code_verifier", "")),
                resource=resource,
                now=time.time(),
            )
        except InvalidGrantError as exc:
            return _oauth_error("invalid_grant", str(exc))
        except OAuthStoreError:
            return _oauth_error("server_error", "Persistent OAuth state is unavailable.")
        return JSONResponse(tokens, headers={"Cache-Control": "no-store"})  # type: ignore[misc]

    if grant_type == "refresh_token":
        if resource and resource != configuration.resource_uri:
            return _oauth_error("invalid_grant", "The requested resource does not match the original grant.")
        try:
            tokens = oauth_store.rotate_refresh_token(
                str(form.get("refresh_token", "")),
                client_id=client_id,
                resource=configuration.resource_uri,
                requested_scope=str(form.get("scope", "")),
                now=time.time(),
            )
        except InvalidGrantError as exc:
            return _oauth_error("invalid_grant", str(exc))
        except OAuthStoreError:
            return _oauth_error("server_error", "Persistent OAuth state is unavailable.")
        return JSONResponse(tokens, headers={"Cache-Control": "no-store"})  # type: ignore[misc]

    return _oauth_error("unsupported_grant_type", "Only authorization_code and refresh_token are supported.")


def _verify_pkce(challenge: str, method: str, verifier: str) -> bool:
    try:
        validate_pkce_challenge(challenge, method)
        validate_pkce_verifier(verifier)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
    except (OAuthRequestError, UnicodeEncodeError):
        return False
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return compare_digest(challenge, computed)


def _oauth_error(error: str, description: str) -> Response:
    return JSONResponse(  # type: ignore[misc]
        {"error": error, "error_description": description},
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


def parse_server_config(argv: list[str] | None = None) -> ServerConfig:
    """Parse command-line options while keeping stdio as the default."""
    parser = ArgumentParser(
        description="Run the local language tutor memory MCP server."
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run an HTTP MCP endpoint instead of the default stdio transport.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind in HTTP mode. Use 0.0.0.0 for Docker access.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind in HTTP mode.",
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help="HTTP MCP endpoint path.",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="HTTP server log level.",
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help=(
            "Allow write-capable MCP tools in HTTP mode. Stdio mode allows "
            "writes by default."
        ),
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Block all write-capable MCP tools for this server process.",
    )
    parser.add_argument(
        "--require-auth",
        action="store_true",
        help="Require HTTP requests to include a bearer token.",
    )
    parser.add_argument(
        "--auth-token-env",
        default="LINGUAMCP_AUTH_TOKEN",
        help="Environment variable containing the HTTP bearer token.",
    )
    parser.add_argument(
        "--oauth",
        action="store_true",
        help="Enable a minimal single-user OAuth flow for ChatGPT connectors.",
    )
    parser.add_argument(
        "--oauth-password-env",
        default="LINGUAMCP_OAUTH_PASSWORD",
        help="Environment variable containing the OAuth approval password.",
    )
    parser.add_argument(
        "--oauth-clients-file",
        default=os.environ.get("LINGUAMCP_OAUTH_CLIENTS_FILE"),
        help="Deployment-owned JSON file containing approved OAuth clients and exact callbacks.",
    )
    parser.add_argument(
        "--public-base-url",
        default=os.environ.get("LINGUAMCP_PUBLIC_BASE_URL"),
        help="Canonical HTTPS issuer origin advertised by OAuth metadata.",
    )
    parser.add_argument(
        "--oauth-state-path",
        default=os.environ.get(
            "LINGUAMCP_OAUTH_STATE_PATH",
            str(TUTOR_DATA_ROOT / ".oauth" / "state.sqlite3"),
        ),
        help="Private SQLite file for persistent OAuth grants.",
    )
    parser.add_argument(
        "--revoke-oauth-grants",
        action="store_true",
        help="Revoke every persisted OAuth grant and exit without starting the server.",
    )
    parser.add_argument(
        "--audit-log",
        default=str(TUTOR_DATA_ROOT / "audit-log.jsonl"),
        help="Path to a JSONL audit log for MCP tool calls.",
    )
    parser.add_argument(
        "--no-audit",
        action="store_true",
        help="Disable local JSONL audit logging.",
    )
    args = parser.parse_args(argv)

    if args.port < 1 or args.port > 65535:
        parser.error("--port must be between 1 and 65535.")
    if not args.path.startswith("/"):
        parser.error("--path must start with '/'.")

    if not args.auth_token_env:
        parser.error("--auth-token-env must not be empty.")
    if not args.oauth_password_env:
        parser.error("--oauth-password-env must not be empty.")

    allow_writes = not args.read_only and (not args.http or args.allow_writes)

    return ServerConfig(
        transport="http" if args.http else "stdio",
        host=args.host,
        port=args.port,
        path=args.path,
        log_level=args.log_level,
        allow_writes=allow_writes,
        require_auth=args.require_auth,
        auth_token_env=args.auth_token_env,
        oauth_enabled=args.oauth,
        oauth_password_env=args.oauth_password_env,
        oauth_clients_file=Path(args.oauth_clients_file) if args.oauth_clients_file else None,
        public_base_url=args.public_base_url,
        oauth_state_path=Path(args.oauth_state_path),
        revoke_oauth_grants=args.revoke_oauth_grants,
        audit_enabled=not args.no_audit,
        audit_log=Path(args.audit_log),
    )


def configure_runtime_security(config: ServerConfig) -> None:
    """Apply runtime security policy used by MCP tool wrappers."""
    global CURRENT_SECURITY
    CURRENT_SECURITY = RuntimeSecurity(
        allow_writes=config.allow_writes,
        audit_enabled=config.audit_enabled,
        audit_log=config.audit_log,
    )


def _oauth_state_path(path: Path, *, data_root: Path | None = None) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    root = (data_root or TUTOR_DATA_ROOT).resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return resolved
    if relative.parts and LANGUAGE_PATTERN.fullmatch(relative.parts[0]):
        raise OAuthConfigurationError(
            "OAuth state must be stored outside every language workspace."
        )
    return resolved


def _http_middleware(config: ServerConfig) -> list[Any]:
    if config.transport != "http":
        return []
    token = os.environ.get(config.auth_token_env)
    oauth_password = os.environ.get(config.oauth_password_env)
    if config.require_auth and not token:
        raise RuntimeError(
            f"HTTP authentication is required, but {config.auth_token_env} is not set."
        )
    if config.oauth_enabled and not oauth_password:
        raise RuntimeError(
            f"OAuth is enabled, but {config.oauth_password_env} is not set."
        )
    oauth_configuration: OAuthConfiguration | None = None
    oauth_store: OAuthStore | None = None
    if config.oauth_enabled:
        try:
            oauth_configuration = OAuthConfiguration.load(
                config.oauth_clients_file or os.environ.get("LINGUAMCP_OAUTH_CLIENTS_FILE"),
                config.public_base_url or os.environ.get("LINGUAMCP_PUBLIC_BASE_URL"),
                config.path,
            )
            store_path = _oauth_state_path(Path(
                os.environ.get("LINGUAMCP_OAUTH_STATE_PATH", str(config.oauth_state_path))
            ))
            oauth_store = OAuthStore(store_path)
        except (OAuthConfigurationError, OAuthStoreError) as exc:
            raise RuntimeError(f"OAuth configuration failed: {exc}") from exc
    if not token and not config.oauth_enabled:
        return []
    if Middleware is None:
        raise RuntimeError(
            "HTTP authentication requires FastMCP and Starlette to be installed."
        )
    return [
        Middleware(
            BearerAuthMiddleware,
            token=token,
            oauth_password=oauth_password if config.oauth_enabled else None,
            oauth_enabled=config.oauth_enabled,
            oauth_configuration=oauth_configuration,
            oauth_store=oauth_store,
        )
    ]


def run_server(config: ServerConfig) -> None:
    """Start FastMCP with the requested transport."""
    configure_runtime_security(config)
    if config.revoke_oauth_grants:
        state_path = _oauth_state_path(
            Path(os.environ.get("LINGUAMCP_OAUTH_STATE_PATH", str(config.oauth_state_path)))
        )
        count = OAuthStore(state_path).revoke_all()
        print(f"Revoked {count} OAuth grant family/families.", file=sys.stderr)
        return

    writer_lock: WriterLock | None = None
    if config.allow_writes:
        writer_lock = WriterLock(TUTOR_DATA_ROOT)
        writer_lock.acquire()
        try:
            recover_data_root(TUTOR_DATA_ROOT)
        except Exception:
            writer_lock.release()
            raise
    try:
        if config.transport == "stdio":
            mcp.run()
            return

        mcp.run(
            transport=config.transport,
            host=config.host,
            port=config.port,
            path=config.path,
            log_level=config.log_level,
            middleware=_http_middleware(config),
        )
    finally:
        if writer_lock is not None:
            writer_lock.release()


def _audit_tool_call(
    tool_name: str,
    status: str,
    *,
    language: str | None = None,
    filename: str | None = None,
    detail: str | None = None,
) -> None:
    if not CURRENT_SECURITY.audit_enabled:
        return
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "status": status,
    }
    if language is not None:
        record["language"] = language
    if filename is not None:
        record["filename"] = filename
    if detail is not None:
        record["detail"] = detail

    try:
        CURRENT_SECURITY.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with CURRENT_SECURITY.audit_log.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError as exc:
        # Audit recording is best-effort; keep diagnostics off MCP stdout and
        # never include learner content, credentials, or filesystem paths.
        OAUTH_LOGGER.warning("LinguaMCP audit write failed (%s).", type(exc).__name__)


def _run_tool(
    tool_name: str,
    operation: Callable[[], Any],
    *,
    writes: bool = False,
    language: str | None = None,
    filename: str | None = None,
) -> Any:
    if writes and not CURRENT_SECURITY.allow_writes:
        _audit_tool_call(
            tool_name,
            "blocked_write",
            language=language,
            filename=filename,
            detail="Write-capable tools are disabled for this server process.",
        )
        raise PermissionError(
            "Write-capable tools are disabled for this server process. "
            "Start with --allow-writes to permit updates."
        )

    try:
        result = operation()
    except Exception as exc:
        _audit_tool_call(
            tool_name,
            "error",
            language=language,
            filename=filename,
            detail=type(exc).__name__,
        )
        raise

    _audit_tool_call(tool_name, "success", language=language, filename=filename)
    return result


def _serialized_storage(function: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize a language operation and recover its published write group."""
    @wraps(function)
    def wrapped(language: str, *args: Any, **kwargs: Any) -> Any:
        data_root = Path(kwargs.get("data_root", TUTOR_DATA_ROOT))
        normalized, language_dir = _language_directory(language, data_root)
        with MEMORY_LOCK:
            if language_dir.exists() and language_dir.is_dir():
                recover_pending(
                    language_dir,
                    read_only=not CURRENT_SECURITY.allow_writes,
                )
            return function(normalized, *args, **kwargs)

    return wrapped


def _normalize_language(language: str) -> str:
    if not isinstance(language, str):
        raise ValueError("Language must be a string.")
    normalized = language.strip().lower()
    if not LANGUAGE_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Invalid language. Use 1-50 lowercase letters, digits, or hyphens, "
            "starting with a letter."
        )
    return normalized


def _require_markdown(content: str, field_name: str = "content") -> str:
    if not isinstance(content, str):
        raise ValueError(f"{field_name} must be a string containing Markdown.")
    return content


def _safe_child(root: Path, *parts: str) -> Path:
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*parts)
    current = resolved_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Invalid path: symbolic links are not allowed in tutor_data.")
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise ValueError("Invalid path: access outside tutor_data is not allowed.")
    return candidate


def _language_directory(language: str, data_root: Path) -> tuple[str, Path]:
    normalized = _normalize_language(language)
    return normalized, _safe_child(data_root, normalized)


def _read_template(filename: str, template_root: Path) -> str:
    template_path = _safe_child(template_root, TEMPLATE_FILES[filename])
    try:
        return template_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required template is missing: {filename}") from exc


def _read_file_text(path: Path) -> tuple[str, str, int]:
    try:
        raw = read_limited_bytes(path)
    except OversizedFileError:
        raise
    except (OSError, StorageError) as exc:
        raise ValueError("Could not read learner memory.") from exc
    try:
        return raw.decode("utf-8"), version_bytes(raw), len(raw)
    except UnicodeDecodeError as exc:
        raise ValueError("Learner memory is not valid UTF-8.") from exc


def _utc_filename(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return timestamp.strftime("%Y-%m-%dT%H-%M-%S-%fZ.md")


def _unique_timestamped_filename(directory: Path, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for _ in range(1_000_000):
        filename = _utc_filename(timestamp)
        if not (directory / filename).exists():
            return filename
        timestamp += timedelta(microseconds=1)
    raise StorageError("Could not allocate a unique timestamped filename.")


def _backup_name(language_dir: Path, filename: str, now: datetime | None = None) -> str:
    if filename in CUMULATIVE_FILES:
        directory = _safe_child(language_dir, "archives", filename.removesuffix(".md"))
        directory.mkdir(parents=True, exist_ok=True)
        archive_name = _unique_timestamped_filename(directory, now)
        return f"archives/{filename.removesuffix('.md')}/{archive_name}"
    return f".backups/{filename}"


def _validate_allowed_filename(filename: str) -> str:
    if not isinstance(filename, str) or filename not in ALLOWED_FILES:
        raise ValueError(
            "Invalid filename. Allowed files: " + ", ".join(ALLOWED_FILES)
        )
    return filename


def _validate_note_filename(filename: str) -> str:
    if (
        not isinstance(filename, str)
        or not NOTE_FILENAME_PATTERN.fullmatch(filename)
    ):
        raise ValueError(
            "Invalid note filename. Use a .md filename containing only letters, "
            "digits, hyphens, or underscores (maximum 102 characters)."
        )
    return filename


def _require_language_profile(language: str, data_root: Path) -> tuple[str, Path]:
    normalized, language_dir = _language_directory(language, data_root)
    if not language_dir.is_dir():
        raise ValueError(f"Language profile does not exist: {normalized}")
    return normalized, language_dir


@_serialized_storage
def initialize_profile(
    language: str,
    profile_markdown: str,
    lesson_plan_markdown: str,
    overwrite_existing: bool = False,
    expected_versions: dict[str, str] | None = None,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
    template_root: Path = TEMPLATE_ROOT,
) -> dict[str, Any]:
    """Create a language directory and its standard Markdown files."""
    normalized, language_dir = _language_directory(language, data_root)
    profile_markdown = _require_markdown(profile_markdown, "profile_markdown")
    lesson_plan_markdown = _require_markdown(
        lesson_plan_markdown, "lesson_plan_markdown"
    )

    validate_content("00-profile.md", profile_markdown)
    validate_content("01-lesson-plan.md", lesson_plan_markdown)
    existing_overwrite: list[str] = []
    if overwrite_existing:
        for filename in ("00-profile.md", "01-lesson-plan.md"):
            path = _safe_child(language_dir, filename)
            if path.exists():
                existing_overwrite.append(filename)
        if existing_overwrite and not isinstance(expected_versions, dict):
            raise StorageError("expected_versions is required when overwrite_existing replaces existing files.")
        for filename in existing_overwrite:
            expected = expected_versions.get(filename) if expected_versions else None
            check_version(_safe_child(language_dir, filename), expected)

    language_dir.mkdir(parents=True, exist_ok=True)
    for directory in ("sessions", "delivery", "archives", "notes"):
        _safe_child(language_dir, directory).mkdir(exist_ok=True)

    supplied_content = {
        "00-profile.md": profile_markdown,
        "01-lesson-plan.md": lesson_plan_markdown,
    }
    created: list[str] = []
    overwritten: list[str] = []
    preserved: list[str] = []

    versions: dict[str, str] = {}
    for filename in ALLOWED_FILES:
        path = _safe_child(language_dir, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = supplied_content.get(filename)
        if content is None:
            content = _read_template(filename, template_root).replace(
                "{{language}}", normalized.title()
            )

        if path.exists():
            if filename in supplied_content and overwrite_existing:
                expected = expected_versions[filename] if expected_versions else None
                backup = _backup_name(language_dir, filename)
                versions[filename] = save_single_file(
                    language_dir, filename, content, expected, backup=backup
                )
                overwritten.append(filename)
            else:
                versions[filename] = file_version(path)
                preserved.append(filename)
            continue

        versions[filename] = save_single_file(
            language_dir, filename, content, ABSENT_VERSION
        )
        created.append(filename)

    return {
        "language": normalized,
        "created": created,
        "overwritten": overwritten,
        "preserved": preserved,
        "file_versions": versions,
        "directories": ["sessions", "delivery", "archives", "notes"],
    }


@_serialized_storage
def read_context(
    language: str, *, data_root: Path = TUTOR_DATA_ROOT
) -> dict[str, Any]:
    """Read the complete current Markdown context for a language."""
    normalized, language_dir = _require_language_profile(language, data_root)

    context: dict[str, Any] = {
        "language": normalized,
        "memory_protocol": MEMORY_PROTOCOL,
    }
    file_versions: dict[str, str] = {}
    inventory: list[dict[str, Any]] = []
    try:
        for filename in CONTEXT_FILES + OPTIONAL_CONTEXT_FILES:
            path = _safe_child(language_dir, filename)
            if not path.is_file():
                if filename in OPTIONAL_CONTEXT_FILES:
                    context[filename] = ""
                    file_versions[filename] = ABSENT_VERSION
                    inventory.append({"filename": filename, "bytes": 0, "characters": 0})
                    continue
                raise ValueError(
                    f"Language profile is incomplete; missing file: {filename}"
                )
            content, version, byte_count = _read_file_text(path)
            if filename == LESSON_CONTRACT_FILE:
                parse_contract_document(content)
            context[filename] = content
            file_versions[filename] = version
            inventory.append({"filename": filename, "bytes": byte_count, "characters": len(content)})
    except OversizedFileError:
        size_inventory = []
        for filename in CONTEXT_FILES + OPTIONAL_CONTEXT_FILES:
            path = _safe_child(language_dir, filename)
            size_inventory.append({
                "filename": filename,
                "bytes": path.stat().st_size if path.exists() else 0,
                "characters": None,
            })
        return {
            "language": normalized,
            "memory_protocol": MEMORY_PROTOCOL,
            "error": {
                "code": "context_too_large",
                "message": "A context file exceeds the 2 MiB read limit. Use get_language_context_status and local recovery before teaching.",
                "files": size_inventory,
            },
            "_meta": {"file_versions": file_versions},
        }

    context["_meta"] = {"file_versions": file_versions}
    serialized_size = len(json.dumps(context, ensure_ascii=False, separators=(",", ":")))
    if serialized_size > CONTEXT_LIMIT_CHARS:
        return {
            "language": normalized,
            "memory_protocol": MEMORY_PROTOCOL,
            "error": {
                "code": "context_too_large",
                "message": "The complete teaching context exceeds 64,000 serialized characters. Use get_language_context_status and read_language_file pages; no lesson context was shortened.",
                "serialized_characters": serialized_size,
                "files": inventory,
            },
            "_meta": {"file_versions": file_versions},
        }
    return context


@_serialized_storage
def write_file(
    language: str,
    filename: str,
    content: str,
    expected_version: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    """Replace one whitelisted Markdown file for an existing language."""
    normalized, language_dir = _require_language_profile(language, data_root)
    filename = _validate_allowed_filename(filename)
    content = _require_markdown(content)
    path = _safe_child(language_dir, filename)
    validate_content(filename, content)
    current = check_version(path, expected_version)
    new_version = version_bytes(content.encode("utf-8"))
    backup = _backup_name(language_dir, filename) if current != ABSENT_VERSION and current != new_version else None
    saved_version = save_single_file(
        language_dir, filename, content, expected_version, backup=backup
    )
    return {
        "language": normalized,
        "filename": filename,
        "characters_written": len(content),
        "file_version": saved_version,
        "backup_file": backup if backup and current != new_version else None,
    }


@_serialized_storage
def list_notes(
    language: str, *, data_root: Path = TUTOR_DATA_ROOT
) -> list[str]:
    """List custom Markdown note filenames for an existing language."""
    _, language_dir = _require_language_profile(language, data_root)
    notes_dir = _safe_child(language_dir, "notes")
    if not notes_dir.exists():
        return []
    if not notes_dir.is_dir():
        raise ValueError("The language notes path is not a directory.")

    notes: list[str] = []
    with os.scandir(notes_dir) as entries:
        for entry in entries:
            if (
                NOTE_FILENAME_PATTERN.fullmatch(entry.name)
                and not entry.is_symlink()
                and entry.is_file(follow_symlinks=False)
            ):
                notes.append(entry.name)
    return sorted(notes, key=str.casefold)


@_serialized_storage
def read_note(
    language: str,
    filename: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_version: str | None = None,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    """Read one validated custom Markdown note for an existing language."""
    normalized, language_dir = _require_language_profile(language, data_root)
    filename = _validate_note_filename(filename)
    notes_dir = _safe_child(language_dir, "notes")
    path = notes_dir / filename
    if path.is_symlink():
        raise ValueError(f"Language note does not exist: {filename}")
    path = _safe_child(notes_dir, filename)
    if not path.is_file():
        raise ValueError(f"Language note does not exist: {filename}")
    page = paged_read(
        path,
        offset_chars=offset_chars,
        limit_chars=limit_chars,
        expected_version=expected_version,
    )
    return {
        "language": normalized,
        "filename": filename,
        **page,
    }


@_serialized_storage
def write_note(
    language: str,
    filename: str,
    content: str,
    expected_version: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    """Create or replace one validated custom Markdown note."""
    normalized, language_dir = _require_language_profile(language, data_root)
    filename = _validate_note_filename(filename)
    content = _require_markdown(content)
    notes_dir = _safe_child(language_dir, "notes")
    notes_dir.mkdir(exist_ok=True)
    relative = f"notes/{filename}"
    path = _safe_child(language_dir, "notes", filename)
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic link.")
    created = not path.exists()
    if not created and not path.is_file():
        raise ValueError("The requested note path is not a regular file.")
    validate_content(relative, content)
    current = check_version(path, expected_version)
    new_version = version_bytes(content.encode("utf-8"))
    backup = f".backups/{relative}" if current != ABSENT_VERSION and current != new_version else None
    saved_version = save_single_file(
        language_dir, relative, content, expected_version, backup=backup
    )
    return {
        "language": normalized,
        "filename": filename,
        "created": created,
        "characters_written": len(content),
        "file_version": saved_version,
        "backup_file": backup,
    }


@_serialized_storage
def append_session(
    language: str,
    session_markdown: str,
    expected_versions: dict[str, str],
    operation_id: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
    template_root: Path = TEMPLATE_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Save a standalone session summary while preserving the checkpoint."""
    normalized, language_dir = _require_language_profile(language, data_root)
    payload = {
        "language": normalized,
        "session_markdown": session_markdown,
        "expected_versions": expected_versions,
    }
    committed = check_operation(language_dir, operation_id, payload)
    if committed is not None:
        return committed
    session_markdown = _require_markdown(session_markdown, "session_markdown")
    if not isinstance(expected_versions, dict) or set(expected_versions) != {"latest-summary.md"}:
        raise StorageError("expected_versions must contain the latest-summary.md version.")
    validate_content("latest-summary.md", session_markdown)
    summary_path = _safe_child(language_dir, "latest-summary.md")
    check_version(summary_path, expected_versions["latest-summary.md"])

    sessions_dir = _safe_child(language_dir, "sessions")
    sessions_dir.mkdir(exist_ok=True)
    filename = _unique_timestamped_filename(sessions_dir, now)
    session_relative = f"sessions/{filename}"
    summary_bytes = session_markdown.encode("utf-8")
    result = {
        "language": normalized,
        "session_file": session_relative,
        "latest_summary_updated": True,
        "active_session_cleared": False,
        "file_versions": {
            "latest-summary.md": version_bytes(summary_bytes),
            session_relative: version_bytes(summary_bytes),
        },
    }
    return commit_write_group(
        language_dir,
        operation_id,
        payload,
        [
            {
                "path": "latest-summary.md",
                "content": summary_bytes,
                "expected_version": expected_versions["latest-summary.md"],
                "backup": _backup_name(language_dir, "latest-summary.md", now),
            },
            {"path": session_relative, "content": summary_bytes, "expected_version": ABSENT_VERSION},
        ],
        result,
    )


@_serialized_storage
def finalize_lesson_storage(
    language: str,
    session_markdown: str,
    homework_markdown: str,
    updates: dict[str, str],
    unchanged_files: list[str],
    expected_versions: dict[str, str],
    operation_id: str,
    expected_contract_version: str = "",
    *,
    data_root: Path = TUTOR_DATA_ROOT,
    template_root: Path = TEMPLATE_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist lesson outcomes as one recoverable, version-checked write group."""
    normalized, language_dir = _require_language_profile(language, data_root)
    payload = {
        "language": normalized,
        "session_markdown": session_markdown,
        "homework_markdown": homework_markdown,
        "updates": updates,
        "unchanged_files": unchanged_files,
        "expected_versions": expected_versions,
        "expected_contract_version": expected_contract_version,
    }
    committed = check_operation(language_dir, operation_id, payload)
    if committed is not None:
        return committed
    normalize_operation_id(operation_id)
    session_markdown = _require_markdown(session_markdown, "session_markdown")
    homework_markdown = _require_markdown(homework_markdown, "homework_markdown")
    validate_content("latest-summary.md", session_markdown)
    validate_content("latest-homework.md", homework_markdown)

    if not isinstance(updates, dict):
        raise ValueError("updates must be an object mapping filenames to Markdown.")
    if any(not isinstance(filename, str) for filename in updates):
        raise ValueError("Every updates key must be a string filename.")
    if not isinstance(unchanged_files, list):
        raise ValueError("unchanged_files must be a list of filenames.")
    if any(not isinstance(filename, str) for filename in unchanged_files):
        raise ValueError("Every unchanged_files entry must be a string filename.")
    if len(unchanged_files) != len(set(unchanged_files)):
        raise ValueError("unchanged_files must not contain duplicates.")

    cumulative_files = set(COMPACTABLE_FILES)
    update_files = set(updates)
    unchanged_set = set(unchanged_files)
    invalid_updates = sorted(update_files - cumulative_files)
    invalid_unchanged = sorted(unchanged_set - cumulative_files)
    overlapping = sorted(update_files & unchanged_set)
    accounted_for = update_files | unchanged_set
    missing = [
        filename
        for filename in COMPACTABLE_FILES
        if filename not in accounted_for
    ]

    if invalid_updates:
        raise ValueError(
            "updates may contain only cumulative files: "
            + ", ".join(COMPACTABLE_FILES)
            + ". Invalid: "
            + ", ".join(invalid_updates)
        )
    if invalid_unchanged:
        raise ValueError(
            "unchanged_files may contain only cumulative files. Invalid: "
            + ", ".join(invalid_unchanged)
        )
    if overlapping:
        raise ValueError(
            "A cumulative file cannot be both updated and unchanged: "
            + ", ".join(overlapping)
        )
    if missing:
        raise ValueError(
            "Account for every cumulative file in updates or unchanged_files. "
            "Missing: "
            + ", ".join(missing)
        )

    for filename, content in updates.items():
        _require_markdown(content, f"updates[{filename}]")
        validate_content(filename, content)

    contract_path = _safe_child(language_dir, LESSON_CONTRACT_FILE)
    contract_exists = contract_path.exists()
    required_versions = set(CUMULATIVE_FILES) | {
        "latest-homework.md",
        "latest-summary.md",
        "active-session.md",
    }
    if contract_exists:
        required_versions.add(LESSON_CONTRACT_FILE)
    if not isinstance(expected_versions, dict):
        raise StorageError("expected_versions must map every finalization file to its read hash.")
    allowed_versions = required_versions | {LESSON_CONTRACT_FILE}
    if set(expected_versions) - allowed_versions:
        raise StorageError("expected_versions contains an unsupported filename.")
    missing_versions = sorted(required_versions - set(expected_versions))
    if missing_versions:
        raise StorageError("Missing file versions for finalization: " + ", ".join(missing_versions))
    if not contract_exists and LESSON_CONTRACT_FILE in expected_versions and expected_versions[LESSON_CONTRACT_FILE] != ABSENT_VERSION:
        raise VersionConflictError("The lesson contract changed after it was read.")
    for filename in required_versions:
        check_version(_safe_child(language_dir, filename), expected_versions[filename])

    completed_contract_text: str | None = None
    completed_contract_metadata: dict[str, Any] | None = None
    mastery_block = ""
    if contract_exists:
        if not expected_contract_version:
            raise ContractValidationError("expected_contract_version is required when a lesson contract exists.")
        contract_text, contract_hash, _ = _read_file_text(contract_path)
        if expected_versions.get(LESSON_CONTRACT_FILE) != contract_hash:
            raise VersionConflictError("The lesson contract file changed after it was read.")
        contract_metadata, contract_body = parse_contract_document(contract_text)
        if contract_metadata["status"] != "active":
            raise ContractValidationError("The lesson contract is already completed.")
        if contract_metadata["version_token"] != expected_contract_version:
            raise VersionConflictError("The lesson contract token is stale; reread it before finalizing.")
        ready, reasons = readiness(contract_metadata)
        if not ready:
            raise ContractValidationError("The current assessment is not ready: " + " ".join(reasons))
        if "02-progress.md" not in updates:
            raise ContractValidationError("02-progress.md must contain the current lesson mastery record.")
        mastery_block = render_mastery_record(contract_metadata)
        progress = updates["02-progress.md"]
        contract_id = contract_metadata["contract_id"]
        if progress.count(mastery_block) != 1:
            raise ContractValidationError("Include the exact returned mastery_record_markdown once in 02-progress.md.")
        if progress.count(f"<!-- lesson-mastery:{contract_id} -->") != 1 or progress.count(f"<!-- /lesson-mastery:{contract_id} -->") != 1:
            raise ContractValidationError("The lesson mastery record markers must appear exactly once.")
        if CONTRACT_START in progress:
            raise ContractValidationError("Copy the compact mastery record, not the contract document, into progress.")
        completed_contract_metadata = mark_completed(contract_metadata)
        completed_contract_text = serialize_contract_document(completed_contract_metadata, contract_body)

    session_directory = _safe_child(language_dir, "sessions")
    session_directory.mkdir(exist_ok=True)
    session_filename = _unique_timestamped_filename(session_directory, now)
    session_relative = f"sessions/{session_filename}"
    checkpoint_reset = _read_template("active-session.md", template_root).replace(
        "{{language}}", normalized.title()
    )
    validate_content("active-session.md", checkpoint_reset)

    targets: list[dict[str, Any]] = []
    updated_files: list[str] = []
    after_versions: dict[str, str] = {}
    for filename in CUMULATIVE_FILES:
        if filename not in updates:
            continue
        content = updates[filename].encode("utf-8")
        current_version = expected_versions[filename]
        backup = _backup_name(language_dir, filename, now) if current_version != ABSENT_VERSION and current_version != version_bytes(content) else None
        targets.append({"path": filename, "content": content, "expected_version": current_version, "backup": backup})
        updated_files.append(filename)
        after_versions[filename] = version_bytes(content)

    for filename, content in (
        ("latest-homework.md", homework_markdown),
        ("latest-summary.md", session_markdown),
        ("active-session.md", checkpoint_reset),
    ):
        encoded = content.encode("utf-8")
        before = expected_versions[filename]
        backup = _backup_name(language_dir, filename, now) if before != ABSENT_VERSION and before != version_bytes(encoded) else None
        targets.append({"path": filename, "content": encoded, "expected_version": before, "backup": backup})
        if filename != "active-session.md":
            updated_files.append(filename)
        after_versions[filename] = version_bytes(encoded)

    session_bytes = session_markdown.encode("utf-8")
    targets.append({"path": session_relative, "content": session_bytes, "expected_version": ABSENT_VERSION})
    after_versions[session_relative] = version_bytes(session_bytes)

    completed_contract_version = ""
    if completed_contract_text is not None and completed_contract_metadata is not None:
        contract_bytes = completed_contract_text.encode("utf-8")
        targets.append({
            "path": LESSON_CONTRACT_FILE,
            "content": contract_bytes,
            "expected_version": expected_versions[LESSON_CONTRACT_FILE],
            "backup": _backup_name(language_dir, LESSON_CONTRACT_FILE, now),
        })
        completed_contract_version = completed_contract_metadata["version_token"]
        after_versions[LESSON_CONTRACT_FILE] = version_bytes(contract_bytes)
    for filename in unchanged_set:
        after_versions[filename] = expected_versions[filename]

    result = {
        "language": normalized,
        "finalized": True,
        "updated_files": updated_files,
        "unchanged_files": [
            filename for filename in COMPACTABLE_FILES if filename in unchanged_set
        ],
        "session_file": session_relative,
        "latest_summary_updated": True,
        "active_session_cleared": True,
        "file_versions": after_versions,
    }
    if completed_contract_metadata is not None:
        result.update({
            "contract_status": "completed",
            "contract_version_token": completed_contract_version,
            "contract_file_version": after_versions[LESSON_CONTRACT_FILE],
        })
    return commit_write_group(language_dir, operation_id, payload, targets, result)


@_serialized_storage
def save_checkpoint(
    language: str,
    checkpoint_markdown: str,
    expected_version: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    """Replace the bounded active-session checkpoint for a language."""
    normalized, language_dir = _require_language_profile(language, data_root)
    checkpoint_markdown = _require_markdown(
        checkpoint_markdown, "checkpoint_markdown"
    )
    path = _safe_child(language_dir, "active-session.md")
    validate_content("active-session.md", checkpoint_markdown)
    current = check_version(path, expected_version)
    new_version = version_bytes(checkpoint_markdown.encode("utf-8"))
    backup = _backup_name(language_dir, "active-session.md") if current != ABSENT_VERSION and current != new_version else None
    saved_version = save_single_file(
        language_dir, "active-session.md", checkpoint_markdown, expected_version, backup=backup
    )
    return {
        "language": normalized,
        "filename": "active-session.md",
        "characters_written": len(checkpoint_markdown),
        "file_version": saved_version,
        "backup_file": backup,
    }


@_serialized_storage
def context_status(
    language: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
    threshold_chars: int = CONTEXT_COMPACTION_THRESHOLD_CHARS,
    total_threshold_chars: int = TOTAL_CONTEXT_COMPACTION_THRESHOLD_CHARS,
) -> dict[str, Any]:
    """Report context sizes without interpreting or summarizing learner data."""
    normalized, language_dir = _require_language_profile(language, data_root)
    if threshold_chars < 1:
        raise ValueError("threshold_chars must be a positive integer.")
    if total_threshold_chars < 1:
        raise ValueError("total_threshold_chars must be a positive integer.")

    files: dict[str, dict[str, Any]] = {}
    total_characters = 0
    cumulative_characters = 0
    incomplete_sizes = False
    for filename in CONTEXT_FILES + OPTIONAL_CONTEXT_FILES:
        path = _safe_child(language_dir, filename)
        if not path.is_file():
            if filename in OPTIONAL_CONTEXT_FILES:
                files[filename] = {
                    "present": False,
                    "bytes": 0,
                    "characters": 0,
                    "compactable": False,
                    "compaction_recommended": False,
                }
                continue
            raise ValueError(f"Language profile is incomplete; missing file: {filename}")
        byte_count = path.stat().st_size
        compactable = filename in CUMULATIVE_FILES
        if byte_count > MAX_READ_BYTES:
            character_count: int | None = None
            incomplete_sizes = True
        else:
            try:
                character_count = len(read_limited_bytes(path).decode("utf-8"))
            except OversizedFileError:
                character_count = None
                incomplete_sizes = True
            except UnicodeDecodeError:
                character_count = None
                incomplete_sizes = True
        if character_count is not None:
            total_characters += character_count
            if compactable:
                cumulative_characters += character_count
        volatile_oversized = (
            not compactable
            and character_count is not None
            and character_count >= threshold_chars
        )
        if filename == LESSON_CONTRACT_FILE and byte_count > 128 * 1024:
            volatile_oversized = True
        status: dict[str, Any] = {
            "present": True,
            "bytes": byte_count,
            "characters": character_count,
            "compactable": compactable,
            "compaction_recommended": compactable
            and character_count is not None
            and character_count >= threshold_chars,
            "hard_read_limit_exceeded": byte_count > MAX_READ_BYTES,
        }
        if volatile_oversized:
            status["oversized"] = True
            status["action"] = (
                "deliberately_revise_contract"
                if filename == LESSON_CONTRACT_FILE
                else "shorten_volatile_file"
            )
        elif not compactable:
            status["oversized"] = False
        files[filename] = status

    recommended_files = [
        filename
        for filename, status in files.items()
        if status["compaction_recommended"]
    ]
    total_warning = cumulative_characters >= total_threshold_chars
    if total_warning and not recommended_files:
        largest_compactable = max(
            COMPACTABLE_FILES,
            key=lambda filename: files[filename]["characters"] or 0,
        )
        files[largest_compactable]["compaction_recommended"] = True
        recommended_files.append(largest_compactable)

    return {
        "language": normalized,
        "threshold_characters": threshold_chars,
        "total_threshold_characters": total_threshold_chars,
        "total_context_characters": total_characters,
        "compaction_basis_characters": cumulative_characters,
        "context_size_incomplete": incomplete_sizes,
        "total_context_compaction_recommended": total_warning,
        "files": files,
        "files_recommended_for_compaction": recommended_files,
    }


@_serialized_storage
def compact_file(
    language: str,
    filename: str,
    compacted_markdown: str,
    expected_version: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Archive a cumulative context file and replace it with an AI summary."""
    normalized, language_dir = _require_language_profile(language, data_root)
    if not isinstance(filename, str) or filename not in COMPACTABLE_FILES:
        raise ValueError(
            "Invalid filename. Compactable files: "
            + ", ".join(COMPACTABLE_FILES)
        )
    compacted_markdown = _require_markdown(
        compacted_markdown, "compacted_markdown"
    )
    validate_content(filename, compacted_markdown)

    source_path = _safe_child(language_dir, filename)
    if not source_path.is_file():
        raise ValueError(f"Language profile is incomplete; missing file: {filename}")
    check_version(source_path, expected_version)
    previous_content, _, _ = _read_file_text(source_path)
    backup = _backup_name(language_dir, filename, now)
    archive_filename = backup.rsplit("/", 1)[-1]
    saved_version = save_single_file(
        language_dir, filename, compacted_markdown, expected_version, backup=backup
    )

    return {
        "language": normalized,
        "filename": filename,
        "archive_file": (
            f"archives/{filename.removesuffix('.md')}/{archive_filename}"
        ),
        "previous_characters": len(previous_content),
        "current_characters": len(compacted_markdown),
        "file_version": saved_version,
    }


@_serialized_storage
def list_archives(
    language: str,
    filename: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> list[str]:
    """List timestamped archives for one cumulative context file."""
    _, language_dir = _require_language_profile(language, data_root)
    if not isinstance(filename, str) or filename not in COMPACTABLE_FILES:
        raise ValueError(
            "Invalid filename. Archived files: " + ", ".join(COMPACTABLE_FILES)
        )
    archive_directory = _safe_child(
        language_dir, "archives", filename.removesuffix(".md")
    )
    if not archive_directory.exists():
        return []
    return sorted(
        path.name
        for path in archive_directory.iterdir()
        if path.is_file() and not path.is_symlink() and TIMESTAMPED_MARKDOWN_PATTERN.fullmatch(path.name)
    )


@_serialized_storage
def read_archive(
    language: str,
    filename: str,
    archive_filename: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_version: str | None = None,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    """Read one validated timestamped archive without loading all history."""
    normalized, language_dir = _require_language_profile(language, data_root)
    if not isinstance(filename, str) or filename not in COMPACTABLE_FILES:
        raise ValueError(
            "Invalid filename. Archived files: " + ", ".join(COMPACTABLE_FILES)
        )
    if not isinstance(archive_filename, str) or not (
        TIMESTAMPED_MARKDOWN_PATTERN.fullmatch(archive_filename)
    ):
        raise ValueError("Invalid archive filename. Use a value returned by list archives.")
    archive_path = _safe_child(
        language_dir,
        "archives",
        filename.removesuffix(".md"),
        archive_filename,
    )
    if not archive_path.is_file():
        raise ValueError(f"Archive does not exist: {archive_filename}")
    if archive_path.is_symlink():
        raise ValueError("Refusing to read a symbolic link.")
    page = paged_read(
        archive_path,
        offset_chars=offset_chars,
        limit_chars=limit_chars,
        expected_version=expected_version,
    )
    return {
        "language": normalized,
        "filename": filename,
        "archive_filename": archive_filename,
        **page,
    }


@_serialized_storage
def read_language_file_storage(
    language: str,
    filename: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_version: str | None = None,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    """Read one known context file in consistent, bounded character pages."""
    normalized, language_dir = _require_language_profile(language, data_root)
    if not isinstance(filename, str) or filename not in READABLE_FILES:
        raise ValueError("Invalid filename. This reader accepts known context files only.")
    path = _safe_child(language_dir, filename)
    if not path.exists() and filename in OPTIONAL_CONTEXT_FILES:
        if offset_chars:
            raise ValueError("The optional context file is absent.")
        return {
            "language": normalized,
            "filename": filename,
            "content": "",
            "version": ABSENT_VERSION,
            "offset_chars": 0,
            "next_offset_chars": None,
            "total_chars": 0,
            "complete": True,
        }
    if not path.is_file():
        raise ValueError(f"Language profile is incomplete; missing file: {filename}")
    contract_version: str | None = None
    if filename == LESSON_CONTRACT_FILE and path.stat().st_size <= MAX_READ_BYTES:
        text, contract_version, _ = _read_file_text(path)
        parse_contract_document(text)
    page = paged_read(
        path,
        offset_chars=offset_chars,
        limit_chars=limit_chars,
        expected_version=expected_version,
    )
    if contract_version is not None and page["version"] != contract_version:
        raise VersionConflictError("The lesson contract changed during this read. Restart the page sequence.")
    return {"language": normalized, "filename": filename, **page}


@_serialized_storage
def list_sessions(
    language: str,
    before: str | None = None,
    limit: int = 20,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    normalized, language_dir = _require_language_profile(language, data_root)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")
    if before is not None and (
        not isinstance(before, str) or not TIMESTAMPED_MARKDOWN_PATTERN.fullmatch(before)
    ):
        raise ValueError("before must be a session filename returned by this tool.")
    sessions_dir = _safe_child(language_dir, "sessions")
    if not sessions_dir.exists():
        return {"language": normalized, "sessions": [], "next_before": None}
    if not sessions_dir.is_dir():
        raise ValueError("The language sessions path is not a directory.")
    names = sorted(
        (
            path.name
            for path in sessions_dir.iterdir()
            if not path.is_symlink()
            and path.is_file()
            and TIMESTAMPED_MARKDOWN_PATTERN.fullmatch(path.name)
        ),
        reverse=True,
    )
    if before is not None:
        if before in names:
            names = names[names.index(before) + 1 :]
        else:
            names = [name for name in names if name < before]
    page_names = names[:limit]
    has_more = len(names) > limit
    return {
        "language": normalized,
        "sessions": page_names,
        "next_before": page_names[-1] if has_more and page_names else None,
    }


@_serialized_storage
def read_session(
    language: str,
    filename: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_version: str | None = None,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    normalized, language_dir = _require_language_profile(language, data_root)
    if not isinstance(filename, str) or not TIMESTAMPED_MARKDOWN_PATTERN.fullmatch(filename):
        raise ValueError("Invalid session filename. Use a value returned by list_language_sessions.")
    path = _safe_child(language_dir, "sessions", filename)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Session does not exist: {filename}")
    page = paged_read(
        path,
        offset_chars=offset_chars,
        limit_chars=limit_chars,
        expected_version=expected_version,
    )
    return {"language": normalized, "filename": filename, **page}


@_serialized_storage
def read_lesson_contract_storage(
    language: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_file_version: str | None = None,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    normalized, language_dir = _require_language_profile(language, data_root)
    path = _safe_child(language_dir, LESSON_CONTRACT_FILE)
    if not path.exists():
        if offset_chars:
            raise ValueError("The lesson contract is absent.")
        return {
            "language": normalized,
            "present": False,
            "filename": LESSON_CONTRACT_FILE,
            "content": "",
            "contract_id": "",
            "version_token": "",
            "lesson_id": "",
            "lesson_title": "",
            "status": "absent",
            "file_version": ABSENT_VERSION,
            "offset_chars": 0,
            "next_offset_chars": None,
            "total_chars": 0,
            "complete": True,
        }
    text, stored_version, _ = _read_file_text(path)
    metadata, _ = parse_contract_document(text)
    page = paged_read(
        path,
        offset_chars=offset_chars,
        limit_chars=limit_chars,
        expected_version=expected_file_version,
    )
    if page["version"] != stored_version:
        raise VersionConflictError("The lesson contract changed during this read. Restart the page sequence.")
    return {
        "language": normalized,
        "present": True,
        "filename": LESSON_CONTRACT_FILE,
        "contract_id": metadata["contract_id"],
        "version_token": metadata["version_token"],
        "lesson_id": metadata["lesson_id"],
        "lesson_title": metadata["lesson_title"],
        "status": metadata["status"],
        "file_version": page["version"],
        **{key: value for key, value in page.items() if key != "version"},
    }


@_serialized_storage
def create_lesson_contract_storage(
    language: str,
    lesson_id: str,
    lesson_title: str,
    contract_markdown: str,
    competencies: list[dict[str, Any]],
    expected_version: str = "",
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    normalized, language_dir = _require_language_profile(language, data_root)
    path = _safe_child(language_dir, LESSON_CONTRACT_FILE)
    if path.exists():
        existing_text, old_file_version, _ = _read_file_text(path)
        existing, _ = parse_contract_document(existing_text)
        if not expected_version or existing["version_token"] != expected_version:
            raise VersionConflictError("Read the current lesson contract and use its version_token before replacing it.")
        prior_file_version = old_file_version
    else:
        if expected_version != "":
            raise VersionConflictError("The lesson contract is absent; use an empty expected_version to create it.")
        prior_file_version = ABSENT_VERSION
    metadata = create_metadata(lesson_id, lesson_title, competencies)
    content = serialize_contract_document(metadata, contract_markdown)
    backup = _backup_name(language_dir, LESSON_CONTRACT_FILE) if prior_file_version != ABSENT_VERSION else None
    saved_version = save_single_file(
        language_dir,
        LESSON_CONTRACT_FILE,
        content,
        prior_file_version,
        backup=backup,
    )
    return {
        "language": normalized,
        "filename": LESSON_CONTRACT_FILE,
        "contract_id": metadata["contract_id"],
        "version_token": metadata["version_token"],
        "lesson_id": metadata["lesson_id"],
        "lesson_title": metadata["lesson_title"],
        "status": metadata["status"],
        "file_version": saved_version,
        "replaced": prior_file_version != ABSENT_VERSION,
    }


def _mutate_lesson_contract(
    language: str,
    expected_version: str,
    mutation: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    data_root: Path,
) -> tuple[str, dict[str, Any], str]:
    normalized, language_dir = _require_language_profile(language, data_root)
    path = _safe_child(language_dir, LESSON_CONTRACT_FILE)
    if not path.is_file():
        raise ValueError("No lesson contract exists for this language.")
    text, old_file_version, _ = _read_file_text(path)
    metadata, body = parse_contract_document(text)
    if metadata["version_token"] != expected_version:
        raise VersionConflictError("The lesson contract token is stale; reread it before continuing.")
    changed = mutation(metadata)
    changed_text = serialize_contract_document(changed, body)
    backup = _backup_name(language_dir, LESSON_CONTRACT_FILE)
    saved_version = save_single_file(
        language_dir,
        LESSON_CONTRACT_FILE,
        changed_text,
        old_file_version,
        backup=backup,
    )
    return normalized, changed, saved_version


@_serialized_storage
def update_lesson_contract_state_storage(
    language: str,
    expected_version: str,
    competency_updates: list[dict[str, Any]],
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    normalized, metadata, saved_version = _mutate_lesson_contract(
        language,
        expected_version,
        lambda current: apply_state_updates(current, competency_updates),
        data_root=data_root,
    )
    return {
        "language": normalized,
        "filename": LESSON_CONTRACT_FILE,
        "contract_id": metadata["contract_id"],
        "version_token": metadata["version_token"],
        "lesson_id": metadata["lesson_id"],
        "lesson_title": metadata["lesson_title"],
        "status": metadata["status"],
        "file_version": saved_version,
        "assessment_cleared": True,
    }


@_serialized_storage
def assess_lesson_contract_storage(
    language: str,
    expected_version: str,
    competencies: list[dict[str, Any]],
    unresolved_critical_gaps: list[str],
    completion_rule_met: bool,
    carry_forward_weaknesses: list[str],
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    normalized, metadata, saved_version = _mutate_lesson_contract(
        language,
        expected_version,
        lambda current: apply_assessment(
            current,
            competencies,
            unresolved_critical_gaps,
            completion_rule_met,
            carry_forward_weaknesses,
        ),
        data_root=data_root,
    )
    completion_ready, blockers = readiness(metadata)
    return {
        "language": normalized,
        "filename": LESSON_CONTRACT_FILE,
        "contract_id": metadata["contract_id"],
        "version_token": metadata["version_token"],
        "lesson_id": metadata["lesson_id"],
        "lesson_title": metadata["lesson_title"],
        "status": metadata["status"],
        "file_version": saved_version,
        "completion_ready": completion_ready,
        "blocking_reasons": blockers,
        "mastery_record_markdown": render_mastery_record(metadata) if completion_ready else "",
    }


def available_languages(*, data_root: Path = TUTOR_DATA_ROOT) -> list[str]:
    """List valid language profile directories in alphabetical order."""
    with MEMORY_LOCK:
        if not data_root.exists():
            return []
        languages = [
            path.name
            for path in data_root.iterdir()
            if not path.name.startswith(".")
            and not path.is_symlink()
            and path.is_dir()
            and LANGUAGE_PATTERN.fullmatch(path.name)
        ]
        return sorted(languages)


@mcp.tool
def initialize_language_profile(
    language: str,
    profile_markdown: str,
    lesson_plan_markdown: str,
    overwrite_existing: bool = False,
    expected_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a new language workspace and seed its Markdown memory.

    Use this once for a new language, not as an ordinary lesson-end tool. It
    creates all standard files from templates, using the supplied content for
    00-profile.md and 01-lesson-plan.md. Existing profile and plan files are
    preserved unless overwrite_existing is explicitly true; other files are
    never reset. For an overwrite, pass the matching hashes from the last
    context read in expected_versions.
    """
    return _run_tool(
        "initialize_language_profile",
        lambda: initialize_profile(
            language,
            profile_markdown,
            lesson_plan_markdown,
            overwrite_existing,
            expected_versions,
        ),
        writes=True,
        language=language,
    )


@mcp.tool
def read_language_context(language: str) -> dict[str, Any]:
    """Mandatory first read before teaching or resuming a lesson.

    Returns the memory protocol plus the current profile, plan, progress,
    vocabulary, mistakes, scenarios, summary, checkpoint, homework, and the
    optional lesson contract. `_meta.file_versions` contains exact hashes from
    the same reads. A `context_too_large` error contains no partial teaching
    context; use status and bounded file pages. Session history, notes, archives,
    and delivery drafts are excluded.
    """
    return _run_tool(
        "read_language_context",
        lambda: read_context(language),
        language=language,
    )


@mcp.tool
def write_language_file(
    language: str, filename: str, content: str, expected_version: str
) -> dict[str, Any]:
    """Persist one complete replacement for an allowed learner-memory file.

    Use after collecting lesson evidence: 00 stores durable profile/preferences;
    01 the plan; 02 observable progress; 03 new vocabulary; 04 errors and
    corrections; 05 scenarios; latest-homework.md homework. This replaces the
    whole file, so read current context or the complete bounded file first,
    preserve useful content, and pass its exact hash as expected_version. On a
    conflict, reread and deliberately merge. Learner memory cannot be blank;
    delivery drafts may be deliberately empty.
    """
    return _run_tool(
        "write_language_file",
        lambda: write_file(language, filename, content, expected_version),
        writes=True,
        language=language,
        filename=filename,
    )


@mcp.tool
def list_language_notes(language: str) -> list[str]:
    """List the custom Markdown notes stored for a language.

    Notes live in the language's notes directory and appear in the Markdown
    viewer under Other. Use this before selecting a note to read or update.
    """
    return _run_tool(
        "list_language_notes",
        lambda: list_notes(language),
        language=language,
    )


@mcp.tool
def read_language_note(
    language: str,
    filename: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Read one custom Markdown note by its listed filename.

    Read the current note before replacing it. Returns a file version and page
    offsets. Reuse its version on later pages, reconstruct all content, and
    pass the hash when replacing the note. Only simple .md filenames inside
    the language notes directory are accepted.
    """
    return _run_tool(
        "read_language_note",
        lambda: read_note(language, filename, offset_chars, limit_chars, expected_version),
        language=language,
        filename=filename,
    )


@mcp.tool
def write_language_note(
    language: str, filename: str, content: str, expected_version: str
) -> dict[str, Any]:
    """Create or completely replace a custom Markdown note.

    Notes appear in the Markdown viewer under Other. Supply a simple .md
    filename such as gender-noun-conventions.md. Use `absent` for creation or
    the current version from read_language_note when replacing it. Blank notes
    are rejected and previous content is kept for recovery.
    """
    return _run_tool(
        "write_language_note",
        lambda: write_note(language, filename, content, expected_version),
        writes=True,
        language=language,
        filename=filename,
    )


@mcp.tool
def append_session_log(
    language: str,
    session_markdown: str,
    expected_versions: dict[str, str],
    operation_id: str,
) -> dict[str, Any]:
    """Save a paused or standalone session without finishing its lesson.

    Creates a permanent timestamped session log, replaces latest-summary.md,
    and preserves active-session.md so an unfinished lesson can resume. Pass
    the current latest-summary.md hash and a client-generated UUID. Retry a
    lost response with that same ID and exact payload. This does not update
    cumulative memory or homework and cannot complete an active contract.
    """
    return _run_tool(
        "append_session_log",
        lambda: append_session(language, session_markdown, expected_versions, operation_id),
        writes=True,
        language=language,
    )


@mcp.tool
def finalize_lesson(
    language: str,
    session_markdown: str,
    homework_markdown: str,
    updates: dict[str, str],
    unchanged_files: list[str],
    expected_versions: dict[str, str],
    operation_id: str,
    expected_contract_version: str = "",
) -> dict[str, Any]:
    """Finalize a lesson and enforce the cumulative-memory checklist.

    Supply complete replacement Markdown for changed files and list every
    other cumulative file in unchanged_files. The two collections must account
    for all six cumulative files. Pass hashes for those files, homework, latest
    summary, checkpoint, and any present contract. Use a fresh UUID operation
    ID; retry a lost response with the same ID and exact payload. With a
    contract, pass its latest token and include its exact passing mastery block
    once in the complete progress replacement. A recoverable write group saves
    memory, homework, session, summary, checkpoint reset, and contract
    completion together. Legacy workspaces still require versions and an ID.
    """
    return _run_tool(
        "finalize_lesson",
        lambda: finalize_lesson_storage(
            language,
            session_markdown,
            homework_markdown,
            updates,
            unchanged_files,
            expected_versions,
            operation_id,
            expected_contract_version,
        ),
        writes=True,
        language=language,
    )


@mcp.tool
def save_session_checkpoint(
    language: str, checkpoint_markdown: str, expected_version: str
) -> dict[str, Any]:
    """Save a concise checkpoint during a long or interruptible lesson.

    Use after a major topic or roleplay, before changing subjects, or about
    every 20-30 turns. Pass the active-session.md hash from context or a file
    read. This replaces only the checkpoint and is not a final session summary.
    """
    return _run_tool(
        "save_session_checkpoint",
        lambda: save_checkpoint(language, checkpoint_markdown, expected_version),
        writes=True,
        language=language,
    )


@mcp.tool
def get_language_context_status(language: str) -> dict[str, Any]:
    """Check context sizes before teaching and after lesson-end writes.

    Reports file sizes and compaction recommendations only; it does not judge
    lesson completeness or update any learner file.
    """
    return _run_tool(
        "get_language_context_status",
        lambda: context_status(language),
        language=language,
    )


@mcp.tool
def compact_language_file(
    language: str, filename: str, compacted_markdown: str, expected_version: str
) -> dict[str, Any]:
    """Compact one cumulative file only when status recommends it.

    Read the complete current file first, preserve active goals and useful
    evidence, including mastery criteria and evidence needed later. Pass the
    exact file hash; one previous copy is archived automatically. Contracts
    and volatile files cannot be compacted.
    """
    return _run_tool(
        "compact_language_file",
        lambda: compact_file(language, filename, compacted_markdown, expected_version),
        writes=True,
        language=language,
        filename=filename,
    )


@mcp.tool
def list_language_file_archives(language: str, filename: str) -> list[str]:
    """List archived versions of one cumulative learner-memory file.

    Use only when older detail is genuinely needed; normal teaching uses the
    current bounded context.
    """
    return _run_tool(
        "list_language_file_archives",
        lambda: list_archives(language, filename),
        language=language,
        filename=filename,
    )


@mcp.tool
def read_language_file_archive(
    language: str,
    filename: str,
    archive_filename: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Read one validated archive selected from list_language_file_archives.

    Returns a version and bounded page metadata. Reuse expected_version on
    later pages to ensure they come from the same archive.
    """
    return _run_tool(
        "read_language_file_archive",
        lambda: read_archive(language, filename, archive_filename, offset_chars, limit_chars, expected_version),
        language=language,
        filename=filename,
    )


@mcp.tool
def read_language_file(
    language: str,
    filename: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Read one allowed context file in bounded, version-checked character pages.

    The first page returns a version, total_chars, next_offset_chars, and
    complete. Reuse its version on every later page and reconstruct all pages
    before replacing a file. The optional lesson contract is readable here but
    is not writable through generic file tools.
    """
    return _run_tool(
        "read_language_file",
        lambda: read_language_file_storage(
            language, filename, offset_chars, limit_chars, expected_version
        ),
        language=language,
        filename=filename,
    )


@mcp.tool
def list_language_sessions(
    language: str, before: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """List timestamped session filenames newest first using a bounded cursor."""
    return _run_tool(
        "list_language_sessions",
        lambda: list_sessions(language, before, limit),
        language=language,
    )


@mcp.tool
def read_language_session(
    language: str,
    filename: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Read a listed session log in bounded, version-checked pages."""
    return _run_tool(
        "read_language_session",
        lambda: read_session(language, filename, offset_chars, limit_chars, expected_version),
        language=language,
        filename=filename,
    )


@mcp.tool
def create_lesson_contract(
    language: str,
    lesson_id: str,
    lesson_title: str,
    contract_markdown: str,
    competencies: list[dict[str, Any]],
    expected_version: str = "",
) -> dict[str, Any]:
    """Create or intentionally replace the current finite lesson contract.

    Read full context first and derive the design from curriculum and learner
    history. Each competency has exactly `id`, `criterion`, boolean `critical`,
    and `required_evidence` (recognized, supported, independent, or
    delayed_or_mixed). The Markdown needs the ten documented section headings.
    Creation does not mean that the learner has demonstrated a skill. For
    replacement, read the existing contract and pass its current version_token;
    replacement resets competency state and assessment.
    """
    return _run_tool(
        "create_lesson_contract",
        lambda: create_lesson_contract_storage(
            language, lesson_id, lesson_title, contract_markdown, competencies, expected_version
        ),
        writes=True,
        language=language,
        filename=LESSON_CONTRACT_FILE,
    )


@mcp.tool
def read_lesson_contract(
    language: str,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_file_version: str | None = None,
) -> dict[str, Any]:
    """Read contract identity, lifecycle, and one bounded content page.

    The first page returns file_version and total_chars. Reuse
    expected_file_version on every later page and reconstruct all pages before
    treating the contract as fully read. An absent contract is complete empty
    content, not a blank active contract. The profile governs teaching method,
    the plan governs curriculum, and an active contract governs lesson scope.
    Optional enrichment and digressions add no requirements.
    """
    return _run_tool(
        "read_lesson_contract",
        lambda: read_lesson_contract_storage(
            language, offset_chars, limit_chars, expected_file_version
        ),
        language=language,
    )


@mcp.tool
def update_lesson_contract_state(
    language: str,
    expected_version: str,
    competency_updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Record meaningful teaching-state changes without changing requirements.

    Each item has `competency_id`, `status` (not_introduced, introduced,
    practising, demonstrated, weak, delayed_retest_required), and `note`.
    This is not assessment or completion. State changes clear prior assessment;
    struggling never weakens a required criterion.
    """
    return _run_tool(
        "update_lesson_contract_state",
        lambda: update_lesson_contract_state_storage(
            language, expected_version, competency_updates
        ),
        writes=True,
        language=language,
        filename=LESSON_CONTRACT_FILE,
    )


@mcp.tool
def assess_lesson_contract(
    language: str,
    expected_version: str,
    competencies: list[dict[str, Any]],
    unresolved_critical_gaps: list[str],
    completion_rule_met: bool,
    carry_forward_weaknesses: list[str],
) -> dict[str, Any]:
    """Assess every required competency from actual learner evidence.

    Each item has `competency_id`, boolean `criterion_met`, `evidence_level`
    (not_observed, introduced, recognized, supported, independent, or
    delayed_or_mixed), and a nonblank `evidence` explanation. Supply every
    contract competency exactly once; unresolved_critical_gaps contains its
    competency IDs. The server computes readiness and returns the compact
    mastery_record_markdown when ready. It does not grade German or write
    permanent progress.
    """
    return _run_tool(
        "assess_lesson_contract",
        lambda: assess_lesson_contract_storage(
            language,
            expected_version,
            competencies,
            unresolved_critical_gaps,
            completion_rule_met,
            carry_forward_weaknesses,
        ),
        writes=True,
        language=language,
        filename=LESSON_CONTRACT_FILE,
    )


@mcp.tool
def list_languages() -> list[str]:
    """List available initialized language workspaces.

    Use when selecting a language before calling its context or write tools.
    """
    return _run_tool("list_languages", available_languages)


if __name__ == "__main__":
    run_server(parse_server_config())
