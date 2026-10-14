from __future__ import annotations

import asyncio
import signal
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from app import polling


class _FakeTelegram:
    """Serves scripted get_updates batches; a BaseException item is raised,
    the string "hang" awaits forever."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.offsets: list[int | None] = []

    async def delete_webhook(self) -> None:
        return None

    async def get_updates(
        self,
        offset: int | None,
        timeout: int,
        allowed_updates: list[str],
    ) -> list[dict[str, object]]:
        self.offsets.append(offset)
        item = self.script.pop(0)
        if item == "hang":
            await asyncio.Future()  # never completes
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def _stub_configure_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _configure(_telegram: object) -> None:
        return None

    monkeypatch.setattr(polling, "configure_bot", _configure)


@pytest.mark.asyncio
async def test_polling_survives_unexpected_handler_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Telegram redelivers updates >= offset: update 1 poisons handle_update
    # (production raise sites: mark_update_seen sqlite errors, queue put on a
    # wedged worker — dispatch failures themselves are caught by the worker),
    # its offset is already advanced past it, and update 2 is redelivered on
    # the next poll instead of being lost with the rest of the batch.
    telegram = _FakeTelegram(
        [
            [{"update_id": 1}, {"update_id": 2}],
            [{"update_id": 2}],
            asyncio.CancelledError(),
        ]
    )
    seen: list[int] = []

    async def handle(update: Mapping[str, object]) -> None:
        if update.get("update_id") == 1:
            raise ValueError("boom")
        seen.append(int(update["update_id"]))  # type: ignore[index]

    with (
        caplog.at_level("ERROR", logger="app.polling"),
        pytest.raises(asyncio.CancelledError),
    ):
        await polling.run_polling(telegram, handle)  # type: ignore[arg-type]

    assert seen == [2]
    assert telegram.offsets == [None, 2, 3]
    assert "unexpected error" in caplog.text


@pytest.mark.asyncio
async def test_polling_retries_http_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    telegram = _FakeTelegram(
        [
            httpx.ConnectError("refused"),
            [{"update_id": 5}],
            asyncio.CancelledError(),
        ]
    )
    seen: list[int] = []

    async def handle(update: Mapping[str, object]) -> None:
        seen.append(int(update["update_id"]))  # type: ignore[index]

    with (
        caplog.at_level("WARNING", logger="app.polling"),
        pytest.raises(asyncio.CancelledError),
    ):
        await polling.run_polling(telegram, handle)  # type: ignore[arg-type]

    assert seen == [5]
    assert "Telegram polling failed" in caplog.text


@pytest.mark.asyncio
async def test_polling_touches_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    heartbeat = tmp_path / "poll-alive"
    monkeypatch.setattr(polling, "POLL_ALIVE_PATH", heartbeat)
    telegram = _FakeTelegram([asyncio.CancelledError()])

    async def handle(_update: Mapping[str, object]) -> None:
        return None

    with pytest.raises(asyncio.CancelledError):
        await polling.run_polling(telegram, handle)  # type: ignore[arg-type]

    assert heartbeat.exists()


@pytest.mark.asyncio
async def test_polling_watchdog_kills_stalled_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A wedged loop (e.g. get_updates never returning) must terminate the
    process via SIGTERM so the supervisor respawns it."""
    monkeypatch.setattr(polling, "POLL_STALL_SECONDS", 0.2)
    monkeypatch.setattr(polling, "_WATCHDOG_TICK_SECONDS", 0.02)
    monkeypatch.setattr(polling, "POLL_ALIVE_PATH", tmp_path / "poll-alive")
    killed: list[int] = []
    monkeypatch.setattr(
        polling.os, "kill", lambda _pid, sig: killed.append(sig)
    )
    telegram = _FakeTelegram([[{"update_id": 1}], "hang"])

    async def handle(_update: Mapping[str, object]) -> None:
        return None

    task = asyncio.create_task(
        polling.run_polling(telegram, handle)  # type: ignore[arg-type]
    )
    try:
        for _ in range(200):
            if killed:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert killed == [signal.SIGTERM]
