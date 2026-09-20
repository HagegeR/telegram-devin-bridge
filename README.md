# Telegram–Devin Bridge

A FastAPI bridge that forwards Telegram conversations to Devin v1 sessions.
Each direct chat or forum topic has an active Devin session, with SQLite
history for switching between sessions.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.set_webhook
# Or run long polling without FastAPI:
python -m app.poll
```

The deployment must expose HTTPS and route the configured public URL to port
8000. `GET /health` returns `{"status":"ok"}` without configuration values.

## Configuration

| Variable | Required | Default |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | — |
| `TELEGRAM_WEBHOOK_SECRET` | webhook only | — |
| `DEVIN_API_KEY` | yes | — |
| `DEVIN_SERVICE_USER_API_KEY` | no | unset |
| `DEVIN_ORG_ID` | no | unset |
| `TELEGRAM_MODE` | no | `webhook` (`webhook` or `polling`) |
| `PUBLIC_BASE_URL` | webhook only | — |
| `DATABASE_PATH` | no | `./bridge.sqlite3` |
| `DEVIN_API_BASE_URL` | no | `https://api.devin.ai` |
| `DEVIN_MAX_ACU_LIMIT` | no | `3` |
| `DEVIN_POLL_FAST_SECONDS` | no | `1.0` |
| `DEVIN_POLL_SECONDS` | no | `5` |
| `DEVIN_WATCH_TIMEOUT_SECONDS` | no | `1800` |
| `DEVIN_SETTLE_SECONDS` | no | `30` |
| `DEVIN_STATUS_AFTER_SECONDS` | no | `8` |
| `DEVIN_SESSION_INSTRUCTIONS` | no | empty |
| `TELEGRAM_DEBOUNCE_SECONDS` | no | `1.5` |
| `TELEGRAM_QUEUE_WHILE_BUSY` | no | `true` |
| `TELEGRAM_LONG_REPLY_CHARS` | no | `3500` |
| `TELEGRAM_RATE_LIMIT_PER_MINUTE` | no | `20` (`0` disables) |
| `TELEGRAM_RICH_MESSAGES` | no | `true` |
| `TELEGRAM_DRAFTS` | no | `false` |
| `TELEGRAM_ALLOWED_USERS` | no | empty |
| `TELEGRAM_ALLOWED_CHAT_IDS` | no | empty |
| `TELEGRAM_ALLOW_ALL_USERS` | no | `false` |
| `TELEGRAM_FREE_RESPONSE_CHATS` | no | empty |
| `TELEGRAM_HOME_CHANNEL` | no | unset |
| `TELEGRAM_NOTIFICATION_MODE` | no | `important` |
| `TELEGRAM_ADMIN_USER_IDS` | no | empty |
| `TRANSCRIPTION_API_KEY` | no | unset |
| `TRANSCRIPTION_BASE_URL` | no | `https://api.openai.com/v1` |
| `TRANSCRIPTION_MODEL` | no | `whisper-1` |
| `TELEGRAM_ATTACH_VOICE` | no | `false` |
| `GITHUB_TOKEN` | no | unset |
| `SELF_UPDATE_COMMAND` | no | `sh deploy/self-update.sh` |
| `NOTIFY_SECRET` | no | unset (`/notify` disabled) |
| `DOCTOR_SECRET` | no | unset (`/doctor` disabled) |
| `ADMIN_SECRET` | no | unset (`/admin` disabled) |
| `ADMIN_ENV_ALLOWLIST` | no | built-in non-secret key list |
| `ADMIN_LOG_PATH` | no | `/var/log/telegram-devin-bridge.log` |
| `ADMIN_RESTART_COMMAND` | no | OpenRC `rc-service` restart |
| `BOT_USERNAME` | no | fetched from Telegram at startup |

With allow-all disabled, an empty user/chat allowlist denies access and sends
the requester their user ID once for onboarding. Group and supergroup messages
must mention `@BOT_USERNAME`, reply to a bot message, or come from a
`TELEGRAM_FREE_RESPONSE_CHATS` chat.

## Commands

`/start`, `/help`, `/new [title]`, `/topic <name>`, `/close`, `/rename <name>`,
`/sessions`, `/resume <n>`, `/status`, `/stop` (`/cancel`), `/playbook [n] [text]`, `/retry`,
`/whoami`, `/sethome`, `/settings`, `/usage`, `/users`, and `/revoke <id>` are
available. `/new` creates a fresh active session without deleting history.
`/topic <name>` creates a Telegram topic with its own Devin session and posts
an instructional seed message into it. Private-chat topics must first be
enabled from the chat's bot settings; groups must have Topics enabled.
`/close` and `/rename` manage the current forum topic. React 🔁 to retry the
last user message or 🛑 to stop the active session. `/stop` asks for inline
confirmation. `/playbook` lists available Devin
playbooks or starts one. `/retry` resends the last user message.
`/settings` controls notification, draft, status timer, and default playbook
behavior for the current chat or topic. `/usage` reports Devin ACUs when an
organization ID is configured. Administrators can approve private-chat access
requests and manage approved users with `/users` and `/revoke`.

