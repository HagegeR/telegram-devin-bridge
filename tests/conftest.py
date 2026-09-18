from __future__ import annotations

import os


def pytest_configure() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "token-placeholder")
    os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "secret-placeholder")
    os.environ.setdefault("DEVIN_API_KEY", "key-placeholder")
    os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost")
