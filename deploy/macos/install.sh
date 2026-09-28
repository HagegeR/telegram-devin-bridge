#!/bin/sh
# Install the bridge in place (this checkout) as a per-user launchd agent
# running in polling mode. Run as your normal user, not root:
#   sh deploy/macos/install.sh [--dry-run]
set -eu

LABEL=telegram-devin-bridge
BRIDGE_HOME=$(CDPATH= cd -P -- "$(dirname -- "$0")/../.." && pwd -P)
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/$LABEL.log"
# launchd starts agents with a bare PATH; self-update needs git and sh
AGENT_PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH="$AGENT_PATH:$PATH"

find_python() {
    for candidate in python3.14 python3.13 python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON=$(find_python || true)
if [ "${1:-}" = "--dry-run" ]; then
    printf 'bridge home: %s\n' "$BRIDGE_HOME"
    printf 'python: %s\n' "${PYTHON:-missing}"
    printf 'git: %s\n' "$(command -v git || echo missing)"
    printf 'launchd agent: %s\n' "$PLIST"
    exit 0
fi

if [ "$(id -u)" -eq 0 ]; then
    printf '%s\n' 'run this as your user; the agent lives in ~/Library/LaunchAgents' >&2
    exit 1
fi
if [ -z "$PYTHON" ] && command -v brew >/dev/null 2>&1; then
    brew install python@3.12
    PYTHON=$(find_python || true)
fi
if [ -z "$PYTHON" ]; then
    printf '%s\n' 'python >= 3.12 is required (brew install python@3.12)' >&2
    exit 1
fi
command -v git >/dev/null 2>&1 || { printf '%s\n' 'git is required (xcode-select --install)' >&2; exit 1; }
[ -e "$BRIDGE_HOME/.git" ] || printf '%s\n' 'not a git checkout; /update will be unavailable' >&2

cd "$BRIDGE_HOME"
[ -x .venv/bin/python ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

[ -f .env ] || cp .env.example .env
set_env_value() {
    escaped=$(printf '%s' "$2" | sed 's/[|&\\]/\\&/g')
    if grep -q "^$1=" .env; then
        sed "s|^$1=.*|$1=$escaped|" .env > .env.tmp
        mv .env.tmp .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}
set_env_value TELEGRAM_MODE polling
set_env_value ADMIN_LOG_PATH "$LOG"
chmod 600 .env

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
# KeepAlive respawns the process after /update or the admin restart exits it
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BRIDGE_HOME/.venv/bin/python</string>
        <string>-m</string>
        <string>app.poll</string>
    </array>
    <key>WorkingDirectory</key><string>$BRIDGE_HOME</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$AGENT_PATH</string>
        <key>PYTHONUNBUFFERED</key><string>1</string>
        <key>BRIDGE_SUPERVISED</key><string>1</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
printf 'started %s; logs: %s\n' "$LABEL" "$LOG"
printf 'fill in %s/.env, then: launchctl kickstart -k %s/%s\n' "$BRIDGE_HOME" "$DOMAIN" "$LABEL"