## Notifications

When `NOTIFY_SECRET` is set, send a notification with:

```bash
curl -X POST http://localhost:8000/notify \
  -H 'Authorization: Bearer replace-with-notify-secret' \
  -H 'Content-Type: application/json' \
  -d '{"text":"Deployment finished","silent":true}'
```

The target is `chat_id`/`thread_id` in the request, the `/sethome` target, or
`TELEGRAM_HOME_CHANNEL`. Notifications can set `markdown` to `false`.

`GET /doctor` requires `DOCTOR_SECRET` (Bearer auth, separate from
`NOTIFY_SECRET`, 30 s cooldown between runs) and runs the deployment
diagnostics, returning `{"results": [...], "ok": bool}`.

## Diagnostics

```bash
python -m app.doctor                # human-readable check list
python -m app.doctor --json         # machine-readable
python -m app.doctor --attempts 10  # more DNS samples
```

Connection failures to the Telegram and Devin APIs (DNS blips, dropped
routes) are retried 5 times with exponential backoff (~15 s total);
mid-flight failures are retried only for idempotent GETs so mutations are
never duplicated. Longer outages still drop the reply and are logged as
`Failed to process Telegram update`.

The doctor verifies `.env` completeness, DNS reliability, `/etc/resolv.conf`
hijacking, default routes and MTU, Tailscale funnel state, the Telegram and
Devin APIs, the webhook registration, and local/public `/health`. Exit code is
1 when any check fails.

## Deployment

See [docs/deployment-alpine-tailscale.md](docs/deployment-alpine-tailscale.md)
for the Alpine + Tailscale Funnel runbook (OpenRC unit in `deploy/openrc`,
dnsmasq cache config in `deploy/dnsmasq`), including the network pitfalls hit
in production (MagicDNS resolv.conf takeover, dead second NIC, jumbo MTU).

Two deployment styles exist — pick one:

- `deploy/openrc/telegram-devin-bridge`: **webhook + Tailscale Funnel** unit —
  uvicorn as root from `/root/telegram-devin-bridge`, supervise-daemon respawn.
- `deploy/vm/install.sh`: **polling-mode** installer — `TELEGRAM_MODE=polling`,
  service account under `/opt`, `python -m app.poll`; needs no public URL.

## Admin API

`POST /admin` with `Authorization: Bearer $ADMIN_SECRET` — a narrow,
no-shell remote control for cloud sessions:

| body | effect |
| --- | --- |
| `{"action":"doctor"}` | run the diagnostics, return the check list |
| `{"action":"logs","lines":200}` | tail of the service log (secrets redacted; max 500) |
| `{"action":"get-env"}` | values of the allowlisted non-secret `.env` keys |
| `{"action":"set-env","key":"DEVIN_MAX_ACU_LIMIT","value":"5"}` | rewrite one allowlisted `.env` key (applies after `restart`) |
| `{"action":"restart"}` | restart the service (detached) |
| `{"action":"update"}` | run the self-updater |

```bash
curl -X POST https://<host>.<tailnet>.ts.net/admin   -H "Authorization: Bearer $ADMIN_SECRET"   -H 'Content-Type: application/json'   -d '{"action":"doctor"}'
```

Security: separate `ADMIN_SECRET`, `ADMIN_ENV_ALLOWLIST` whitelists only
non-secret keys (tokens/keys are never readable or writable), every call is
audit-logged and posts a Telegram notification, 10 req/min rate limit, and
log output is secret-redacted. Failed auth attempts are tarpitted one at a
time (1 s delay); while one is in flight every request gets 429 (≈60
guesses/min cap), so a flood of bad tokens can temporarily block valid ones —
use Tailscale SSH as fallback. Command and path keys can never be set via the
API.

## Self-update

`deploy/self-update.sh` takes a host-wide lock and records a deploy marker, fetches `origin/<branch>`
(`SELF_UPDATE_BRANCH`, default `main`), checks out the remote head, reinstalls requirements when
`requirements.txt` changed, and restarts the OpenRC service detached.
`--check` reports without touching anything. The host checkout is
deploy-only: `git checkout -B` discards local changes on purpose. Admins can
run it from Telegram with `/update` or preview with `/update check`
(requires `TELEGRAM_ADMIN_USER_IDS`).

On Alpine, install the bundled cron entry to poll every 15 minutes:

```sh
cp deploy/openrc/telegram-devin-bridge-update /etc/periodic/15min/
chmod +x /etc/periodic/15min/telegram-devin-bridge-update
```

## Devin Knowledge

`docs/devin-knowledge.md` is published to the Devin Knowledge API with:

