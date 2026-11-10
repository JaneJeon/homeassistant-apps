# VictoriaLogs MCP

This app runs the VictoriaLogs MCP server from upstream commit `0baa4f48c532d64162c88bad939d00f6e6524649` over Streamable HTTP.

## Connect a client

Use `http://homeassistant:18082/mcp` as the MCP URL in Codex Desktop. For Claude Desktop, configure the local HTTP-to-stdio bridge with the same URL.

## App options

- **Backend URL** defaults to `http://24c4882f-victoria-logs:9428`.
- **Backend bearer token** is optional and is sent only to VictoriaLogs.

The app exposes port `18082/tcp`. It does not authenticate MCP callers, so access is controlled by the LAN and tailnet network boundary.

The Go server and web UI are built from the pinned upstream source commit for the selected Home Assistant architecture. Its `go.mod` and `go.sum` are part of that pinned source.
