# OrangeChat / Zeabur remote MCP

This fork keeps the original stdio MCP entrypoint unchanged and adds a separate Streamable HTTP deployment entrypoint.

## Zeabur

Deploy branch: `feature/orangechat-web-observer`

Required environment variables:

```text
NOWHERE_HOME=/app/data
NOWHERE_MCP_KEY=<a long random secret>
```

Optional:

```text
NOWHERE_QWEATHER_KEY=<QWeather key>
LOG_LEVEL=info
```

Mount a persistent volume at `/app/data` so journey state, marks and postcards survive restarts.

After assigning a domain:

- Observer UI: `https://<domain>/`
- Health check: `https://<domain>/health`
- MCP endpoint: `https://<domain>/mcp`

The remote observer reuses the existing Nowhere map UI, but its public surface is intentionally read-only. It exposes the map/state/history/marks/sightings/postcards feeds and static assets; message posting, postcard mutation and direct tool endpoints remain unavailable remotely. Remote actions continue to go through MCP only.

## OrangeChat / private gateway

- Transport: Streamable HTTP
- URL: `https://<domain>/mcp`
- Custom header: `X-API-Key: <NOWHERE_MCP_KEY>`

The MCP authentication contract is unchanged from `orangechat-http`, so existing gateway configuration can keep using the same URL/key mapping.

Do not expose the MCP endpoint without `NOWHERE_MCP_KEY`.

## License

The upstream project is licensed under CC BY-NC 4.0. Keep upstream attribution and do not use it commercially.
