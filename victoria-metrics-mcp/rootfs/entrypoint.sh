#!/usr/bin/bashio
set -e

export VM_INSTANCE_ENTRYPOINT="$(bashio::config 'backend_url')"
export VM_INSTANCE_TYPE="$(bashio::config 'instance_type')"
export VM_INSTANCE_BEARER_TOKEN="$(bashio::config 'backend_bearer_token')"
# Upstream builds an in-memory search index of the bundled VictoriaMetrics
# docs (about 575 MB on HA Green) unless the documentation tool is disabled.
# MCP_DISABLED_TOOLS replaces upstream's default list, so the option repeats it.
if bashio::config.has_value 'disabled_tools'; then
    export MCP_DISABLED_TOOLS="$(bashio::config 'disabled_tools')"
fi
export MCP_SERVER_MODE=http
export MCP_LISTEN_ADDR=0.0.0.0:18081

exec /usr/local/bin/mcp-victoriametrics
