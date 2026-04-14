#!/usr/bin/env bashio

export SERVER_ADDR=":8081"
export AUTH_TOKEN="$(bashio::config 'auth_token')"
export BROWSER_MAX_WIDTH="$(bashio::config 'browser_max_width')"
export BROWSER_MAX_HEIGHT="$(bashio::config 'browser_max_height')"
export BROWSER_FLAGS="$(bashio::config 'browser_flags')"
export API_SILENCE_REQUEST_LOG_PATH="/healthz"
export LOG_LEVEL="$(bashio::config 'log_level')"

if bashio::config.has_value 'grafana_service_account_token'; then
    echo "Setting Grafana service account token"
    export BROWSER_HEADER="Authorization=Bearer $(bashio::config 'grafana_service_account_token')"
fi

exec /usr/bin/grafana-image-renderer "$@"
