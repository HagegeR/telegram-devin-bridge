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
