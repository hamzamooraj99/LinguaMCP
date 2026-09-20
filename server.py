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
import time
from urllib.parse import urlencode
from hmac import compare_digest
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
    "or a saved change without a successful write."
)

MEMORY_PROTOCOL = """# LinguaMCP Memory Protocol

Use this context as the current source of truth for teaching and learner memory.

- Start or resume: read this context and check status before new material.
- During: save a checkpoint after a major topic or roleplay and about every 20-30 turns.
- End: promote lesson evidence into cumulative files. New words go to 03-vocabulary.md; errors and corrections to 04-mistakes.md; observable progress to 02-progress.md; durable preferences, goals, and constraints to 00-profile.md; next topics to 01-lesson-plan.md; scenarios to 05-scenarios.md.
- For each category, update it or explicitly decide that it is unchanged or has no new entries. Save homework and the session summary after cumulative updates.
- A conversational promise is not stored memory until a write succeeds.
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
    audit_enabled: bool = True
    audit_log: Path = TUTOR_DATA_ROOT / "audit-log.jsonl"


@dataclass(frozen=True)
class RuntimeSecurity:
    """Mutable runtime security policy used by MCP tool wrappers."""

    allow_writes: bool = True
    audit_enabled: bool = True
    audit_log: Path = TUTOR_DATA_ROOT / "audit-log.jsonl"


CURRENT_SECURITY = RuntimeSecurity()
OAUTH_AUTH_CODES: dict[str, dict[str, Any]] = {}
OAUTH_ACCESS_TOKENS: dict[str, dict[str, Any]] = {}
OAUTH_CODE_TTL_SECONDS = 300
OAUTH_TOKEN_TTL_SECONDS = 86_400
OAUTH_LOGGER = logging.getLogger("uvicorn.error")


class BearerAuthMiddleware(BaseHTTPMiddleware):  # type: ignore[misc,valid-type]
    """Require a bearer token for MCP requests and optionally serve OAuth."""

    def __init__(
        self,
        app: Any,
        token: str | None = None,
        oauth_password: str | None = None,
    ) -> None:
        super().__init__(app)
        self._expected_header = f"Bearer {token}" if token else None
        self._oauth_password = oauth_password

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        oauth_response = await self._handle_oauth_request(request)
        if oauth_response is not None:
            return oauth_response

        authorization = request.headers.get("authorization", "")
        if not self._is_authorized(authorization):
            challenge = "Bearer"
            if self._oauth_password is not None:
                resource_metadata = (
                    f"{_oauth_base_url(request)}"
                    "/.well-known/oauth-protected-resource"
                )
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
        token_record = OAUTH_ACCESS_TOKENS.get(token)
        if not token_record:
            return False
        if token_record["expires_at"] < time.time():
            OAUTH_ACCESS_TOKENS.pop(token, None)
            return False
        return True

    async def _handle_oauth_request(self, request: Request) -> Response | None:
        path = request.url.path.rstrip("/") or "/"
        if path == "/.well-known/oauth-authorization-server":
            return JSONResponse(_oauth_authorization_metadata(request))  # type: ignore[misc]
        if path == "/.well-known/oauth-protected-resource":
            return JSONResponse(_oauth_protected_resource_metadata(request))  # type: ignore[misc]
        if path == "/oauth/authorize" and request.method == "GET":
            return _oauth_authorize_form(request)
        if path == "/oauth/authorize" and request.method == "POST":
            form = await request.form()
            return _oauth_authorize_submit(request, dict(form), self._oauth_password)
        if path == "/oauth/token" and request.method == "POST":
            form = await request.form()
            return _oauth_token_response(dict(form))
        return None


def _oauth_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _oauth_authorization_metadata(request: Request) -> dict[str, Any]:
    base_url = _oauth_base_url(request)
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
    }


def _oauth_protected_resource_metadata(request: Request) -> dict[str, Any]:
    base_url = _oauth_base_url(request)
    return {
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
        "bearer_methods_supported": ["header"],
    }


def _oauth_authorize_form(request: Request) -> Response:
    query = request.query_params
    values = {
        "response_type": query.get("response_type", ""),
        "client_id": query.get("client_id", ""),
        "redirect_uri": query.get("redirect_uri", ""),
        "state": query.get("state", ""),
        "code_challenge": query.get("code_challenge", ""),
        "code_challenge_method": query.get("code_challenge_method", "plain"),
    }
    error = _validate_authorize_values(values)
    if error:
        return JSONResponse({"error": "invalid_request", "error_description": error}, status_code=400)  # type: ignore[misc]

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
    return HTMLResponse(body)  # type: ignore[misc]


def _oauth_authorize_submit(
    request: Request, form: dict[str, Any], oauth_password: str | None
) -> Response:
    if not oauth_password:
        return JSONResponse({"error": "server_error"}, status_code=500)  # type: ignore[misc]
    submitted_password = str(form.get("password", ""))
    if not compare_digest(submitted_password, oauth_password):
        return JSONResponse({"error": "access_denied"}, status_code=403)  # type: ignore[misc]

    values = {
        "response_type": str(form.get("response_type", "")),
        "client_id": str(form.get("client_id", "")),
        "redirect_uri": str(form.get("redirect_uri", "")),
        "state": str(form.get("state", "")),
        "code_challenge": str(form.get("code_challenge", "")),
        "code_challenge_method": str(form.get("code_challenge_method", "plain")),
    }
    error = _validate_authorize_values(values)
    if error:
        return JSONResponse({"error": "invalid_request", "error_description": error}, status_code=400)  # type: ignore[misc]

    code = secrets.token_urlsafe(32)
    OAUTH_AUTH_CODES[code] = {
        "client_id": values["client_id"],
        "redirect_uri": values["redirect_uri"],
        "code_challenge": values["code_challenge"],
        "code_challenge_method": values["code_challenge_method"],
        "expires_at": time.time() + OAUTH_CODE_TTL_SECONDS,
    }
    redirect_params = {"code": code}
    if values["state"]:
        redirect_params["state"] = values["state"]
    separator = "&" if "?" in values["redirect_uri"] else "?"
    redirect_url = values["redirect_uri"] + separator + urlencode(redirect_params)
    response = RedirectResponse(redirect_url, status_code=302)  # type: ignore[misc]
    redacted_location = response.headers["location"].replace(
        urlencode({"code": code}),
        "code=<redacted>",
        1,
    )
    OAUTH_LOGGER.info("OAuth authorization redirect Location: %s", redacted_location)
    return response


def _validate_authorize_values(values: dict[str, str]) -> str | None:
    if values["response_type"] != "code":
        return "response_type must be code."
    if not values["client_id"]:
        return "client_id is required."
    if not values["redirect_uri"]:
        return "redirect_uri is required."
    if not _is_allowed_redirect_uri(values["redirect_uri"]):
        return "redirect_uri must be https or localhost http."
    method = values["code_challenge_method"] or "plain"
    if method not in {"plain", "S256"}:
        return "Unsupported code_challenge_method."
    return None


def _is_allowed_redirect_uri(redirect_uri: str) -> bool:
    return redirect_uri.startswith("https://") or redirect_uri.startswith(
        "http://127.0.0.1"
    ) or redirect_uri.startswith("http://localhost")


def _oauth_token_response(form: dict[str, Any]) -> Response:
    grant_type = str(form.get("grant_type", ""))
    code = str(form.get("code", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    client_id = str(form.get("client_id", ""))
    code_verifier = str(form.get("code_verifier", ""))

    if grant_type != "authorization_code":
        return _oauth_error("unsupported_grant_type", "Only authorization_code is supported.")
    code_record = OAUTH_AUTH_CODES.pop(code, None)
    if not code_record:
        return _oauth_error("invalid_grant", "Authorization code is invalid.")
    if code_record["expires_at"] < time.time():
        return _oauth_error("invalid_grant", "Authorization code has expired.")
    if redirect_uri != code_record["redirect_uri"]:
        return _oauth_error("invalid_grant", "redirect_uri does not match.")
    if client_id and client_id != code_record["client_id"]:
        return _oauth_error("invalid_grant", "client_id does not match.")
    if not _verify_pkce(
        code_record["code_challenge"],
        code_record["code_challenge_method"],
        code_verifier,
    ):
        return _oauth_error("invalid_grant", "PKCE verification failed.")

    access_token = secrets.token_urlsafe(48)
    OAUTH_ACCESS_TOKENS[access_token] = {
        "client_id": code_record["client_id"],
        "expires_at": time.time() + OAUTH_TOKEN_TTL_SECONDS,
    }
    return JSONResponse(  # type: ignore[misc]
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": OAUTH_TOKEN_TTL_SECONDS,
            "scope": "linguamcp",
        }
    )


def _verify_pkce(challenge: str, method: str, verifier: str) -> bool:
    if not challenge:
        return True
    if not verifier:
        return False
    if method == "plain":
        return compare_digest(challenge, verifier)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return compare_digest(challenge, computed)


def _oauth_error(error: str, description: str) -> Response:
    return JSONResponse(  # type: ignore[misc]
        {"error": error, "error_description": description},
        status_code=400,
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
        )
    ]


def run_server(config: ServerConfig) -> None:
    """Start FastMCP with the requested transport."""
    configure_runtime_security(config)
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

    CURRENT_SECURITY.audit_log.parent.mkdir(parents=True, exist_ok=True)
    with CURRENT_SECURITY.audit_log.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record, ensure_ascii=True) + "\n")


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
            detail=f"{type(exc).__name__}: {exc}",
        )
        raise

    _audit_tool_call(tool_name, "success", language=language, filename=filename)
    return result


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
    candidate = resolved_root.joinpath(*parts).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
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


def _utc_filename(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return timestamp.strftime("%Y-%m-%dT%H-%M-%S-%fZ.md")


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


def initialize_profile(
    language: str,
    profile_markdown: str,
    lesson_plan_markdown: str,
    overwrite_existing: bool = False,
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
                path.write_text(content, encoding="utf-8")
                overwritten.append(filename)
            else:
                preserved.append(filename)
            continue

        path.write_text(content, encoding="utf-8")
        created.append(filename)

    return {
        "language": normalized,
        "created": created,
        "overwritten": overwritten,
        "preserved": preserved,
        "directories": ["sessions", "delivery", "archives", "notes"],
    }


def read_context(
    language: str, *, data_root: Path = TUTOR_DATA_ROOT
) -> dict[str, str]:
    """Read the complete current Markdown context for a language."""
    normalized, language_dir = _require_language_profile(language, data_root)

    context: dict[str, str] = {
        "language": normalized,
        "memory_protocol": MEMORY_PROTOCOL,
    }
    for filename in CONTEXT_FILES:
        path = _safe_child(language_dir, filename)
        if not path.is_file():
            raise ValueError(
                f"Language profile is incomplete; missing file: {filename}"
            )
        context[filename] = path.read_text(encoding="utf-8")
    return context


def write_file(
    language: str,
    filename: str,
    content: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    """Replace one whitelisted Markdown file for an existing language."""
    normalized, language_dir = _require_language_profile(language, data_root)
    filename = _validate_allowed_filename(filename)
    content = _require_markdown(content)

    path = _safe_child(language_dir, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "language": normalized,
        "filename": filename,
        "characters_written": len(content),
    }


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


def read_note(
    language: str,
    filename: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, str]:
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
    return {
        "language": normalized,
        "filename": filename,
        "content": path.read_text(encoding="utf-8"),
    }


def write_note(
    language: str,
    filename: str,
    content: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    """Create or replace one validated custom Markdown note."""
    normalized, language_dir = _require_language_profile(language, data_root)
    filename = _validate_note_filename(filename)
    content = _require_markdown(content)
    notes_dir = _safe_child(language_dir, "notes")
    notes_dir.mkdir(exist_ok=True)
    path = notes_dir / filename
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic link.")
    path = _safe_child(notes_dir, filename)
    created = not path.exists()
    if not created and not path.is_file():
        raise ValueError("The requested note path is not a regular file.")
    path.write_text(content, encoding="utf-8")
    return {
        "language": normalized,
        "filename": filename,
        "created": created,
        "characters_written": len(content),
    }


def append_session(
    language: str,
    session_markdown: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
    template_root: Path = TEMPLATE_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a timestamped session log and refresh latest-summary.md."""
    normalized, language_dir = _require_language_profile(language, data_root)
    session_markdown = _require_markdown(session_markdown, "session_markdown")

    sessions_dir = _safe_child(language_dir, "sessions")
    sessions_dir.mkdir(exist_ok=True)
    filename = _utc_filename(now)
    session_path = _safe_child(sessions_dir, filename)
    session_path.write_text(session_markdown, encoding="utf-8")

    latest_summary = _safe_child(language_dir, "latest-summary.md")
    latest_summary.write_text(session_markdown, encoding="utf-8")
    active_session = _safe_child(language_dir, "active-session.md")
    active_session.write_text(
        _read_template("active-session.md", template_root).replace(
            "{{language}}", normalized.title()
        ),
        encoding="utf-8",
    )
    return {
        "language": normalized,
        "session_file": f"sessions/{filename}",
        "latest_summary_updated": True,
        "active_session_cleared": True,
    }


