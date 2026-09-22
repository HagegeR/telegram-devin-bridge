---
name: testing-bridge-live-smoke
description: Live end-to-end smoke test of the telegram-devin-bridge FastAPI app without production credentials — real uvicorn + injected MockTransport clients.
---

# Live smoke harness for the bridge

Use this when pytest is not enough and you need the real HTTP app running
(webhook endpoints, lifespan startup/shutdown, watchers, janitor) but cannot or
should not touch production Telegram/Devin.

## Key facts

- `import app.main` runs a module-level `create_app()` → `Settings()` (reads
  shell env AND `.env`) and opens `DATABASE_PATH` (default `./bridge.sqlite3`).
  BEFORE importing, hard-assign `os.environ[...] = ...` — NOT `setdefault`:
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `DEVIN_API_KEY`,
  `PUBLIC_BASE_URL` get dummies and `DATABASE_PATH` gets an absolute temp path.
  On a deployment checkout real shell/`.env` values otherwise survive, and the
  "fake" run would open the production DB and hand live creds to `/doctor`.
- `create_app(settings, store=, devin=, telegram=)` accepts injected clients.
  `TelegramClient(bot_token, base_url=..., transport=httpx.MockTransport(h))`
  and `DevinClient(key, base_url, max_acu, transport=...)` fake ALL outbound
  calls. `DevinClient.public_client` (GitHub PR fetches) shares the same
  transport — intercept `request.url.host == "api.github.com"`.
- Inject `rich_enabled=settings.telegram_rich_messages` on the TelegramClient
  yourself — the settings flag only applies when `create_app` builds the client.
  With rich on, text sends go to `/sendRichMessage` under
  `body["rich_message"]["markdown"]`, not `body["text"]`.
- Run `uvicorn.Server(uvicorn.Config(app, ...))` as an asyncio task in the same
  process: you get a real socket server AND can introspect
  `app.state.bridge` (janitor task, `_transcription_client`, in-memory dicts).
  `server.should_exit = True` drives the real lifespan shutdown.
- `startup()` calls `telegram.get_me()` — unreachable/bad token crashes boot
  (raises after transport retries). Patch `app.clients.RETRY_BACKOFF` to
  `(0.01,)*4` to test the failure quickly.
- Fake `getMe` must return `{"ok":true,"result":{"username":"testbot",...}}`;
  `configure_bot` also calls setMyCommands×3/setMyDescription/
  setMyShortDescription at startup — stub them ok. It does NOT call setWebhook.
- Webhook updates are dispatched in background tasks — poll fake state with a
  `wait_for(pred)` helper instead of sleeping.
- Session correlation: record `(prompt, session_id)` from `POST /v1/sessions`
  responses; earlier test traffic also consumes session numbering, never assume
  `sess-N` ordering.
- Devin message shape: `{"type":"devin_message","event_id":"e1","message":...,
  "timestamp":"<epoch str>"}`. Watcher delivers only `devin_message` with
  event_id; `last_event_id` dedups across polls (verify no redelivery).
- Attachments: put `https://app.devin.ai/attachments/{s}/{f}` URLs in message
  text; the client rewrites to `{devin_base_url}/v1/attachments/{s}/{f}` so your
  fake Devin serves bytes even though the URL says app.devin.ai.
- Telegram 400 fallbacks: drive them via fake 400s on marker text. Use markers
  WITHOUT MarkdownV2 special chars (no `_`, `*`, `#`, `.`, `!`) — send_markdown
  escapes them ("MARKER_CNF" → "MARKER\_CNF"). Old code retried any 400 with
  parse_mode once; new code only retries "can't parse"/markup descriptions —
  assert send-attempt counts, not just outcomes. A permanently failing send
  kills the watcher (outer except → "⚠ Couldn't reach Devin" to the chat).
- Voice transcription (`backend=api`): `_transcription_client` is a real
  AsyncClient on `settings.transcription_base_url` — run a tiny stdlib
  `ThreadingHTTPServer` returning `{"text": ...}` for `/audio/transcriptions`.
  Voice file bytes come through the Telegram transport (getFile→`/file/bot.../`).
- Janitor interval is `app.main._JANITOR_INTERVAL_SECONDS` — monkeypatch to
  ~0.5s before startup and seed stale dicts to watch pruning live.
- `/admin` bad-auth triggers lockout+429 — do valid-bearer checks first.
- `/doctor` hits REAL api.telegram.org (getMe with settings token) and
  REAL devin_api_base_url — with hard-set dummy env vars those checks fail
  safely. Deliberately assigning real read-only creds makes them green while
  app traffic stays fake; only do that consciously, never by leftover env.
  `local health` checks port 8000 hardcoded in create_app's route registration
  — expect fail on a custom port.
- DB: `store.cleanup_*` run at startup; new `idx_*` indexes queryable via
  `sqlite3.connect("file:db?mode=ro", uri=True)` while app runs.

## Devin Secrets Needed

Optional: `TELEGRAM_DEVIN_BRIDGE_BOT_TOKEN` + `DEVIN_API_KEY_TELEGRAM_BRIDGE`
— only when you deliberately want `/doctor`'s real probes green (assign them
yourself; never rely on ambient shell env or `.env`). Dummy hard-assigns work
otherwise.
