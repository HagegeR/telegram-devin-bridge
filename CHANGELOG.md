# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) — see
[docs/versioning.md](docs/versioning.md) for the bump rules, release steps,
and deployment update channels.

## [Unreleased]

### Changed

- Forum topic names are capped at five words (plus an ellipsis) when the
  bridge derives them — generated session titles run to sentence length,
  which made every subject unreadable in the topic list. Explicit names
  (`/new`, `/rename`, `/topic`) are never shortened, and `/status` and
  `/sessions` keep the full title.

### Fixed

- Local sessions could still be titled by the CLI's ~80-char
  provisional: a `session_info_update` title derived from the agent's
  own reply head (rather than the prompt) slipped past the prefix check,
  and the resume path took the persisted `session/list` title — often
  the provisional — unconditionally. Long (~60+ char) titles ending in
  `...`/`…` are now treated as provisionals on both paths, so truncated
  prompt or reply fragments never become the subject while short
  ellipsis-ended titles survive.

## [v3.0.0] - 2026-10-18

### Breaking

- **The local-CLI backend is an admin-only privilege tier now.** Setting
  `/platform local`, creating or messaging a local session,
  OPTIONS:/`cfg:platform` button taps, `/steer`, `/retry`, forwarded
  slash commands, and local mode/model/thinking changes all check the
  admin list; `bypass`/`smart` modes are admin-only on every chat.
  **Upgrade action:** set `TELEGRAM_ADMIN_USER_IDS` to the
  comma-separated Telegram user IDs allowed to run local sessions. If it
  stays empty the admin list falls back to `TELEGRAM_ALLOWED_USERS`, so
  every allowed user keeps local access — the tier only restricts once
  you name it.
- **Local permission prompts fail closed.** A prompt that can't be
  delivered to a chat is rejected instead of auto-allowed — unattended
  local turns can now stop where they previously proceeded.
- **`/new` suspends the running local session's process** instead of
  leaving it detached (the session stays resumable).

### Security

- The local-CLI backend is now an admin-only privilege tier: setting
  `/platform local` (command or `cfg:platform` settings button),
  creating or messaging a local session (turns, OPTIONS: button taps,
  `/steer`, `/retry`, forwarded slash commands), and changing its
  mode/model/thinking level all require `TELEGRAM_ADMIN_USER_IDS`
  membership (`bypass`/`smart` modes are admin-only on every chat).
  `TELEGRAM_ADMIN_USER_IDS` left empty falls back to
  `TELEGRAM_ALLOWED_USERS` — every allowed user keeps local access, so
  the tier only restricts once the admin list is named.
- Local ACP permission prompts now fail closed: when no conversation is
  mapped or the Telegram prompt can't be delivered, the request is
  answered with the reject option (or the ACP cancelled outcome)
  instead of being auto-allowed. `/new` also suspends the running local
  session's process, so a detached agent can't keep acting on the host
  (the session stays resumable).

### Fixed

- The `/crawl` SSRF guard no longer trusts hostnames: crawler fetches now
  resolve the host, require every address to be global, connect to the
  vetted IP (SNI/Host keep the original name, so DNS rebinding can't
  reroute the fetch), and re-vet every redirect hop and `og:image`
  download — non-canonical numeric IPv4 spellings like `2130706433` or
  `0x7f.1` resolve to private addresses and are rejected.
- One local session that can't be respawned at startup (e.g. an `OSError`
  from the acp subprocess) no longer aborts `_replay_interrupted_turns`:
  its pending prompt is dropped like a "gone" session's and later
  conversations still get their replay instead of every boot dying on
  the same conversation.
- A subprocess that exits in the same instant `_run_command`'s 300s
  timeout fires no longer crashes the bridge: `process.kill()` on the
  already-reaped child raised `ProcessLookupError`, which escaped the
  `/update` handler and killed the app (supervisor respawned it; seen in
  the production log as an unhandled traceback).
- A single oversized `tool_call` command from `devin acp` could stall the
  whole bridge: the credential-scrubbing regex backtracked catastrophically
  on inputs like `-key-key-…` and ran on the uncapped command string on
  the event loop. Commands are now truncated before scrubbing, ACP lines
  past 16MB are dropped (settling any pending turn they answered), and
  the scrubbing regex is replaced by a linear token scan so credential
  names of any length still mask (200KB input: ~105s → ~2ms).
- A forged `cfg:acu:<n>` settings callback could store any integer (or crash
  on non-numeric input), bypassing the `/acu` 1-1000 bound and lifting the
  per-session ACU cap; the callback now validates like `/acu` and
  `DevinClient.create_session` clamps the value to 1-1000 before sending.
