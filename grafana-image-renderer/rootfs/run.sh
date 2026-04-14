#!/usr/bin/env bashio

export SERVER_ADDR=":8081"
export AUTH_TOKEN="$(bashio::config 'auth_token')"
export BROWSER_MAX_WIDTH="$(bashio::config 'browser_max_width')"
export BROWSER_MAX_HEIGHT="$(bashio::config 'browser_max_height')"
export BROWSER_FLAGS="$(bashio::config 'browser_flags')"
export API_SILENCE_REQUEST_LOG_PATH="/healthz"
export LOG_LEVEL="$(bashio::config 'log_level')"

# Desperation...
export BROWSER_READINESS_DOM_HASHCODE_TIMEOUT=1s
export BROWSER_READINESS_GIVE_UP_ON_ALL_QUERIES=1s
export BROWSER_READINESS_ITERATION_INTERVAL=5s
export BROWSER_READINESS_NETWORK_IDLE_TIMEOUT=1s
export BROWSER_READINESS_PRIOR_WAIT=5s

if bashio::config.has_value 'grafana_service_account_token'; then
    echo "Setting Grafana service account token"
    export BROWSER_HEADER="Authorization=Bearer $(bashio::config 'grafana_service_account_token')"
fi

exec /usr/bin/grafana-image-renderer "$@"
