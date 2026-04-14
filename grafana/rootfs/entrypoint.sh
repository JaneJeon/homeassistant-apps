#!/usr/bin/env bashio

## Grafana
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
