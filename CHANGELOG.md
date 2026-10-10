# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) — see
[docs/versioning.md](docs/versioning.md) for the bump rules, release steps,
and deployment update channels.

## [Unreleased]

### Added

- Local CLI capability surfacing (`/platform local` chats): `/commands`
  opens a category → command browser built from the CLI's
  `available_commands_update` (Session/Skills/System/…), with tap-to-run
  buttons that send `/<name>` to the active session; any `/foo` message
  that isn't a bridge command is forwarded to the local session too, so
  `/fast`, `/compact`, `/loop`, and skills like `/ponytail:ponytail` work
  straight from Telegram. `/think [level]` sets the `thought_level`
  config option (medium/high/max) per chat, applies to the running
  session, and joins 🧠 Model as the 💭 Thinking row in `/settings`.
  `/mode` and its submenu now show the CLI's display names and
  descriptions (`Smart — auto-approves safe actions`), and `/status`
  reports the running session's live mode/model/thinking.
- Per-user command menus in private chats: the bot pushes a chat-scoped
  `setMyCommands` on first contact and whenever `/platform` changes, so
  local-only commands (`/model`, `/think`, `/commands`) only autocomplete
  for `platform=local` chats and admin commands (`/update`, `/users`,
  `/revoke`, `/sethome`) only for admins. Groups keep the shared menu.

### Fixed

- Local-session replies are now delivered at paragraph boundaries instead
  of arbitrary timed chunks, so interim Telegram messages are coherent
  paragraphs rather than mid-sentence fragments.
- Delivery gaps are no longer silent or permanent: watchers log their exit
  reason (watch cap, non-active close), a local turn whose buffered output
  is discarded by `suppress_turn` warns, a live local session emitting an
  event with no watcher alive restarts delivery immediately, and a
  janitor sweep remains as the backstop for cursors that still fall behind.
- The "⏳ Working…" status message re-posts after each delivered reply so
  live progress stays the last message in the thread instead of scrolling
  out of view.

- `/settings`, `/repos`, `/platform` (and the other toggles) now persist
  at the chat level: a setting made in one forum topic applies to every
  new topic instead of resetting to defaults. An explicitly-set value on
  an existing topic row still overrides the chat-level one for that topic.

### Added

- `/platform <name>` — per-chat session placement (v3 only). Set an
  outpost pool name or a hosted platform label (`linux`, `windows`,
  `macos`) so new sessions in that chat run on your own machines;
  `/platform default` resets to the org default. Also shown in the
  `/settings` 🖥 Platform row, whose submenu lists every platform label
  and outpost pool the org accepts (parsed from the create-session 400
  body, same probe as `devin_modes()`), and `/platform` with no args
  prints the same list.
- `/model <slug>` + `/settings` → 🧠 Model — pick the model local CLI
  sessions run (enumerated from the ACP `model` config option, applied at create and
  to the running session; settings option submenus page 30 per screen when a list is longer).
  `/mode` and the 🤖 Devin mode submenu enumerate the local ACP modes
  (accept-edits/smart/ask/plan/bypass) instead of cloud modes and apply
  immediately to the running session. Local sessions are now titled with
  the conversation title instead of the ACP session id.
- `/platform local` — prototype local-CLI backend: new sessions spawn a
  `devin acp` subprocess on the bridge host itself (fully local execution,
  the CLI's own model set, no cloud session URL). Shown as
  `local (this host)` in the 🖥 Platform submenu. Cloud session options
  (mode/repos/acu/secrets/…) don't apply, file attachments can't be
  delivered, and sessions resume across bridge restarts via ACP
  `session/load` (the CLI keeps them in its own DB). Overlong ACP output
  lines no longer kill the session reader (a >64KB chunk used to silently
  orphan every turn), and reply text streams into the topic every ~20s
  while a turn runs instead of appearing only at stopReason. The edited
  ⏳ Working status message also shows a live `→ …` line with the latest
  local thought or tool call (cloud exposes no thought stream — it gets
  the line only for free-text `status_detail` values).
  Requires the Devin CLI on the host (`DEVIN_LOCAL_CLI`,
  `DEVIN_LOCAL_CWD`); the service-user key is reused for `/login`.
- `/mode [name]` — per-chat Devin mode for new sessions, validated
  against the org's mode list (`/mode` no-args prints it,
  `/mode default` resets). Mirrors the /settings 🤖 Devin mode submenu;
  verified to combine with `platform` (outpost sessions accept
  `devin_mode`).
- `/repos` now validates names against the org's connected repos
  (`GET /v3beta1/organizations/{org}/repositories`, cached 5 min) —
  a typo replies `Not connected to this org: …` instead of silently
  breaking future sessions. The `/settings` 📂 Repos submenu toggles
  repos from the same list with ✓ marks; `/repos all` still resets.
- `/acu [n]` — per-chat ACU limit for new sessions, plus preset buttons in
  the `/settings` ⚡ ACU limit submenu; `/acu default` resets.
- `/tags [a,b]` — extra tags on new sessions (`telegram-bridge` is always
  sent); `/tags clear` resets.
- `/secrets [KEY,KEY2]` — attach org secrets to new sessions by key,
  resolved to IDs via `GET /v3/organizations/{org}/secrets` and toggleable
  in the `/settings` 🔑 Secrets submenu; `/secrets clear` resets.
- `/knowledge [id,id]` and `/snapshot [id]` — per-chat knowledge entries
  and environment snapshot for new sessions (IDs are free-text; the API
  has no list endpoint for them); `clear` resets.
- `/settings` 👁 Unlisted and 🔁 Idempotent rows — per-chat tri-state
  flags (inherit/on/off) passed to create-session when set.
