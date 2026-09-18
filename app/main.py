from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import cast

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from app.access import is_allowed, should_respond_in_group, strip_bot_mention
from app.commands import SYSTEM_PREAMBLE, handle_command
from app.config import Settings, get_settings
from app.devin import DevinClient, Playbook, SessionState
from app.formatting import chunk, markdown_to_telegram_markdown_v2
from app.notify import register_notify_route
from app.store import Conversation, Store
from app.telegram import TelegramClient
from app.watcher import SessionWatcher

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class Bridge:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        devin: DevinClient,
        telegram: TelegramClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.devin = devin
        self.telegram = telegram
        self.bot_username = settings.bot_username or ""
        self.watchers: dict[str, asyncio.Task[None]] = {}
        self.background_tasks: set[asyncio.Task[None]] = set()
        self.denied_notices: set[tuple[int, int]] = set()

    async def startup(self) -> None:
        if not self.bot_username:
            self.bot_username = await self.telegram.get_me()

    async def shutdown(self) -> None:
        for task in self.watchers.values():
            task.cancel()
        if self.watchers:
            await asyncio.gather(*self.watchers.values(), return_exceptions=True)
        for task in self.background_tasks:
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        await self.devin.close()
        await self.telegram.close()
        self.store.close()

    async def handle_update(self, update: Mapping[str, object]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or not self.store.mark_update_seen(update_id):
            return
        task = asyncio.create_task(self._dispatch_update(update))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def _dispatch_update(self, update: Mapping[str, object]) -> None:
        try:
            callback = _mapping(update.get("callback_query"))
            if callback:
                await self.handle_callback(callback)
                return
            message = _mapping(update.get("message")) or _mapping(
                update.get("channel_post")
            )
            if message:
                await self.handle_message(message)
        except Exception:
            logger.exception("Failed to process Telegram update")

    async def handle_message(self, message: Mapping[str, object]) -> None:
        sender = _mapping(message.get("from"))
        chat = _mapping(message.get("chat"))
        user_id = _int(sender.get("id"))
        chat_id = _int(chat.get("id"))
        if not is_allowed(message, self.settings):
            key = (user_id, chat_id)
            if key not in self.denied_notices:
                self.denied_notices.add(key)
                await self._send_to_ids(
                    chat_id,
                    f"This bot is private. Your user id is {user_id}.",
                )
            logger.warning("Rejected Telegram message user=%s chat=%s", user_id, chat_id)
            return
        if not should_respond_in_group(
            message,
            self.bot_username,
            self.settings.free_response_chats,
        ):
            return
        text = _expand_text_links(message, "text")
        if not text:
            text = _expand_text_links(message, "caption") or ""
        if chat.get("type") in {"group", "supergroup"}:
            text = strip_bot_mention(text, self.bot_username)
        if text.startswith("/"):
            await handle_command(self, message, text)
            return
        await self.handle_user_turn(message, text)

    async def handle_user_turn(
        self,
        message: Mapping[str, object],
        text: str,
    ) -> None:
        chat = _mapping(message.get("chat"))
        chat_id = _int(chat.get("id"))
        thread_id = _thread_id(message)
        conv_key = Store.conv_key(
            chat_id,
            thread_id,
            is_forum=bool(chat.get("is_forum")),
        )
        message_id = _int(message.get("message_id"))
        await self.telegram.set_message_reaction(chat_id, message_id, "👀")
        await self.telegram.send_chat_action(chat_id, thread_id=thread_id)
        attachment = await self._attachment(message)
        if attachment is not None:
            filename, content, content_type = attachment
            url = await self.devin.upload_attachment(filename, content, content_type)
            text = f"{text}\n\nAttached file: {url} ({filename})".strip()
        if not text:
            text = "Please inspect the attached file."
        conversation = self.store.get_conversation(conv_key)
        if conversation is None or await self._is_finished(conversation.session_id):
            title = f"Telegram: {text[:60]}"
            conversation = await self.create_session_for_message(
                message,
                SYSTEM_PREAMBLE + text,
                title,
                last_user_text=text,
                start_watcher=False,
            )
        else:
            await self.send_session_message(conversation.session_id, text)
            conversation = self.store.get_conversation(conv_key) or conversation
        await self.start_watcher(conversation, trigger_message_id=message_id)

    async def create_session_for_message(
        self,
        message: Mapping[str, object],
        prompt: str,
        title: str,
        *,
        playbook_id: str | None = None,
        last_user_text: str | None = None,
        start_watcher: bool = True,
    ) -> Conversation:
        chat = _mapping(message.get("chat"))
        chat_id = _int(chat.get("id"))
        thread_id = _thread_id(message)
        conv_key = Store.conv_key(
            chat_id,
            thread_id,
            is_forum=bool(chat.get("is_forum")),
        )
        session_id, session_url = await self.devin.create_session(
            prompt,
            title,
            playbook_id,
        )
        self.store.save_conversation(
            conv_key=conv_key,
            chat_id=chat_id,
            thread_id=thread_id,
            session_id=session_id,
            session_url=session_url,
            title=title,
            last_user_text=last_user_text or prompt,
        )
        self.store.add_history(
            conv_key=conv_key,
            session_id=session_id,
            session_url=session_url,
            title=title,
        )
        await self.send_text(message, f"Started session: {session_url}", silent=True)
        conversation = self.store.get_conversation(conv_key)
        if conversation is None:
            raise RuntimeError("Conversation was not saved")
        if start_watcher:
            await self.start_watcher(
                conversation,
                trigger_message_id=_int(message.get("message_id")),
            )
        return conversation

    async def start_watcher(
        self,
        conversation: Conversation,
        *,
        trigger_message_id: int | None = None,
        poll_seconds: float | None = None,
    ) -> None:
        existing = self.watchers.get(conversation.session_id)
        if existing is not None and not existing.done():
            return
        watcher = SessionWatcher(
            conversation,
            self.store,
            self.devin,
            self.telegram,
            self.settings,
            poll_seconds=poll_seconds,
            trigger_message_id=trigger_message_id,
        )
        task = asyncio.create_task(watcher.run())
        self.watchers[conversation.session_id] = task
        task.add_done_callback(
            lambda _: self.watchers.pop(conversation.session_id, None)
        )

    async def handle_callback(self, callback: Mapping[str, object]) -> None:
        callback_id = _text(callback.get("id")) or ""
        sender = _mapping(callback.get("from"))
        callback_message = _mapping(callback.get("message"))
        authorization_message = {
            "from": sender,
            "chat": callback_message.get("chat", {}),
        }
        if not is_allowed(authorization_message, self.settings):
            await self.telegram.answer_callback_query(callback_id, "This bot is private.")
            return
        data = _text(callback.get("data")) or ""
        choice = self.store.get_choice(data)
        if choice is None:
            await self.telegram.answer_callback_query(callback_id, "This choice expired")
            return
        conv_key, session_id, option = choice
        chat = _mapping(callback_message.get("chat"))
        chat_id = _int(chat.get("id"))
        callback_message_id = _int(callback_message.get("message_id"))
        self.store.delete_choices(conv_key)
        if option.startswith("__cmd:terminate:"):
            await self.devin.terminate(option.removeprefix("__cmd:terminate:"))
            self.store.clear_conversation(conv_key)
            updated = "Session terminated."
        elif option == "__cmd:cancel":
            updated = "Cancelled."
        else:
            await self.devin.send_message(session_id, option)
            updated = f"✅ {option}"
        if callback_message_id:
            try:
                await self.telegram.edit_message_text(chat_id, callback_message_id, updated)
            except (httpx.HTTPError, RuntimeError):
                await self.telegram.edit_message_reply_markup(chat_id, callback_message_id)
        await self.telegram.answer_callback_query(callback_id)
        conversation = self.store.get_conversation(conv_key)
        if conversation is not None:
            await self.start_watcher(conversation)

    async def send_text(
        self,
        message: Mapping[str, object],
        text: str,
        *,
        silent: bool = False,
    ) -> None:
        chat = _mapping(message.get("chat"))
        rendered = markdown_to_telegram_markdown_v2(text)
        for part in chunk(rendered):
            await self.telegram.send_message(
                _int(chat.get("id")),
                part,
                thread_id=_thread_id(message),
                parse_mode="MarkdownV2",
                disable_notification=silent,
            )

    async def send_markup(
        self,
        message: Mapping[str, object],
        text: str,
        markup: dict[str, object],
    ) -> None:
        chat = _mapping(message.get("chat"))
        await self.telegram.send_message(
            _int(chat.get("id")),
            text,
            thread_id=_thread_id(message),
            parse_mode="MarkdownV2",
            reply_markup=markup,
        )

    async def get_session_status(self, session_id: str) -> str:
        return (await self.devin.get_session(session_id)).status_enum

    async def send_session_message(self, session_id: str, text: str) -> None:
        await self.devin.send_message(session_id, text)

    async def get_state(self, session_id: str) -> SessionState:
        return await self.devin.get_session(session_id)

    async def list_playbooks(self) -> list[Playbook]:
        return await self.devin.list_playbooks()

    def new_choice_id(self) -> str:
        return secrets.token_urlsafe(8)

    async def notify(
        self,
        text: str,
        *,
        chat_id: int | None,
        thread_id: int | None,
        silent: bool,
        markdown: bool,
    ) -> int:
        target_chat = chat_id
        target_thread = thread_id
        if target_chat is None:
            stored_chat = self.store.get_setting("home_chat_id")
            target_chat = (
                int(stored_chat)
                if stored_chat is not None
                else self.settings.telegram_home_channel
            )
            stored_thread = self.store.get_setting("home_thread_id")
            if target_thread is None and stored_thread:
                target_thread = int(stored_thread)
        if target_chat is None:
            raise ValueError("No notification target configured")
        rendered = markdown_to_telegram_markdown_v2(text) if markdown else text
        sent = 0
        for part in chunk(rendered):
            await self.telegram.send_message(
                target_chat,
                part,
                thread_id=target_thread,
                parse_mode="MarkdownV2" if markdown else None,
                disable_notification=silent,
            )
            sent += 1
        return sent

    async def _is_finished(self, session_id: str) -> bool:
        return (await self.devin.get_session(session_id)).status_enum in {
            "expired",
            "finished",
        }

    async def _attachment(
        self,
        message: Mapping[str, object],
    ) -> tuple[str, bytes, str] | None:
        photos = message.get("photo")
        file_id: str | None = None
        filename = "photo.jpg"
        content_type = "image/jpeg"
        if isinstance(photos, list) and photos:
            largest = _mapping(photos[-1])
            file_id = _text(largest.get("file_id"))
        attachment_fields = (
            ("document", "document", "application/octet-stream"),
            ("voice", "voice.ogg", "audio/ogg"),
            ("audio", "audio.mp3", "audio/mpeg"),
            ("video", "video.mp4", "video/mp4"),
            ("video_note", "video_note.mp4", "video/mp4"),
        )
        for field, default_name, default_type in attachment_fields:
            candidate = _mapping(message.get(field))
            if not candidate:
                continue
            size = candidate.get("file_size")
            if isinstance(size, int) and size > 20 * 1024 * 1024:
                raise ValueError("Telegram attachments are limited to 20 MB")
            file_id = _text(candidate.get("file_id"))
            filename = _text(candidate.get("file_name")) or default_name
            content_type = _text(candidate.get("mime_type")) or default_type
            break
        if file_id is None:
            return None
        file_path = await self.telegram.get_file(file_id)
        return filename, await self.telegram.download_file(file_path), content_type

    async def _send_to_ids(self, chat_id: int, text: str) -> None:
        for part in chunk(text):
            await self.telegram.send_message(chat_id, part)


def create_app(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    devin: DevinClient | None = None,
    telegram: TelegramClient | None = None,
) -> FastAPI:
    actual_settings = settings or get_settings()
    runtime = Bridge(
        actual_settings,
        store or Store(actual_settings.database_path),
        devin
        or DevinClient(
            actual_settings.devin_api_key,
            actual_settings.devin_api_base_url,
            actual_settings.devin_max_acu_limit,
        ),
        telegram or TelegramClient(actual_settings.telegram_bot_token),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.startup()
        yield
        await runtime.shutdown()

    application = FastAPI(title="Telegram–Devin Bridge", lifespan=lifespan)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        if x_telegram_bot_api_secret_token != actual_settings.telegram_webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
        payload = await request.json()
        if isinstance(payload, dict):
            await runtime.handle_update(cast(Mapping[str, object], payload))
        return {"accepted": True}

    register_notify_route(application, runtime, actual_settings)
    application.state.bridge = runtime
    return application


app = create_app()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _expand_text_links(
    message: Mapping[str, object],
    field: str,
) -> str | None:
    text = _text(message.get(field))
    if text is None:
        return None
    entities = message.get("entities" if field == "text" else "caption_entities")
    if not isinstance(entities, list):
        return text
    raw_text = text.encode("utf-16-le")
    links: list[tuple[int, int, str]] = []
    for entity_value in entities:
        entity = _mapping(entity_value)
        if entity.get("type") != "text_link":
            continue
        url = _text(entity.get("url"))
        offset = entity.get("offset")
        length = entity.get("length")
        if (
            url is None
            or not isinstance(offset, int)
            or not isinstance(length, int)
            or offset < 0
            or length <= 0
        ):
            continue
        start = offset * 2
        end = (offset + length) * 2
        if end > len(raw_text):
            continue
        try:
            start_index = len(raw_text[:start].decode("utf-16-le"))
            end_index = len(raw_text[:end].decode("utf-16-le"))
        except UnicodeDecodeError:
            continue
        links.append((start_index, end_index, url))
    expanded = text
    for start, end, url in sorted(links, reverse=True):
        expanded = f"{expanded[:end]} ({url}){expanded[end:]}"
    return expanded


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _thread_id(message: Mapping[str, object]) -> int | None:
    value = message.get("message_thread_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None
