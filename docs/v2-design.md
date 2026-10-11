# Bridge v2 design (lead-authored; implementation brief)

> **Status note (v1.2.0+):** the deployed key is now a **v3 service key** — the
> "v1 only" facts below are historical. `DevinClient` selects its API version
> at init: v3 (`/v3/...`) when the service-key + org settings are present
> (`v3_enabled`), v1 when they are absent — a rejected v3 request does not
> retry v1. v3-only features (devin_mode, repos, platform, `status_detail`,
> `acus_consumed`, structured blocks) degrade quietly on v1 deployments.
> `devin.py`/`telegram.py` are re-export shims — both clients live in
> `clients.py`.

## Grounded API facts (verified against api.devin.ai with the deployed key)

The key is a **v1** key; every `/v3/...` call returns 403. Use only:

- `POST /v1/sessions` body `{prompt, title?, playbook_id?, max_acu_limit?, tags?}` -> `{session_id, url}`
- `POST /v1/sessions/{id}/message` body `{message}`
- `GET /v1/sessions/{id}` -> `{status, status_enum, title, pull_request:{url}|null, messages:[{type, event_id, message, timestamp, username, origin}]}`
  - `status_enum` values: `working, blocked, expired, finished, suspend_requested, suspend_requested_frontend, resume_requested, resume_requested_frontend, resumed`
  - `blocked` == Devin is waiting for the user. `working` == busy.
  - message `type` values seen: `initial_user_message`, `user_message`, `devin_message`. Only `devin_message` is a Devin reply.
  - Devin's `user_question` options are **not** exposed by the API — only the message text.
- `GET /v1/sessions?limit=N` list; `DELETE /v1/sessions/{id}` terminate (irreversible).
- `GET /v1/playbooks` -> `{items:[{playbook_id,title,...}]}` (note: `items` may be a list; treat as list).
- `POST /v1/attachments` multipart `file` -> JSON string URL.

Telegram Bot API: standard; secret via `X-Telegram-Bot-Api-Secret-Token` header (already done).

## Architecture

Single FastAPI process. Webhook handler validates + dedupes + enqueues; all Devin work runs in
asyncio background tasks. One **SessionWatcher** task per active Devin session streams new
`devin_message`s to Telegram as they appear (not only the last one), keeps a typing indicator
alive, and stops when `status_enum` is not `working`/`resumed`/`resume_requested*` or after
`DEVIN_WATCH_TIMEOUT_SECONDS` (default 1800).

### Modules (app/)
- `config.py` – Settings (see env table below).
- `store.py` – SQLite. Tables:
  - `conversations(conv_key TEXT PK, chat_id INT, thread_id INT NULL, session_id TEXT, session_url TEXT, title TEXT, last_event_id TEXT, created_at REAL)` – **active** session per conversation.
  - `session_history(id INTEGER PK, conv_key, session_id, session_url, title, created_at)` – for `/sessions` + `/resume`.
  - `processed_updates(update_id INTEGER PK, seen_at REAL)` – dedupe; prune > 24h.
  - `pending_updates(update_id INTEGER PK, payload TEXT, seen_at REAL)` – the
    accepted-but-undelivered queue; written in the same transaction as the
    dedupe marker, kept while the update sits in `pending_turns`/`queued_turns`
    (debounce or busy-queue staging), deleted only when its turn reaches
    `handle_user_turn` (or is deliberately cleared). An edit that rewrites a
    staged fragment keeps its own row with that fragment, so a restart
    replays the original update then the edit and delivers corrected text.
    Startup replays rows sequentially in update_id order (concurrent
    dispatch could interleave a message→edit pair), staging replayed
    turns in `pending_turns` and flushing them all at the end so a
    following edit lands before the original sends. A replayed update
    whose `message_id` is at or below the `sent_turn:{conv_key}` marker —
    the highest message_id confirmed delivered to Devin, written right
    after each `handle_user_turn` reaches Devin — is dropped instead of
    re-sent; the check is replay-only because live dispatch runs on
    concurrent workers that can legitimately deliver an older update
    behind a newer one (stale >24h rows are dropped rather than
    replayed).
  - `pending_choices(choice_id TEXT PK, conv_key, session_id, option_text, created_at)` – inline-keyboard callbacks (`callback_data` <= 64 bytes, so store a short random id).
  - `settings(key TEXT PK, value TEXT)` – `home_chat_id`, `home_thread_id` set by `/sethome`, `crawl_sites` set by the `/settings` pre-crawl submenu.
  - conv_key = `f"{chat_id}"` for DMs/plain groups, `f"{chat_id}:{message_thread_id}"` for forum topics (only when `chat.is_forum` and `message_thread_id` present).
  - `conversation_settings` writes (`Store.update_chat_settings`) target the chat-level key (`chat_id` without the thread) and clear the same fields on the invoking topic's row, so a setting persists across new topics and a stale per-topic override can't shadow it; `get_settings` still reads the topic row first, so any remaining explicitly-set topic field (including an explicit `off` for tri-state fields; `silent` cannot express an explicit off) overrides the chat-level value for that topic.
