# OrangeChat / Zeabur remote MCP

This fork keeps the original stdio MCP entrypoint unchanged and adds a separate Streamable HTTP deployment entrypoint.

## Zeabur

Deploy branch: `orangechat-http`

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

- Health check: `https://<domain>/health`
- MCP endpoint: `https://<domain>/mcp`

## OrangeChat

- Transport: Streamable HTTP
- URL: `https://<domain>/mcp`
- Custom header: `X-API-Key: <NOWHERE_MCP_KEY>`

Do not expose the MCP endpoint without `NOWHERE_MCP_KEY`.

## License

The upstream project is licensed under CC BY-NC 4.0. Keep upstream attribution and do not use it commercially.
