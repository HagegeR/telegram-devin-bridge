# Configuration reference

Every environment variable, grouped by domain. All live in `.env` (see
`.env.example`); only `TELEGRAM_BOT_TOKEN` and `DEVIN_API_KEY` are always
required.

> **Pitfall:** delete empty optional lines instead of leaving `KEY=` — an empty
> string fails `int | None` parsing and pydantic refuses to start.

## Core

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | — | From @BotFather. Use a dedicated token — sharing one with another consumer conflicts. |
| `TELEGRAM_MODE` | no | `webhook` | `webhook` or `polling`. |
| `TELEGRAM_WEBHOOK_SECRET` | webhook | — | Random token (`openssl rand -hex 32`); verified via the `X-Telegram-Bot-Api-Secret-Token` header. |
| `PUBLIC_BASE_URL` | webhook | — | Public HTTPS base URL routed to port 8000. |
| `DATABASE_PATH` | no | `./bridge.sqlite3` | Use an absolute path in production; `/data/bridge.sqlite3` on Fly. |
| `BOT_USERNAME` | no | fetched from Telegram at startup | Set to skip the `getMe` call. |

## Devin

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `DEVIN_API_KEY` | yes | — | **v1** key (`apk_user_…` personal or `apk_…` service). A service-user token (`cog_…`) is NOT accepted by `/v1` (403). |
| `DEVIN_SERVICE_USER_API_KEY` | no | unset | `cog_…` service-user token; only used by `/usage`. |
| `DEVIN_ORG_ID` | no | unset | Required for `/usage`. |
| `DEVIN_API_BASE_URL` | no | `https://api.devin.ai` | |
| `DEVIN_MAX_ACU_LIMIT` | no | `3` | ACU cap passed to new sessions. |
| `DEVIN_SESSION_INSTRUCTIONS` | no | empty | Prepended verbatim to the prompt of every session the bridge starts — use it for org-specific guidance such as which injected secret grants Devin v3 API access for editing automations. |
| `DEVIN_POLL_FAST_SECONDS` | no | `1.0` | Watcher interval while Devin is actively replying. |
| `DEVIN_POLL_SECONDS` | no | `5` | Watcher interval otherwise. |
| `DEVIN_WATCH_TIMEOUT_SECONDS` | no | `1800` | Max watcher lifetime; a later user message restarts it. |
| `DEVIN_SETTLE_SECONDS` | no | `30` | Keeps a new watcher alive while the API still reports a stale non-active status after submit. |
| `DEVIN_STATUS_AFTER_SECONDS` | no | `8` | Delay before the bridge reports session status. |

`DEVIN_SESSION_INSTRUCTIONS` can also carry a reply style. A "product voice"
recipe that mirrors the bridge's own message chrome (◆/→/✓/⚠/ℹ/⏳/✅/💾):

```dotenv
DEVIN_SESSION_INSTRUCTIONS="Style every reply like a shipped product, not a chat log. While working, send short standalone progress messages: ◆ Phase to open a phase, → step for an in-flight action, ✓ result when a step lands (always with the concrete fact — ✓ 42 tests pass, ✓ PR opened, never ✓ done), ⚠ for warnings, ℹ for context the user needs. When you propose or save a self-improvement (skill, knowledge note, playbook, blueprint, saved secret), send a 💾 Self-improvement: <what changed and where to review it> message. Final answers: verdict first in 1-3 sentences, then details, then full-URL links. Failures get ⚠ or ❌ plus the next step or an OPTIONS: line. Numbers and paths beat adjectives. No filler openers (Let me, Great question, I will help you)."
```

