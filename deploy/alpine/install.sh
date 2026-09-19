#!/bin/sh
set -eu

apk add python3 py3-pip git

if ! id telegram-devin >/dev/null 2>&1; then
    adduser -D -H telegram-devin
fi

mkdir -p /opt/telegram-devin-bridge
cp -R . /opt/telegram-devin-bridge
cd /opt/telegram-devin-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
if [ ! -f .env ]; then
    cp .env.example .env
    sed -i 's/^TELEGRAM_MODE=.*/TELEGRAM_MODE=polling/' .env
fi
chown -R telegram-devin:telegram-devin /opt/telegram-devin-bridge
install -m 0755 deploy/alpine/telegram-devin-bridge.initd \
    /etc/init.d/telegram-devin-bridge
rc-update add telegram-devin-bridge default
