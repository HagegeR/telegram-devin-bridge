# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) — see
[docs/versioning.md](docs/versioning.md) for the bump rules, release steps,
and deployment update channels.

## [Unreleased]

### Added

- `POST /notify` accepts `html` (+ `html_name`) and delivers it as an
  `Open <name>` report button (`/r/<token>`), or as a document when
  `PUBLIC_BASE_URL` is unset.

### Changed

- `/status` now shows the v3 `status_detail` reason (idle timeout, paused
  on request, waiting on your reply/approval), live `ACUs` consumed, and
  every PR the session opened instead of only the first.

## [v1.2.0] - 2026-10-01

### Added

- **macOS and Windows installs**: `deploy/macos/install.sh` (per-user
  launchd agent) and `deploy/windows/install.ps1` (scheduled task running a
  restart loop), both polling mode with `/update` support.
- `deploy/vm/install.sh` supports openSUSE (`zypper`) and hosts whose
  `python3` is older than 3.12 (versioned package, else a uv-managed
  interpreter). CI installs it on Alpine, Debian, Ubuntu, Fedora, Rocky,
  Arch and openSUSE containers, and runs macOS and Windows jobs.
- **Devin API v3**: when `DEVIN_SERVICE_USER_API_KEY` + `DEVIN_ORG_ID` are
  configured, session create/message/read/playbooks/terminate/upload run
  through `/v3/organizations/{org}/...`; v1 remains the fallback for
  deployments without a service key and still serves attachment downloads.
- `/settings` gains a **Devin mode** submenu (org default plus every mode
  the API accepts, discovered live with a built-in fallback list) and a
  **Repos** row;
  `/repos [a/b,c/d]` shows, sets, or clears a per-chat repo list applied to
  the next new session.
- `/doctor` adds a `devin api v3` check when the service key and org are
  set.
- `python -m app.publish_knowledge` publishes to
  `/v3/.../knowledge/notes` (paginated lookup) when a service key is
  configured, else the v1 endpoint.

### Changed

- `deploy/self-update.sh` no longer requires `flock` (falls back to a
  `mkdir` lock) and finds Windows venvs (`.venv/Scripts`).
- `ADMIN_RESTART_COMMAND` now defaults to `rc-service` only when running as
  root with OpenRC; otherwise empty, which exits the process for the
  supervisor (systemd, launchd, the Windows loop, Docker) to respawn.
- Queued turns for the same conversation are now coalesced into a single
  Devin message per batch (turns carrying attachments still get their own
  batch), cutting the number of Devin turns spent flushing the queue.
- `/new [title]` no longer creates a Devin session just to greet — it
  stores a pending title and replies immediately; the next message you send
  starts the session with that title.
- v3 `suspended` sessions show 💤 and auto-resume on the next message
  instead of being treated as expired and recreated.
- Conversation-settings writes are atomic per column — concurrent updates
  (a `/settings` tap vs `/repos`) can't clobber each other, and v3 session
  polls resume at the last delivered message page instead of re-reading
  the full history.

### Fixed

- Devin replies emitted before your new message was forwarded (leftovers
  of the previous turn that the prior watcher never delivered) are now
  delivered unattributed: they no longer thread as the answer to your
  message and no longer mark the turn answered, which previously made the
  bridge close the turn instantly and orphan the real reply until your
  next message. Watchers triggered on a `finished`/`expired` session also
  wait out the settle window before closing so an in-flight resume can
  register.

## [v1.1.0] - 2026-09-23

### Added

- `/admin` actions `db-check` (SQLite `PRAGMA integrity_check`) and `backup`
  (online backup to `<name>-backup-<UTC stamp>.sqlite3` beside the database).
- Startup logs one summary line (mode, bot, database, access counts) and
  warns when placeholder `replace-with-*` secrets are still configured.
- `/start` sends a short onboarding greeting; `/help` is grouped by area
  with one-line descriptions.
- **CI**: ruff, pytest on Python 3.12/3.13, Docker build, ShellCheck,
  yamllint, and a non-blocking `pip-audit` job; code scanning runs via
  GitHub's default CodeQL setup; a tag-push release workflow validates
  `vX.Y.Z` tags reachable from `main` and builds release notes from the
  changelog plus generated notes.
- **Onboarding**: `docs/getting-started.md` (polling-first, five minutes),
  `docker-compose.yml`, `ROADMAP.md`, README badges/demo/scaling note, and
  `docs/hardening.md` (production checklist).

### Changed

- Empty `KEY=` in `.env` or the environment now reads as unset for every
  optional variable — a copied-but-unfilled file can no longer crash on
  settings parsing (e.g. `TELEGRAM_HOME_CHANNEL=`).
- `.env.example`: optional variables are commented out — copying the file
  verbatim no longer crashes on empty values.
- `Dockerfile`: runs as an unprivileged user (uid 10001), stores the SQLite
  database on a `/data` volume, and gains a `/health` healthcheck.

### Fixed

- `deploy/self-update.sh` only SIGTERMs the caller when it is the service's
  own `MainPID` — it previously killed any caller on systemd hosts, including
  manual shells and test subprocesses.

### Performance

- Session polling no longer rebuilds `DevinMessage` objects for events the
  watcher already delivered (`get_session(since_event_id=)`); the v1 API
  still returns the full history, so this saves CPU rather than bandwidth.
- Update dispatch now uses a bounded queue (256) with eight workers instead
  of one unbounded task per update; webhook callers get backpressure when
  the queue is full.
- Attachment downloads and GitHub PR metadata fetches are parallelized
  (bounded), and PR metadata is cached for 60 seconds keyed by URL and
  token.
- `fit_photo`/`photo_fits_unchanged` Pillow work runs off the event loop;
  remote transcription reuses a single `httpx.AsyncClient`.
- Capped in-memory collections (pending/queued turns, transient message
  ids, implicit topics) and a ten-minute janitor evicts stale rate-limit
  windows, access prompts, denied notices, and implicit topics.
- `_expand_text_links` decodes UTF-16 offsets incrementally instead of a
  prefix decode per entity.
- Telegram 400 fallbacks only retry on parse/markup error descriptions.
- Added covering SQLite indexes (`conversations`, `session_history`,
  `pending_choices`, `long_texts`, `processed_updates`, `message_index`);
  `processed_updates` pruning runs hourly instead of per update.

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
