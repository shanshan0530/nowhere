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

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from nowhere.server import mcp
from nowhere.web import app as observer_app


class McpApiKeyMiddleware:
    """Protect every HTTP request that reaches the mounted MCP application."""

    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") == "http":
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
    """Expose only the observer's read-only surface on the public web route."""

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
        allowed_path = path in self._READ_ONLY_PATHS or path.startswith("/static/")

        if not allowed_path:
            response = JSONResponse(
                {"ok": False, "error": "not found"},
                status_code=404,
            )
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


# FastMCP recommends mounting its ASGI app beneath an outer Starlette app and
# forwarding its lifespan so the Streamable HTTP session manager is initialized.
# Use path="/" internally so the public endpoint remains exactly /mcp after mount.
mcp_app = mcp.http_app(path="/")
protected_mcp_app = McpApiKeyMiddleware(mcp_app)
protected_observer_app = ObserverReadOnlyMiddleware(observer_app)

app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/mcp", app=protected_mcp_app),
        Mount("/", app=protected_observer_app),
    ],
    lifespan=mcp_app.lifespan,
)
