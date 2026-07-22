"""Zeabur entrypoint for the remote Nowhere MCP service."""

from __future__ import annotations

import os

import uvicorn

from nowhere.remote import app


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
