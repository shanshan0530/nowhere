"""Combined public deployment for the Nowhere observer UI and remote MCP.

Routes:
    /       -> password-protected observer UI
    /mcp    -> existing token-protected Streamable HTTP MCP

The observer and MCP share one Python process, so both see the same WorldState.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount

from nowhere.server import mcp
from nowhere.web import app as observer_app


_MCP_TOKEN = os.getenv("NOWHERE_MCP_TOKEN", "").strip()
if not _MCP_TOKEN:
    raise RuntimeError("NOWHERE_MCP_TOKEN is required for remote deployment")

_WEB_USERNAME = os.getenv("NOWHERE_WEB_USERNAME", "observer").strip() or "observer"
_WEB_PASSWORD = os.getenv("NOWHERE_WEB_PASSWORD", "").strip() or _MCP_TOKEN


class ObserverBasicAuth:
    """Protect the observer UI without changing its existing frontend code.

    Browsers remember HTTP Basic credentials for same-origin requests, so the
    observer page's existing fetch() calls keep working without embedding a
    token in JavaScript or URLs.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        username, password = _basic_credentials(headers.get("authorization", ""))
        if (
            secrets.compare_digest(username, _WEB_USERNAME)
            and secrets.compare_digest(password, _WEB_PASSWORD)
        ):
            await self.app(scope, receive, send)
            return

        response = PlainTextResponse(
            "Nowhere observer authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Nowhere Observer"'},
        )
        await response(scope, receive, send)


def _basic_credentials(value: str) -> tuple[str, str]:
    if not value.lower().startswith("basic "):
        return "", ""
    encoded = value[6:].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return "", ""
    username, separator, password = decoded.partition(":")
    if not separator:
        return "", ""
    return username, password


# FastMCP's docs recommend path="/" when mounting beneath an outer /mcp
# prefix, and require forwarding the MCP ASGI lifespan to the outer app.
mcp_app = mcp.http_app(path="/")
protected_observer_app = ObserverBasicAuth(observer_app)

app = Starlette(
    routes=[
        Mount("/mcp", app=mcp_app),
        Mount("/", app=protected_observer_app),
    ],
    lifespan=mcp_app.lifespan,
)


def main() -> None:
    host = os.getenv("NOWHERE_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("NOWHERE_MCP_PORT", "8080")))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
