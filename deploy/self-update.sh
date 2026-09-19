#!/bin/sh
# Update the bridge checkout to the latest origin/<branch> and restart the
# service if anything changed.
# Usage: self-update.sh [--check] [branch] (uses a host-wide lock and deploy marker)
#   branch default: $SELF_UPDATE_BRANCH or main
# NOTE: `git reset --hard` + `git checkout -f -B` discards local
# commits/changes on purpose — the host checkout is deploy-only and must
# track the remote exactly. `git clean` is deliberately NOT run: it could
# delete .env/.venv/bridge.sqlite3 if they are ever un-ignored.
set -eu
cd "$(dirname "$0")/.."
LOCK="${SELF_UPDATE_LOCK:-.self-update.lock}"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -n 9 || { echo "another update is running"; exit 0; }
else
  mkdir "$LOCK.d" 2>/dev/null || { echo "another update is running"; exit 0; }
  trap 'rmdir "$LOCK.d"' EXIT INT TERM
fi
CHECK=0; [ "${1:-}" = "--check" ] && { CHECK=1; shift; }
BRANCH="${1:-${SELF_UPDATE_BRANCH:-main}}"
SERVICE="${SELF_UPDATE_SERVICE:-telegram-devin-bridge}"
MARKER=.self-update-rev
PREV=$(cat "$MARKER" 2>/dev/null || true)
git fetch -q origin "$BRANCH"
LOCAL=$(git rev-parse HEAD); REMOTE=$(git rev-parse "origin/$BRANCH")
if [ "$LOCAL" = "$REMOTE" ] && git diff --quiet && git diff --cached --quiet && [ "$PREV" = "$REMOTE" ]; then echo "up to date at $(git rev-parse --short HEAD) ($BRANCH)"; exit 0; fi
if [ "$LOCAL" = "$REMOTE" ]; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "local changes detected; resetting to origin/$BRANCH"
  elif [ -z "$PREV" ]; then
    echo "no deploy marker; installing"
  else
    echo "previous update of $(git rev-parse --short "$REMOTE") incomplete; retrying install"
  fi
else
  echo "update available: $(git rev-parse --short "$LOCAL") -> $(git rev-parse --short "$REMOTE") ($BRANCH)"
  git log --oneline "$LOCAL..$REMOTE" | head -20
fi
[ "$CHECK" = 1 ] && exit 0
git reset -q --hard
git checkout -q -f -B "$BRANCH" "origin/$BRANCH"
[ "$(git rev-parse HEAD)" = "$REMOTE" ] || { echo "checkout failed"; exit 1; }
if [ -z "$PREV" ] || ! git cat-file -e "$PREV^{commit}" 2>/dev/null; then
  .venv/bin/pip install -q -r requirements.txt
elif ! git diff --quiet "$PREV" "$REMOTE" -- requirements.txt; then
  .venv/bin/pip install -q -r requirements.txt
fi
echo "$REMOTE" > "$MARKER"
# restart detached so a caller running inside the service (the /update
# command) can still reply before the process is recycled
if command -v rc-service >/dev/null 2>&1; then
  nohup sh -c "sleep 2; rc-service $SERVICE restart" >/dev/null 2>&1 &
  echo "restarting $SERVICE"
fi
echo "updated to $(git rev-parse --short "$REMOTE")"