## Telegram behavior

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `TELEGRAM_DEBOUNCE_SECONDS` | no | `1.5` | Rapid text/file messages are debounced per chat/topic and joined into one Devin turn. |
| `TELEGRAM_QUEUE_WHILE_BUSY` | no | `true` | Later turns queue and drain in order after the session finishes; `/status` shows the count. |
| `TELEGRAM_LONG_REPLY_CHARS` | no | `3500` | Replies longer than this paginate with a `Show more` button; very large replies become `reply.md`. |
| `TELEGRAM_RATE_LIMIT_PER_MINUTE` | no | `20` | Per-user sliding-window limit (`0` disables); sends at most one warning per minute. |
| `TELEGRAM_RICH_MESSAGES` | no | `true` | Bot API 10.3 rich Markdown for Devin replies; falls back to MarkdownV2 + 4096-char chunking. |
| `TELEGRAM_DRAFTS` | no | `false` | Private-chat drafts while Devin works; falls back to typing if rejected. Groups always get typing. |
| `TELEGRAM_IMAGES_AS_DOCUMENTS` | no | `auto` | `auto` = `sendPhoto` when the image fits unchanged (≤1280 px longest side); `true` = always `sendDocument`; `false` = always `sendPhoto`. |
| `TELEGRAM_ATTACH_VOICE` | no | `false` | Keep the original audio attached alongside the transcript. |
| `TELEGRAM_NOTIFICATION_MODE` | no | `important` | `important` delivers intermediate replies silently while Devin is working. |
| `TELEGRAM_HOME_CHANNEL` | no | unset | Fallback target for `/notify` (the `/sethome` chat wins if set). |
| `TELEGRAM_FREE_RESPONSE_CHATS` | no | empty | Group chats the bot answers in without a mention or reply. |

## Access control

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `TELEGRAM_ALLOWED_USERS` | no | empty | `id` or `id:label` entries, e.g. `123:Ruben,456:Sarah`; labels show in `/users`. |
| `TELEGRAM_ALLOWED_CHAT_IDS` | no | empty | |
| `TELEGRAM_ALLOW_ALL_USERS` | no | `false` | |
| `TELEGRAM_ADMIN_USER_IDS` | no | falls back to `TELEGRAM_ALLOWED_USERS` | Can approve access requests, `/users`/`/revoke`, and `/update`. |

With allow-all disabled, an empty user/chat allowlist denies access and sends
the requester their user ID once for onboarding; admins approve via `/users`.
Group and supergroup messages must mention `@BOT_USERNAME`, reply to a bot
message, or come from a `TELEGRAM_FREE_RESPONSE_CHATS` chat.

## Transcription

Voice, audio, and video-note messages are transcribed when a backend is
configured; otherwise they arrive as attachments. `TRANSCRIPTION_API_KEY`
enables the `api` backend (any OpenAI-compatible `/v1` endpoint via
`TRANSCRIPTION_BASE_URL`).

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `TRANSCRIPTION_API_KEY` | `api` backend | unset | |
| `TRANSCRIPTION_BASE_URL` | no | `https://api.openai.com/v1` | |
| `TRANSCRIPTION_BACKEND` | no | `api` | `api` / `local` / `whispercpp` / `command` / `docker` |
| `TRANSCRIPTION_MODEL` | no | `whisper-1` | `api` model, or the faster-whisper size for `local`. |
| `TRANSCRIPTION_LANGUAGE` | no | auto-detect | Force a language, e.g. `en`. Per-user override: `/lang`. |
| `TRANSCRIPTION_COMMAND` | `command` | unset | Any argv; root-only, not settable via the admin API. |
| `TRANSCRIPTION_DOCKER_IMAGE` | `docker` | unset | Validated image reference; root-only (it picks the code that runs). |
| `TRANSCRIPTION_DOCKER_MEMORY` | no | `400m` | Admin-settable. |
| `WHISPER_CPP_BIN` | no | `whisper-cli` | |
| `WHISPER_CPP_MODEL` | no | `/opt/whisper.cpp/models/ggml-base.en.bin` | |
| `WHISPER_CPP_FAST` | no | `true` | Greedy decoding + audio context sized to the clip (~2.5× faster on CPU); `false` for max accuracy. |
| `WHISPER_CPP_EXTRA_ARGS` | no | unset | Extra flags; `-f`, `-m`, `-o*` are rejected. |

### Backends

