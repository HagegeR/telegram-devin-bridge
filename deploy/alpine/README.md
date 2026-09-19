# Alpine polling deployment

This example runs the bridge as a dedicated OpenRC service in Telegram
polling mode. The installer expects the repository to be available locally and
installs it under `/opt/telegram-devin-bridge`.

```sh
sh deploy/alpine/install.sh
vi /opt/telegram-devin-bridge/.env
rc-service telegram-devin-bridge start
```

The service runs `.venv/bin/python -m app.poll` and stores logs under
`/var/log`. Set the Telegram and Devin credentials in `.env`; the installer
sets `TELEGRAM_MODE=polling` so a public webhook URL is not required.

Fly's SQLite volume is mounted at `/data`. To migrate the database:

```sh
flyctl ssh sftp get /data/bridge.sqlite3
cp bridge.sqlite3 /opt/telegram-devin-bridge/bridge.sqlite3
```
