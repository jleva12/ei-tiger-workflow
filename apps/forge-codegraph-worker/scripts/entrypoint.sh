#!/bin/sh
set -eu
if [ "$(id -u)" = 0 ]; then
    mkdir -p /var/lib/codegraph
    chown 10001:10001 /var/lib/codegraph
    exec gosu 10001:10001 "$@"
fi
exec "$@"
