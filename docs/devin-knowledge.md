---
name: Telegram–Devin Bridge (how to talk to the user and the bridge)
trigger_description: Use when a session was started from Telegram via the telegram-devin-bridge (prompt mentions Telegram, the bridge, OPTIONS buttons, or a "Started session" from a Telegram chat), or when asked to notify a Telegram chat, or when the task touches the HagegeR/telegram-devin-bridge repo or the devin-bridge host.
---

# Telegram–Devin Bridge

The user is talking to you from Telegram through `telegram-devin-bridge`
(https://github.com/HagegeR/telegram-devin-bridge). A FastAPI process on the
user's home server receives Telegram webhooks, creates/continues a Devin v1
session per chat or forum topic, and streams every `devin_message` you write
back to Telegram. The user only ever sees your messages — never the Devin UI.

## Writing replies the bridge renders well

- Keep replies short; Telegram chunks at 4096 chars and long replies get a
  "show more" callback. Prefer one clear message over several.
- Standard Markdown is fine (bold, italics, inline code, fenced code, links,
  bullets, headings, pipe tables). The bridge converts to Telegram rich
  Markdown/MarkdownV2 and preserves fenced code and tables.
- To offer choices, end the message with exactly one line:
  `OPTIONS: first choice | second choice | third choice`
  (max 8 options, each under 60 chars). The bridge strips that line and
  renders inline buttons; the pressed label is sent back to this session as a
  normal user message.
- Do not ask the user to open a UI, click in Devin, or check a dashboard.
  Report results, PR URLs, and questions inline.
- Attachments from the user arrive as `Attached file: <url>` lines in the
  prompt (photos, documents, voice/audio/video up to 20 MB). Voice may be
  transcribed if the deployment enabled it.
- Text the bridge prepends (`DEVIN_SESSION_INSTRUCTIONS`) is deployment
  policy from the user — follow it.

## Bridge commands the user has (so you can point to them)

`/new [title]`, `/topic <name>`, `/sessions`, `/resume <n>`, `/status`,
`/stop`, `/playbook`, `/retry`, `/whoami`, `/sethome`, `/help`. The user can
switch sessions; each Telegram chat/topic has one active Devin session.

## Sending a notification to Telegram from a running task

The bridge exposes `POST /notify` when `NOTIFY_SECRET` is set. It delivers to
the `/sethome` target (or `TELEGRAM_HOME_CHANNEL`) unless `chat_id` /
`thread_id` are given:

```bash
curl -sS -X POST "$BRIDGE_PUBLIC_BASE_URL/notify" \
  -H "Authorization: Bearer $BRIDGE_NOTIFY_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Deployment finished","silent":true,"markdown":true}'
```

Only do this if `BRIDGE_PUBLIC_BASE_URL` and `BRIDGE_NOTIFY_SECRET` were
provided as secrets for the session; never guess or ask the user to paste the
secret into chat. Normal replies do not need `/notify` — anything you write in
the session is already forwarded.

## The deployment (reference deployment on the user's home server)

- Host: Alpine Linux VM, OpenRC. Service `telegram-devin-bridge`
  (`rc-service telegram-devin-bridge start|stop|restart|status`), code in
  `/root/telegram-devin-bridge` with a venv at `.venv`, config in `.env`
  (chmod 600, never print it), log `/var/log/telegram-devin-bridge.log`.
- Public HTTPS via Tailscale Funnel: `https://devin-bridge.<tailnet>.ts.net`
  -> `127.0.0.1:8000` (`tailscale funnel status`). Webhook path is
  `/telegram/webhook`; `GET /health` returns `{"status":"ok"}`.
- Diagnostics: `cd /root/telegram-devin-bridge && .venv/bin/python -m app.doctor`
  checks env, DNS, Telegram/Devin API reachability, local and public health,
  webhook registration, funnel, and known host pitfalls (MTU, extra default
  routes, MagicDNS overriding resolv.conf). Same report via
  `GET /doctor` with `Authorization: Bearer <NOTIFY_SECRET>`.
- Runbook for re-deploying from scratch: `docs/deployment-alpine-tailscale.md`
  in the repo (DNS cache with dnsmasq, udhcpc `NO_GATEWAY`, Funnel prerequisites
  in the Tailscale admin console, OpenRC unit, `.env` pitfalls).
- No-tunnel alternative: `TELEGRAM_MODE=polling` needs no public URL.

## Devin API facts the bridge relies on (v1 key)

`POST /v1/sessions`, `POST /v1/sessions/{id}/message`, `GET /v1/sessions/{id}`
(`status_enum`: `working`, `blocked` = waiting for user, `finished`,
`expired`, ...), `GET /v1/playbooks`, `POST /v1/attachments`,
`DELETE /v1/sessions/{id}`. Only messages with `type == "devin_message"` are
forwarded to Telegram; `user_question` options are not exposed by the API,
which is why the `OPTIONS:` line convention exists.
