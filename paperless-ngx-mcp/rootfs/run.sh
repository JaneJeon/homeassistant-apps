#!/bin/sh
set -eu

export PAPERLESS_URL="$(jq -er '.paperless_url | select(type == "string" and length > 0)' /data/options.json)"
api_token="$(jq -r '.api_token // ""' /data/options.json)"
if [ -z "$api_token" ] || [ "$api_token" = "-" ]; then
    echo "Set the Paperless API token in this app's Options before starting it." >&2
    exit 1
fi
export API_KEY="$api_token"
unset api_token

exec node /app/build/index.js --http --port 3000
