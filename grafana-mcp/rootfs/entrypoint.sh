#!/usr/bin/env bashio
set -euo pipefail

export GRAFANA_URL="$(bashio::config 'grafana_url')"

service_account_token="$(bashio::config 'service_account_token')"
if [[ -n "${service_account_token}" && "${service_account_token}" != "-" ]]; then
    install -d -m 0700 /run/secrets
    umask 077
    printf '%s' "${service_account_token}" > /run/secrets/grafana-service-account-token
    export GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE=/run/secrets/grafana-service-account-token
fi
unset service_account_token

exec /usr/local/bin/mcp-grafana \
    --transport streamable-http \
    --address 0.0.0.0:8000 \
    --endpoint-path /mcp \
    --allowed-hosts 'homeassistant:18080,grafana-mcp:8000,localhost:8000,127.0.0.1:8000,[::1]:8000' \
    --usage-stats disabled
