#!/bin/sh
# Update the bridge checkout to the latest origin/<branch> and restart the
# service if anything changed.
# Usage: self-update.sh [--check] [branch]
#   branch default: $SELF_UPDATE_BRANCH or main
# NOTE: `git checkout -B` discards local commits/changes on purpose — the
# host checkout is deploy-only and must track the remote exactly.
set -eu
cd "$(dirname "$0")/.."
CHECK=0; [ "${1:-}" = "--check" ] && { CHECK=1; shift; }
BRANCH="${1:-${SELF_UPDATE_BRANCH:-main}}"
SERVICE="${SELF_UPDATE_SERVICE:-telegram-devin-bridge}"
git fetch -q origin "$BRANCH"
LOCAL=$(git rev-parse HEAD); REMOTE=$(git rev-parse "origin/$BRANCH")
if [ "$LOCAL" = "$REMOTE" ]; then echo "up to date at $(git rev-parse --short HEAD) ($BRANCH)"; exit 0; fi
echo "update available: $(git rev-parse --short "$LOCAL") -> $(git rev-parse --short "$REMOTE") ($BRANCH)"
git log --oneline "$LOCAL..$REMOTE" | head -20
[ "$CHECK" = 1 ] && exit 0
git checkout -q -B "$BRANCH" "origin/$BRANCH"
if ! git diff --quiet "$LOCAL" "$REMOTE" -- requirements.txt; then
  .venv/bin/pip install -q -r requirements.txt
fi
# restart detached so a caller running inside the service (the /update
# command) can still reply before the process is recycled
if command -v rc-service >/dev/null 2>&1; then
  nohup sh -c "sleep 2; rc-service $SERVICE restart" >/dev/null 2>&1 &
  echo "restarting $SERVICE"
fi
echo "updated to $(git rev-parse --short "$REMOTE")"
