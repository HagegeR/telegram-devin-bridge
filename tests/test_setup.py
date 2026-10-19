"""Coverage for `python -m app.setup` (app/setup.py).

Prompts are scripted by replacing input()/getpass.getpass() with iterators,
so every test runs offline.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import setup
from app.config import Settings


def _feed(
    monkeypatch: pytest.MonkeyPatch,
    inputs: list[str],
    secrets_in: list[str] | None = None,
) -> None:
    answers = iter(inputs)
    secret_answers = iter(secrets_in or [])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    monkeypatch.setattr(
        setup.getpass, "getpass", lambda _p="": next(secret_answers)
    )


def _run(tmp_path: Path, *argv: str) -> tuple[int, Path]:
    target = tmp_path / ".env"
    return setup.main(["--path", str(target), *argv]), target


def _values(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if "=" in line and not line.startswith("#")
    )


def test_setup_writes_valid_polling_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(
        monkeypatch,
        inputs=["111:me", "", ""],
        secrets_in=["12345:bot-token", "apk_devin-key"],
    )
    rc, path = _run(tmp_path)
    assert rc == 0
    text = path.read_text()
    assert "TELEGRAM_BOT_TOKEN=12345:bot-token" in text
    assert "DEVIN_API_KEY=apk_devin-key" in text
    assert "TELEGRAM_MODE=polling" in text
    assert "TELEGRAM_ALLOWED_USERS=111:me" in text
    assert "DATABASE_PATH=./bridge.sqlite3" in text
    for key in (
        "TELEGRAM_WEBHOOK_SECRET",
        "NOTIFY_SECRET",
        "DOCTOR_SECRET",
        "ADMIN_SECRET",
    ):
        line = next(l for l in text.splitlines() if l.startswith(key + "="))
        assert len(line.split("=", 1)[1]) > 20
    # the written file must load through the real Settings — init kwargs
    # so the host's own environment can't shadow the file being checked
    assert Settings(**_values(text)).telegram_bot_token == "12345:bot-token"
    # secrets are not world-readable (POSIX; Windows has no mode bits)
    if os.name != "nt":
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_setup_webhook_mode_collects_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(
        monkeypatch,
        inputs=["111", "webhook", "https://bridge.example.com", ""],
        secrets_in=["12345:bot-token", "apk_devin-key"],
    )
    rc, path = _run(tmp_path)
    assert rc == 0
    text = path.read_text()
    assert "TELEGRAM_MODE=webhook" in text
    assert "PUBLIC_BASE_URL=https://bridge.example.com" in text
    settings = Settings(**_values(text))
    assert settings.telegram_webhook_secret
    assert settings.public_base_url == "https://bridge.example.com"


def test_setup_refuses_to_clobber_existing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_BOT_TOKEN=original\n")
    _feed(monkeypatch, inputs=["n"])
    rc, _ = _run(tmp_path)
    assert rc == 1
    assert path.read_text() == "TELEGRAM_BOT_TOKEN=original\n"

    # --force rewrites without asking
    _feed(
        monkeypatch,
        inputs=["111", "", ""],
        secrets_in=["12345:bot-token", "apk_devin-key"],
    )
    rc, _ = _run(tmp_path, "--force")
    assert rc == 0
    assert "DEVIN_API_KEY=apk_devin-key" in path.read_text()


def test_failed_validation_preserves_existing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a bad answer must not destroy a working configuration
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_BOT_TOKEN=original\n")
    _feed(
        monkeypatch,
        inputs=["123,abc", "", ""],
        secrets_in=["12345:bot-token", "apk_devin-key"],
    )
    rc, _ = _run(tmp_path, "--force")
    assert rc == 1
    assert path.read_text() == "TELEGRAM_BOT_TOKEN=original\n"


def test_setup_blank_users_defaults_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(
        monkeypatch,
        inputs=["", "n", "", ""],
        secrets_in=["12345:bot-token", "apk_devin-key"],
    )
    rc, path = _run(tmp_path)
    assert rc == 0
    text = path.read_text()
    assert "TELEGRAM_ALLOWED_USERS" not in text
    assert "TELEGRAM_ALLOW_ALL_USERS" not in text


def test_setup_allow_all_requires_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(
        monkeypatch,
        inputs=["", "y", "", ""],
        secrets_in=["12345:bot-token", "apk_devin-key"],
    )
    rc, path = _run(tmp_path)
    assert rc == 0
    assert "TELEGRAM_ALLOW_ALL_USERS=true" in path.read_text()


def test_setup_reprompts_a_malformed_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _feed(
        monkeypatch,
        inputs=["111", "", ""],
        secrets_in=["no-colon-here", "12345:good-token", "apk_devin-key"],
    )
    rc, path = _run(tmp_path)
    assert rc == 0
    assert "TELEGRAM_BOT_TOKEN=12345:good-token" in path.read_text()


def test_setup_reprompts_bad_mode_url_and_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a mistyped mode must not silently become polling, a non-https URL is
    # useless to Telegram, and a cog_ key fails every v1 call
    _feed(
        monkeypatch,
        inputs=["111", "webhok", "webhook", "http://nope", "https://ok.example", ""],
        secrets_in=["12345:bot-token", "cog_service", "apk_devin-key"],
    )
    rc, path = _run(tmp_path)
    assert rc == 0
    text = path.read_text()
    assert "TELEGRAM_MODE=webhook" in text
    assert "PUBLIC_BASE_URL=https://ok.example" in text
    assert "DEVIN_API_KEY=apk_devin-key" in text
