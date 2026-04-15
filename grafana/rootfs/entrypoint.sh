#!/usr/bin/env bashio

## Grafana
HA_DATA_DIR=/data
export GF_PATHS_DATA=$HA_DATA_DIR/data
export GF_PATHS_LOGS=$HA_DATA_DIR/logs
export GF_PATHS_PLUGINS=$HA_DATA_DIR/plugins

if bashio::config 'enable_profiling'; then
    export GF_DIAGNOSTICS_PROFILING_ENABLED=true
    export GF_DIAGNOSTICS_PROFILING_ADDR=0.0.0.0
    export GF_DIAGNOSTICS_PROFILING_PORT=8079
fi

export GF_SERVER_HTTP_PORT=8080
export GF_SERVER_DOMAIN="$(bashio::config 'domain')"

export GF_RENDERING_SERVER_URL="$(bashio::config 'grafana_rendering_server_url')"
export GF_RENDERING_CALLBACK_URL="http://$GF_SERVER_DOMAIN:$GF_SERVER_HTTP_PORT/"
export GF_RENDERING_RENDERER_TOKEN="$(bashio::config 'grafana_rendering_renderer_token')"

export GF_ANALYTICS_ENABLED=false
export GF_ANALYTICS_REPORTING_ENABLED=false
export GF_ANALYTICS_CHECK_FOR_UPDATES=false

export GF_LOG_CONSOLE_FORMAT="$(bashio::config 'log_format')"
export GF_LOG_LEVEL="$(bashio::config 'log_level')"

export GF_PLUGINS_PREINSTALL="$(bashio::config 'plugins')"

exec /run.sh "$@"
