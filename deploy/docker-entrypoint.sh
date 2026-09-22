#!/bin/sh
# Runs as root: repair ownership of the mounted data directory, then drop to
# the unprivileged user. Mounted volumes (Fly, compose) hide the image-time
# /data ownership and may contain root-owned databases from older images.
set -eu

mkdir -p /data
chown -R bridge:bridge /data
exec su -s /bin/sh bridge -c 'exec "$0" "$@"' -- "$@"
