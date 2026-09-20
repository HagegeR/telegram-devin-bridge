from __future__ import annotations

from pathlib import Path

import pytest

from app.main import Bridge
from app.store import Store
from tests.test_v2 import _FakeDevin, _FakeTelegram, settings


def _runtime(tmp_path: Path):
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path, telegram_admin_user_ids="42"),
        Store(":memory:"),
        _FakeDevin(),
        telegram,
    )  # type: ignore[arg-type]
    return runtime, telegram


@pytest.mark.asyncio
async def test_self_update_sends_ack_and_notify_env(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    sent: list[str] = []

    async def fake_send(message, text, **kwargs):
        sent.append(text)
        return 1

    runtime.send_text = fake_send  # type: ignore[assignment]

    calls: list[dict[str, object]] = []

    async def fake_run(argv, cwd, env=None):
        calls.append({"argv": argv, "env": env})
        return 0, "up to date\n"

    runtime._run_command = fake_run

    await runtime.self_update({"from": {"id": 42}, "chat": {"id": 123}}, "")
    assert sent[0] == "Checking for updates…"
    assert "```" in sent[-1]
    assert calls[-1]["env"] == {"SELF_UPDATE_NOTIFY": "123"}

    sent.clear()
    await runtime.self_update(
        {
            "from": {"id": 42},
            "chat": {"id": 123},
            "message_thread_id": 7,
        },
        "check",
    )
    assert sent[0] == "Checking for updates…"
    assert calls[-1]["env"] == {"SELF_UPDATE_NOTIFY": "123:7"}
    assert calls[-1]["argv"][-1] == "--check"


@pytest.mark.asyncio
async def test_announce_update_to_marker_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, telegram = _runtime(tmp_path)
    marker_dir = tmp_path / "repo"
    marker_dir.mkdir()
    (marker_dir / ".self-update-pending").write_text(
        "abcdef1234567\n1234567890abc\n555:9\n"
    )
    monkeypatch.setattr("app.main._REPO_ROOT", marker_dir)

    async def fake_run(argv, cwd, env=None):
        return 0, "a1b2c3d first\n1234567 second\n"

    runtime._run_command = fake_run
    await runtime._announce_update()

    assert len(telegram.sent) == 1
    msg = telegram.sent[0]
    assert msg["chat_id"] == 555
    assert msg["thread_id"] == 9
    assert "updated abcdef1 → 1234567" in msg["text"]
    assert "a1b2c3d first" in msg["text"]
    assert "1234567 second" in msg["text"]
    assert not (marker_dir / ".self-update-pending").exists()


@pytest.mark.asyncio
async def test_announce_update_home_chat_when_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = _runtime(tmp_path)
    marker_dir = tmp_path / "repo"
    marker_dir.mkdir()
    (marker_dir / ".self-update-pending").write_text(
        "abcdef1234567\n1234567890abc\n\n"
    )
    monkeypatch.setattr("app.main._REPO_ROOT", marker_dir)

    notified: list[dict[str, object]] = []

    async def fake_notify(text, **kwargs):
        notified.append({"text": text, **kwargs})
        return 1

    runtime.notify = fake_notify  # type: ignore[assignment]

    async def fake_run(argv, cwd, env=None):
        return 0, ""

    runtime._run_command = fake_run
    await runtime._announce_update()

    assert len(notified) == 1
    assert notified[0]["chat_id"] is None
    assert notified[0]["markdown"] is True
    assert "updated abcdef1 → 1234567" in notified[0]["text"]


@pytest.mark.asyncio
async def test_announce_update_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, telegram = _runtime(tmp_path)
    monkeypatch.setattr("app.main._REPO_ROOT", tmp_path)
    await runtime._announce_update()
    assert telegram.sent == []