- **`api`** — any OpenAI-compatible transcription endpoint; needs `TRANSCRIPTION_API_KEY`.
- **`local`** — key-free faster-whisper: `pip install -r requirements-transcription.txt`.
  Models `tiny` (~75 MB), `base` (~150 MB, recommended on CPU), `small`
  (~500 MB), `medium`, `large-v3`; the first request downloads the model.
- **`whispercpp`** — for musl/Alpine hosts where faster-whisper has no wheels.
  Build it on the host:

  ```bash
  git clone https://github.com/ggml-org/whisper.cpp /opt/whisper.cpp
  cd /opt/whisper.cpp
  cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j2 --target whisper-cli
  sh models/download-ggml-model.sh base.en
  ```

  Requires `ffmpeg`; set `WHISPER_CPP_BIN=/opt/whisper.cpp/build/bin/whisper-cli`.
- **`command`** — runs `TRANSCRIPTION_COMMAND` per request: the bridge converts
  the clip to 16 kHz mono WAV, pipes it on stdin, reads the transcript from
  stdout, and exports `TRANSCRIPTION_LANGUAGE` into the child's environment.
- **`docker`** — the bounded form of `command`: fixed
  `docker run --rm -i --pull never --network none --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --env TRANSCRIPTION_LANGUAGE --memory <MEM> --name <NAME> <IMAGE>`
  argv, with `docker rm -f <NAME>` on timeout or failure so a stuck container
  never outlives the request. See `deploy/moonshine/` for a sidecar
  (Moonshine, English-only, zero RAM while idle):
  `TRANSCRIPTION_BACKEND=docker` + `TRANSCRIPTION_DOCKER_IMAGE=moonshine-asr`.

## Admin, notifications, and integrations

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `NOTIFY_SECRET` | no | unset (`/notify` disabled) | See [operations.md](operations.md#notify). |
| `DOCTOR_SECRET` | no | unset (`/doctor` disabled) | Separate from `NOTIFY_SECRET`; 30 s cooldown between runs. |
| `ADMIN_SECRET` | no | unset (`/admin` disabled) | See [operations.md](operations.md#admin-api). |
| `ADMIN_ENV_ALLOWLIST` | no | built-in non-secret key list | Keys settable via `set-env`. Tokens/keys/secrets are never readable or writable. |
| `ADMIN_LOG_PATH` | no | `/var/log/telegram-devin-bridge.log` | Log file tailed by the `logs` action. |
| `ADMIN_RESTART_COMMAND` | no | OpenRC `rc-service` restart | Command run by the `restart` action. |
| `SELF_UPDATE_COMMAND` | no | `sh deploy/self-update.sh` | Command run by `/update` and the admin `update` action. |
| `SELF_UPDATE_BRANCH` | no | `main` | Branch the self-updater tracks. |
| `GITHUB_TOKEN` | no | unset | Enables richer GitHub metadata (e.g. PR cards). |

## Message and media behavior

- Telegram hidden `text_link` entities are expanded to `visible text (URL)`
  before the prompt is sent, including links following emoji.
- A final `OPTIONS: one | two` line becomes inline buttons (≤ 8 options,
  ≤ 60 chars each); the picked choice is marked with a disabled button. `/stop`
  confirmations use the Bot API 10.3 danger/primary styles. Unknown
  rich-message methods latch rich delivery off for the process.
- Large fenced code blocks are extracted as `snippet-*` documents.
- Reply and forward context is included in prompts.
- Forum and private-chat topics use independent sessions whenever Telegram
  provides `message_thread_id` with either `chat.is_forum` or
  `is_topic_message`; ordinary DMs and groups use the chat ID.
  `message_thread_id` is preserved for replies and notifications. The first
  message in an implicitly named topic renames it from its first line.
- `/help`, `/sessions`, `/status`, `/whoami` use ephemeral group replies when
  Telegram accepts them, retrying as normal messages if not.
- Polling mode calls `deleteWebhook` without dropping pending updates, then
  uses 50-second long polling.
- Retries: connection failures to the Telegram and Devin APIs are retried 5
  times with exponential backoff (~15 s total); mid-flight failures retry only
  idempotent GETs so mutations are never duplicated. Longer outages drop the
  reply and log `Failed to process Telegram update`.
