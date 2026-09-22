# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) — see
[docs/versioning.md](docs/versioning.md) for the bump rules, release steps,
and deployment update channels.

## [Unreleased]


## [1.0.0] - 2026-09-22

First tagged release. Everything that was already running on `main` at this
point, grouped:

### Added

- **Release channels for self-update**: `SELF_UPDATE_CHANNEL` selects what a
  deployment tracks — a branch (`main`, classic behavior), `stable` (newest
  `vX.Y.Z` tag), `vX` (within a major), `vX.Y` (within a minor line), or an
  exact `vX.Y.Z` pin. Works for cron runs, `/update`, and the admin API.
  `SELF_UPDATE_BRANCH` remains as a fallback.

- FastAPI bridge mapping each Telegram DM or forum topic to its own Devin v1
  session, with SQLite-backed history (`/sessions`, `/resume`), a per-session
  watcher streaming `devin_message`s back to Telegram, and both webhook and
  long-polling transports.
- Full command set: `/new`, `/topic`, `/close`, `/rename`, `/status`,
  `/stop`, `/steer`, `/retry`, `/playbook`, `/settings`, `/usage`, `/lang`,
  `/whoami`, `/sethome`, `/users`, `/revoke`, `/update`, `/help`; 🔁/🛑
  message reactions.
- Rich Telegram delivery: MarkdownV2 fallback and Bot API 10.3 rich messages,
  `OPTIONS:` inline buttons, ephemeral group replies, drafts, typing
  indicators, long-reply pagination and `reply.md` export, PR cards, and
  image/document attachment forwarding in both directions (≤ 20 MB).
- Voice/audio/video-note transcription via five backends: OpenAI-compatible
  API, faster-whisper `local`, `whispercpp`, external `command`, and a bounded
  `docker` sidecar (Moonshine image in `deploy/moonshine/`).
- Access control: user/chat allowlists with labels, admin approval flow,
  group mention/reply gating, free-response chats, per-user rate limiting,
  turn debounce and busy-queueing.
- Operations surface: `GET /health`, `GET /doctor` (+ `python -m app.doctor`
  diagnostics), `POST /notify`, and a rate-limited `POST /admin` API
  (`doctor`, `logs`, `get-env`, `set-env`, `restart`, `update`) with
  secret-redacted output and audit notifications.
- Self-updating deployment: `deploy/self-update.sh` (lock + deploy marker +
  conditional pip reinstall + detached restart), `/update` and `/update
  check` from Telegram, and the 15-minute cron wrapper.
- Deploy recipes: Alpine + Tailscale Funnel webhook (`deploy/openrc/`,
  dnsmasq config, runbook in `docs/deployment-alpine-tailscale.md`), generic
  VM polling installer (`deploy/vm/`, OpenRC/systemd), Fly.io (`fly.toml`),
  and Alpine helper scripts (`deploy/alpine/`).

### Docs

- README reworked into a landing page (banner, badges, mermaid architecture,
  quick start); reference split into `docs/configuration.md`,
  `docs/operations.md`, `docs/versioning.md`; community health files
  (CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue/PR templates); MIT license.
