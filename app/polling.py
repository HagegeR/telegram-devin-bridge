from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import httpx

from app.set_webhook import configure_bot
from app.telegram import TelegramClient
from app.telegram_updates import ALLOWED_UPDATES

logger = logging.getLogger(__name__)

# Touched once per poll iteration so a host watchdog can distinguish a live
# polling loop from a wedged one (an exception escaping run_polling kills the
# task silently — the process stays up and nothing polls Telegram again).
POLL_ALIVE_PATH = Path("/tmp/telegram-devin-bridge.poll-alive")
# Iteration gap beyond this = wedged; the watchdog self-SIGTERMs so the
# supervisor (supervise-daemon / systemd Restart=always) respawns a live
# process — the same self-kill mechanism as the admin restart.
POLL_STALL_SECONDS = 600.0
_WATCHDOG_TICK_SECONDS = 60.0


async def run_polling(
    telegram: TelegramClient,
    handle_update: Callable[[Mapping[str, object]], Awaitable[None]],
) -> None:
    last_beat = time.monotonic()

    def _beat() -> None:
        nonlocal last_beat
        last_beat = time.monotonic()
        try:
            POLL_ALIVE_PATH.touch()
        except OSError as exc:
            logger.warning("poll heartbeat write failed: %s", exc)

    async def _watchdog() -> None:
        while True:
            await asyncio.sleep(_WATCHDOG_TICK_SECONDS)
            idle = time.monotonic() - last_beat
            if idle > POLL_STALL_SECONDS:
                logger.critical(
                    "Telegram polling stalled for %.0fs; self-SIGTERM "
                    "for supervisor restart",
                    idle,
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return

    watchdog = asyncio.create_task(_watchdog())
    try:
        await telegram.delete_webhook()
        await configure_bot(telegram)
        offset: int | None = None
        backoff = 1.0
        while True:
            _beat()
            try:
                updates = await telegram.get_updates(
                    offset,
                    timeout=50,
                    allowed_updates=ALLOWED_UPDATES,
                )
                backoff = 1.0
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    await handle_update(update)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning("Telegram polling failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception:
                # Unexpected (e.g. mark_update_seen sqlite errors inside
                # handle_update): the update's offset was already advanced,
                # so retrying skips the poisoned update instead of dying
                # silently.
                logger.exception(
                    "Telegram polling hit an unexpected error; continuing"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
    finally:
        watchdog.cancel()
