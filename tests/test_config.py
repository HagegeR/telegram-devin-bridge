from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "bot1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "DEVIN_API_KEY": "apk_x",
    "TELEGRAM_MODE": "polling",
}


def env_file(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(f"{k}={v}" for k, v in REQUIRED.items()) + "\n" + extra
    )
    return path


def test_empty_optional_vars_read_as_unset(tmp_path: Path) -> None:
    config = Settings(
        _env_file=env_file(
            tmp_path,
            "TELEGRAM_HOME_CHANNEL=\n"
            "ADMIN_SECRET=\n"
            "DEVIN_ORG_ID=\n"
            "TRANSCRIPTION_LANGUAGE=\n",
        )
    )
    assert config.telegram_home_channel is None
    assert config.admin_secret is None
    assert config.devin_org_id is None
    assert config.transcription_language is None


def test_empty_required_vars_still_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # os.environ values win over .env — clear the conftest default
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=t\n"
        "DEVIN_API_KEY=k\n"
        "TELEGRAM_MODE=webhook\n"
        "PUBLIC_BASE_URL=\n"
    )
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings(_env_file=path)


def test_set_values_unchanged(tmp_path: Path) -> None:
    config = Settings(
        _env_file=env_file(tmp_path, "TELEGRAM_HOME_CHANNEL=-100\n")
    )
    assert config.telegram_home_channel == -100
