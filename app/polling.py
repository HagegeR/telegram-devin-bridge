from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import httpx

from app.set_webhook import configure_bot
from app.telegram import TelegramClient
from app.telegram_updates import ALLOWED_UPDATES

logger = logging.getLogger(__name__)

# Touched once per poll iteration so a host watchdog can distinguish a live
# polling loop from a wedged one (an exception escaping run_polling kills the
# task silently — the HTTP server stays up and nothing polls Telegram again).
POLL_ALIVE_PATH = Path("/tmp/telegram-devin-bridge.poll-alive")


def _beat() -> None:
    try:
        POLL_ALIVE_PATH.touch()
    except OSError as exc:
        logger.warning("poll heartbeat write failed: %s", exc)


async def run_polling(
    telegram: TelegramClient,
    handle_update: Callable[[Mapping[str, object]], Awaitable[None]],
) -> None:
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
            # Unexpected (e.g. a bug in handle_update): the update's offset was
            # already advanced, so retrying skips the poisoned update instead
            # of dying silently.
            logger.exception("Telegram polling hit an unexpected error; continuing")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
