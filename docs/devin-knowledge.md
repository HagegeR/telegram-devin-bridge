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
  prompt (photos, documents, voice/audio/video up to 20 MB).
- Voice notes: when the deployment has a transcription backend (whisper API,
  faster-whisper, whisper.cpp, a bounded docker sidecar such as the Moonshine
  image in `deploy/moonshine/`, or an external command) the message arrives
  as text prefixed `Voice note transcript:` — treat it as a normal message,
  no need to transcribe yourself. If it arrives as a bare `voice.ogg`
  attachment instead, transcription is off or failed on the host.
- Attachments you send: images are delivered as photos when they fit
  Telegram's photo limits unchanged (≤1280 px longest side), otherwise as
  full-resolution documents. The attachment is sent on its own (photos get the
  filename as caption) and your message text arrives as a separate message.
- Size limits, so a reply is never rejected by Telegram: keep a message under
  ~3500 chars, tables under ~20 rows, and put tables/charts in their own
  message rather than alongside prose. Anything longer (full drill-downs,
  multi-month tables, high-res charts) goes as a file attachment with a 2–3
  line summary in the text. A rejected reply makes the bridge re-send the
  original prompt, so you will see the same question again — do not redo the
  work, shorten the answer.
- Long-running work (sweeps, backfills, sims over ~30 min): post the plan and
  an ETA first, then one result message; no per-step progress messages.
- Before saying "done" on a PR: re-fetch it and confirm mergeable, zero
  unresolved review threads, and CI green on the HEAD you tested.
- Text the bridge prepends (`DEVIN_SESSION_INSTRUCTIONS`) is deployment
  policy from the user — follow it.

## Bridge commands the user has (so you can point to them)

`/new [title]`, `/topic <name>`, `/close`, `/rename <name>`, `/sessions`,
`/resume <n>`, `/status`, `/stop`, `/steer <text>`, `/playbook`, `/retry`,
`/settings`, `/usage`, `/lang [code|auto|off]` (per-user voice-note
language), `/whoami`, `/sethome`, `/users`, `/revoke <id>`, `/update`,
`/help`. The user can switch sessions; each Telegram chat/topic has one
active Devin session.

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
  `GET /doctor` with `Authorization: Bearer <DOCTOR_SECRET>`, a secret
  separate from `NOTIFY_SECRET`.
- Runbook for re-deploying from scratch: `docs/deployment-alpine-tailscale.md`
  in the repo (DNS cache with dnsmasq, udhcpc `NO_GATEWAY`, Funnel prerequisites
  in the Tailscale admin console, OpenRC unit, `.env` pitfalls).
- No-tunnel alternative: `TELEGRAM_MODE=polling` needs no public URL.
- Voice transcription on the host: `TRANSCRIPTION_BACKEND=whispercpp` with
  whisper.cpp built from source in `/opt/whisper.cpp` (binary
  `build/bin/whisper-cli`, model `models/ggml-base.en.bin`, ~1.4 GB RAM box,
  musl — faster-whisper/ctranslate2 wheels do not install there).
- Self-update: the host follows the update channel configured in
  `/etc/conf.d/telegram-devin-bridge` (`SELF_UPDATE_CHANNEL`, `main` today) —
  a cron job pulls every 15 min, or the admin sends `/update` in Telegram
  (`/update <channel>` for a one-shot override). Merging to `main` is how
  code reaches the host — never edit files on the host by hand.
- After any bridge PR merges, assume the host still runs the OLD build until
  the user has run `/update` (or 15 min passed) — end the merge message with
  that reminder, and treat "it still does X" reports right after a merge as
  "not redeployed yet" first.

## Fixing the deployment from a cloud session

Two remote-control paths exist. Use the narrowest one that does the job, and
tell the user what you did. Both need secrets provided to the session — never
ask the user to paste them into chat, and never print them.

### 1. Bridge admin API (`POST /admin`) — narrow, no shell

`POST $BRIDGE_PUBLIC_BASE_URL/admin` with
`Authorization: Bearer $BRIDGE_ADMIN_SECRET` and a JSON body:

| body | effect |
| --- | --- |
| `{"action":"doctor"}` | run the diagnostics, return the check list |
| `{"action":"logs","lines":200}` | tail of the service log (secrets redacted; max 500) |
| `{"action":"get-env"}` | values of the allowlisted non-secret `.env` keys |
| `{"action":"set-env","key":"DEVIN_MAX_ACU_LIMIT","value":"5"}` | rewrite one allowlisted key in `.env` (takes effect after `restart`) |
| `{"action":"restart"}` | restart the service (detached; reply arrives before the restart) |
| `{"action":"update"}` | run the self-updater (pull `main`, pip if needed, restart) |

`set-env` refuses keys outside `ADMIN_ENV_ALLOWLIST` (tokens, API keys and
secrets are never settable this way). Every admin call is logged and a
notification is posted to the Telegram home chat, so the user sees it.
Typical fix flow: `doctor` -> read failing check + `logs` -> `set-env` ->
`restart` -> `doctor` again.

### 2. Tailscale SSH — full shell on the VM

The VM (`devin-bridge`, Tailscale IP `100.127.21.58`) runs Tailscale SSH;
the tailnet ACL allows `tag:devin` nodes to SSH in as `root`. Join the tailnet
from the session with the ephemeral, pre-authorized auth key provided as the
secret `TAILSCALE_AUTHKEY` (the node is deleted automatically when it goes
offline):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscaled --state=mem: >/tmp/tailscaled.log 2>&1 &
sudo tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname=devin-session --ssh=false
ssh -o StrictHostKeyChecking=accept-new root@100.127.21.58 'rc-service telegram-devin-bridge status'
```

Without root in the sandbox use userspace networking instead:

```bash
tailscaled --tun=userspace-networking --socks5-server=localhost:1055 --state=mem: >/tmp/tailscaled.log 2>&1 &
tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname=devin-session
ssh -o ProxyCommand='nc -x localhost:1055 %h %p' -o StrictHostKeyChecking=accept-new root@100.127.21.58
```

Tailscale SSH authenticates by tailnet identity — no SSH keys or passwords.
On the VM, keep to: `rc-service telegram-devin-bridge …`, editing
`/root/telegram-devin-bridge/.env` (never print it), `sh deploy/self-update.sh`,
`.venv/bin/python -m app.doctor`, `tail /var/log/telegram-devin-bridge.log`,
`tailscale funnel status`. Do not change routing, `/etc/udhcpc/udhcpc.conf`,
Docker, or other services on the VM — another deployment (`ibkr-gateway`)
shares it. Code changes go through a PR to `main`, not edits on the host.

## Devin API facts the bridge relies on (v1 key)

`POST /v1/sessions`, `POST /v1/sessions/{id}/message`, `GET /v1/sessions/{id}`
(`status_enum`: `working`, `blocked` = waiting for user, `finished`,
`expired`, ...), `GET /v1/playbooks`, `POST /v1/attachments`,
`DELETE /v1/sessions/{id}`. Only messages with `type == "devin_message"` are
forwarded to Telegram; `user_question` options are not exposed by the API,
which is why the `OPTIONS:` line convention exists.
