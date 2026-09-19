from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

import httpx

from app.set_webhook import configure_bot
from app.telegram import TelegramClient
from app.telegram_updates import ALLOWED_UPDATES

logger = logging.getLogger(__name__)


async def run_polling(
    telegram: TelegramClient,
    handle_update: Callable[[Mapping[str, object]], Awaitable[None]],
) -> None:
    await telegram.delete_webhook()
    await configure_bot(telegram)
    offset: int | None = None
    backoff = 1.0
    while True:
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
