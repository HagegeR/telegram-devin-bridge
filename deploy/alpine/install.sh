#!/bin/sh
set -eu
exec sh "$(dirname "$0")/../vm/install.sh" "$@"
