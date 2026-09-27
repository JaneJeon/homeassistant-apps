#!/usr/bin/bashio
set -e

export VM_INSTANCE_ENTRYPOINT="$(bashio::config 'backend_url')"
export VM_INSTANCE_TYPE="$(bashio::config 'instance_type')"
export VM_INSTANCE_BEARER_TOKEN="$(bashio::config 'backend_bearer_token')"
export MCP_SERVER_MODE=http
export MCP_LISTEN_ADDR=0.0.0.0:18081

exec /usr/local/bin/mcp-victoriametrics
