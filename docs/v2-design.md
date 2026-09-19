# Bridge v2 design (lead-authored; implementation brief)

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
  - `pending_choices(choice_id TEXT PK, conv_key, session_id, option_text, created_at)` – inline-keyboard callbacks (`callback_data` <= 64 bytes, so store a short random id).
  - `settings(key TEXT PK, value TEXT)` – `home_chat_id`, `home_thread_id` set by `/sethome`.
  - conv_key = `f"{chat_id}"` for DMs/plain groups, `f"{chat_id}:{message_thread_id}"` for forum topics (only when `chat.is_forum` and `message_thread_id` present).
- `devin.py` – `DevinClient` (v1 only): `create_session(prompt, title, playbook_id=None) -> (session_id, url)`, `send_message`, `get_session -> SessionState(status_enum, title, pr_url, messages)`, `list_playbooks`, `terminate`, `upload_attachment(filename, bytes, content_type) -> url`.
- `telegram.py` – `TelegramClient`: `send_message(chat_id, text, *, thread_id=None, reply_to=None, parse_mode=None, reply_markup=None, disable_notification=False, disable_web_page_preview=True)`, `edit_message_text`, `edit_message_reply_markup`, `send_chat_action(typing)`, `set_message_reaction(chat_id, message_id, emoji|None)`, `answer_callback_query`, `get_file`+`download_file(file_path) -> bytes`, `set_my_commands`, `set_webhook`. Handles 429 by reading `parameters.retry_after`, sleeping, retrying once. On 400 with parse_mode set, retry as plain text.
- `formatting.py` – `markdown_to_telegram_markdown_v2(text) -> str` and `chunk(text, limit=4096) -> list[str]` (never split inside a fenced code block; if a block itself exceeds the limit, close and reopen the fence; append ` (i/N)` suffix when N>1). Escape all MarkdownV2 reserved chars outside entities: `_ * [ ] ( ) ~ \` > # + - = | { } . !`. Support: `**bold**`/`__bold__`->`*bold*`, `*em*`/`_em_`->`_em_`, `~~s~~`->`~s~`, inline code, fenced code (```lang), `[text](url)`, headings `#..` -> bold line, `> quote` -> `>quote`, bullet `- ` -> `• `. Also `extract_options(text) -> (text_without_line, list[str])`: if the last non-empty line matches `^OPTIONS:\s*(.+)$` split on `|`, strip; max 8 options, each <= 60 chars.
- `access.py` – `is_allowed(update_message) -> bool` and `should_respond_in_group(message, bot_username) -> bool`.
- `commands.py` – slash command handlers (below).
- `watcher.py` – `SessionWatcher` (below).
- `notify.py` – `POST /notify` route.
- `main.py` – app wiring, webhook route, `/health`.
- `set_webhook.py` – also calls `set_my_commands` with the command list.

### Inbound flow (webhook)
1. Verify secret header (403 otherwise). Parse `update_id`; if already in `processed_updates` return `{accepted:true}`; else insert.
2. Route: `callback_query` -> `handle_callback`; `message` or `channel_post` with `text`/`caption`/`photo`/`document` -> `handle_message`; anything else ignored.
3. Access (`access.py`):
   - if `TELEGRAM_ALLOW_ALL_USERS` false: `from.id` must be in `TELEGRAM_ALLOWED_USERS` (if that list is non-empty) AND, if `TELEGRAM_ALLOWED_CHAT_IDS` non-empty, `chat.id` must be in it. Empty both lists + allow_all false => deny everything with a one-time reply "This bot is private. Your user id is N." (so onboarding is easy). Log `Rejected ... user=%s chat=%s`.
   - Groups/supergroups: respond only if text mentions `@<bot_username>` (strip the mention), or the message is a reply to a bot message, or `chat.id` in `TELEGRAM_FREE_RESPONSE_CHATS`. Otherwise ignore silently.
   - Ignore messages where `from.is_bot`.
4. Slash command (`text` starts with `/`) -> `commands.py`. Otherwise -> `handle_user_turn`.

### `handle_user_turn(conv, message)`
- React 👀 on the user's message (`set_message_reaction`), start typing.
- If message has `photo` (largest size) or `document` (<= 20 MB, Telegram getFile limit): download, `upload_attachment`, and prepend `Attached file: <url>` (+ original filename) to the text/caption.
- If conversation has no active session (or its `status_enum` is `expired`/`finished` -> auto-create new): `create_session(prompt=SYSTEM_PREAMBLE + text, title=f"Telegram: {first 60 chars}")`, insert into `conversations` + `session_history`, reply with `Started session: <url>` (silent).
  - `SYSTEM_PREAMBLE` (constant in `commands.py`): "You are chatting with a user over Telegram via a bridge. Keep replies concise. Telegram renders Markdown. When you need the user to pick between a small set of options, end your message with one line exactly like `OPTIONS: first option | second option | third option` (max 8, each under 60 chars); the bridge turns it into buttons. Never ask the user to open a UI; they only see your messages.\n\nUser message: "
- Else `send_message(session_id, text)`.
- Ensure a `SessionWatcher` is running for that session (idempotent registry keyed by session_id).

### `SessionWatcher.run()`
Loop every `DEVIN_POLL_SECONDS` (default 3):
- `state = get_session()`. For each `devin_message` newer than `conversations.last_event_id` (walk in order; "newer" = appears after the stored id in the list; if stored id is None, only messages with timestamp >= watcher start time - 5s to avoid replaying history on `/resume`): render via formatting, `extract_options`; send chunks (`disable_notification = mode=="important" and state.status_enum == "working"`); for the last chunk attach an inline keyboard if options (one button per row; `callback_data = choice_id`, store `pending_choices`). Update `last_event_id` after each send.
- Refresh typing every loop while `working`.
- Stop when `status_enum in {"blocked","finished","expired"}` and no unsent messages: react 👍 on the triggering user message (👎 if we hit an exception / `expired`), and if `pr_url` newly appeared send `PR: <url>`. Clear reaction of 👀 by setting 👍.
- On timeout: send "Still working — I'll keep you posted" is NOT sent; just stop watching silently (a later user message restarts the watcher). Actually: send one silent notice "Devin is still working; I'll deliver replies when you next message." only if nothing was delivered during the watch.
- Any exception: log, send "Couldn't reach Devin: <short reason>" once, stop.

### Callback (`handle_callback`)
- Authorization: same `is_allowed` on `callback_query.from`.
- Look up `pending_choices[data]`. Wrong chat/conv -> `answer_callback_query("This choice expired")`. If the row exists and the session is still current -> `send_message(session_id, option_text)`, delete the other choices for that conv, `edit_message_text` to `✅ <option_text>` (plain `edit_message_reply_markup` fallback), `answer_callback_query`, start watcher. If the row is missing or its session was superseded, recover the button label from the message's `reply_markup` (or use the stored `option_text`) and forward it as a normal user turn — sending to the current session or creating a new one; `__cmd:` options and command-button labels (Terminate/Cancel) still expire.

### Commands (`commands.py`) – reply in the same conv/thread
- `/start`, `/help` – list commands.
- `/new [title]` – archive current conv mapping (keep history), create a session with prompt = preamble + ("Hello" if no text after) — actually: if title given, use it as `title` and prompt "The user started a new conversation titled '<title>'. Greet briefly and wait." else prompt "The user started a new conversation. Greet briefly and wait." Reply `Started session: <url>`.
- `/sessions` – last 10 from `session_history` for this conv, numbered with title + status (fetch status for each, tolerate errors), mark active with `*`.
- `/resume <n>` – set that history entry as active (`last_event_id` = id of its latest devin_message so no replay), reply `Resumed: <title> <url>`.
- `/status` – active session: title, `status_enum`, url, pr url.
- `/stop` – terminate active session via `DELETE` (confirm with an inline keyboard "Terminate | Cancel" using the same `pending_choices` mechanism but with `option_text` starting `__cmd:terminate:<session_id>`; handle_callback must special-case `__cmd:` prefixes). After terminate, clear active mapping.
- `/playbook` – list playbooks (numbered, title only); `/playbook <n> [text]` – create session with that `playbook_id`, prompt = preamble + (text or "Run this playbook."), becomes active.
- `/retry` – resend the last user text stored on the conversation (add column `last_user_text`).
- `/whoami` – user id, chat id, thread id, allowed yes/no, is-home yes/no.
- `/sethome` – store chat/thread as home (allowed users only). Reply confirms.
- Unknown `/cmd` – "Unknown command; /help".

### `/notify` (notify.py)
`POST /notify` with `Authorization: Bearer <NOTIFY_SECRET>` (403 otherwise; if `NOTIFY_SECRET` unset, route returns 404). JSON `{text: str, chat_id?: int, thread_id?: int, silent?: bool, markdown?: bool=true}`. Target = provided chat or `settings.home_*` or `TELEGRAM_HOME_CHANNEL` env; 400 if none. Chunk + format like normal messages. Returns `{sent: N}`.

### Env (config.py) – add:
`TELEGRAM_ALLOWED_USERS` (csv ints), `TELEGRAM_ALLOW_ALL_USERS` (bool, default false), `TELEGRAM_FREE_RESPONSE_CHATS` (csv), `TELEGRAM_HOME_CHANNEL` (int|None), `TELEGRAM_NOTIFICATION_MODE` (`all|important`, default `important`), `NOTIFY_SECRET` (str|None), `DEVIN_WATCH_TIMEOUT_SECONDS` (1800), `DEVIN_POLL_SECONDS` (3), `BOT_USERNAME` (optional; if unset call `getMe` at startup). Keep existing ones; `DEVIN_REPLY_TIMEOUT_SECONDS` is removed.
Existing deploy has `TELEGRAM_ALLOWED_CHAT_IDS` unset; keep supporting it.

### Tests (pytest, httpx.MockTransport – no new runtime deps; add `requirements-dev.txt` with pytest + pytest-asyncio + anyio)
- formatting: bold/italic/code/fence/link conversion; escaping of reserved chars; chunking never splits a fence and adds (i/N); `extract_options` happy path + ignore when >8 or absent.
- access: allowlist deny/allow, allow_all, group mention gating, bot messages ignored.
- store: conv_key for forum topic vs DM; dedupe of update_id; history + resume.
- webhook e2e with mocked Devin+Telegram transports: (a) new DM text -> create_session called with preamble, watcher delivers two devin_messages in order with 👀 then 👍 reaction; (b) message with `OPTIONS:` line renders inline keyboard and callback sends the option; (c) duplicate update_id ignored; (d) `/new`, `/sessions`, `/resume`, `/status`; (e) `/notify` auth + delivery; (f) photo upload path calls `/v1/attachments` and prepends URL.
- Make watcher poll interval injectable (0 in tests).

### Non-goals (explicitly skipped)
Network IP failover, stickers/vision, TTS, /model, /memory, /goal, kanban, streaming edits of partial text (Devin API has no partial-message stream).
