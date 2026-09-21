---
name: testing-bridge-local-transcription
description: Test telegram-devin-bridge transcription subprocesses, Docker lifecycle, or admin settings locally without production credentials.
---

# Isolated runtime checks

1. Before importing `app.main`, supply dummy `TELEGRAM_BOT_TOKEN`,
   `DEVIN_API_KEY`, `DEVIN_SERVICE_USER_API_KEY`, `TELEGRAM_MODE=polling`, and
   an absolute temporary `DATABASE_PATH`. Import constructs the app immediately.
   Use `Settings(_env_file=None, ...)` for isolated settings checks.
2. For `/admin`, register the production `register_admin_route` on a local
   FastAPI app with a dummy bearer token and notification sink. Its temporary
   `.env` must contain the required dummy settings too: set-env validates the
   entire rewritten file, not just the changed field. Assert the HTTP response
   and reread the persisted value.
3. Build `deploy/moonshine` locally when Docker/network is available. Generate
   speech with espeak, convert to Ogg/Opus with ffmpeg, and feed it through
   `Bridge._transcribe`. Use an isolated Store and no live Telegram/Devin clients.
   A lightweight probe image can inspect WAV format and environment, but cannot
   establish speech-recognition accuracy.
4. Exercise timeout and task cancellation through Bridge dispatch. Observe a
   live named container before the event and assert it no longer exists after
   the call finishes. Killing the Docker CLI alone is not evidence the
   daemon-owned workload ended. Remove only identified test containers on failure.
5. For shared concurrency, record real child start/end timestamps and calculate
   overlap against the current configured limit. Use a single asyncio loop per
   harness because process-wide asyncio semaphores can bind after contention.

## Devin Secrets Needed

None for these local tests. Keep real credentials out of fixture files and logs.
Production Telegram delivery is a separate test requiring explicit authorization.
