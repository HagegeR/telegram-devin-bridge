#!/bin/sh
set -eu

SERVICE_NAME=telegram-devin-bridge
SERVICE_USER=telegram-devin
BRIDGE_HOME=${BRIDGE_HOME:-/opt/telegram-devin-bridge}
DATA_DIR=/var/lib/telegram-devin-bridge
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
else
    DRY_RUN=false
fi

if command -v apk >/dev/null 2>&1; then
    PKG_MANAGER=apk
elif command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER=apt-get
elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER=dnf
elif command -v yum >/dev/null 2>&1; then
    PKG_MANAGER=yum
elif command -v pacman >/dev/null 2>&1; then
    PKG_MANAGER=pacman
elif command -v zypper >/dev/null 2>&1; then
    PKG_MANAGER=zypper
else
    PKG_MANAGER=unknown
fi

if command -v rc-service >/dev/null 2>&1 && [ -x /sbin/openrc-run ]; then
    INIT_SYSTEM=openrc
elif command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    INIT_SYSTEM=systemd
else
    INIT_SYSTEM=manual
fi

# distro python3 is often older than 3.12 (Debian 12, Ubuntu 22.04, RHEL 9);
# versioned binaries or a uv-managed interpreter cover those
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
if [ -n "$PYTHON" ]; then
    PYTHON_VERSION=$("$PYTHON" --version 2>&1)
elif command -v uv >/dev/null 2>&1; then
    PYTHON_VERSION='uv-managed 3.12'
else
    PYTHON_VERSION=missing
fi

if [ "$DRY_RUN" = true ]; then
    printf 'package manager: %s\n' "$PKG_MANAGER"
    printf 'init: %s\n' "$INIT_SYSTEM"
    printf 'python: %s\n' "$PYTHON_VERSION"
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'run this installer as root (for example: sudo sh deploy/vm/install.sh)' >&2
    exit 1
fi

case "$PKG_MANAGER" in
    apk)
        apk add python3 py3-pip git
        apk add flock >/dev/null 2>&1 || apk add util-linux-misc
        ;;
    apt-get)
        apt-get update
        apt-get install -y python3 python3-venv python3-pip git
        ;;
    dnf)
        dnf install -y python3 python3-pip git tar
        ;;
    yum)
        yum install -y python3 python3-pip git tar
        ;;
    pacman)
        pacman -Sy --noconfirm python python-pip git tar
        ;;
    zypper)
        zypper --non-interactive install python3 python3-pip git shadow tar
        ;;
    unknown)
        printf '%s\n' 'install python3 (>=3.12), pip, git manually, then rerun this installer' >&2
        ;;
esac

PYTHON=$(find_python || true)
if [ -z "$PYTHON" ]; then
    # best effort: the distro may package a newer interpreter next to python3
    case "$PKG_MANAGER" in
        apt-get) apt-get install -y python3.12 python3.12-venv >/dev/null 2>&1 || true ;;
        dnf|yum) "$PKG_MANAGER" install -y python3.12 python3.12-pip >/dev/null 2>&1 || true ;;
        zypper) zypper --non-interactive install python312 >/dev/null 2>&1 || true ;;
    esac
    PYTHON=$(find_python || true)
fi
if [ -z "$PYTHON" ] && ! command -v uv >/dev/null 2>&1; then
    printf 'python >= 3.12 is required; found %s.\n' \
        "$(python3 --version 2>&1 || echo none)" >&2
    printf '%s\n' 'install uv (https://docs.astral.sh/uv/) and rerun, or use docker compose' >&2
    exit 1
fi

if id "$SERVICE_USER" >/dev/null 2>&1; then
    :
elif [ "$PKG_MANAGER" = apk ] && command -v adduser >/dev/null 2>&1; then
    adduser -D -H "$SERVICE_USER"
elif command -v useradd >/dev/null 2>&1; then
    NOLOGIN=$(command -v nologin || echo /usr/sbin/nologin)
    useradd -r -M -s "$NOLOGIN" "$SERVICE_USER"
