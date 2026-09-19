from __future__ import annotations

import asyncio
import logging

import httpx

from app.clients import DevinClient, TelegramClient
from app.config import get_settings
from app.main import Bridge
from app.set_webhook import configure_bot
from app.store import Store
from app.telegram_updates import ALLOWED_UPDATES

logger = logging.getLogger(__name__)


async def run_polling(bridge: Bridge) -> None:
    await bridge.telegram.delete_webhook()
    await configure_bot(bridge.telegram)
    offset: int | None = None
    backoff = 1.0
    while True:
        try:
            updates = await bridge.telegram.get_updates(
                offset,
                timeout=50,
                allowed_updates=ALLOWED_UPDATES,
            )
            backoff = 1.0
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                await bridge.handle_update(update)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning("Telegram polling failed: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def main() -> None:
    settings = get_settings()
    bridge = Bridge(
        settings,
        Store(settings.database_path),
        DevinClient(
            settings.devin_api_key,
            settings.devin_api_base_url,
            settings.devin_max_acu_limit,
        ),
        TelegramClient(
            settings.telegram_bot_token,
            rich_enabled=settings.telegram_rich_messages,
        ),
    )
    await bridge.startup()
    try:
        await run_polling(bridge)
    finally:
        await bridge.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