- Restart backlog digest — when a bridge restart leaves 3+ undelivered
  Devin replies in a conversation, the recovery watcher sends one
  `📥 While the bridge was restarting` digest instead of bursting every
  reply individually. Below the threshold (and for `/resume`) replies
  deliver one-by-one as before.
- `CRAWL_SITES` + `/crawl` — bridge-wide opt-in URL pre-crawling. Set
  `CRAWL_SITES=instagram,article` as the default and matching URLs in user
  messages are fetched in the bridge; extracted text + media are attached
  to the Devin prompt, so sessions skip the crawl work. The `/settings`
  🔎 Pre-crawl submenu toggles the active set live (persisted in the DB,
  "env default" reverts to `CRAWL_SITES`); `/crawl` reports the active set.
  Ships with `instagram` (oEmbed: caption + cover image) and `article`
  (generic page title/description/body) crawlers in `app/crawlers.py`.

## [v1.4.0] - 2026-10-05

### Added

- `SUGGEST:` reply marker — `SUGGEST: <what> — <why>` … `END SUGGEST` renders
  a 💡 suggestion card (native `details` block, plain-text fallback) for
  proposing skills, knowledge entries, or environment changes; paired with
  `OPTIONS:` the session gets a complete propose → approve loop in-chat.
  Markers inside its body stay literal, like `DETAILS:`.

### Fixed

- The turn-close 👍 always lands now — a `REACT:` acknowledgment earlier in
  the turn no longer suppresses the completion mark on the triggering
  message.
- Finish notices carry the v3 `status_detail` reason when it disambiguates
  (`· approval`, `· idle timeout`, `· paused on request`), so a suspended or
  waiting session reports *why* instead of only the coarse status.
- `/help`, `/status`, `/sessions`, `/repos`, `/lang` are back in the
  command menu — `is_ephemeral` now applies only to the group-chats scope
  (clients hide ephemeral commands from the private-chat autocomplete).
- Replies no longer strand until your next message: a session still
  `working` past `DEVIN_WATCH_TIMEOUT_SECONDS` keeps its watcher (bounded
  by the new `DEVIN_ACTIVE_WATCH_TIMEOUT_SECONDS`, default 24 h); the
  30-min cap now only ends watches on inactive sessions.
- Admin API outcome notifications are held by a tracked task set now — the
  event loop can no longer garbage-collect the send, and shutdown drains it
  (bounded 5 s) before closing the Telegram client so a restart's outcome
  notice actually lands.

## [v1.3.0] - 2026-10-02

### Added

- `POST /notify` accepts `html` (+ `html_name`) and delivers it as an
  `Open <name>` report button (`/r/<token>`), or as a document when
  `PUBLIC_BASE_URL` is unset.
- `AGENTS.md` — agent-facing orientation: module map, test/lint commands, and
  the invariants that bite (marker surface ↔ preamble ↔ design doc sync,
  MarkdownV2 expandable-quote rules, v1/v3 degradation).
- `docs/v2-design.md` — status note clarifying the deployment is now on a v3
  service key and that `devin.py`/`telegram.py` are re-export shims for
  `clients.py` (the "v1 only" facts are historical).

### Fixed

- Expandable quotes (`**>` … `||`) in Devin replies now render as real
  collapsible blocks: every continuation line gets a `>` prefix (a bare
  line ends the quote and Telegram rejects the message), a `||` on its
  own line is glued to the last quote line (Telegram requires the closer
  at the end of the line), a trailing spoiler is not mistaken for the
  closer, such replies bypass `sendRichMessage` (whose markdown cannot
  render them), and a quote that crosses a message split is closed and
  reopened at the boundary.
- `REACT:` with an emoji outside Telegram's fixed reaction set no longer
  fails silently — the reaction degrades to 👍.
- The session preamble now also teaches `||spoiler||` and the `REACT:`
  emoji-set fallback, so fresh sessions know the full marker set.
- The `🔗 PR` status card is no longer appended to every reply once the
  session has a PR — it now only appears when the reply links a PR.

### Changed

- `/status` now shows the v3 `status_detail` reason (idle timeout, paused
  on request, waiting on your reply/approval), `ACUs` consumed when the
  API reports non-zero usage, and every PR the session opened instead
  of only the first.
- `/status`, `/usage`, `/sessions`, and `/help` send HTML messages with
  tap-to-expand detail blocks (`<blockquote expandable>`): session id,
  detail, ACUs, and PRs under `/status`; the per-day breakdown under
  `/usage`; session links under `/sessions`; and per-section command
  lists under `/help`.
- Bot API 10.x structured Rich Messages: the same four commands now
  prefer `sendRichMessage` `blocks` — native `details` collapsibles for
  `/status`, `/sessions`, and `/help`, and a real striped table for the
  `/usage` daily breakdown — falling back to the HTML expandable text
  when rich messages are unavailable.
- `/help`, `/status`, `/sessions`, `/repos`, and `/lang` are registered
  with `is_ephemeral`, so their invocations and replies stay private in
  group chats.
- Devin replies can emit two structured markers: `TABLE:` + `| col |`
  pipe rows + `END TABLE` renders a native rich-message table, and
  `DETAILS: <summary>` + lines + `END DETAILS` renders a tap-to-expand
  `details` block; the session preamble now documents both plus the
  expandable-quote syntax. Marker-free text around them keeps the
  normal Markdown path, and unsupported deployments get marker-free
  fallback text.
- Replies also accept control markers, each on its own line:
  `REACT: <emoji>` reacts to the triggering user message, `PIN:` pins
  the reply, `URGENT:`/`SILENT:` override notification quieting,
  `PROGRESS:` edits the session's previous progress message instead of
  sending a new one, and `POLL: question | a | b` sends a native poll.
  `||spoiler||` and `**>`/`||` expandable quotes now also survive the
  MarkdownV2 fallback converter.

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