def finalize_lesson_storage(
    language: str,
    session_markdown: str,
    homework_markdown: str,
    updates: dict[str, str],
    unchanged_files: list[str],
    *,
    data_root: Path = TUTOR_DATA_ROOT,
    template_root: Path = TEMPLATE_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist cumulative lesson outcomes before appending the session."""
    normalized, _ = _require_language_profile(language, data_root)
    session_markdown = _require_markdown(session_markdown, "session_markdown")
    homework_markdown = _require_markdown(homework_markdown, "homework_markdown")

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

    updated_files: list[str] = []
    for filename in COMPACTABLE_FILES:
        if filename in updates:
            write_file(
                normalized,
                filename,
                updates[filename],
                data_root=data_root,
            )
            updated_files.append(filename)

    write_file(
        normalized,
        "latest-homework.md",
        homework_markdown,
        data_root=data_root,
    )
    updated_files.append("latest-homework.md")

    session_result = append_session(
        normalized,
        session_markdown,
        data_root=data_root,
        template_root=template_root,
        now=now,
    )

    return {
        "language": normalized,
        "finalized": True,
        "updated_files": updated_files,
        "unchanged_files": [
            filename for filename in COMPACTABLE_FILES if filename in unchanged_set
        ],
        "session_file": session_result["session_file"],
        "latest_summary_updated": session_result["latest_summary_updated"],
        "active_session_cleared": session_result["active_session_cleared"],
    }


def save_checkpoint(
    language: str,
    checkpoint_markdown: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, Any]:
    """Replace the bounded active-session checkpoint for a language."""
    normalized, language_dir = _require_language_profile(language, data_root)
    checkpoint_markdown = _require_markdown(
        checkpoint_markdown, "checkpoint_markdown"
    )
    path = _safe_child(language_dir, "active-session.md")
    path.write_text(checkpoint_markdown, encoding="utf-8")
    return {
        "language": normalized,
        "filename": "active-session.md",
        "characters_written": len(checkpoint_markdown),
    }


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
    for filename in CONTEXT_FILES:
        path = _safe_child(language_dir, filename)
        if not path.is_file():
            raise ValueError(
                f"Language profile is incomplete; missing file: {filename}"
            )
        character_count = len(path.read_text(encoding="utf-8"))
        total_characters += character_count
        compactable = filename in COMPACTABLE_FILES
        files[filename] = {
            "characters": character_count,
            "compactable": compactable,
            "compaction_recommended": compactable
            and character_count >= threshold_chars,
        }

    recommended_files = [
        filename
        for filename, status in files.items()
        if status["compaction_recommended"]
    ]
    total_warning = total_characters >= total_threshold_chars
    if total_warning and not recommended_files:
        largest_compactable = max(
            COMPACTABLE_FILES,
            key=lambda filename: files[filename]["characters"],
        )
        files[largest_compactable]["compaction_recommended"] = True
        recommended_files.append(largest_compactable)

    return {
        "language": normalized,
        "threshold_characters": threshold_chars,
        "total_threshold_characters": total_threshold_chars,
        "total_context_characters": total_characters,
        "total_context_compaction_recommended": total_warning,
        "files": files,
        "files_recommended_for_compaction": recommended_files,
    }


def compact_file(
    language: str,
    filename: str,
    compacted_markdown: str,
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

    source_path = _safe_child(language_dir, filename)
    if not source_path.is_file():
        raise ValueError(f"Language profile is incomplete; missing file: {filename}")
    previous_content = source_path.read_text(encoding="utf-8")

    archive_directory = _safe_child(
        language_dir, "archives", filename.removesuffix(".md")
    )
    archive_directory.mkdir(parents=True, exist_ok=True)
    archive_filename = _utc_filename(now)
    archive_path = _safe_child(archive_directory, archive_filename)
    archive_path.write_text(previous_content, encoding="utf-8")
    source_path.write_text(compacted_markdown, encoding="utf-8")

    return {
        "language": normalized,
        "filename": filename,
        "archive_file": (
            f"archives/{filename.removesuffix('.md')}/{archive_filename}"
        ),
        "previous_characters": len(previous_content),
        "current_characters": len(compacted_markdown),
    }


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
        if path.is_file() and TIMESTAMPED_MARKDOWN_PATTERN.fullmatch(path.name)
    )


def read_archive(
    language: str,
    filename: str,
    archive_filename: str,
    *,
    data_root: Path = TUTOR_DATA_ROOT,
) -> dict[str, str]:
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
    return {
        "language": normalized,
        "filename": filename,
        "archive_filename": archive_filename,
        "content": archive_path.read_text(encoding="utf-8"),
    }


def available_languages(*, data_root: Path = TUTOR_DATA_ROOT) -> list[str]:
    """List valid language profile directories in alphabetical order."""
    if not data_root.exists():
        return []
    languages = [
        path.name
        for path in data_root.iterdir()
        if path.is_dir() and LANGUAGE_PATTERN.fullmatch(path.name)
    ]
    return sorted(languages)


@mcp.tool
def initialize_language_profile(
    language: str,
    profile_markdown: str,
    lesson_plan_markdown: str,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    """Create a new language workspace and seed its Markdown memory.

    Use this once for a new language, not as an ordinary lesson-end tool. It
    creates all standard files from templates, using the supplied content for
    00-profile.md and 01-lesson-plan.md. Existing profile and plan files are
    preserved unless overwrite_existing is explicitly true; other files are
    never reset.
    """
    return _run_tool(
        "initialize_language_profile",
        lambda: initialize_profile(
            language, profile_markdown, lesson_plan_markdown, overwrite_existing
        ),
        writes=True,
        language=language,
    )


@mcp.tool
def read_language_context(language: str) -> dict[str, str]:
    """Mandatory first read before teaching or resuming a lesson.

    Returns the memory protocol plus the current profile, plan, progress,
    vocabulary, mistakes, scenarios, latest summary, active checkpoint, and
    homework. It excludes custom notes, permanent session logs, archives, and
    delivery drafts; use list_language_notes and read_language_note for notes.
    """
    return _run_tool(
        "read_language_context",
        lambda: read_context(language),
        language=language,
    )


@mcp.tool
def write_language_file(
    language: str, filename: str, content: str
) -> dict[str, Any]:
    """Persist one complete replacement for an allowed learner-memory file.

    Use after collecting lesson evidence: 00 stores durable profile/preferences;
    01 the plan; 02 observable progress; 03 new vocabulary; 04 errors and
    corrections; 05 scenarios; latest-homework.md homework. This replaces the
    whole file, so read current context first and preserve useful content.
    """
    return _run_tool(
        "write_language_file",
        lambda: write_file(language, filename, content),
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
def read_language_note(language: str, filename: str) -> dict[str, str]:
    """Read one custom Markdown note by its listed filename.

    Read the current note before replacing it so useful material is preserved.
    Only simple .md filenames inside the language's notes directory are accepted.
    """
    return _run_tool(
        "read_language_note",
        lambda: read_note(language, filename),
        language=language,
        filename=filename,
    )


@mcp.tool
def write_language_note(
    language: str, filename: str, content: str
) -> dict[str, Any]:
    """Create or completely replace a custom Markdown note.

    Notes appear in the Markdown viewer under Other. Supply a simple .md
    filename such as gender-noun-conventions.md. When updating an existing
    note, read it first and include all material that should be retained.
    """
    return _run_tool(
        "write_language_note",
        lambda: write_note(language, filename, content),
        writes=True,
        language=language,
        filename=filename,
    )


@mcp.tool
def append_session_log(language: str, session_markdown: str) -> dict[str, Any]:
    """Store the final session summary after learner-memory updates are complete.

    Creates a permanent timestamped session log, replaces latest-summary.md,
    and clears active-session.md. It does not update profile, plan, progress,
    vocabulary, mistakes, scenarios, or homework; use write_language_file (or
    finalize_lesson) for those first.
    """
    return _run_tool(
        "append_session_log",
        lambda: append_session(language, session_markdown),
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
) -> dict[str, Any]:
    """Finalize a lesson and enforce the cumulative-memory checklist.

    Supply complete replacement Markdown in updates for every cumulative file
    that changed, and list every other cumulative file in unchanged_files. The
    two collections must account for all of 00-profile.md through
    05-scenarios.md. If new words or errors occurred, update 03 and 04 rather
    than marking them unchanged. This tool writes cumulative files and
    homework before creating the session log, latest summary, and clearing the
    active checkpoint. Prefer it over separate end-of-lesson writes.
    """
    return _run_tool(
        "finalize_lesson",
        lambda: finalize_lesson_storage(
            language,
            session_markdown,
            homework_markdown,
            updates,
            unchanged_files,
        ),
        writes=True,
        language=language,
    )


@mcp.tool
def save_session_checkpoint(
    language: str, checkpoint_markdown: str
) -> dict[str, Any]:
    """Save a concise checkpoint during a long or interruptible lesson.

    Use after a major topic or roleplay, before changing subjects, or about
    every 20-30 turns. This replaces the previous checkpoint and is not a final
    lesson summary or a substitute for cumulative-file updates.
    """
    return _run_tool(
        "save_session_checkpoint",
        lambda: save_checkpoint(language, checkpoint_markdown),
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
    language: str, filename: str, compacted_markdown: str
) -> dict[str, Any]:
    """Compact one cumulative file only when status recommends it.

    Read the complete current file first, preserve active goals and useful
    evidence in the replacement, and remember that the previous version is
    archived automatically.
    """
    return _run_tool(
        "compact_language_file",
        lambda: compact_file(language, filename, compacted_markdown),
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
    language: str, filename: str, archive_filename: str
) -> dict[str, str]:
    """Read one validated archive selected from list_language_file_archives.

    Use for recovering older detail, not as a replacement for current context.
    """
    return _run_tool(
        "read_language_file_archive",
        lambda: read_archive(language, filename, archive_filename),
        language=language,
        filename=filename,
    )


@mcp.tool
def list_languages() -> list[str]:
    """List available initialized language workspaces.

    Use when selecting a language before calling its context or write tools.
    """
    return _run_tool("list_languages", available_languages)


if __name__ == "__main__":
    run_server(parse_server_config())