- A crafted page of unclosed tags could stall the whole bridge for
  minutes when the `article` crawler ran quadratic regexes over up to
  1 MB of HTML on the event loop; article extraction now parses the
  first 200 KB in a single pass via `html.parser`, off the event loop
  in `asyncio.to_thread` with a 5s timeout.
- A tap on a stale inline button (older than ~1min, or an update replayed
  after a restart) no longer takes the callback handler's error path:
  Telegram's `query is too old` rejection of the cosmetic ack is now a
  warning instead of a "Failed to process Telegram update" traceback.
- One transient error (a timed-out Telegram edit, a Devin API flap) no
  longer kills a session watcher mid-turn — previously the conversation
  stopped streaming replies until the janitor's 10-minute backlog probe
  or the user's next message, and a bogus "Couldn't reach Devin" plus a
  👎 expired mark landed on the turn. The poll loop now retries up to 5
  consecutive failures before reporting.
- A local session whose `devin acp` subprocess stayed alive but stopped
  answering mid-turn (the CLI's internal event channel can deadlock —
  observed blocked for 40+min) showed `working` forever: a watchdog now
  probes turns silent on stdout for ~5min with `session/list`, and a
  process that doesn't answer is respawned — the session reloads from
  the CLI's DB, the in-flight prompt is retried once (not looped), and
  the user sees `⚠ the local CLI stopped answering mid-turn`.
- Prompts queued behind a dead/wedged turn no longer write to the reaped
  process (and hang): they transfer to the resumed process in send
  order, and a pending prompt stranded by a mid-turn crash replays on
  the next contact instead of being silently overwritten.
- A local session whose acp process died outside the watchdog kept its
  map entry — every send failed `gone` until a bridge restart; the next
  send now reloads the persisted session on a fresh process.
- A control request that timed out left its pending slot behind — a late
  reply resolving the cancelled future killed the session's stdout
  reader on the spot.

## [v2.1.0] - 2026-10-15

### Changed

- `/model`'s available-model list and the 🧠 Model settings picker are now
  sorted by price then novelty instead of the CLI's raw order: `adaptive`
  first, then 🎁 Free → 💎 High cost, newer families first inside a tier
  (the `models list` family order is the freshness signal).
- Per-message status marks: every user message that feeds a turn now shows
  its own lifecycle — 🫡 on receipt by the bridge (the 🫡 stands in for
  the second checkmark: the Bot API cannot send one on the user's own
  messages and no checkmark emoji is a valid reaction), 🤔 while queued
  behind a busy session, 👀 once the agent actually has the text, 👎 on
  failure or expiry. Previously only the turn's triggering message got a
  finish mark (👍).

### Fixed

- Local session topics were still renamed to the prompt-derived
  provisional title: the CLI truncates the prompt head to ~80 chars,
  collapses newlines to spaces, and ends it in `...`, so it is not a
  raw prefix of the prompt and slipped past the provisional-title
  check — every topic got named after the bridge's own instructions
  boilerplate, and the real generated title arriving after could no
  longer replace it (`title_pending` was already cleared). The
  provisional check now compares against the whitespace-normalized
  prompt head with a trailing ellipsis stripped.
- Streamed local progress could still arrive as one fused blob: any
  line containing inline `code` was skipped entirely by the run-on
  splitter, which is most progress text. The splitter now masks code
  spans and splits the prose around them, so a line ending in an
  inline-code span followed by a fused sentence (`.While it runs…`)
  breaks correctly.
- Telegram polling no longer dies silently: an unexpected error in update
  handling (e.g. a `mark_update_seen` sqlite failure or a handler bug) used
  to kill the polling task for good while the process kept running — the
  bridge looked alive but consumed nothing. Such errors are now logged and
  the loop continues; the poisoned update is skipped (its offset is already
  advanced) and batch siblings redeliver. A watchdog task self-SIGTERMs the
  process if the loop has not iterated for 10 minutes so the supervisor
  respawns it, and the loop touches
  `/tmp/telegram-devin-bridge.poll-alive` every iteration for external
  watchdogs (`deploy/openrc/telegram-devin-bridge-watchdog`, installed into
  `/etc/periodic/15min/` by `install.sh` and kept current by
  `self-update.sh`).
- Transient `500`/`5xx` replies from the Devin API on idempotent calls
  are retried like connection failures instead of surfacing to the chat
  as `⚠ Couldn't reach Devin`.
- Local session topics were renamed to a provisional title the CLI
  derives from the prompt itself — the first line was the bridge's own
  instructions boilerplate (or worse, serialized tool text). Reported
  titles that prefix the sent prompt, or look like a tool-call
  serialization (`name:N{…}`), are now ignored, and later
  `session_info_update` titles replace provisional ones so the real
  generated title still lands; an explicit `/new` title is never
  overwritten, and on resume the `session/list` title is authoritative
  so a replayed provisional can't clobber it.
