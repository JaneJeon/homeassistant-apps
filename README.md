# Jane's Home Assistant apps

Home Assistant app repository for services running on HA Green. Add `https://github.com/JaneJeon/homeassistant-apps` in **Settings → Apps → Install app → ⋮ → Repositories**.

| App                    | Purpose                       | MCP URL                          |
| ---------------------- | ----------------------------- | -------------------------------- |
| Grafana (Fixed)        | Grafana service               | —                                |
| Grafana Image Renderer | Rendering service for Grafana | —                                |
| Home Assistant MCP     | Home Assistant tools          | `http://homeassistant:9583/mcp`  |
| Grafana MCP            | Grafana tools                 | `http://homeassistant:18080/mcp` |
| VictoriaMetrics MCP    | Metrics queries               | `http://homeassistant:18081/mcp` |
| VictoriaLogs MCP       | Log queries                   | `http://homeassistant:18082/mcp` |

Each MCP is a separate app and container. The four endpoints use Streamable HTTP. Their Home Assistant app Options hold backend addresses and credentials; desktop client configurations contain only the MCP URL. See each app's `DOCS.md` for its options and pinned upstream version.

## Desktop clients

Use the **same URL** for a given MCP in both clients. Codex supports Streamable HTTP directly:

```toml
[mcp_servers.grafana]
url = "http://homeassistant:18080/mcp"
```

Claude Desktop reaches that URL from this computer through a local transport bridge:

```json
{
  "mcpServers": {
    "grafana": {
      "command": "uvx",
      "args": [
        "fastmcp-remote@4.0.7",
        "http://homeassistant:18080/mcp",
        "--auth",
        "none",
        "--silent"
      ]
    }
  }
}
```

Repeat with the other URLs in the table. The bridge forwards MCP traffic; Grafana and its credentials stay on HA Green. Claude's account-level remote connector connects from Anthropic's cloud and cannot reach these private addresses.

## Access boundary

The MCP listeners have no separate caller token. Any device that can reach their ports on the home LAN or tailnet can use the exposed tools with the app's backend permissions. Keep these ports off the public internet. Store the Grafana service account token only in the Grafana MCP app's password option. Home Assistant MCP obtains its backend authorization from Supervisor.

## Releasing changes

The existing GitHub Actions workflows lint each app and build changed app directories for `aarch64` and `amd64`. Pull requests build without publishing; a push to `main` publishes versioned images to GHCR. Keep each app's `config.yaml` version aligned with its published image tag.
