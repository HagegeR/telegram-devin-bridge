# Security Policy

## Reporting a vulnerability

This bridge holds real credentials (a Telegram bot token and a Devin API key)
and can start paid AI sessions, so please report issues privately rather than
in a public issue.

- **Preferred:** [GitHub private vulnerability reporting](../../security/advisories/new)
- **Otherwise:** open a minimal issue saying you have a security report, and a
  maintainer will arrange a private channel — do not include details.

Please include the affected endpoint/command path, reproduction steps, and
which secrets or chat content could be exposed or misused. Reports are
acknowledged within a few days.

## Scope

The bridge is a single-user/self-hosted service: the threat model centers on
**unauthorized Telegram users gaining access** and **secrets leaking through
logs, replies, or the admin surface**.

Particularly in scope:

- Bypasses of the user/chat allowlist or the group mention gate (`app/access.py`).
- Secret handling: `TELEGRAM_WEBHOOK_SECRET`, `NOTIFY_SECRET`, `DOCTOR_SECRET`,
  `ADMIN_SECRET` verification, rate limiting, or tarpit logic that could be
  bypassed.
- The `/admin` API: `set-env` writing a key outside `ADMIN_ENV_ALLOWLIST`,
  reading a secret value, or executing a command key.
- Transcription subprocess/docker escapes (`app/transcription.py`).
- Path or injection issues in `SELF_UPDATE_COMMAND` / `ADMIN_RESTART_COMMAND`.
- Markdown/entity handling that leaks content across chats or threads.

Out of scope: vulnerabilities in Telegram's or Devin's own APIs, issues
requiring the host already be compromised, and missing security "best
practices" without a concrete exploit path.

## Deployment hardening expectations

- `TELEGRAM_ALLOW_ALL_USERS` defaults to `false`; keep an allowlist.
- Generate every `*_SECRET` independently (`openssl rand -hex 32`).
- Serve the webhook over HTTPS only; keep `.env` at `chmod 600`.
- Use a dedicated Devin v1 API key with the smallest scope available.
- The self-updater intentionally runs `git checkout -B` — never commit local
  secrets on the host.

## Threat model

Threat model used to triage Devin security scans. Scan findings are ranked against it, so keep it current when the deployment or trust boundaries change.

The scope and hardening expectations above still apply. All attacker profiles listed here are in scope.

### Components
| Component | Code | Trust boundary |
|---|---|---|
| **Bridge Core** | `app/` (main.py webhook, access.py, commands.py, watcher.py, formatting.py, clients.py, store.py, crawlers.py, images.py) | Telegram users -> bridge -> Devin API |
| **Bridge Host & Admin** | `app/admin.py`, `notify.py`, `doctor.py`, `local.py` (ACP agent on host), `transcription.py`, `deploy/` (self-update.sh, installers), `Dockerfile`, `fly.toml` | Bearer-secret HTTP APIs and subprocesses on the bridge host |

### Overview
FastAPI single process (uvicorn `0.0.0.0:8000`, served via Tailscale Funnel on the `devin-bridge` Alpine VM; Fly config exists). Maps Telegram chats/topics to Devin sessions (cloud API) or a local `devin acp` subprocess. SQLite at `DATABASE_PATH`. Self-updates from `main` by cron (`deploy/self-update.sh`: `git reset --hard`), so merging to `main` is the deploy.

### Entry points

- `POST /telegram/webhook` - `X-Telegram-Bot-Api-Secret-Token` vs `TELEGRAM_WEBHOOK_SECRET` (main.py ~4068); or long polling.
- Telegram updates: messages, slash commands, `callback_data` (`cfg:*`, user-controllable), reactions, voice notes, attachments, URLs (crawler).
- `POST /notify` (Bearer `NOTIFY_SECRET`), `POST /admin` (Bearer `ADMIN_SECRET`, disabled when unset; `set-env` allowlist `ADMIN_ENV_ALLOWLIST`), `GET /doctor` (`DOCTOR_SECRET`), `GET /health`, `GET /r/{token}` redirect tokens.
- Devin session output streamed back (markers `OPTIONS:`, `DETAILS:` ... parsed from agent text).

### Assets
Telegram bot token, Devin API keys (v1 + service-user v3), `*_SECRET`s, ACU budget, Devin org secrets attachable per chat (`cfg:secrets`), chat content per conversation, the bridge host (local ACP agent = shell-equivalent).

### Attackers
Telegram users not on `TELEGRAM_ALLOWED_USERS`/`TELEGRAM_ALLOWED_CHAT_IDS`; allowed non-admin users escalating to admin-tier (local platform, bypass mode, secrets, `/sethome`); group members bypassing the mention gate; internet callers of the Funnel endpoints; content injected via Devin output, crawled URLs, or audio files.

### Controls
`access.py` allowlist + admin ids; `hmac.compare_digest` for secrets; tarpit; `_rate_limited` on paths reaching `devin.send_message`; credential scrubbing in logs/errors; ffmpeg `-protocol_whitelist file` + `-format_whitelist`; SSRF hardening in crawler; unprivileged uid 10001 in Docker.

### Deployment facts
- The bridge runs only behind Tailscale Funnel on the VM; `fly.toml` is not deployed (no public `fly.dev` host).
