# Roadmap

Rough direction, not commitments — open an issue to discuss anything here.

## Recently shipped

- Release channels for self-update (`main`, `stable`, `vN`, `vN.N`, `vN.N.N`)
  with semver tags — see [docs/versioning.md](docs/versioning.md)
- Voice transcription backends: Whisper API, faster-whisper, whisper.cpp,
  external command, bounded Docker sidecar (Moonshine)
- Rich Telegram replies: `OPTIONS:` buttons, PR cards, image/document delivery
- Access control: allowlists, admin approval flow, free-response chats
- CI (ruff, pytest matrix, docker build, shellcheck, dep audit) and CodeQL

## Next

- **Group `/help` and onboarding polish** — categorized command help, better
  `/start` for first-time and denied users
- **Backup/restore commands** — SQLite backup via `/admin` and a `db_check`
  diagnostic
- **Setup wizard** — `python -m app.setup` interactive `.env` writer
- **Metrics** — structured logging and counters for messages, sessions,
  watcher activity
- **Docker Compose / one-click deploys** beyond Fly.io

## Later (scaling)

- **Split `app/main.py`** — it's the dumping ground; extract watcher, message
  pipeline, and access-control modules
- **Persistent work queue** — today's in-memory debounce/queue means a restart
  drops a few seconds of buffered input; Redis or SQLite-backed queue would
  survive restarts and unlock multi-worker
- **Horizontal scaling** — single-process + SQLite is a deliberate constraint
  for self-hosting; a Redis broker is the documented upgrade path when it
  ever becomes the bottleneck
