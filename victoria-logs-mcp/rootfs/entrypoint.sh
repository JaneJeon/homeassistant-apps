#!/usr/bin/bashio
set -e

export VL_INSTANCE_ENTRYPOINT="$(bashio::config 'backend_url')"
export VL_INSTANCE_BEARER_TOKEN="$(bashio::config 'backend_bearer_token')"
export MCP_SERVER_MODE=http
export MCP_LISTEN_ADDR=0.0.0.0:18082

exec /usr/local/bin/mcp-victorialogs
