# AGENTS.md — orientation for coding agents

A FastAPI bridge: Telegram chat messages become Devin sessions, Devin replies
stream back to Telegram. Single process; SQLite persistence; deploys by
self-updating from `main` (a host cron pulls every ~15 min — merging a PR IS
the deploy).

## Ground truth

- `docs/v2-design.md` — the canonical behavior + API reference. If you change
  behavior, update it in the same PR (the contribution guide requires this).
- `CONTRIBUTING.md` — human setup, PR style, release pointers.
- `CHANGELOG.md` — user-visible changes go under `Unreleased`.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. .venv/bin/pytest -q      # ~75s, ~375 tests, httpx.MockTransport — no live APIs
.venv/bin/ruff check app/ tests/
```

## Module map (app/)

- `clients.py` — `DevinClient` (v3 API; `devin.py` re-exports it) +
  `TelegramClient` (`telegram.py` re-exports). Rich-message send, attachments,
  sessions, consumption, reactions.
- `formatting.py` — MarkdownV2 conversion (`markdown_to_telegram_markdown_v2`),
  `chunk` (4096 splitting, fence+quote safe), marker extraction
  (`extract_options`, `extract_controls`, `parse_rich_segments`,
  `has_expandable_quote`, `normalize_rich_linebreaks`).
- `commands.py` — slash commands, `_help_text`, `SYSTEM_PREAMBLE`.
- `watcher.py` — `SessionWatcher`: streams Devin replies into Telegram, applies
  markers, options buttons, attachments, reactions.
- `main.py` — app wiring + webhook route. `store.py` — SQLite (conversations,
  settings incl. per-chat `silent`/`devin_mode`/`repos`). `access.py` — user
  allowlist. `notify.py`, `admin.py`, `doctor.py`, `poll.py`, `polling.py`,
  `transcription.py`, `images.py` — named feature areas.

## Invariants that bite if you miss them

- **Marker surface**: a reply marker is only real when it is stripped in
  `extract_controls` or parsed by `parse_rich_segments`, taught in
  `SYSTEM_PREAMBLE`, documented in `docs/v2-design.md` and the CHANGELOG.
  `test_preamble_documents_every_marker` fails if the preamble omits one.
- **Quoted preamble**: `docs/v2-design.md` embeds `SYSTEM_PREAMBLE` verbatim —
  update both together.
- **MarkdownV2 expandable quotes**: every continuation line needs a `>` prefix;
  a bare line ends the quote and a trailing `||` then parses as an unclosed
  spoiler → Telegram rejects the whole message. `sendRichMessage` markdown
  cannot render `**>` — `has_expandable_quote` replies bypass the rich path.
- **Marker awareness**: controls inside ``` fences or `DETAILS:`/`TABLE:` bodies
  must not trigger — the parsers are fence-aware; keep it that way.
- **`callback_data` ≤ 64 bytes**; 4096-char message cap (`chunk` reopens fences
  and quotes at boundaries); file upload limit 20 MB (Telegram `getFile`).
- **Reactions**: Telegram accepts a fixed emoji set — `react()` degrades
  off-set emojis to 👍.
- **v1 fallback**: the client picks its API version at init — v3 when the
  service-key + org settings exist (`v3_enabled`), v1 when they don't (no
  per-request retry). v3-only features (modes, repos, `status_detail`,
  blocks) must degrade — check `v3_enabled`/`rich_enabled` gating before
  assuming v3.

## Style

async/await everywhere; type hints; pydantic settings for new env vars (also
`.env.example` + `docs/configuration.md`); one fix/feature per PR; short
imperative commits (`fix:`/`feat:`/`docs:`). Minimal diffs — reuse helpers
before adding new ones.
