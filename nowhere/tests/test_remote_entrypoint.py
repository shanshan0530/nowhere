import pytest

from nowhere import server


def test_http_mode_requires_token_by_default(monkeypatch):
    monkeypatch.delenv("NOWHERE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("NOWHERE_ALLOW_UNAUTHENTICATED_HTTP", raising=False)

    with pytest.raises(SystemExit):
        server.main(["--http", "--port", "8080"])


def test_http_mode_uses_streamable_mcp_path(monkeypatch):
    calls = []
    monkeypatch.setenv("NOWHERE_ALLOW_UNAUTHENTICATED_HTTP", "true")
    monkeypatch.delenv("NOWHERE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))

    server.main(["--http", "--host", "127.0.0.1", "--port", "9010"])

    assert calls == [
        {
            "transport": "http",
            "host": "127.0.0.1",
            "port": 9010,
            "path": "/mcp",
        }
    ]


def test_http_and_observer_modes_are_mutually_exclusive(monkeypatch):
    monkeypatch.setenv("NOWHERE_ALLOW_UNAUTHENTICATED_HTTP", "true")

    with pytest.raises(SystemExit):
        server.main(["--http", "--web", "8077"])
