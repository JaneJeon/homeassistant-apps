#!/bin/sh
set -eu

# HAOS controls the group IDs of the bind-mounted system journals.
groups="$(
    for path in /var/log/journal/*/system.journal /run/log/journal/*/system.journal /var/log/journal/*/ /run/log/journal/*/; do
        if [ -e "$path" ]; then stat -c '%g' "$path"; fi
    done | awk '$1 != 0' | sort -nu | paste -sd, -
)"
if [ -n "$groups" ]; then
    set -- --groups="$groups"
else
    set -- --clear-groups
fi
# Supervisor writes options with mode 0600. Open it before dropping privileges.
exec setpriv --reuid=65534 --regid=65534 "$@" --no-new-privs /usr/local/bin/app-disk-io < /data/options.json
