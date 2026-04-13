#!/usr/bin/env bash
set -euo pipefail

OPTIONS=/data/options.json

# Read HA addon configuration
export SERVER_AUTH_TOKEN=$(jq -r '.auth_token' "$OPTIONS")
export PORT=$(jq -r '.port // 8081' "$OPTIONS")
export SERVER_ADDR=":${PORT}"
export BROWSER_MAX_WIDTH=$(jq -r '.browser_max_width // 1920' "$OPTIONS")
export BROWSER_MAX_HEIGHT=$(jq -r '.browser_max_height // 1080' "$OPTIONS")
export BROWSER_FLAG=$(jq -r '.browser_flags // "--disable-dev-shm-usage,--no-sandbox,--disable-extensions"' "$OPTIONS")
export API_SILENCE_REQUEST_LOG_PATH="/healthz"

exec tini -- /usr/bin/grafana-image-renderer server
