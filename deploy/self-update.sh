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
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -n 9 || { echo "another update is running"; exit 0; }
else
  # macOS and Git Bash on Windows ship no flock: mkdir is atomic everywhere.
  # The lock dir holds the owner pid (none yet = still being taken, for a
  # minute). A run whose owner died takes the dir over in place; the inner
  # claim mkdir makes that takeover single-winner.
  owner_alive() {
    OWNER=$(cat "$LOCK.d/pid" 2>/dev/null || true)
    if [ -n "$OWNER" ]; then
      kill -0 "$OWNER" 2>/dev/null
    else
      [ -z "$(find "$LOCK.d" -maxdepth 0 -mmin +1 2>/dev/null)" ]
    fi
  }
  busy() { echo "another update is running"; exit 0; }
  if ! mkdir "$LOCK.d" 2>/dev/null; then
    owner_alive && busy
    find "$LOCK.d/claim" -maxdepth 0 -mmin +1 -exec rmdir {} \; 2>/dev/null || true
    mkdir "$LOCK.d/claim" 2>/dev/null || busy
    if owner_alive; then rmdir "$LOCK.d/claim"; busy; fi
    echo $$ > "$LOCK.d/pid"
    rmdir "$LOCK.d/claim"
  else
    echo $$ > "$LOCK.d/pid"
  fi
  # only ever remove our own lock, and never anything but the pid file + dir
  trap '[ "$(cat "$LOCK.d/pid" 2>/dev/null)" = "$$" ] && rm -f "$LOCK.d/pid" && rmdir "$LOCK.d" 2>/dev/null' EXIT
fi
VENV_BIN=.venv/bin; [ -d .venv/Scripts ] && VENV_BIN=.venv/Scripts
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
  # drop stale remote refs first: a deleted origin/release would otherwise
  # block creating refs/remotes/origin/release/v2 on a channel switch
  git remote prune origin >/dev/null 2>&1 || true
  # explicit refspec: branch names like +canary or @ must not be read as
  # refspec syntax or HEAD shorthand
  git fetch -q origin "+refs/heads/$CHANNEL:refs/remotes/origin/$CHANNEL"
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
# deploy-only checkout: detached HEAD in both modes — creating local branch
# refs only buys name/dir conflicts when channels switch nesting
git checkout -q -f --detach "$REMOTE"
[ "$(git rev-parse HEAD)" = "$REMOTE" ] || { echo "checkout failed"; exit 1; }
if [ -z "$PREV" ] || ! git cat-file -e "$PREV^{commit}" 2>/dev/null; then
  "$VENV_BIN/pip" install -q -r requirements.txt
elif ! git diff --quiet "$PREV" "$REMOTE" -- requirements.txt; then
  "$VENV_BIN/pip" install -q -r requirements.txt
fi
if [ -f requirements-transcription.txt ] \
  && "$VENV_BIN/python" -c "import faster_whisper" >/dev/null 2>&1 \
  && [ -n "$PREV" ] \
  && git cat-file -e "$PREV^{commit}" 2>/dev/null \
  && ! git diff --quiet "$PREV" "$REMOTE" -- requirements-transcription.txt; then
  "$VENV_BIN/pip" install -q -r requirements-transcription.txt
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
restart_after() {
  write_pending
  if command -v flock >/dev/null 2>&1; then
    # serialize concurrent detached restarts on the update lock: two
    # stop+start pairs racing (e.g. /update + cron) can leave a start
    # outliving the other's stop and spawn duplicate supervise-daemons.
    # flock with no -w is safe here: the lock releases when the holder's
    # fd closes, even on death — a live-but-hung updater should block the
    # restart rather than race it. The lock path travels as $0 so an
    # apostrophe in SELF_UPDATE_LOCK can't break the inner shell.
    nohup sh -c "sleep 2; exec 9>\"\$0\"; flock 9; $1" "$LOCK" >/dev/null 2>&1 9>&- &
  else
    nohup sh -c "sleep 2; $1" >/dev/null 2>&1 9>&- &
  fi
  echo "restarting $SERVICE${2:+ ($2)}"
}
# the bridge exports BRIDGE_PID to the commands it runs; a Windows-native
# parent is invisible to $PPID under Git Bash, so it needs taskkill
BRIDGE_PID="${BRIDGE_PID:-$PPID}"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) KILL="taskkill //F //PID $BRIDGE_PID" ;;
  *) KILL="kill -TERM $BRIDGE_PID" ;;
esac
if [ "$(id -u)" -eq 0 ] && command -v rc-service >/dev/null 2>&1; then
  # rc-service only stops the pidfile-tracked supervise-daemon — orphans
  # from earlier races keep their child holding the port, so the new
  # process dies on EADDRINUSE forever (the "stuck update" loop). Sweep
  # every supervise-daemon for this service before starting.
  # d[a]emon: pkill -f matches against every cmdline, including this
  # restart shell's own (the pattern text lives in its arguments). The
  # bracket keeps the regex from matching its literal self.
  restart_after "rc-service $SERVICE stop; pkill -f 'supervise-d[a]emon $SERVICE --start' 2>/dev/null; sleep 1; rc-service $SERVICE start"
elif [ "$(id -u)" -eq 0 ] && command -v systemctl >/dev/null 2>&1; then
  restart_after "systemctl restart $SERVICE"
elif [ -n "${RC_SVCNAME:-}" ] || [ -n "${BRIDGE_SUPERVISED:-}" ]; then
  # OpenRC, launchd (deploy/macos) and deploy/windows/run.ps1 respawn us
  restart_after "$KILL" "supervisor respawn"
elif [ -n "${INVOCATION_ID:-}" ]; then
  # only TERM the caller when it is the service's own main process:
  # INVOCATION_ID leaks into every shell on systemd hosts, so a manual
  # run (or a test subprocess) would otherwise kill the caller's shell
  SYSTEM_MAINPID=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || true)
  USER_MAINPID=$(systemctl --user show -p MainPID --value "$SERVICE" 2>/dev/null || true)
  if [ "${SYSTEM_MAINPID:-0}" = "$BRIDGE_PID" ] || [ "${USER_MAINPID:-0}" = "$BRIDGE_PID" ]; then
    restart_after "$KILL" "supervisor respawn"
  else
    echo "restart $SERVICE manually to load the update"
  fi
else
  echo "restart $SERVICE manually to load the update"
fi
echo "updated to $(git rev-parse --short "$REMOTE") ($TRACK)"
