# VictoriaMetrics MCP

This app runs the VictoriaMetrics MCP server from upstream commit `4f00299f1130835ab81969dd953ed6d26906c675` over Streamable HTTP.

## Connect a client

Use `http://homeassistant:18081/mcp` as the MCP URL in Codex Desktop. For Claude Desktop, configure the local HTTP-to-stdio bridge with the same URL.

## App options

- **Backend URL** defaults to `http://1bd4a9fb-victoria-metrics:8428`.
- **Instance type** defaults to `single`; choose `cluster` for a cluster endpoint.
- **Backend bearer token** is optional and is sent only to VictoriaMetrics.

The app exposes port `18081/tcp`. It does not authenticate MCP callers, so access is controlled by the LAN and tailnet network boundary.

The Go server and web UI are built from the pinned upstream source commit for the selected Home Assistant architecture. Its `go.mod` and `go.sum` are part of that pinned source.