```bash
python -m app.publish_knowledge            # create or update by name
python -m app.publish_knowledge --dry-run  # print the payload only
```

## Media, topics, and formatting

Photos, documents, voice messages, audio, video, and video notes up to
Telegram's 20 MB download limit are uploaded to Devin and referenced in the
user prompt. Telegram hidden `text_link` entities are expanded to
`visible text (URL)` before the prompt is sent, including links following
emoji. Devin replies use Telegram 10.3 rich Markdown messages when enabled, with
MarkdownV2 formatting and 4096-character chunking as a fallback. Rich delivery
normalizes hard line breaks while preserving fenced code and pipe tables. A
final `OPTIONS: one | two` line becomes inline buttons (up to eight options,
each at most 60 characters); selected choices are marked with a disabled
button. Stop confirmations use the Bot API 10.3 danger and primary button
styles. Unknown rich-message methods disable rich delivery for the process and
continue with MarkdownV2.

When `TELEGRAM_DRAFTS=true`, private chats receive a Telegram draft while Devin
is working. Groups continue to receive typing actions. If drafts are rejected,
the watcher falls back to typing for that session.

Bot API 10.3 topic creation and implicit-topic renaming are supported. The
first message in an implicitly named topic renames it from its first line.
`/help`, `/sessions`, `/status`, and `/whoami` use ephemeral group replies
when Telegram accepts them, and retry as normal messages if it does not.

`DEVIN_SESSION_INSTRUCTIONS` is prepended verbatim to the prompt of every
session the bridge starts — use it for org-specific guidance such as which
injected secret grants Devin v3 API access for editing automations.

`DEVIN_SETTLE_SECONDS` keeps a newly started watcher alive while Devin's API
still reports a stale non-active status after the message is submitted.

`TRANSCRIPTION_API_KEY` enables transcription for voice, audio, and video-note
messages. `TELEGRAM_ATTACH_VOICE=true` keeps the original audio attached.
Artifact images and documents from Devin are forwarded to Telegram, and GitHub
pull requests are rendered as compact cards when metadata is available.

Rapid text and file messages are debounced per chat/topic and joined into one
Devin turn. When a session is working, later turns are queued and drained in
order after it finishes; `/status` shows the queued count. Replies extract
large fenced code blocks as `snippet-*` documents and paginate long text with
a `Show more` button. Very large replies are sent as `reply.md` with a preview.
Reply and forward context is included in prompts. The per-user sliding-window
rate limit sends at most one warning per minute; set it to zero to disable it.

Forum and private-chat topics use independent sessions whenever Telegram
provides `message_thread_id` with either `chat.is_forum` or
`is_topic_message`; ordinary DMs and groups use the chat ID.
`message_thread_id` is preserved for replies and notifications.

Polling mode calls `deleteWebhook` without dropping pending updates, then uses
50-second Telegram long polling. The VM installer in `deploy/vm` runs
`python -m app.poll` on Alpine (OpenRC) or any systemd Linux. Fly stores the
SQLite database at `/data/bridge.sqlite3`; migrate it with:

```bash
flyctl ssh sftp get /data/bridge.sqlite3
```

## VM deployment

From a checked-out repository, install or upgrade the bridge with:

```bash
sh deploy/vm/install.sh
```

The installer detects `apk`, `apt-get`, `dnf`, `yum`, or `pacman`, creates the
`telegram-devin` service account, installs the bridge under
`/opt/telegram-devin-bridge`, and configures polling with the database at
`/var/lib/telegram-devin-bridge/bridge.sqlite3`. It is safe to rerun: pull
the new revision and run the same command to upgrade in place. Use
`BRIDGE_HOME=/some/path` to choose another installation directory. Inspect
the detected package manager, init system, and Python version without making
changes:

```bash
sh deploy/vm/install.sh --dry-run
```

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
`TELEGRAM_ALLOWED_CHAT_IDS`, and `NOTIFY_SECRET`. Add any optional
transcription or GitHub values needed by the deployment. `TELEGRAM_MODE` and
`DATABASE_PATH` are set automatically for VM polling and should not contain
real values in documentation.

To migrate the SQLite database from Fly, download it and install it at the VM
data path before starting the service:

```bash
flyctl ssh sftp get /data/bridge.sqlite3
sudo install -o telegram-devin -g telegram-devin -m 0640 \
  bridge.sqlite3 /var/lib/telegram-devin-bridge/bridge.sqlite3
flyctl scale count 0
```

## Safety

- Never commit `.env`, tokens, API keys, or real user/chat identifiers.
- Keep the webhook behind HTTPS and use a random webhook secret.
- Use a dedicated Devin API key with the smallest available scope.

## `OPTIONS:` convention

Ask Devin to end a response with exactly one line such as:

```text
OPTIONS: Run it | Explain it | Cancel
```

The bridge removes that line from the message and renders one button per
choice. The selected text is sent back to the same Devin session.
