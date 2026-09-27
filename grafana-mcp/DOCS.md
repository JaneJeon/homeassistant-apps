# Grafana MCP

This app runs the official `mcp-grafana` 1.6.0 release as a Streamable HTTP
server. Both Codex and the local Claude bridge use:

```text
http://homeassistant:18080/mcp
```

## Options

| Option                  | Default                        | Purpose                                                                   |
| ----------------------- | ------------------------------ | ------------------------------------------------------------------------- |
| `grafana_url`           | `http://894a6dc5-grafana:8080` | Internal Grafana service URL                                              |
| `service_account_token` | `-`                            | Grafana service-account token; enter it in the Home Assistant app Options |

The service-account token stays in the app's Options and is supplied to the
server through a runtime secret file. It does not belong in desktop client
configuration. The server has no separate caller token. Any device that can
reach port 18080 can invoke its tools with the configured Grafana permissions.

The server binds port 8000 inside the app. The app maps it to host port 18080.
Its HTTP Host allowlist admits `homeassistant:18080`, the app service name, and
the loopback names used by local probes. It does not use a wildcard.

The image downloads the upstream v1.6.0 Linux release binary for each supported
architecture and checks the official SHA-256 digest during the build. Release
v1.6.0 points to upstream commit `6cdd5d1d1e45783203d3a9445e771758aa1c7a6e`.