- Option buttons in the rebuilt disabled markup no longer scramble order when
  `pending_choices` rows share a `created_at` — the column is a float64 that
  ties routinely (systematic on Windows' coarse `time.time()`, possible
  everywhere). `list_choices` now orders by `rowid` (insertion order), which
  can never tie.
- `⚠ prompt failed` no longer relays raw ACP stderr into chat — the
  agent's own structured log lines (skills discovery stats, SessionEnd
  noise) and object reprs like `<WriteUnixTransport …>` are stripped so
  only a single capped reason shows (`acp process exited`, `the handler
  is closed`); the full tail still lands in the journal. The restart
  digest header now counts only digested replies and caps the session
  title at 60 chars.
- Implicit topics no longer get renamed to the first line of the user's
  prompt — that rename raced the generated session title and could
  clobber it with unrelated prompt text. Untitled sessions now wait for
  the generated title; sessions opened with an explicit `/new` title
  rename the topic to that title instead. If the session settles
  before a title ever arrives the watcher waits a few more polls,
  then names the topic from the prompt so it is not left with
  Telegram's generic name.
- Local sessions opened without an explicit title now pick up the
  CLI-generated title (`session_info_update`), so the Telegram topic
  gets renamed like cloud sessions do. An explicit title is never
  overwritten.
- `deploy/vm/install.sh`: retry the zypper install up to 5 times with
  30s backoff and a metadata clean between attempts — openSUSE mirrors
  serve stale/forbidden repodata for minutes at a time (this flake red
  the `linux-install (opensuse/tumbleweed)` cell on the v2.0.0 build).
- Local sessions no longer fuse streamed progress updates into a
  wall-of-text run-on: the CLI's chunk stream can drop line separators
  between updates, so emitted replies now reinsert a break where
  sentence punctuation is immediately followed by a capital or status
  marker (`⚠`, `✓`, `◆`, `→`, `#`). Thought chunks (one line per
  update) are also joined with newlines so the `→` activity tail shows
  the real last line instead of a fused blob.
## [v2.0.0] - 2026-10-11

### Added

- Local `/model` listings and the 🧠 Model settings submenu now prefix
  each model with a pricing icon from `devin models list --format json`:
  🎁 Free, 🏷️ Low cost, 💰 Med cost, 💎 High cost (bare slug when the
  CLI reports no tier).
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
- Interactive permission approval on `/platform local`: the CLI's
  `session/request_permission` prompts render as inline-keyboard buttons
  (`🔐 Permission requested: <action>`) and the picked option is written
  back to the acp process — instead of every request being auto-allowed.
  Auto-allow remains as the fallback when the prompt can't reach the
  chat, so turns never stall on an invisible question.
- Photos and documents now reach local sessions: they're saved under
  `DEVIN_LOCAL_CWD/attachments/` on the bridge host and the prompt hands
  the agent the file path, instead of being dropped with a "not sent"
  note. Files older than 7 days are swept on each save so the directory
  can't grow without bound.

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

### Fixed

- Local CLI turns no longer lose their tail to a bridge restart: emitted
  reply events are mirrored into a `local_events` table and re-seeded on
  `session/load`, so a recovery watcher still delivers the turn-end
  message + "💬 Waiting for your reply" (cloud sessions get this
  durability server-side). The janitor sweep now also checks persisted
  rows, so replies stranded on a session outside the live-process map
  still reach Telegram.
- Local sessions survive a bridge restart mid-turn: the prompt is kept in
  a durable `pending_prompt` column while its `session/prompt` call is in
  flight and replayed at the next boot, so a self-update deploy no longer
  silently eats the user's question. The marker is kept when the process
  died (shutdown/crash/evict — an interrupted turn, not a failed one) and
  dropped atomically when the conversation swaps sessions.
- Local resume restores the chat's configured mode/model/thought_level —
  `session/load` only reloads CLI state, so e.g. bypass mode silently
  reverted to asking for approvals after a restart. Resume-time control
  updates (`config_option_update`, `current_mode_update`) now pass the
  replayed-history guard; only reply content is suppressed.
- The local activity line (`→ …`) now shows the real command from
  `rawInput.command` instead of the tool title, so wrapper commands
  surface as e.g. `rtk pytest -q …` rather than a bare `Ran rtk`.

- Self-update no longer wedges itself: the restarted service inherited
  the update lock's fd, so every later `/update` (and the cron pull)
  printed `another update is running` forever. The restart command now
  runs with the lock fd closed — serialization during the restart is
  kept, the spawned service does not inherit the flock.

- Local permission prompts with non-integer request ids (the CLI can send
  UUID ids) no longer crash the prompt task — the crash left the request
  permanently unanswered and the local session looked stuck on
  `⏳ working` forever. Request ids are now echoed back verbatim and the
  answer callback accepts string ids.

