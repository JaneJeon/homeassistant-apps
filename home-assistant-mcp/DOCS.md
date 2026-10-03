# Home Assistant MCP

This app packages the `ha-mcp` 7.2.0 server from upstream commit
`651adae00bd9386fa0d609698a8be8ce153aba73`. It serves Streamable HTTP at
`http://homeassistant:9583/mcp` on the Home Assistant app network.

## Access and credentials

The app uses the Supervisor API token supplied to the container. It does not
create a caller token or a secret URL path. Any device that can reach port 9583
can call the MCP tools with this app's Home Assistant permissions. Keep access
to the Home Assistant host within the intended LAN and tailnet boundary. Do not
forward port 9583 from the WAN.

Add-on discovery and settings reads use Supervisor's REST API directly with
the app's token and `manager` role. Supervisor rejects app-originated
`supervisor/api` commands through the Core WebSocket proxy. This packaging
adapts that transport while retaining the pinned 7.2.0 server and tool surface.
The adapter accepts only the three read paths used by those pinned tools:
installed apps, the app store, and details for a validated app slug.
The manager role allows add-on management and settings access. It does not
grant the admin-only ability to change another app's protection mode.

No Home Assistant credential belongs in a Codex or Claude client configuration.
Codex connects to the URL directly. Claude Desktop can use the same URL through
the `fastmcp-remote` HTTP-to-stdio bridge.

## Options

- `backup_hint`: controls when the server suggests a backup (`strong`,
  `normal`, `weak`, or `auto`). The default is `normal`.
- `enable_skills`: expose bundled skills. The default is `true`.
- `enable_skills_as_tools`: expose skills as tools. The default is `false`.
- `enable_tool_search`: use search-based tool discovery. The default is
  `false`.
- `enable_yaml_config_editing`: enable YAML configuration editing tools. The
  default is `false`.

Restart the app after changing options.
