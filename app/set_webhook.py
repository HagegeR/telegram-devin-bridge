from __future__ import annotations

import asyncio
import logging

import httpx

from app.clients import TelegramClient
from app.config import get_settings

logger = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Start the bridge"),
    ("help", "List available commands"),
    ("new", "Start a new Devin session"),
    ("topic", "Create a topic with its own Devin session"),
    ("close", "Close the current topic"),
    ("rename", "Rename the current topic"),
    ("sessions", "List saved sessions"),
    ("resume", "Resume a saved session"),
    ("status", "Show active session status"),
    ("stop", "Terminate the active session"),
    ("cancel", "Cancel the active session"),
    ("playbook", "List or run a Devin playbook"),
    ("retry", "Retry the last user message"),
    ("whoami", "Show Telegram identity and access"),
    ("sethome", "Set this chat as notification home"),
]

GROUP_COMMANDS = {
    "new",
    "status",
    "stop",
    "cancel",
    "sessions",
    "resume",
    "retry",
    "topic",
    "close",
    "rename",
    "whoami",
    "help",
}

DESCRIPTION = (
    "Chat with Devin, the AI software engineer. Each chat or topic keeps its own "
    "Devin session; send text or files, tap buttons to answer questions, react 🔁 "
    "to retry or 🛑 to stop."
)


async def configure_bot(telegram: TelegramClient) -> None:
    full_commands = [
        {"command": command, "description": description}
        for command, description in COMMANDS
    ]
    group_commands = [
        command
        for command in full_commands
        if command["command"] in GROUP_COMMANDS
    ]
    try:
        await telegram.set_my_commands(full_commands)
        await telegram.set_my_commands(
            full_commands,
            {"type": "all_private_chats"},
        )
        await telegram.set_my_commands(
            group_commands,
            {"type": "all_group_chats"},
        )
        await telegram.set_my_description(DESCRIPTION)
        await telegram.set_my_short_description("Two-way bridge to Devin sessions")
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.warning("Could not configure bot commands or description: %s", exc)


async def main() -> None:
    settings = get_settings()
    client = TelegramClient(settings.telegram_bot_token)
    try:
        await client.set_webhook(
            settings.public_base_url or "",
            settings.telegram_webhook_secret or "",
        )
        await configure_bot(client)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
