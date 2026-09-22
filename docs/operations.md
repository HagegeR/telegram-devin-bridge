# Operations reference

Runtime endpoints, remote control, self-update, and service management. For
environment variables see [configuration.md](configuration.md); for the Alpine
runbook see [deployment-alpine-tailscale.md](deployment-alpine-tailscale.md).

## Endpoints

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /health` | — | `{"status":"ok"}`; exposes no configuration. |
| `POST /telegram/webhook` | `X-Telegram-Bot-Api-Secret-Token` | Telegram update receiver (webhook mode). |
| `POST /notify` | `Authorization: Bearer $NOTIFY_SECRET` | Push a notification to a chat. |
| `GET /doctor` | `Authorization: Bearer $DOCTOR_SECRET` | Run deployment diagnostics. |
| `POST /admin` | `Authorization: Bearer $ADMIN_SECRET` | Narrow remote control for cloud sessions. |

### `/notify`

When `NOTIFY_SECRET` is set:

```bash
curl -X POST http://localhost:8000/notify \
  -H 'Authorization: Bearer replace-with-notify-secret' \
  -H 'Content-Type: application/json' \
  -d '{"text":"Deployment finished","silent":true}'
```

The target is `chat_id`/`thread_id` in the request, the `/sethome` target, or
`TELEGRAM_HOME_CHANNEL`. Set `markdown` to `false` for plain text.

### `/doctor`

`GET /doctor` requires `DOCTOR_SECRET` (Bearer auth, separate from
`NOTIFY_SECRET`, 30 s cooldown between runs) and returns
`{"results": [...], "ok": bool}`.

The same diagnostics run locally:

```bash
python -m app.doctor                # human-readable check list
python -m app.doctor --json         # machine-readable
python -m app.doctor --attempts 10  # more DNS samples
```

The doctor verifies `.env` completeness, DNS reliability, `/etc/resolv.conf`
hijacking, default routes and MTU, Tailscale funnel state, the Telegram and
Devin APIs, the webhook registration, and local/public `/health`. Exit code is
1 when any check fails.

## Admin API

`POST /admin` — a narrow, no-shell remote control for cloud sessions:

| body | effect |
| --- | --- |
| `{"action":"doctor"}` | run the diagnostics, return the check list |
| `{"action":"logs","lines":200}` | tail of the service log (secrets redacted; max 500) |
| `{"action":"get-env"}` | values of the allowlisted non-secret `.env` keys |
| `{"action":"set-env","key":"DEVIN_MAX_ACU_LIMIT","value":"5"}` | rewrite one allowlisted `.env` key (applies after `restart`) |
| `{"action":"restart"}` | restart the service (detached; reply arrives before the restart) |
| `{"action":"update"}` | run the self-updater |

```bash
curl -X POST https://<host>.<tailnet>.ts.net/admin \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"action":"doctor"}'
```

Security: separate `ADMIN_SECRET`; `ADMIN_ENV_ALLOWLIST` whitelists only
non-secret keys (tokens/keys are never readable or writable); every call is
audit-logged and posts a Telegram notification; 10 req/min rate limit; log
output is secret-redacted. Failed auth attempts are tarpitted one at a time
(1 s delay); while one is in flight every request gets 429 (≈60 guesses/min
cap), so a flood of bad tokens can temporarily block valid ones — use
Tailscale SSH as fallback. Command and path keys can never be set via the API.

## Self-update

`deploy/self-update.sh` takes a host-wide lock and records a deploy marker,
resolves the configured **update channel** to a revision, checks it out,
reinstalls requirements when `requirements.txt` changed, and restarts the
OpenRC/systemd service detached. `--check` reports without touching anything.
The host checkout is deploy-only: local changes are discarded on purpose.
VM installs keep the source git checkout under `BRIDGE_HOME` so `/update`
works; unprivileged services exit and let supervise-daemon or systemd respawn
them.

### Update channels

`SELF_UPDATE_CHANNEL` picks what the updater tracks (falling back to
`SELF_UPDATE_BRANCH`, then `main`):

| Channel | Tracks |
| --- | --- |
| `main` or any branch | `origin/<branch>` head |
| `stable` | newest `vX.Y.Z` tag reachable from `origin/main` |
| `v1` | newest tag within major 1 |
| `v1.2` | newest tag within minor 1.2 |
| `v1.2.3` | that exact tag (pin) |

Tag mode fetches `v*` tags and checks out the tag detached; branch mode keeps
the old `git checkout -B` behavior. See [versioning.md](versioning.md) for the
bump rules and release flow.

Admins can run it from Telegram with `/update` or preview with
`/update check` (`TELEGRAM_ADMIN_USER_IDS`, or `TELEGRAM_ALLOWED_USERS` when
unset). `/update` acknowledges immediately with "Checking for updates…", and
after the restart the bridge posts "Bridge updated … and back online" (plus a
short changelog) to the chat that triggered it; cron updates post to the home
chat set with `/sethome`, if any.

On Alpine, install the bundled cron entry to poll every 15 minutes:

```sh
cp deploy/openrc/telegram-devin-bridge-update /etc/periodic/15min/
chmod +x /etc/periodic/15min/telegram-devin-bridge-update
```

## VM service management

From a checked-out repository, install or upgrade the bridge with:

```bash
sh deploy/vm/install.sh
```

The installer detects `apk`, `apt-get`, `dnf`, `yum`, or `pacman`, creates the
`telegram-devin` service account, installs the bridge under
`/opt/telegram-devin-bridge`, and configures polling with the database at
`/var/lib/telegram-devin-bridge/bridge.sqlite3`. It is safe to rerun: pull the
new revision and run the same command to upgrade in place. Use
`BRIDGE_HOME=/some/path` to choose another installation directory. Inspect the
detected package manager, init system, and Python version without making
changes:

```bash
sh deploy/vm/install.sh --dry-run
```

When run from a git checkout, the installer preserves `.git` under
`BRIDGE_HOME` so `/update` is available; a non-git source prints a note and
does not support `/update`. The OpenRC service uses `supervise-daemon`, and
unprivileged updates restart by exiting so OpenRC or systemd can respawn it.

On Alpine/OpenRC:

```bash
rc-service telegram-devin-bridge status
rc-service telegram-devin-bridge restart
tail -f /var/log/telegram-devin-bridge.log
tail -f /var/log/telegram-devin-bridge.err
```

On systemd Linux:

```bash
systemctl status telegram-devin-bridge
systemctl restart telegram-devin-bridge
journalctl -u telegram-devin-bridge -f
```

The installer creates `.env` from `.env.example` only when it does not exist,
and preserves it during upgrades. Fill in `TELEGRAM_BOT_TOKEN`,
`DEVIN_API_KEY`, and, when used, `DEVIN_SERVICE_USER_API_KEY`, `DEVIN_ORG_ID`,
`TELEGRAM_ADMIN_USER_IDS`, `TELEGRAM_ALLOWED_USERS`,
`TELEGRAM_ALLOWED_CHAT_IDS`, and `NOTIFY_SECRET`. `TELEGRAM_MODE` and
`DATABASE_PATH` are set automatically for VM polling.

To migrate the SQLite database from Fly, download it and install it at the VM
data path before starting the service:

```bash
flyctl ssh sftp get /data/bridge.sqlite3
sudo install -o telegram-devin -g telegram-devin -m 0640 \
  bridge.sqlite3 /var/lib/telegram-devin-bridge/bridge.sqlite3
flyctl scale count 0
```

## Devin Knowledge

`docs/devin-knowledge.md` is published to the Devin Knowledge API with:

```bash
python -m app.publish_knowledge            # create or update by name
python -m app.publish_knowledge --dry-run  # print the payload only
```
