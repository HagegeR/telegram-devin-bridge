import asyncio

from app.clients import TelegramClient
from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    client = TelegramClient(settings.telegram_bot_token)
    try:
        await client.set_webhook(
            settings.public_base_url,
            settings.telegram_webhook_secret,
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