else
    printf 'cannot create %s; install adduser or useradd manually\n' "$SERVICE_USER" >&2
    exit 1
fi

mkdir -p "$BRIDGE_HOME" "$DATA_DIR"
BRIDGE_HOME=$(CDPATH= cd -P -- "$BRIDGE_HOME" && pwd -P)
SOURCE_DIR=$(CDPATH= cd -P -- "$SOURCE_DIR" && pwd -P)
if [ "$SOURCE_DIR" = "$BRIDGE_HOME" ]; then
    printf '%s\n' 'installing in place'
else
    rm -rf "$BRIDGE_HOME/app" "$BRIDGE_HOME/deploy" "$BRIDGE_HOME/tests"
    if [ -e "$SOURCE_DIR/.git" ]; then
        rm -rf "$BRIDGE_HOME/.git"
        GIT_EXCLUDE=
    else
        rm -rf "$BRIDGE_HOME/.git"
        printf '%s\n' 'source is not a git checkout; /update will be unavailable' >&2
        GIT_EXCLUDE='--exclude=.git'
    fi
    tar -C "$SOURCE_DIR" \
        $GIT_EXCLUDE \
        --exclude='.venv' \
        --exclude='.python' \
        --exclude='*.sqlite3' \
        --exclude='.env' \
        -cf - . | tar -C "$BRIDGE_HOME" -xf -
fi

cd "$BRIDGE_HOME"
if [ ! -x .venv/bin/python ]; then
    if [ -n "$PYTHON" ]; then
        "$PYTHON" -m venv .venv
    else
        # keep the interpreter inside BRIDGE_HOME so the service user can
        # read it; --seed adds pip for deploy/self-update.sh
        UV_PYTHON_INSTALL_DIR="$BRIDGE_HOME/.python" \
            uv venv --seed --python 3.12 .venv
    fi
fi
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
fi
set_env_value() {
    key=$1
    value=$2
    escaped=$(printf '%s' "$value" | sed 's/[|&\\]/\\&/g')
    if grep -q "^${key}=" .env; then
        sed "s|^${key}=.*|${key}=${escaped}|" .env > .env.tmp
        mv .env.tmp .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}
set_env_value TELEGRAM_MODE polling
set_env_value DATABASE_PATH "$DATA_DIR/bridge.sqlite3"
chmod 600 .env

chown -R "$SERVICE_USER:$SERVICE_USER" "$BRIDGE_HOME" "$DATA_DIR"

install_service_file() {
    source_file=$1
    target_file=$2
    escaped_home=$(printf '%s\n' "$BRIDGE_HOME" | sed 's/[|&]/\\&/g')
    sed "s|/opt/telegram-devin-bridge|$escaped_home|g" \
        "$source_file" > "$target_file"
    chmod 0755 "$target_file"
}

case "$INIT_SYSTEM" in
    openrc)
        install -d /etc/init.d
        if [ -x /etc/init.d/telegram-devin-bridge ]; then
            rc-service "$SERVICE_NAME" stop || true
        fi
        install_service_file \
            "$SOURCE_DIR/deploy/vm/telegram-devin-bridge.initd" \
            /etc/init.d/telegram-devin-bridge
        rc-update add "$SERVICE_NAME" default
        rc-service "$SERVICE_NAME" start
        ;;
    systemd)
        install -d /etc/systemd/system
        install_service_file \
            "$SOURCE_DIR/deploy/vm/telegram-devin-bridge.service" \
            /etc/systemd/system/telegram-devin-bridge.service
        chmod 0644 /etc/systemd/system/telegram-devin-bridge.service
        systemctl daemon-reload
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            systemctl restart "$SERVICE_NAME"
        else
            systemctl enable --now "$SERVICE_NAME"
        fi
        ;;
    manual)
        printf 'no supported init system detected; run %s/.venv/bin/python -m app.poll\n' \
            "$BRIDGE_HOME"
        ;;
esac
