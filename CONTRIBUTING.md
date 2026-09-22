# Contributing

Thanks for improving the Telegram–Devin Bridge. This document covers the
development setup and the expectations for pull requests.

## Setup

```bash
python -m venv .venv        # Python >= 3.12
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Copy `.env.example` to `.env` for local runs; never commit `.env` or any real
tokens, API keys, or user/chat identifiers.

## Checks before opening a PR

```bash
pytest          # test suite (httpx.MockTransport, no live Telegram/Devin needed)
ruff check .    # lint
```

Add or update tests when you change behavior — the suite mocks both the
Telegram and Devin transports, so new command, watcher, or endpoint paths are
cheap to cover.

## Making changes

- Branch from `main` and open a PR — deployments track `main` and self-update
  from it, so everything lands through review.
- Keep changes focused: one fix or feature per PR.
- Match the existing style: async/await throughout, type hints, pydantic
  settings for new env vars (add them to `.env.example` and
  [docs/configuration.md](docs/configuration.md)).
- New bot commands go in `app/commands.py` (handler + `_help_text()` + the
  README commands table); new endpoints are registered alongside the existing
  ones in `app/main.py`, `app/notify.py`, `app/admin.py`, or `app/doctor.py`.
- Update the docs that cover what you touched — the README only summarizes;
  detailed reference lives in `docs/`.

## Commit and PR style

- Short imperative commit subjects (`fix:`, `docs:`, `feat:` prefixes are
  used but not enforced).
- Fill in the PR template — what changed, why, and how you verified.
- CI runs CodeQL and a Python analysis workflow; keep them green.

## Reporting bugs and proposing features

Use the issue templates — [bug report](../../issues/new?template=bug_report.yml)
or [feature request](../../issues/new?template=feature_request.yml). Include
the deployment mode (webhook or polling), the relevant `.env` keys (values
redacted), and log lines from `python -m app.doctor` when applicable.

For security issues, do **not** open a public issue — see
[SECURITY.md](SECURITY.md).
