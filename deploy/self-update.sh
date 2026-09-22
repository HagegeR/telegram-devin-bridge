#!/bin/sh
# Update the bridge checkout to the latest revision for its update channel and
# restart the service if anything changed; supervised unprivileged services
# exit for their supervisor to respawn because they cannot invoke the service
# manager directly.
# Usage: self-update.sh [--check] [channel] (uses a host-wide lock and deploy marker)
#   channel default: $SELF_UPDATE_CHANNEL or $SELF_UPDATE_BRANCH or main
#   channels:
#     <branch>   track origin/<branch> head (e.g. main) — the classic behavior
#     stable     newest semver release tag (vX.Y.Z) reachable from origin/main
#     vX         newest tag within major X      (e.g. v1  -> v1.*.*)
#     vX.Y       newest tag within minor X.Y    (e.g. v1.2 -> v1.2.*)
#     vX.Y.Z     pin that exact tag             (e.g. v1.2.3)
# NOTE: `git reset --hard` + `git checkout -f` discards local commits/changes on
# purpose — the host checkout is deploy-only and must track the remote exactly.
# `git clean` is deliberately NOT run: it could delete .env/.venv/bridge.sqlite3
# if they are ever un-ignored.
set -eu
cd "$(dirname "$0")/.."
LOCK="${SELF_UPDATE_LOCK:-.self-update.lock}"
command -v flock >/dev/null 2>&1 || { echo "flock not found (apk add util-linux-misc)"; exit 1; }
exec 9>"$LOCK"
flock -n 9 || { echo "another update is running"; exit 0; }
CHECK=0; [ "${1:-}" = "--check" ] && { CHECK=1; shift; }
CHANNEL="${1:-${SELF_UPDATE_CHANNEL:-${SELF_UPDATE_BRANCH:-main}}}"
SERVICE="${SELF_UPDATE_SERVICE:-telegram-devin-bridge}"
MARKER=.self-update-rev
PREV=$(cat "$MARKER" 2>/dev/null || true)

# semver numeric component: no leading zeroes
NUM='0|[1-9][0-9]*'
MODE=branch; PATTERN=
if [ "$CHANNEL" = stable ]; then
  MODE=tag; PATTERN='v*'
elif printf '%s\n' "$CHANNEL" | grep -qE "^v($NUM)(\\.($NUM)){0,2}$"; then
  MODE=tag
  V="${CHANNEL#v}"
  case "$V" in
    *.*.*) PATTERN="v$V" ;;     # exact pin   v1.2.3
    *.*)   PATTERN="v$V.*" ;;   # minor line  v1.2.*
    *)     PATTERN="v$V.*.*" ;; # major line  v1.*.*
  esac
fi

TAG=
if [ "$MODE" = tag ]; then
  git fetch -q --prune origin '+refs/tags/v*:refs/tags/v*'
  # tag channels only ever deploy commits merged into main; fail closed if
  # origin/main cannot be resolved rather than admitting unmerged tags
  git fetch -q origin '+refs/heads/main:refs/remotes/origin/main' || \
    { echo "release channels require fetching origin/main"; exit 1; }
  TAG=$(git tag -l "$PATTERN" --sort=-v:refname --merged origin/main | \
    grep -E "^v($NUM)\\.($NUM)\\.($NUM)$" | head -n 1)
  [ -n "$TAG" ] || { echo "no release tag matches channel '$CHANNEL'"; exit 1; }
  REMOTE=$(git rev-parse "$TAG^{commit}")
  TRACK="$TAG"
else
  git fetch -q origin "$CHANNEL"
  REMOTE=$(git rev-parse "origin/$CHANNEL")
  TRACK="$CHANNEL"
fi

LOCAL=$(git rev-parse HEAD)
if [ "$LOCAL" = "$REMOTE" ] && git diff --quiet && git diff --cached --quiet && [ "$PREV" = "$REMOTE" ]; then echo "up to date at $(git rev-parse --short HEAD) ($TRACK)"; exit 0; fi
if [ "$LOCAL" = "$REMOTE" ]; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "local changes detected; resetting to $TRACK"
  elif [ -z "$PREV" ]; then
    echo "no deploy marker; installing"
  else
    echo "previous update of $(git rev-parse --short "$REMOTE") incomplete; retrying install"
  fi
else
  echo "update available: $(git rev-parse --short "$LOCAL") -> $(git rev-parse --short "$REMOTE") ($TRACK)"
  git log --oneline "$LOCAL..$REMOTE" | head -20
fi
[ "$CHECK" = 1 ] && exit 0
git reset -q --hard
if [ "$MODE" = tag ]; then
  git checkout -q -f --detach "$TAG"
else
  git checkout -q -f -B "$CHANNEL" "origin/$CHANNEL"
fi
[ "$(git rev-parse HEAD)" = "$REMOTE" ] || { echo "checkout failed"; exit 1; }
if [ -z "$PREV" ] || ! git cat-file -e "$PREV^{commit}" 2>/dev/null; then
  .venv/bin/pip install -q -r requirements.txt
elif ! git diff --quiet "$PREV" "$REMOTE" -- requirements.txt; then
  .venv/bin/pip install -q -r requirements.txt
fi
if [ -f requirements-transcription.txt ] \
  && .venv/bin/python -c "import faster_whisper" >/dev/null 2>&1 \
  && [ -n "$PREV" ] \
  && git cat-file -e "$PREV^{commit}" 2>/dev/null \
  && ! git diff --quiet "$PREV" "$REMOTE" -- requirements-transcription.txt; then
  .venv/bin/pip install -q -r requirements-transcription.txt
fi
echo "$REMOTE" > "$MARKER"
# restart detached so a caller running inside the service (the /update
# command) can still reply before the process is recycled. Close the lock fd
# (9>&-) so the restarted service does not inherit the flock and block every
# later update with "another update is running".
# leave a pending-notification marker for the restarted process: old rev,
# new rev, an optional chat target ($SELF_UPDATE_NOTIFY, empty for cron); the
# bridge appends a delivery attempt count and removes the file once announced
write_pending() { printf '%s\n%s\n%s\n' "$LOCAL" "$REMOTE" "${SELF_UPDATE_NOTIFY:-}" > .self-update-pending; }
if [ "$(id -u)" -eq 0 ]; then
  if command -v rc-service >/dev/null 2>&1; then
    write_pending
    nohup sh -c "sleep 2; rc-service $SERVICE restart" >/dev/null 2>&1 9>&- &
    echo "restarting $SERVICE"
  elif command -v systemctl >/dev/null 2>&1; then
    write_pending
    nohup sh -c "sleep 2; systemctl restart $SERVICE" >/dev/null 2>&1 9>&- &
    echo "restarting $SERVICE"
  fi
elif [ -n "${RC_SVCNAME:-}" ] || [ -n "${INVOCATION_ID:-}" ]; then
  write_pending
  nohup sh -c "sleep 2; kill -TERM $PPID" >/dev/null 2>&1 9>&- &
  echo "restarting $SERVICE (supervisor respawn)"
else
  echo "restart $SERVICE manually to load the update"
fi
echo "updated to $(git rev-parse --short "$REMOTE") ($TRACK)"