- `devin.py` – `DevinClient` (v1 only): `create_session(prompt, title, playbook_id=None) -> (session_id, url)`, `send_message`, `get_session -> SessionState(status_enum, title, pr_url, messages)`, `list_playbooks`, `terminate`, `upload_attachment(filename, bytes, content_type) -> url`.
- `telegram.py` – `TelegramClient`: rich Markdown (`send_rich_message`/`send_markdown`), private-chat drafts, forum-topic creation/renaming, `send_message`, `edit_message_text`, `edit_message_reply_markup`, `send_chat_action(typing)`, `set_message_reaction(chat_id, message_id, emoji|None)`, `answer_callback_query`, `get_file`+`download_file(file_path) -> bytes`, `set_my_commands`, `set_webhook`. Handles 429 by reading `parameters.retry_after`, sleeping, retrying once. Rich-message 4xx responses fall back to MarkdownV2; unknown-method errors latch rich mode off, while transport and 5xx errors propagate.
- `formatting.py` – `markdown_to_telegram_markdown_v2(text) -> str` and `chunk(text, limit=4096) -> list[str]` (never split inside a fenced code block; if a block itself exceeds the limit, close and reopen the fence; append ` (i/N)` suffix when N>1). Escape all MarkdownV2 reserved chars outside entities: `_ * [ ] ( ) ~ \` > # + - = | { } . !`. Support: `**bold**`/`__bold__`->`*bold*`, `*em*`/`_em_`->`_em_`, `~~s~~`->`~s~`, inline code, fenced code (```lang), `[text](url)`, headings `#..` -> bold line, `> quote` -> `>quote`, bullet `- ` -> `• `. Also `extract_options(text) -> (text_without_line, list[str])`: if the last non-empty line matches `^OPTIONS:\s*(.+)$` split on `|`, strip; max 8 options, each <= 60 chars.
- `access.py` – `is_allowed(update_message) -> bool` and `should_respond_in_group(message, bot_username) -> bool`.
- `commands.py` – slash command handlers (below).
- `watcher.py` – `SessionWatcher` (below). Replies containing `TABLE:`/`DETAILS:`/`SUGGEST:` markers are split by `parse_rich_segments` into ordered markdown/`blocks` segments (`SUGGEST: <what> — <why>` … `END SUGGEST` renders a `details` card whose summary is prefixed `💡`, meant for proposing skills/knowledge/environment changes; pairing it with `OPTIONS:` lets the user approve in-chat): markdown segments go through `send_markdown`, block segments through `sendRichMessage` (native `table`/`details` blocks), with buttons/reply threading on the outer segments and marker-free fallback text when rich messages are unavailable. Control markers (each on its own line, stripped by `extract_controls`): `REACT: <emoji>` reacts to the triggering user message; `PIN:` pins the delivered reply; `URGENT:`/`SILENT:` override the computed `disable_notification`; `PROGRESS:` edits the previous progress message via `editMessageText` instead of sending a new one (MarkdownV2 edit; falls back to a normal send); `POLL: question | a | b` sends a native poll. `||spoiler||` and `**>`/`||` expandable quotes also pass through the MarkdownV2 converter: every continuation line inside a `**>` quote is emitted with a `>` prefix (MarkdownV2 requires it — a bare line ends the quote and the trailing `||` parses as an unclosed spoiler), a `||` on its own line closes the quote on the previous content line, and replies containing `**>` outside a code fence (`has_expandable_quote`) bypass `sendRichMessage` (whose markdown cannot render expandable quotes) and send via `sendMessage`/`MarkdownV2` instead, skipping the Show-more split while `chunk` closes and reopens the quote at each 4096 boundary.
- `notify.py` – `POST /notify` route.
- `main.py` – app wiring, webhook route, `/health`.
- `set_webhook.py` – also calls `set_my_commands` per scope: the full list for
  default + `all_private_chats`, the `GROUP_COMMANDS` subset for
  `all_group_chats`; `is_ephemeral` is set on the group scope only (clients
  hide ephemeral commands from the private-chat command menu). Private chats
  additionally get a `chat`-scoped menu (`chat_commands`) pushed on the first
  message and on every `/platform` change: local-only commands (`/model`,
  `/think`, `/commands`) appear only for `platform=local` chats and the
  admin commands (`/update`, `/users`, `/revoke`, `/sethome`) only for
  `TELEGRAM_ADMIN_USER_IDS` — a `chat` scope outranks `all_private_chats` in
  clients. Pushes are serialized under a lock and re-read settings inside
  it, so a `/platform` change racing the first-contact push always lands
  last; a failed push retries on the next message. Group chats keep the
  shared group menu; a chat-scoped menu there would leak one member's
  settings to everyone.
- Delivery watchdog: every local event emission fires `on_emit`, which
  restarts the watcher immediately when none is alive — a watcher that
  exits mid-turn (settle close, restart) no longer strands output until
  the next poll. The janitor sweep (every 10 min) stays as the backstop:
  it rechecks live local sessions whose emitted events outnumber the
  conversation's persisted `last_event_id`, and probes every cloud
  conversation with no live watcher via `get_session(since_event_id)` —
  a post-cursor `devin_message` means the remote history moved past the
  cursor (a lone post-cursor `user_message` is ignored, since the
  watcher can't advance the cursor on it and would restart every sweep;
  the cursor filters client-side, so each probe re-reads history until
  the marker — one request per conversation per sweep).
  Watchers also log their exit
  reason (watch cap, non-active close) and a `suppress_turn` discard
  warns, so delivery gaps
  are diagnosable instead of silent. Recovery watchers thread their
  first reply (and the restart digest) onto `last_user_message_id` —
  persisted whenever a watcher gets a user trigger — and use
  `trigger_at=updated_at` as the turn boundary so late output from a
  previous turn delivers un-attributed rather than claiming the newer
  message. The "⏳ Working…" status re-posts after delivered replies
  (10s cooldown) so progress stays last in the thread.

### Inbound flow (webhook)
1. Verify secret header (403 otherwise). Parse `update_id`; if already in `processed_updates` return `{accepted:true}`; else insert the marker + the full payload into `pending_updates` atomically, so a restart before dispatch replays it at startup instead of losing an accepted update.
2. Route: `callback_query` -> `handle_callback`; `message` or `channel_post` with `text`/`caption`/`photo`/`document` -> `handle_message`; anything else ignored.
3. Access (`access.py`):
   - if `TELEGRAM_ALLOW_ALL_USERS` false: `from.id` must be in `TELEGRAM_ALLOWED_USERS` (if that list is non-empty) AND, if `TELEGRAM_ALLOWED_CHAT_IDS` non-empty, `chat.id` must be in it. Empty both lists + allow_all false => deny everything with a one-time reply "This bot is private. Your user id is N." (so onboarding is easy). Log `Rejected ... user=%s chat=%s`.
   - Groups/supergroups: respond only if text mentions `@<bot_username>` (strip the mention), or the message is a reply to a bot message, or `chat.id` in `TELEGRAM_FREE_RESPONSE_CHATS`. Otherwise ignore silently.
   - Ignore messages where `from.is_bot`.
4. Slash command (`text` starts with `/`) -> `commands.py`. Otherwise -> `handle_user_turn`.

### `handle_user_turn(conv, message)`
- React 👀 on the user's message (`set_message_reaction`), start typing.
- If message has `photo` (largest size) or `document` (<= 20 MB, Telegram getFile limit): download, `upload_attachment`, and prepend `Attached file: <url>` (+ original filename) to the text/caption.
- If conversation has no active session (or its `status_enum` is `expired`, or the send below fails with 404/410 -> auto-create new; `finished`/`blocked`/suspended sessions resume when messaged and keep their context): `create_session(prompt=SYSTEM_PREAMBLE + text, title=f"Telegram: {first 60 chars}")`, insert into `conversations` + `session_history`, reply with `Started session: <url>` (silent).
  - `SYSTEM_PREAMBLE` (constant in `commands.py`): "You are chatting with a user over Telegram via a bridge. Keep replies concise. Telegram renders Markdown. When you need the user to pick between a small set of options, end your message with one line exactly like `OPTIONS: first option | second option | third option` (max 8, each under 60 chars); the bridge turns it into buttons. For rich or long results (tables, charts, reports), attach a self-contained .html file; the bridge serves it and adds an Open button. To collapse a long section inline, use a Telegram expandable quote: first line prefixed `**>`, the rest prefixed `>`, and end with `||`. Inline `||spoiler||` hides text until tapped. Markers that render native blocks: `DETAILS: <summary>` on its own line, then content lines, then `END DETAILS` gives a tap-to-expand section; `TABLE:` on its own line, then `| col | col |` pipe rows, then `END TABLE` gives a real table; `SUGGEST: <what> — <short why>` on its own line, then the proposal body, then `END SUGGEST` renders a 💡 suggestion card — use it to propose a skill, knowledge entry, or environment change, and pair it with `OPTIONS:` (e.g. `OPTIONS: Save it | Skip`) so the user can approve; apply their choice with your own tools. Control markers, each on its own line: `REACT: <emoji>` reacts to the user's message (common emojis — off-set ones degrade to 👍); `PIN:` pins the reply; `URGENT:`/`SILENT:` override notification quieting; `PROGRESS:` edits your previous progress message instead of sending a new one; `POLL: question | a | b` sends a poll. Never ask the user to open a UI; they only see your messages.\n\nUser message: "
- Else `send_message(session_id, text)`.
- Ensure a `SessionWatcher` is running for that session (idempotent registry keyed by session_id).

### `SessionWatcher.run()`
Loop every `DEVIN_POLL_SECONDS` (default 3):
- `state = get_session()`. For each `devin_message` newer than `conversations.last_event_id` (walk in order; "newer" = appears after the stored id in the list; if stored id is None, only messages with timestamp >= watcher start time - 5s to avoid replaying history on `/resume`): render via formatting, `extract_options`; send chunks (`disable_notification = mode=="important" and state.status_enum == "working"`); for the last chunk attach an inline keyboard if options (one button per row; `callback_data = choice_id`, store `pending_choices`). Update `last_event_id` after each send.
- Restart recovery: on startup, watchers resume for every conversation whose session was touched within the active watch window. When such a recovery watcher's first poll finds 3+ undelivered replies, they collapse into one `📥 While the bridge was restarting` digest (markers/options stripped, per-reply text capped) instead of a per-message burst; `last_event_id` advances past all of them. Backlogged replies lose keyboards/attachments in the digest — the next user message gets normal delivery.
- Refresh typing every loop while `working`.
- Stop when `status_enum in {"blocked","finished","expired"}` and no unsent messages: post the finish notice (`💬 Waiting for your reply` / `✓ Finished` / `⚠ Session expired`, suffixed `· <reason>` when v3 `status_detail` disambiguates — `· approval` / `· idle timeout` / `· paused on request`), react 👍 on the triggering user message (👎 if we hit an exception / `expired`), and if `pr_url` newly appeared send `🔗 PR: <url>`. Clear reaction of 👀 by setting 👍. The mark always lands, even when the session acked with `REACT:` earlier in the turn. The notice is skipped when nothing was delivered this turn and the status never moved (restart-resume duplicate), or when turns are still queued.
- On timeout: the watch ends at `DEVIN_WATCH_TIMEOUT_SECONDS` (30 min) once the session is no longer active; while `status_enum` stays active (`working`/`resumed`/`resume_requested*`) the watcher keeps polling up to `DEVIN_ACTIVE_WATCH_TIMEOUT_SECONDS` (24 h) so long replies still deliver without a user message. When a watch does time out, send one silent notice "Devin is still working; I'll deliver replies when you next message." only if nothing was delivered during the watch (a later user message restarts the watcher).
- Any exception: log, send "Couldn't reach Devin: <short reason>" once, stop.

### Callback (`handle_callback`)
- Authorization: same `is_allowed` on `callback_query.from`.
- Look up `pending_choices[data]`; if missing -> `answer_callback_query("This choice expired")`. Else `send_message(session_id, option_text)`, delete the other choices for that conv, `edit_message_reply_markup` to remove buttons and append `\n\n✅ <option_text>` via `edit_message_text` (plain fallback), `answer_callback_query`, start watcher.

### Commands (`commands.py`) – reply in the same conv/thread
- `/start`, `/help` – list commands (`/help` collapses the command list per section; prefers a `sendRichMessage` `blocks` payload of `details` blocks, falling back to HTML `<blockquote expandable>` when rich messages are unavailable).
- `/new [title]` – archive current conv mapping (keep history), store a pending title, reply `◆ New conversation: <title>\nSend your first message to start Devin.` — no Devin call; the next user message creates the session with that title.
- `/sessions` – last 10 from `session_history` for this conv, numbered with title + status (fetch status for each, tolerate errors), mark active with `*`; session urls collapse into a `details` rich block or an HTML `<blockquote expandable>` fallback (dropped when the reply would exceed ~4000 chars).
- `/resume <n>` – set that history entry as active (`last_event_id` = id of its latest devin_message so no replay), reply `Resumed: <title> <url>`.
- `/status` – active session: title, `status_enum` (plus the v3 `status_detail` reason when it adds information), url; session id, raw `status_detail`, ACUs consumed, `updated_at`, and every PR url in a `details` rich block or an HTML `<blockquote expandable>` fallback (dropped when the reply would exceed ~4000 chars; oversized titles are truncated to fit). `/usage` renders its daily breakdown as a rich `table` block with the same fallback.
- `/stop` – terminate active session via `DELETE` (confirm with an inline keyboard "Terminate | Cancel" using the same `pending_choices` mechanism but with `option_text` starting `__cmd:terminate:<session_id>`; handle_callback must special-case `__cmd:` prefixes). After terminate, clear active mapping.
- `/playbook` – list playbooks (numbered, title only); `/playbook <n> [text]` – create session with that `playbook_id`, prompt = preamble + (text or "Run this playbook."), becomes active.
- `/retry` – resend the last user text stored on the conversation (add column `last_user_text`).
- `/repos [a/b,c/d]` – per-chat repo restriction for new sessions (`/repos all` resets). Names are validated against `DevinClient.repos()` — the org's connected repos from `GET /v3beta1/organizations/{org}/repositories`, cached 5 min; an empty fetch means "couldn't verify" and is accepted. The `/settings` 📂 Repos submenu toggles repos from that same list (✓ marks selected, "all repos" resets; names whose callback would exceed Telegram's 64-byte `callback_data` cap are skipped).
- `/platform [name]` – per-chat session placement for new sessions: an outpost pool name or a hosted platform label, passed as `platform` to the v3 create-session call (`/platform default` resets to the org default; v1 deployments warn-and-ignore). Also surfaced as the 🖥 Platform row in `/settings`, whose submenu lists every platform label + outpost pool the org accepts — enumerated by `DevinClient.platforms()` by parsing the create-session 400 body once (same trick as `devin_modes()`'s 422 probe).
  - `/platform local` (prototype) routes new sessions to a **local-CLI
    backend** (`app/local.py`): each session is a `devin acp` subprocess on
    the bridge host speaking ACP JSON-RPC over stdio (`initialize` +
    `session/new`, prompts via `session/prompt`, replies collected from
    `agent_message_chunk` updates; buffered chunks are flushed into the
    topic every ~20s mid-turn (paragraph-complete chunks only — emission
    waits for a blank line or the buffered text to exceed 3072 chars, the
    trailing partial paragraph stays buffered, and a flush inside an
    unclosed code fence is deferred — so each interim event is a coherent
    message and markers/fences are never split), with the remainder
    emitted at stopReason; `agent_thought_chunk` and `tool_call` updates
    set the session's activity, shown live as a `→ …` line in the edited
    ⏳ Working status message (cloud sessions get the same line from any
    non-enum `status_detail`). A thought line longer than the 120-char
    display cap keeps only its tail, and a cut that lands mid-word drops
    the partial token so the status never starts inside a word; the
    status message re-posts after each
    delivered reply so it stays the last message in the thread; acp
    stdout is read in chunks and split on
    newlines manually, so replies of any size survive — no line-length
    limit). Sessions
    persist in the CLI's own DB and are reloaded via `session/load` after a
    bridge restart (the in-memory map is only the live-process index; `/stop`
    still deletes the record), have no cloud URL, can't receive
    file attachments, and ignore the cloud session options — the CLI's own
    mode/model config applies instead. The service-user key is sent as a
    `/login` command on session start when set. Requires the Devin CLI
    (`DEVIN_LOCAL_CLI`/`DEVIN_LOCAL_CWD`).
  - On `platform=local` chats, `/mode` + the 🤖 Devin mode submenu
    enumerate the local ACP modes (accept-edits/smart/ask/plan/bypass,
    probed via a throwaway `devin acp` and cached 5 min) and apply live via
    `session/set_mode` (reset maps to the CLI default `accept-edits`);
    the submenu and `/mode` listing show the CLI's display names and
    descriptions (`mode` configOption options, e.g. `Smart — auto-approves
    safe actions`).
    `/model` + a 🧠 Model settings row enumerate the values the ACP `model` config option accepts (`session/new` configOptions), paged 30 per screen (like every long option submenu) — `devin models list` advertises families the account may not be able to select
    and apply via `session/set_config_option configId=model`, persisted in
    `conversation_settings.local_model` and sent at session create.
    `/think` + a 💭 Thinking settings row do the same for the
    `thought_level` configOption (`conversation_settings.thought_level`).
    `/commands` browses the CLI's `available_commands_update` slash-command
    list as a category → paged-command inline keyboard (`cmd:` callback
    namespace; `cmd:r:<name>` sends `/<name>` to the active local session);
    any `/foo` message that isn't a bridge command is forwarded to a local
    session as a prompt, so every CLI command and skill (`/fast`, `/compact`,
    `/loop`, `/<plugin>:<skill>`) works from Telegram. `/status` shows the
    session's live mode/model/thinking (`config_option_update` +
    `current_mode_update` tracked per session, rendered as
    `local: mode X · model Y · thought_level Z`).
- `/mode [name]` – per-chat Devin mode for new sessions (`/mode default` resets). No-arg prints the current mode plus `Available: …` from `DevinClient.devin_modes()`; a name not in that list is rejected with `Unknown mode` (empty probe = accepted, Devin arbitrates). Same option list as the /settings 🤖 Devin mode submenu.
- `/acu [n]` – per-chat `max_acu_limit` for new sessions (`/acu default` resets). `/settings` ⚡ ACU limit submenu offers preset values.
- `/tags [a,b]` – extra tags merged into `tags` on create (`telegram-bridge` is always included; `/tags clear` resets).
- `/secrets [KEY,KEY2]` – per-chat secrets for new sessions, given as secret *keys*; resolved to IDs via `DevinClient.secrets()` (`GET /v3/organizations/{org}/secrets`, cached 5 min) and stored as `secret_ids`. `/settings` 🔑 Secrets submenu toggles them (✓ marks selected; ids whose callback would exceed the 64-byte `callback_data` cap are skipped). Unknown keys are rejected; `/secrets clear` resets.
- `/knowledge [id,id]` – per-chat `knowledge_ids` for new sessions (free-text IDs; no list endpoint exists, so no validation or submenu; `/knowledge clear` resets).
- `/snapshot [id]` – per-chat `snapshot_id` (environment snapshot) for new sessions (same free-text caveat; `/snapshot clear` resets).
- `unlisted` / `idempotent` – tri-state (inherit/on/off) per-chat flags toggled in `/settings` (👁 Unlisted, 🔁 Idempotent rows), passed through to create-session when set.
- `/crawl` – show which site crawlers are active (read-only). The active set is bridge-wide: `CRAWL_SITES` env is the default, and the `/settings` 🔎 Pre-crawl submenu overrides it live via the `settings` table key `crawl_sites` (a csv; empty = off; deleting the key reverts to env; toggles are admin-only — `TELEGRAM_ADMIN_USER_IDS`, falling back to all allowed users when unset). When enabled, URLs in a user message matching an enabled crawler (`app/crawlers.py`: `instagram` via the oEmbed API — caption + cover image; `article` — generic HTML title/description/body, preferring `<article>`/`<main>`) are fetched in the bridge before the turn reaches Devin: extracted text is appended to the prompt as `[Crawled <site>: <url>]` blocks and media is uploaded as session attachments. Up to 3 crawler-owned URLs per message; fetches stream with a body cap, validate the host on every redirect hop, and reject loopback/private IP literals; failures are skipped silently.
- `/whoami` – user id, chat id, thread id, allowed yes/no, is-home yes/no.
- `/sethome` – store chat/thread as home (allowed users only). Reply confirms.
- `/commands` – category → command browser for local CLI slash commands (tap-to-run needs an active local session; browsing works on any chat).
- `/think [level]` – per-chat `thought_level` for local sessions (`/think default` resets; applied to the running session via `session/set_config_option`).
- Unknown `/cmd` – forwarded to the active local session as a CLI slash command when the conversation is local (👀 ack + watcher), else "Unknown command; /help".

### `/notify` (notify.py)
`POST /notify` with `Authorization: Bearer <NOTIFY_SECRET>` (403 otherwise; if `NOTIFY_SECRET` unset, route returns 404). JSON `{text: str, chat_id?: int, thread_id?: int, silent?: bool, markdown?: bool=true, html?: str, html_name?: str="report.html"}`. `html` is stored as a report and linked with an `Open` URL button when `PUBLIC_BASE_URL` is set, otherwise sent as a document. Target = provided chat or `settings.home_*` or `TELEGRAM_HOME_CHANNEL` env; 400 if none. Chunk + format like normal messages. Returns `{sent: N}`.

### Env (config.py) – add:
`TELEGRAM_ALLOWED_USERS` (csv ints), `TELEGRAM_ALLOW_ALL_USERS` (bool, default false), `TELEGRAM_FREE_RESPONSE_CHATS` (csv), `TELEGRAM_HOME_CHANNEL` (int|None), `TELEGRAM_NOTIFICATION_MODE` (`all|important`, default `important`), `TELEGRAM_RICH_MESSAGES` (bool, default true), `TELEGRAM_DRAFTS` (bool, default false), `NOTIFY_SECRET` (str|None), `DEVIN_WATCH_TIMEOUT_SECONDS` (1800), `DEVIN_POLL_SECONDS` (3), `BOT_USERNAME` (optional; if unset call `getMe` at startup). Keep existing ones; `DEVIN_REPLY_TIMEOUT_SECONDS` is removed.
Existing deploy has `TELEGRAM_ALLOWED_CHAT_IDS` unset; keep supporting it.

### Tests (pytest, httpx.MockTransport – no new runtime deps; add `requirements-dev.txt` with pytest + pytest-asyncio + anyio)
- formatting: bold/italic/code/fence/link conversion; escaping of reserved chars; chunking never splits a fence and adds (i/N); `extract_options` happy path + ignore when >8 or absent.
- access: allowlist deny/allow, allow_all, group mention gating, bot messages ignored.
- store: conv_key for forum topic vs DM; dedupe of update_id; history + resume.
- webhook e2e with mocked Devin+Telegram transports: (a) new DM text -> create_session called with preamble, watcher delivers two devin_messages in order with 👀 then 👍 reaction; (b) message with `OPTIONS:` line renders inline keyboard and callback sends the option; (c) duplicate update_id ignored; (d) `/new`, `/sessions`, `/resume`, `/status`; (e) `/notify` auth + delivery; (f) photo upload path calls `/v1/attachments` and prepends URL.
- Make watcher poll interval injectable (0 in tests).

### Non-goals (explicitly skipped)
Network IP failover, stickers/vision, TTS, /model, /memory, /goal, kanban, streaming edits of partial cloud text (the cloud Devin API has no partial-message stream; local sessions do stream interim lines — see below).

### Bot API 10.3 behavior
Rich Markdown is the primary Devin response path when `TELEGRAM_RICH_MESSAGES`
is enabled; 4xx errors fall back to MarkdownV2 and unknown-method errors
disable rich delivery for the process. `TELEGRAM_DRAFTS` sends empty private
chat drafts while Devin works and falls back to typing on rejection. Implicit
topics are renamed from the first user message. Option buttons include
`disabled: {}` after selection, while `/stop` uses `danger` and `primary`
button styles. Group command replies support ephemeral delivery and retry
normally when Telegram rejects the ephemeral parameters.
