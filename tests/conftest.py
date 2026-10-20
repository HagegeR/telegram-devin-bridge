from __future__ import annotations

import os


def pytest_configure() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "token-placeholder")
    os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "secret-placeholder")
    os.environ.setdefault("DEVIN_API_KEY", "key-placeholder")
    os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost")
    # committing from a linked worktree exports the outer repo's GIT_DIR &
    # friends into the pre-commit env; every subprocess a test spawns
    # would then hit the outer repo instead of its tmp repo
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_QUARANTINE_PATH",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
    ):
        os.environ.pop(key, None)
