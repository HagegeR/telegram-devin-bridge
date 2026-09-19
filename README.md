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
```

The deployment must expose HTTPS and route the configured public URL to port
8000. `GET /health` returns `{"status":"ok"}` without configuration values.

## Configuration

| Variable | Required | Default |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | — |
| `TELEGRAM_WEBHOOK_SECRET` | yes | — |
| `DEVIN_API_KEY` | yes | — |
| `PUBLIC_BASE_URL` | yes | — |
| `DATABASE_PATH` | no | `./bridge.sqlite3` |
| `DEVIN_API_BASE_URL` | no | `https://api.devin.ai` |
| `DEVIN_MAX_ACU_LIMIT` | no | `3` |
| `DEVIN_POLL_SECONDS` | no | `3` |
| `DEVIN_WATCH_TIMEOUT_SECONDS` | no | `1800` |
| `DEVIN_SETTLE_SECONDS` | no | `30` |
| `TELEGRAM_ALLOWED_USERS` | no | empty |
| `TELEGRAM_ALLOWED_CHAT_IDS` | no | empty |
| `TELEGRAM_ALLOW_ALL_USERS` | no | `false` |
| `TELEGRAM_FREE_RESPONSE_CHATS` | no | empty |
| `TELEGRAM_HOME_CHANNEL` | no | unset |
| `TELEGRAM_NOTIFICATION_MODE` | no | `important` |
| `NOTIFY_SECRET` | no | unset (`/notify` disabled) |
| `BOT_USERNAME` | no | fetched from Telegram at startup |

With allow-all disabled, an empty user/chat allowlist denies access and sends
the requester their user ID once for onboarding. Group and supergroup messages
must mention `@BOT_USERNAME`, reply to a bot message, or come from a
`TELEGRAM_FREE_RESPONSE_CHATS` chat.

## Commands

`/start`, `/help`, `/new [title]`, `/topic <name>`, `/sessions`, `/resume <n>`, `/status`,
`/stop`, `/playbook [n] [text]`, `/retry`, `/whoami`, and `/sethome` are
available. `/new` creates a fresh active session without deleting history.
`/topic <name>` creates a Telegram topic with its own Devin session and posts
an instructional seed message into it. Private-chat topics must first be
enabled from the chat's bot settings; groups must have Topics enabled.
`/stop` asks for inline confirmation. `/playbook` lists available Devin
playbooks or starts one. `/retry` resends the last user message.

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

## Media, topics, and formatting

Photos, documents, voice messages, audio, video, and video notes up to
Telegram's 20 MB download limit are uploaded to Devin and referenced in the
user prompt. Telegram hidden `text_link` entities are expanded to
`visible text (URL)` before the prompt is sent, including links following
emoji. Devin replies are formatted as
Telegram MarkdownV2, split at 4096 characters, and preserve fenced code
blocks. A final `OPTIONS: one | two` line becomes inline buttons (up to eight
options, each at most 60 characters).

`DEVIN_SETTLE_SECONDS` keeps a newly started watcher alive while Devin's API
still reports a stale non-active status after the message is submitted.

Forum and private-chat topics use independent sessions whenever Telegram
provides `message_thread_id` with either `chat.is_forum` or
`is_topic_message`; ordinary DMs and groups use the chat ID.
`message_thread_id` is preserved for replies and notifications.

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
choice. The selected text is sent back to the same Devin session. Clicking an
old button still works: the label is forwarded as a normal message (to the
current session, or a new one if it finished). Only buttons clicked from a
different chat, or command buttons like Terminate/Cancel, expire.
