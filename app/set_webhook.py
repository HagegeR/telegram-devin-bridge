from __future__ import annotations

import asyncio

from app.clients import TelegramClient
from app.config import get_settings

COMMANDS = [
    ("start", "Start the bridge"),
    ("help", "List available commands"),
    ("new", "Start a new Devin session"),
    ("topic", "Create a topic with its own Devin session"),
    ("sessions", "List saved sessions"),
    ("resume", "Resume a saved session"),
    ("status", "Show active session status"),
    ("stop", "Terminate the active session"),
    ("playbook", "List or run a Devin playbook"),
    ("retry", "Retry the last user message"),
    ("whoami", "Show Telegram identity and access"),
    ("sethome", "Set this chat as notification home"),
]


async def main() -> None:
    settings = get_settings()
    client = TelegramClient(settings.telegram_bot_token)
    try:
        await client.set_webhook(
            settings.public_base_url,
            settings.telegram_webhook_secret,
        )
        await client.set_my_commands(
            [
                {"command": command, "description": description}
                for command, description in COMMANDS
            ]
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
