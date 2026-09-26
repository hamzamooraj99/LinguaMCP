"""Starlette application for the read-only LinguaMCP Markdown viewer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .rendering import render_markdown
from .storage import (
    DocumentNotFoundError,
    DocumentTooLargeError,
    InvalidRequestError,
    LanguageNotFoundError,
    RecoveryUnavailableError,
    StorageFailureError,
    ViewerStorageError,
    find_document,
    ensure_language_ready,
    list_document_groups,
    list_languages,
    read_document,
    resolve_data_root,
)


STATIC_ROOT = Path(__file__).resolve().parent / "static"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
NO_STORE_HEADERS = {"Cache-Control": "no-store"}
PWA_METADATA_PATHS = {"manifest.webmanifest", "service-worker.js"}


def _error_payload(error: ViewerStorageError) -> dict[str, dict[str, str]]:
    return {"error": {"code": error.code, "message": error.message}}


def _error_response(error: ViewerStorageError, status_code: int) -> JSONResponse:
    return JSONResponse(
        _error_payload(error),
        status_code=status_code,
        headers=NO_STORE_HEADERS,
    )


def _storage_error_response(error: Exception) -> JSONResponse:
    if isinstance(error, InvalidRequestError):
        return _error_response(error, 400)
    if isinstance(error, (LanguageNotFoundError, DocumentNotFoundError)):
        return _error_response(error, 404)
    if isinstance(error, DocumentTooLargeError):
        return _error_response(error, 413)
    if isinstance(error, RecoveryUnavailableError):
        return _error_response(error, 503)
    if isinstance(error, ViewerStorageError):
        return _error_response(error, 500)
    return _error_response(StorageFailureError(), 500)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"}, headers=NO_STORE_HEADERS)


async def languages(request: Request) -> JSONResponse:
    try:
        payload = {"languages": list_languages(data_root=request.app.state.data_root)}
        return JSONResponse(payload, headers=NO_STORE_HEADERS)
    except Exception as error:  # Keep filesystem details out of the response.
        return _storage_error_response(error)


async def documents(request: Request) -> JSONResponse:
    try:
        language = request.path_params["language"]
        payload = {
            "language": language,
            "groups": list_document_groups(
                language,
                data_root=request.app.state.data_root,
            ),
        }
        return JSONResponse(payload, headers=NO_STORE_HEADERS)
    except Exception as error:  # Keep filesystem details out of the response.
        return _storage_error_response(error)


async def document(request: Request) -> JSONResponse:
    try:
        language = request.path_params["language"]
        record = find_document(
            language,
            request.path_params["relative_path"],
            data_root=request.app.state.data_root,
        )
        ensure_language_ready(language, data_root=request.app.state.data_root)
        source = read_document(record)
        payload: dict[str, Any] = {
            "language": language,
            **record.metadata(),
            "html": render_markdown(source),
        }
        return JSONResponse(payload, headers=NO_STORE_HEADERS)
    except Exception as error:  # Keep filesystem details out of the response.
        return _storage_error_response(error)


async def api_not_found(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": "not_found",
                "message": "Resource not found.",
            }
        },
        status_code=404,
        headers=NO_STORE_HEADERS,
    )


async def method_not_allowed(request: Request) -> JSONResponse:
    response = JSONResponse(
        {
            "error": {
                "code": "method_not_allowed",
                "message": "Only GET requests are supported.",
            }
        },
        status_code=405,
        headers={**NO_STORE_HEADERS, "Allow": "GET, HEAD"},
    )
    return response


def _apply_security_headers(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


class HeadersAndMethodsMiddleware(BaseHTTPMiddleware):
    """Apply security headers and keep every route read-only."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.method not in {"GET", "HEAD"}:
            response = await method_not_allowed(request)
        else:
            response = await call_next(request)
        if request.url.path == "/healthz" or request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return _apply_security_headers(response)


class ProductionStaticFiles(StaticFiles):
    """Serve the approved UI without exposing the Phase 0 mock fixture."""

    async def get_response(self, path: str, scope: dict[str, Any]) -> Response:
        if path == "mock-data.js":
            return PlainTextResponse("Not Found", status_code=404)
        response = await super().get_response(path, scope)
        if path in PWA_METADATA_PATHS:
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    *,
    data_root: str | Path | None = None,
    static_dir: str | Path | None = None,
) -> Starlette:
    """Create the isolated viewer application for development or deployment."""

    resolved_static_dir = Path(static_dir) if static_dir is not None else STATIC_ROOT
    routes = [
        Route("/healthz", healthz, methods=["GET", "HEAD"]),
        Route("/api/languages", languages, methods=["GET", "HEAD"]),
        Route(
            "/api/languages/{language}/documents",
            documents,
            methods=["GET", "HEAD"],
        ),
        Route(
            "/api/languages/{language}/documents/{relative_path:path}",
            document,
            methods=["GET", "HEAD"],
        ),
        Route("/api", api_not_found, methods=["GET", "HEAD"]),
        Route("/api/{rest:path}", api_not_found, methods=["GET", "HEAD"]),
        Mount(
            "/",
            app=ProductionStaticFiles(directory=str(resolved_static_dir), html=True),
            name="static",
        ),
    ]
    application = Starlette(routes=routes)
    application.state.data_root = resolve_data_root(data_root)
    application.add_middleware(HeadersAndMethodsMiddleware)

    return application


app = create_app()
