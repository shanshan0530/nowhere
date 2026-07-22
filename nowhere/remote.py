"""Remote Streamable HTTP entrypoint for Nowhere.

This keeps the original stdio MCP server untouched while exposing the same
FastMCP instance over HTTP for clients such as OrangeChat.
"""

from __future__ import annotations

import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from nowhere.server import mcp


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Protect the MCP endpoint with a fixed ``X-API-Key`` header."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Keep health checks public, but protect every MCP request.
        if request.url.path.startswith("/mcp"):
            expected = os.environ.get("NOWHERE_MCP_KEY", "").strip()
            if not expected:
                return JSONResponse(
                    {"ok": False, "error": "NOWHERE_MCP_KEY is not configured"},
                    status_code=503,
                )

            supplied = request.headers.get("x-api-key", "")
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse(
                    {"ok": False, "error": "unauthorized"},
                    status_code=401,
                )

        return await call_next(request)


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
app.add_route("/health", health, methods=["GET"])
