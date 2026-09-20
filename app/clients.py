from __future__ import annotations

import asyncio
import ipaddress
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar, cast
from urllib.parse import urljoin, urlparse

import httpx

from app.formatting import (
    chunk,
    markdown_to_telegram_markdown_v2,
    normalize_rich_linebreaks,
)
from app.telegram_updates import ALLOWED_UPDATES

RETRY_ATTEMPTS = 5
RETRY_BACKOFF = (1.0, 2.0, 4.0, 8.0)  # ~15s total, covers DNS/route blips
T = TypeVar("T")


async def _is_public_host(hostname: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    except OSError:
        return False
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    return bool(addresses) and all(address.is_global for address in addresses)


async def _with_transport_retry(
    send: Callable[[], Awaitable[T]],
    *,
    idempotent: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return await send()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # failed before anything was transmitted — always safe to retry
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            await sleep(RETRY_BACKOFF[attempt])
        except httpx.TransportError:
            # mid-flight failure: retry only for idempotent requests so
            # mutations are never duplicated
            if not idempotent or attempt == RETRY_ATTEMPTS - 1:
                raise
            await sleep(RETRY_BACKOFF[attempt])
    raise AssertionError("unreachable")


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
    structured_output: object | None = None


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
        service_user_api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_acu_limit = max_acu_limit
        self.service_user_api_key = service_user_api_key
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self.public_client = httpx.AsyncClient(
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
        await self._call(
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
            structured_output=payload.get("structured_output"),
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
        await self._call("DELETE", f"/v1/sessions/{session_id}")

    async def upload_attachment(
        self,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> str:
        response = await _with_transport_retry(
            lambda: self.client.post(
                "/v1/attachments",
                files={"file": (filename, content, content_type)},
            ),
            idempotent=False,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, str):
            raise TypeError("Devin attachment response was not a URL")
        return value

    async def download_attachment(
        self,
        url: str,
    ) -> tuple[bytes, str] | None:
        parsed_url = urlparse(url)
        match = re.fullmatch(r"/attachments/([^/]+)/([^/]+)", parsed_url.path)
        if parsed_url.hostname != "app.devin.ai" or match is None:
            return None
        request_path = f"/v1/attachments/{match.group(1)}/{match.group(2)}"
        current_url = urljoin(self.base_url, request_path)
        for hop in range(4):
            if hop > 0 and urlparse(current_url).scheme != "https":
                return None
            client = self.client if hop == 0 else self.public_client

            async def _hop(
                client: httpx.AsyncClient = client,
                current_url: str = current_url,
                hop: int = hop,
            ) -> tuple[bytes, str] | str | None:
                async with client.stream(
                    "GET",
                    request_path if hop == 0 else current_url,
                    follow_redirects=False,
                ) as response:
                    if 300 <= response.status_code < 400:
                        if hop == 3:
                            return None
                        location = response.headers.get("location")
                        if not location:
                            return None
                        redirect_url = urljoin(current_url, location)
                        if urlparse(redirect_url).scheme != "https":
                            return None
                        if not await _is_public_host(
                            urlparse(redirect_url).hostname or ""
                        ):
                            return None
                        return redirect_url
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            if int(content_length) > 20 * 1024 * 1024:
                                return None
                        except ValueError:
                            pass
                    chunks: list[bytes] = []
                    content_size = 0
                    async for chunk in response.aiter_bytes():
                        content_size += len(chunk)
                        if content_size > 20 * 1024 * 1024:
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get(
                        "content-type",
                        "application/octet-stream",
                    ).split(";", 1)[0]

            try:
                result = await _with_transport_retry(_hop, idempotent=True)
            except httpx.HTTPError:
                return None
            if isinstance(result, str):
                current_url = result
                continue
            return result
        return None

    async def session_consumption(
        self,
        org_id: str,
        session_id: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, object]:
        response = await self.client.get(
            f"/v3/organizations/{org_id}/consumption/daily/sessions/{session_id}",
            params={
                "time_after": int(start.astimezone(timezone.utc).timestamp()),
                "time_before": int(end.astimezone(timezone.utc).timestamp()),
            },
            headers=(
                {"Authorization": f"Bearer {self.service_user_api_key}"}
                if self.service_user_api_key
                else None
            ),
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("Devin consumption response was not an object")
        return cast(dict[str, object], value)

    async def fetch_github_pr(
        self,
        url: str,
        token: str | None = None,
    ) -> dict[str, object] | None:
        if re.fullmatch(
            r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+",
            url,
        ) is None:
            return None
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        api_url = url.replace(
            "https://github.com/",
            "https://api.github.com/repos/",
        ).replace("/pull/", "/pulls/")
        try:
            response = await self.public_client.get(api_url, headers=headers, timeout=10)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        try:
            value = response.json()
        except ValueError:
            return None
        return cast(dict[str, object], value) if isinstance(value, dict) else None

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
    ) -> None:
        response = await _with_transport_retry(
            lambda: self.client.request(method, path, json=json),
            idempotent=method == "GET",
        )
        response.raise_for_status()

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        response = await _with_transport_retry(
            lambda: self.client.request(method, path, json=json),
            idempotent=method == "GET",
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("Devin API response was not an object")
        return cast(dict[str, object], value)

    async def close(self) -> None:
        await self.client.aclose()
        await self.public_client.aclose()

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
        rich_enabled: bool = True,
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
        self.rich_enabled = rich_enabled

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
        receiver_user_id: int | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
            "link_preview_options": {"is_disabled": True},
        }
        if thread_id is not None:
            body["message_thread_id"] = thread_id
        if reply_to is not None:
            body["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        if parse_mode is not None:
            body["parse_mode"] = parse_mode
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        if receiver_user_id is not None:
            body["ephemeral_message_parameters"] = {
                "receiver_user_id": receiver_user_id,
            }
        return await self._request("POST", "/sendMessage", body, parse_mode)

    async def send_rich_message(
        self,
        chat_id: int,
        markdown: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, object] | None = None,
        disable_notification: bool = False,
        receiver_user_id: int | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": normalize_rich_linebreaks(markdown)},
            "disable_notification": disable_notification,
        }
        if thread_id is not None:
            body["message_thread_id"] = thread_id
        if reply_to_message_id is not None:
            body["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        if receiver_user_id is not None:
            body["ephemeral_message_parameters"] = {
                "receiver_user_id": receiver_user_id,
            }
        return await self._request("POST", "/sendRichMessage", body, None)

    async def send_markdown(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, object] | None = None,
        disable_notification: bool = False,
        receiver_user_id: int | None = None,
    ) -> list[dict[str, object]]:
        if self.rich_enabled and 0 < len(text) <= 32768:
            try:
                return [
                    await self.send_rich_message(
                        chat_id,
                        text,
                        thread_id=thread_id,
                        reply_to_message_id=reply_to_message_id,
                        reply_markup=reply_markup,
                        disable_notification=disable_notification,
                        receiver_user_id=receiver_user_id,
                    )
                ]
            except RuntimeError as exc:
                reason = str(exc).casefold()
                if (
                    "method not found" in reason
                    or ("method" in reason and "not found" in reason)
                    or "unknown method" in reason
                ):
                    self.rich_enabled = False
        rendered = markdown_to_telegram_markdown_v2(text)
        parts = chunk(rendered)
        results: list[dict[str, object]] = []
        for index, part in enumerate(parts):
            results.append(
                await self.send_message(
                    chat_id,
                    part,
                    thread_id=thread_id,
                    reply_to=(
                        reply_to_message_id if index == 0 else None
                    ),
                    parse_mode="MarkdownV2",
                    reply_markup=reply_markup if index == len(parts) - 1 else None,
                    disable_notification=disable_notification,
                    receiver_user_id=receiver_user_id,
                )
            )
        return results

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
        markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return await self._request(
            "POST",
            "/editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": markup or {"inline_keyboard": []},
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

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str = "",
        *,
        thread_id: int | None = None,
    ) -> None:
        body: dict[str, object] = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": text,
        }
        if thread_id is not None:
            body["message_thread_id"] = thread_id
        await self._request("POST", "/sendMessageDraft", body, None)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self._request(
            "POST",
            "/deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
            None,
        )

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

    async def create_forum_topic(self, chat_id: int, name: str) -> int:
        payload = await self._request(
            "POST",
            "/createForumTopic",
            {"chat_id": chat_id, "name": name},
            None,
        )
        thread_id = payload.get("message_thread_id")
        if not isinstance(thread_id, int):
            raise TypeError(
                "Telegram createForumTopic response did not include message_thread_id"
            )
        return thread_id

    async def edit_forum_topic(self, chat_id: int, thread_id: int, name: str) -> None:
        await self._request(
            "POST",
            "/editForumTopic",
            {"chat_id": chat_id, "message_thread_id": thread_id, "name": name},
            None,
        )

    async def delete_forum_topic(self, chat_id: int, thread_id: int) -> None:
        await self._request(
            "POST",
            "/deleteForumTopic",
            {"chat_id": chat_id, "message_thread_id": thread_id},
            None,
        )

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        *,
        thread_id: int | None = None,
        caption: str | None = None,
        reply_to: int | None = None,
        content_type: str = "text/markdown",
    ) -> dict[str, object]:
        data: dict[str, str] = {"chat_id": str(chat_id)}
        if thread_id is not None:
            data["message_thread_id"] = str(thread_id)
        if caption is not None:
            data["caption"] = caption
        if reply_to is not None:
            data["reply_parameters"] = (
                f'{{"message_id": {reply_to}, '
                '"allow_sending_without_reply": true}'
            )
        response = await _with_transport_retry(
            lambda: self.client.post(
                "/sendDocument",
                data=data,
                files={"document": (filename, content, content_type)},
            ),
            idempotent=False,
        )
        response.raise_for_status()
        payload = self._json_object(response)
        if payload.get("ok") is False:
            description = payload.get("description")
            if isinstance(description, str):
                raise RuntimeError(description)
            raise RuntimeError("Telegram API request failed")
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return {}
        return cast(dict[str, object], result)

    async def send_photo(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        *,
        thread_id: int | None = None,
        caption: str | None = None,
        reply_to: int | None = None,
        content_type: str = "image/jpeg",
    ) -> dict[str, object]:
        data: dict[str, str] = {"chat_id": str(chat_id)}
        if thread_id is not None:
            data["message_thread_id"] = str(thread_id)
        if caption is not None:
            data["caption"] = caption
        if reply_to is not None:
            data["reply_parameters"] = (
                f'{{"message_id": {reply_to}, '
                '"allow_sending_without_reply": true}'
            )
        response = await _with_transport_retry(
            lambda: self.client.post(
                "/sendPhoto",
                data=data,
                files={"photo": (filename, content, content_type)},
            ),
            idempotent=False,
        )
        response.raise_for_status()
        payload = self._json_object(response)
        if payload.get("ok") is False:
            raise RuntimeError("Telegram API request failed")
        result = payload.get("result", {})
        return cast(dict[str, object], result) if isinstance(result, dict) else {}

    async def get_updates(
        self,
        offset: int | None,
        timeout: int,
        allowed_updates: list[str],
    ) -> list[dict[str, object]]:
        body: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": allowed_updates,
        }
        if offset is not None:
            body["offset"] = offset
        response = await self.client.post(
            "/getUpdates",
            json=body,
            timeout=timeout + 10,
        )
        response.raise_for_status()
        payload = self._json_object(response)
        if payload.get("ok") is False:
            description = payload.get("description")
            if isinstance(description, str):
                raise RuntimeError(description)
            raise RuntimeError("Telegram API request failed")
        result = payload.get("result")
        if not isinstance(result, list):
            return []
        return [
            cast(dict[str, object], item)
            for item in result
            if isinstance(item, dict)
        ]

    async def delete_webhook(self) -> None:
        await self._request(
            "POST",
            "/deleteWebhook",
            {"drop_pending_updates": False},
            None,
        )

    async def download_file(self, file_path: str) -> bytes:
        return await _with_transport_retry(
            lambda: self._download(file_path), idempotent=True
        )

    async def _download(self, file_path: str) -> bytes:
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

    async def get_me(self) -> dict[str, object]:
        payload = await self._request("GET", "/getMe", None, None)
        return payload

    async def set_my_commands(
        self,
        commands: list[dict[str, str]],
        scope: Mapping[str, object] | None = None,
    ) -> None:
        body: dict[str, object] = {"commands": commands}
        if scope is not None:
            body["scope"] = dict(scope)
        await self._request("POST", "/setMyCommands", body, None)

    async def set_my_description(self, text: str) -> None:
        await self._request("POST", "/setMyDescription", {"description": text}, None)

    async def set_my_short_description(self, text: str) -> None:
        await self._request(
            "POST",
            "/setMyShortDescription",
            {"short_description": text},
            None,
        )

    async def set_webhook(self, public_base_url: str, webhook_secret: str) -> None:
        await self._request(
            "POST",
            "/setWebhook",
            {
                "url": f"{public_base_url.rstrip('/')}/telegram/webhook",
                "secret_token": webhook_secret,
                "allowed_updates": ALLOWED_UPDATES,
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
        idempotent = method == "GET"
        response = await _with_transport_retry(
            lambda: self.client.request(method, path, json=body),
            idempotent=idempotent,
        )
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
                response = await _with_transport_retry(
                    lambda: self.client.request(method, path, json=body),
                    idempotent=idempotent,
                )
        if response.status_code == 400 and parse_mode is not None and body is not None:
            plain_body = dict(body)
            plain_body.pop("parse_mode", None)
            response = await _with_transport_retry(
                lambda: self.client.request(method, path, json=plain_body),
                idempotent=idempotent,
            )
            if response.status_code == 400 and "reply_markup" in plain_body:
                plain_body.pop("reply_markup", None)
                response = await _with_transport_retry(
                    lambda: self.client.request(method, path, json=plain_body),
                    idempotent=idempotent,
                )
        if response.is_error:
            payload = self._json_object(response)
            description = payload.get("description")
            if isinstance(description, str):
                raise RuntimeError(description)
        response.raise_for_status()
        payload = self._json_object(response)
        if payload.get("ok") is False:
            description = payload.get("description")
            if isinstance(description, str):
                raise RuntimeError(description)
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
