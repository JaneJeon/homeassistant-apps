#!/usr/bin/env bashio

export PORT="$(bashio::config 'port')"
export SERVER_ADDR=":${PORT}"
export SERVER_AUTH_TOKEN="$(bashio::config 'auth_token')"
export BROWSER_MAX_WIDTH="$(bashio::config 'browser_max_width')"
export BROWSER_MAX_HEIGHT="$(bashio::config 'browser_max_height')"
export BROWSER_FLAGS="$(bashio::config 'browser_flags')"
export API_SILENCE_REQUEST_LOG_PATH="/healthz"

exec /usr/bin/grafana-image-renderer "$@"
