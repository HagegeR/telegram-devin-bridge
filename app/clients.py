import asyncio
import time
from collections.abc import Iterable

import httpx

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


class DevinClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        poll_seconds: float,
        reply_timeout_seconds: float,
        max_acu_limit: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.poll_seconds = poll_seconds
        self.reply_timeout_seconds = reply_timeout_seconds
        self.max_acu_limit = max_acu_limit
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )

    async def create_session(self, prompt: str, chat_id: int) -> str:
        response = await self.client.post(
            "/v1/sessions",
            json={
                "prompt": prompt,
                "title": f"Telegram chat {chat_id}",
                "max_acu_limit": self.max_acu_limit,
                "tags": ["telegram-bridge"],
            },
        )
        response.raise_for_status()
        return response.json()["session_id"]

    async def send_message(self, session_id: str, message: str) -> None:
        response = await self.client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": message},
        )
        response.raise_for_status()

    async def wait_for_reply(
        self,
        session_id: str,
        previous_message_id: str | None,
    ) -> tuple[str, str] | None:
        deadline = time.monotonic() + self.reply_timeout_seconds
        while time.monotonic() < deadline:
            response = await self.client.get(f"/v1/sessions/{session_id}")
            response.raise_for_status()
            messages = response.json().get("messages", [])
            reply = self._latest_new_reply(messages, previous_message_id)
            if reply:
                return reply
            await asyncio.sleep(self.poll_seconds)
        return None

    @staticmethod
    def _latest_new_reply(
        messages: Iterable[dict[str, object]],
        previous_message_id: str | None,
    ) -> tuple[str, str] | None:
        candidates = [
            message
            for message in messages
            if message.get("type") == "devin_message"
            and isinstance(message.get("event_id"), str)
            and isinstance(message.get("message"), str)
            and message["event_id"] != previous_message_id
        ]
        if not candidates:
            return None
        message = candidates[-1]
        return str(message["event_id"]), str(message["message"])

    async def close(self) -> None:
        await self.client.aclose()


class TelegramClient:
    def __init__(self, bot_token: str) -> None:
        self.client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{bot_token}",
            timeout=30,
        )

    async def set_webhook(self, public_base_url: str, webhook_secret: str) -> None:
        response = await self.client.post(
            "/setWebhook",
            json={"url": f"{public_base_url.rstrip('/')}/telegram/webhook"},
            params={"secret_token": webhook_secret},
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError(response.text)

    async def send_message(self, chat_id: int, text: str) -> None:
        for start in range(0, max(len(text), 1), TELEGRAM_MAX_MESSAGE_LENGTH):
            response = await self.client.post(
                "/sendMessage",
                json={"chat_id": chat_id, "text": text[start : start + TELEGRAM_MAX_MESSAGE_LENGTH]},
            )
            response.raise_for_status()

    async def close(self) -> None:
        await self.client.aclose()
