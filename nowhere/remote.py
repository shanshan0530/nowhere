"""Remote Streamable HTTP entrypoint for Nowhere.

This keeps the original stdio MCP server untouched while exposing the same
FastMCP instance over HTTP for clients such as OrangeChat. The existing web
observer is mounted at ``/`` as a read-only window, while ``/mcp`` keeps its
existing API-key authentication and transport unchanged.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from nowhere.server import mcp
from nowhere.web import app as observer_app


class ApiKeyMiddleware:
    """Protect the MCP endpoint with a fixed ``X-API-Key`` header.

    This is implemented as pure ASGI middleware so Streamable HTTP responses
    remain fully streaming and are not wrapped or buffered.
    """

    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") == "http" and scope.get("path", "").startswith("/mcp"):
            expected = os.environ.get("NOWHERE_MCP_KEY", "").strip()
            if not expected:
                response = JSONResponse(
                    {"ok": False, "error": "NOWHERE_MCP_KEY is not configured"},
                    status_code=503,
                )
                await response(scope, receive, send)
                return

            supplied = Headers(scope=scope).get("x-api-key", "")
            if not hmac.compare_digest(supplied, expected):
                response = JSONResponse(
                    {"ok": False, "error": "unauthorized"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


class ObserverReadOnlyMiddleware:
    """Expose only the observer's read-only surface on the public web route.

    The local observer app also contains message, postcard-reply, delete and
    direct tool endpoints. Those are intentionally not exposed by this remote
    entrypoint; MCP remains the only remote action surface.
    """

    _READ_ONLY_PATHS = {
        "/",
        "/state",
        "/history",
        "/marks",
        "/sightings",
        "/postcards",
    }

    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or "/"
        method = str(scope.get("method", "GET")).upper()

        if path.startswith("/mcp") or path == "/health":
            await self.app(scope, receive, send)
            return

        allowed_path = path in self._READ_ONLY_PATHS or path.startswith("/static/")
        if not allowed_path:
            response = JSONResponse({"ok": False, "error": "not found"}, status_code=404)
            await response(scope, receive, send)
            return

        if method not in {"GET", "HEAD"}:
            response = JSONResponse(
                {"ok": False, "error": "observer is read-only"},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "nowhere-mcp",
            "transport": "streamable-http",
        }
    )


# FastMCP supplies the ASGI lifespan required for Streamable HTTP sessions.
app = mcp.http_app(path="/mcp")
app.add_middleware(ApiKeyMiddleware)
app.add_middleware(ObserverReadOnlyMiddleware)
app.add_route("/health", health, methods=["GET"])
app.mount("/", observer_app)
