from __future__ import annotations

import asyncio

from app.clients import DevinClient, TelegramClient
from app.config import get_settings
from app.main import Bridge
from app.polling import run_polling
from app.store import Store


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
        await run_polling(bridge.telegram, bridge.handle_update)
    finally:
        await bridge.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