- Restart digests no longer swallow interactive replies: a pending reply
  carrying `OPTIONS:` is excluded from the digest and delivered normally
  with its buttons instead of being stripped and marked covered.

- `OPTIONS:` button labels up to 100 chars now render — labels over the
  old 60-char cap silently dropped the whole marker, leaving the raw
  `OPTIONS: …` line visible instead of buttons.

- `/sessions` no longer silently truncates at 10 entries — it lists up to
  200 history rows, and on `platform=local` chats it merges the CLI's
  own session DB (`session/list`) so dormant untracked sessions appear
  as `dormant (local)` rows — they are listed but not resumable, since
  the CLI does not record which chat owns them; `/resume <n>` numbers
  against the same merged list. Long lists that exceed the message cap
  now end with an `…and N older sessions` note instead of dropping
  entries silently.

- A non-admin tap on a local permission prompt no longer consumes the
  keyboard — the choice rows are now deleted only after the admin check,
  so the prompt stays answerable for an admin.
- The local probe behind `/settings`, `/mode`, `/model`, `/think`, and
  `/commands` now deletes the throwaway session it creates — each call
  previously left a persistent phantom entry in the CLI's session DB
  (`devin list`).
- Answering one permission prompt no longer expires the buttons on
  another: choice cleanup is scoped to the tapped message and the
  conversation-wide wipe preserves `__cmd:perm:` rows.
- An attachment sent to an expired local session no longer routes to the
  freshly created session that replaced it — the session-id check runs
  before the routing decision.
- Attachment filenames embedded in local prompts are sanitized to
  `[\w .-]` (80 chars max), so a hostile name can't inject prompt text.
- Local permission buttons are admin-only when
  `TELEGRAM_ADMIN_USER_IDS` is configured — a group member can no
  longer approve a permission request on someone else's session.
- The `⚠ local sessions ignore cloud options` notice now also lists
  unlisted/idempotent when they were explicitly turned off — `False` was
  previously treated as unset.
- Starting a local session with cloud-only options set (repos, acu,
  tags, secrets, knowledge, snapshot, unlisted, idempotent, playbook) now
  posts a `⚠ local sessions ignore cloud options: …` notice instead of
  silently dropping them — previously only playbooks warned.
- The live `→ …` activity line in `⏳ Working` no longer starts mid-word:
  a thought line over the display cap keeps its tail but drops a partial
  leading token, so `Orphaned supervise-daemon…` can't render as
  `rphaned supervise-daemon…`.
- Local-session replies are now delivered at paragraph boundaries instead
  of arbitrary timed chunks, so interim Telegram messages are coherent
  paragraphs rather than mid-sentence fragments.
- Delivery gaps are no longer silent or permanent: watchers log their exit
  reason (watch cap, non-active close), a local turn whose buffered output
  is discarded by `suppress_turn` warns, a live local session emitting an
  event with no watcher alive restarts delivery immediately, and a
  janitor sweep remains as the backstop for cursors that still fall
  behind — now covering cloud conversations too, via a
  `get_session(since_event_id)` probe on sessions with no live watcher.
- The "⏳ Working…" status message re-posts after each delivered reply so
  live progress stays the last message in the thread instead of scrolling
  out of view (10s cooldown so bursts can't churn it).
- Recovered replies and restart digests now thread under the user message
  that prompted the turn, and a turn boundary keeps late output from a
  previous turn from claiming the newer message.
- A restart no longer drops accepted Telegram updates: the dedupe marker
  and the full payload land in `pending_updates` atomically, undispatched
  rows are replayed in order at startup, and a `sent_turn` marker drops
  rows that outlived their delivery so replays can't re-send a prompt
  Devin already received. Edits applied to debounce-staged messages
  survive the same restart.

- `/settings`, `/repos`, `/platform` (and the other toggles) now persist
  at the chat level: a setting made in one forum topic applies to every
  new topic instead of resetting to defaults. An explicitly-set value on
  an existing topic row still overrides the chat-level one for that topic.
- OpenRC self-update restarts now sweep orphaned `supervise-daemon`
  processes between stop and start, so a stale supervisor's child can't
  keep `:8000` and wedge the service in an `EADDRINUSE` respawn loop.
  Recovery steps for already-stuck hosts are in `docs/operations.md`.

### Changed

- Service log lines now carry an `asctime` timestamp — both app loggers
  and uvicorn's access/error/shutdown lines (restamped at app startup;
  uvicorn builds its handlers before `app.main` is imported) — so
  restart-correlated entries can be told apart from days-old residue.
- CI now runs the test suite on Python 3.12 through 3.15 (previously
  3.12/3.13 only).

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
