from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import httpx


@dataclass(frozen=True)
class DevinMessage:
    message_type: str
    event_id: str | None
    message: str
    timestamp: str | None


@dataclass(frozen=True)
class SessionState:
    status_enum: str
    title: str
    pr_url: str | None
    messages: list[DevinMessage]


@dataclass(frozen=True)
class Playbook:
    playbook_id: str
    title: str


class DevinClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        max_acu_limit: int,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_acu_limit = max_acu_limit
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def create_session(
        self,
        prompt: str,
        title: str,
        playbook_id: str | None = None,
    ) -> tuple[str, str]:
        body: dict[str, object] = {
            "prompt": prompt,
            "title": title,
            "max_acu_limit": self.max_acu_limit,
            "tags": ["telegram-bridge"],
        }
        if playbook_id is not None:
            body["playbook_id"] = playbook_id
        payload = await self._json("POST", "/v1/sessions", json=body)
        return self._required_str(payload, "session_id"), self._required_str(
            payload, "url"
        )

    async def send_message(self, session_id: str, message: str) -> None:
        await self._json(
            "POST",
            f"/v1/sessions/{session_id}/message",
            json={"message": message},
        )

    async def get_session(self, session_id: str) -> SessionState:
        payload = await self._json("GET", f"/v1/sessions/{session_id}")
        messages_value = payload.get("messages", [])
        messages: list[DevinMessage] = []
        if isinstance(messages_value, list):
            for item in messages_value:
                if not isinstance(item, dict):
                    continue
                messages.append(
                    DevinMessage(
                        message_type=self._optional_str(item.get("type")) or "",
                        event_id=self._optional_str(item.get("event_id")),
                        message=self._optional_str(item.get("message")) or "",
                        timestamp=self._optional_str(item.get("timestamp")),
                    )
                )
        pull_request = payload.get("pull_request")
        pr_url: str | None = None
        if isinstance(pull_request, dict):
            pr_url = self._optional_str(pull_request.get("url"))
        return SessionState(
            status_enum=self._optional_str(payload.get("status_enum")) or "",
            title=self._optional_str(payload.get("title")) or "",
            pr_url=pr_url,
            messages=messages,
        )

    async def list_playbooks(self) -> list[Playbook]:
        payload = await self._json("GET", "/v1/playbooks")
        items = payload.get("items", [])
        if not isinstance(items, list):
            return []
        result: list[Playbook] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            playbook_id = self._optional_str(item.get("playbook_id"))
            title = self._optional_str(item.get("title"))
            if playbook_id is not None and title is not None:
                result.append(Playbook(playbook_id, title))
        return result

    async def terminate(self, session_id: str) -> None:
        await self._json("DELETE", f"/v1/sessions/{session_id}")

    async def upload_attachment(
        self,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> str:
        response = await self.client.post(
            "/v1/attachments",
            files={"file": (filename, content, content_type)},
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, str):
            raise TypeError("Devin attachment response was not a URL")
        return value

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        response = await self.client.request(method, path, json=json)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("Devin API response was not an object")
        return cast(dict[str, object], value)

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _optional_str(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @classmethod
    def _required_str(cls, payload: Mapping[str, object], key: str) -> str:
        value = cls._optional_str(payload.get(key))
        if value is None:
            raise RuntimeError(f"Devin response did not include {key}")
        return value


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        self.base_url = (
            base_url.rstrip("/")
            if base_url is not None
            else f"https://api.telegram.org/bot{bot_token}"
        )
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
        )
        self._file_base_url = self._derive_file_base_url(self.base_url)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        parse_mode: str | None = None,
        reply_markup: dict[str, object] | None = None,
        disable_notification: bool = False,
        disable_web_page_preview: bool = True,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if thread_id is not None:
            body["message_thread_id"] = thread_id
        if reply_to is not None:
            body["reply_parameters"] = {"message_id": reply_to}
        if parse_mode is not None:
            body["parse_mode"] = parse_mode
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return await self._request("POST", "/sendMessage", body, parse_mode)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode is not None:
            body["parse_mode"] = parse_mode
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return await self._request("POST", "/editMessageText", body, parse_mode)

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
    ) -> dict[str, object]:
        return await self._request(
            "POST",
            "/editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
            None,
        )

    async def send_chat_action(
        self,
        chat_id: int,
        *,
        thread_id: int | None = None,
    ) -> None:
        body: dict[str, object] = {"chat_id": chat_id, "action": "typing"}
        if thread_id is not None:
            body["message_thread_id"] = thread_id
        await self._request("POST", "/sendChatAction", body, None)

    async def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        emoji: str | None,
    ) -> None:
        reaction: list[dict[str, object]] = []
        if emoji is not None:
            reaction = [{"type": "emoji", "emoji": emoji}]
        await self._request(
            "POST",
            "/setMessageReaction",
            {"chat_id": chat_id, "message_id": message_id, "reaction": reaction},
            None,
        )

    async def answer_callback_query(
        self,
        callback_id: str,
        text: str | None = None,
    ) -> None:
        body: dict[str, object] = {"callback_query_id": callback_id}
        if text is not None:
            body["text"] = text
        await self._request("POST", "/answerCallbackQuery", body, None)

    async def get_file(self, file_id: str) -> str:
        payload = await self._request("POST", "/getFile", {"file_id": file_id}, None)
        file_path = payload.get("file_path")
        if not isinstance(file_path, str):
            raise TypeError("Telegram getFile response did not include file_path")
        return file_path

    async def download_file(self, file_path: str) -> bytes:
        limit = 20 * 1024 * 1024
        async with self.client.stream(
            "GET",
            f"{self._file_base_url}/{file_path.lstrip('/')}",
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    length = int(content_length)
                except ValueError:
                    length = 0
                if length > limit:
                    raise ValueError("Telegram attachments are limited to 20 MB")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > limit:
                    raise ValueError("Telegram attachments are limited to 20 MB")
            return bytes(content)

    async def get_me(self) -> str:
        payload = await self._request("GET", "/getMe", None, None)
        username = payload.get("username")
        if not isinstance(username, str):
            raise TypeError("Telegram getMe response did not include username")
        return username

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        await self._request("POST", "/setMyCommands", {"commands": commands}, None)

    async def set_webhook(self, public_base_url: str, webhook_secret: str) -> None:
        await self._request(
            "POST",
            "/setWebhook",
            {
                "url": f"{public_base_url.rstrip('/')}/telegram/webhook",
                "secret_token": webhook_secret,
            },
            None,
        )

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None,
        parse_mode: str | None,
    ) -> dict[str, object]:
        response = await self.client.request(method, path, json=body)
        if response.status_code == 429:
            payload = self._json_object(response)
            parameters = payload.get("parameters")
            retry_after = (
                parameters.get("retry_after")
                if isinstance(parameters, dict)
                else None
            )
            if isinstance(retry_after, (int, float)):
                await asyncio.sleep(float(retry_after))
                response = await self.client.request(method, path, json=body)
        if response.status_code == 400 and parse_mode is not None and body is not None:
            plain_body = dict(body)
            plain_body.pop("parse_mode", None)
            response = await self.client.request(method, path, json=plain_body)
            if response.status_code == 400 and "reply_markup" in plain_body:
                plain_body.pop("reply_markup", None)
                response = await self.client.request(method, path, json=plain_body)
        response.raise_for_status()
        payload = self._json_object(response)
        if payload.get("ok") is False:
            raise RuntimeError("Telegram API request failed")
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return {}
        return cast(dict[str, object], result)

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, object]:
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("Telegram API response was not an object")
        return cast(dict[str, object], value)

    @staticmethod
    def _derive_file_base_url(base_url: str) -> str:
        marker = "/bot"
        if marker not in base_url:
            return base_url
        prefix, token = base_url.split(marker, 1)
        return f"{prefix}/file{marker}{token}"

    async def close(self) -> None:
        await self.client.aclose()
