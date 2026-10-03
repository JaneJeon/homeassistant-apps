#!/usr/bin/env python3
"""Configure the pinned ha-mcp package for Home Assistant Supervisor."""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    options_path = Path("/data/options.json")
    try:
        options = json.loads(options_path.read_text()) if options_path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Unable to read app options: {exc}", file=sys.stderr, flush=True)
        return 1
    if not isinstance(options, dict):
        print("App options must be a JSON object.", file=sys.stderr, flush=True)
        return 1

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        print("SUPERVISOR_TOKEN is missing; enable Supervisor API access.", file=sys.stderr, flush=True)
        return 1

    os.environ.update(
        {
            "HOMEASSISTANT_URL": "http://supervisor/core",
            "HOMEASSISTANT_TOKEN": supervisor_token,
            "BACKUP_HINT": str(options.get("backup_hint", "normal")),
            "ENABLE_SKILLS": str(options.get("enable_skills", True)).lower(),
            "ENABLE_SKILLS_AS_TOOLS": str(options.get("enable_skills_as_tools", False)).lower(),
            "ENABLE_TOOL_SEARCH": str(options.get("enable_tool_search", False)).lower(),
            "ENABLE_YAML_CONFIG_EDITING": str(options.get("enable_yaml_config_editing", False)).lower(),
            "MCP_PORT": "9583",
            "MCP_SECRET_PATH": "/mcp",
        }
    )

    # App-originated supervisor/api WebSocket commands are blocked by Supervisor.
    # Use the app's scoped token against the supported Supervisor REST API.
    from ha_mcp.tools import tools_addons
    from supervisor_api import supervisor_api_call

    tools_addons._supervisor_api_call = supervisor_api_call

    # Use the upstream v7.2.0 HTTP entry point, which configures its server,
    # stateless Streamable HTTP transport, browser response, and shutdown hooks.
    from ha_mcp.__main__ import main_web

    main_web()
    return 0


if __name__ == "__main__":
    sys.exit(main())
