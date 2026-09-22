# Getting started in 5 minutes

The fastest path runs the bridge in **polling mode**: no public URL, no TLS,
no tunnel — any machine that can reach Telegram and Devin works.

## 1. Create the bot

Talk to [@BotFather](https://t.me/BotFather), `/newbot`, and copy the token.
While you're there, `/setprivacy` → **Disable** if you want the bot to read
messages in groups it gets added to (not needed for DMs).

## 2. Get a Devin API key

Create a **v1** API key in Devin (`apk_user_…` or `apk_…`; a service-user
`cog_…` token is NOT accepted). See [configuration.md](configuration.md#devin).

## 3. Configure

```bash
cp .env.example .env
```

Minimal `.env` for polling — only the two secrets plus the mode:

```dotenv
TELEGRAM_BOT_TOKEN=<token from BotFather>
DEVIN_API_KEY=<your Devin v1 key>
TELEGRAM_MODE=polling
```

Restrict the bot to yourself from the start — find your user id by sending
`/whoami` once (the bot replies with it even before you're allowlisted), or
any ID-lookup bot:

```dotenv
TELEGRAM_ALLOWED_USERS=<your numeric user id>
TELEGRAM_ADMIN_USER_IDS=<your numeric user id>
```

## 4. Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.poll
```

Or with Docker:

```bash
docker compose up -d
# upgrades later: docker compose up -d --build (image rebuild, not /update)
```

## 5. Talk to it

Open a DM with your bot and send `/start`. Then send any task, e.g.
"fix the typo in README.md of myrepo" — the bridge creates a Devin session
and streams its replies back as Telegram messages, with `OPTIONS:` buttons
when Devin offers choices.

Useful next steps:

- `/settings` — tune notifications and reply style per conversation
- `/sessions` — list and `/resume` older sessions
- `python -m app.doctor` — full deployment health check
- [operations.md](operations.md) — `/notify` webhook, `/doctor` endpoint,
  `/admin` remote control
- [deployment-alpine-tailscale.md](deployment-alpine-tailscale.md) — when you
  outgrow polling: webhook behind Tailscale Funnel on a dedicated host
