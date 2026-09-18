# Telegram–Devin Bridge

A small FastAPI service that forwards private Telegram chats to Devin sessions.

## Architecture

1. Telegram sends updates to `POST /telegram/webhook`.
2. The bridge creates one Devin session per Telegram chat, then forwards later messages to it.
3. The bridge polls the Devin session for a new assistant message and sends it back to Telegram.

The bridge stores only the Telegram chat ID, Devin session ID, and the last delivered Devin message ID in SQLite. Credentials are read from environment variables and are never written to the database.

## Requirements

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Devin API key
- A public HTTPS URL for the deployed service

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `DEVIN_API_KEY`, and `PUBLIC_BASE_URL` in `.env`, then run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Register the webhook:

```bash
python -m app.set_webhook
```

The deployment must expose HTTPS and route the configured public URL to port 8000.

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
| `DEVIN_POLL_SECONDS` | no | `2` |
| `DEVIN_REPLY_TIMEOUT_SECONDS` | no | `180` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | no | empty (allow all chats) |

For production, set `TELEGRAM_ALLOWED_CHAT_IDS` to a comma-separated list of Telegram chat IDs so the bot cannot be used by unintended users.

## Safety notes

- Do not commit `.env`.
- Use a dedicated Devin API key with the smallest available scope.
- Keep the Telegram webhook endpoint behind HTTPS.
- Use a random webhook secret so arbitrary callers cannot enqueue Devin work.
- Set `TELEGRAM_ALLOWED_CHAT_IDS` before sharing the bot.

## Health check

`GET /health` returns `{"status":"ok"}` without exposing configuration or credentials.
