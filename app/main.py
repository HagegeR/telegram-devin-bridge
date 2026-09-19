from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import cast

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from app.access import (
    is_allowed,
    is_topic_chat,
    should_respond_in_group,
    strip_bot_mention,
)
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
        self.bot_topics_enabled = False
        self.implicit_topics: set[tuple[int, int]] = set()
        self.watchers: dict[str, asyncio.Task[None]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.lock_refs: dict[str, int] = {}
        self.background_tasks: set[asyncio.Task[None]] = set()
        self.denied_notices: set[tuple[int, int]] = set()

    async def startup(self) -> None:
        profile = await self.telegram.get_me()
        username = _text(profile.get("username"))
        if not self.bot_username and username is not None:
            self.bot_username = username
        self.bot_topics_enabled = bool(profile.get("has_topics_enabled"))

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
        except Exception as exc:
            logger.exception("Failed to process Telegram update")
            await self._report_processing_failure(update, exc)

    async def handle_message(self, message: Mapping[str, object]) -> None:
        sender = _mapping(message.get("from"))
        chat = _mapping(message.get("chat"))
        user_id = _int(sender.get("id"))
        chat_id = _int(chat.get("id"))
        topic_created = _mapping(message.get("forum_topic_created"))
        if topic_created:
            thread_id = _thread_id(message)
            if thread_id is not None:
                key = (chat_id, thread_id)
                if bool(topic_created.get("is_name_implicit")):
                    self.implicit_topics.add(key)
                else:
                    self.implicit_topics.discard(key)
        if any(
            message.get(field) is not None
            for field in (
                "forum_topic_created",
                "forum_topic_edited",
                "forum_topic_closed",
                "forum_topic_reopened",
            )
        ):
            return
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
            command = _command_name(text)
            if command in {"new", "resume", "retry", "stop", "playbook"}:
                async with self._lock(self._conversation_key(message)):
                    await handle_command(self, message, text)
            else:
                await handle_command(self, message, text)
            return
        attachment = await self._attachment(message)
        if not text and attachment is None:
            await self.telegram.send_message(
                chat_id,
                "Unsupported message type; send text, a photo, a document, or a voice note.",
                thread_id=_thread_id(message),
            )
            return
        await self.handle_user_turn(message, text, attachment=attachment)

    async def handle_user_turn(
        self,
        message: Mapping[str, object],
        text: str,
        *,
        attachment: tuple[str, bytes, str] | None = None,
    ) -> None:
        conv_key = self._conversation_key(message)
        async with self._lock(conv_key):
            await self._handle_user_turn_locked(message, text, attachment)

    async def _handle_user_turn_locked(
        self,
        message: Mapping[str, object],
        text: str,
        attachment: tuple[str, bytes, str] | None,
    ) -> None:
        chat = _mapping(message.get("chat"))
        chat_id = _int(chat.get("id"))
        thread_id = _thread_id(message)
        conv_key = self._conversation_key(message)
        message_id = _int(message.get("message_id"))
        conversation = self.store.get_conversation(conv_key)
        if conversation is not None:
            self.store.update_conversation(
                conv_key,
                conversation.session_id,
                last_user_text=text,
            )
        await self.telegram.set_message_reaction(chat_id, message_id, "👀")
        await self.telegram.send_chat_action(chat_id, thread_id=thread_id)
        if attachment is not None:
            filename, content, content_type = attachment
            url = await self.devin.upload_attachment(filename, content, content_type)
            text = f"{text}\n\nAttached file: {url} ({filename})".strip()
        if not text:
            text = "Please inspect the attached file."
        if conversation is None or await self._is_finished(conversation.session_id):
            title = f"Telegram: {text[:60]}"
            conversation = await self.create_session_for_message(
                message,
                SYSTEM_PREAMBLE + text,
                title,
                last_user_text=text,
                start_watcher=False,
            )
            if thread_id is not None and (chat_id, thread_id) in self.implicit_topics:
                topic_name = text[:60].splitlines()[0] or "Devin"
                try:
                    await self.telegram.edit_forum_topic(
                        chat_id,
                        thread_id,
                        topic_name,
                    )
                except RuntimeError:
                    logger.warning(
                        "Failed to rename implicit topic chat=%s thread=%s",
                        chat_id,
                        thread_id,
                    )
                finally:
                    self.implicit_topics.discard((chat_id, thread_id))
        else:
            self.store.update_conversation(
                conv_key,
                conversation.session_id,
                last_user_text=text,
            )
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
            is_forum=is_topic_chat(message),
        )
        session_id, session_url = await self.devin.create_session(
            prompt,
            title,
            playbook_id,
        )
        conversation = await self.replace_conversation(
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
        if start_watcher:
            await self.start_watcher(
                conversation,
                trigger_message_id=_int(message.get("message_id")),
            )
        return conversation

    async def replace_conversation(
        self,
        *,
        conv_key: str,
        chat_id: int,
        thread_id: int | None,
        session_id: str,
        session_url: str,
        title: str,
        last_event_id: str | None = None,
        last_user_text: str | None = None,
    ) -> Conversation:
        previous = self.store.get_conversation(conv_key)
        if previous is not None and previous.session_id != session_id:
            task = self.watchers.pop(previous.session_id, None)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.store.save_conversation(
            conv_key=conv_key,
            chat_id=chat_id,
            thread_id=thread_id,
            session_id=session_id,
            session_url=session_url,
            title=title,
            last_event_id=last_event_id,
            last_user_text=last_user_text,
        )
        conversation = self.store.get_conversation(conv_key)
        if conversation is None:
            raise RuntimeError("Conversation was not saved")
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
        chat = _mapping(callback_message.get("chat"))
        chat_id = _int(chat.get("id"))
        callback_message_id = _int(callback_message.get("message_id"))
        conv_key = self._conversation_key(callback_message)
        async with self._lock(conv_key):
            data = _text(callback.get("data")) or ""
            choice = self.store.get_choice(data)
            if choice is None:
                await self.telegram.answer_callback_query(callback_id, "This choice expired")
                return
            stored_conv_key, session_id, stored_chat_id, option = choice
            active = self.store.get_conversation(conv_key)
            if (
                stored_conv_key != conv_key
                or stored_chat_id != chat_id
                or active is None
                or active.session_id != session_id
            ):
                await self.telegram.answer_callback_query(callback_id, "This choice expired")
                return
            choices = self.store.list_choices(conv_key)
            self.store.delete_choices(conv_key)
            plain_option = not option.startswith("__cmd:")
            if option.startswith("__cmd:terminate:"):
                await self.devin.terminate(option.removeprefix("__cmd:terminate:"))
                self.store.clear_conversation(conv_key, session_id)
                updated = "Session terminated."
                active = None
            elif option == "__cmd:cancel":
                updated = "Cancelled."
            else:
                self.store.update_conversation(
                    conv_key,
                    session_id,
                    last_user_text=option,
                )
                await self.devin.send_message(session_id, option)
                updated = f"✅ {option}"
            if callback_message_id:
                try:
                    if plain_option:
                        markup = {
                            "inline_keyboard": [
                                [
                                    {
                                        "text": (
                                            f"✅ {choice_option}"
                                            if choice_id == data
                                            else choice_option
                                        ),
                                        "callback_data": choice_id,
                                        "disabled": {},
                                    }
                                ]
                                for choice_id, choice_option in choices
                            ]
                        }
                        await self.telegram.edit_message_reply_markup(
                            chat_id,
                            callback_message_id,
                            markup,
                        )
                    else:
                        await self.telegram.edit_message_text(
                            chat_id,
                            callback_message_id,
                            updated,
                        )
                except (httpx.HTTPError, RuntimeError):
                    await self.telegram.edit_message_reply_markup(
                        chat_id,
                        callback_message_id,
                    )
            await self.telegram.answer_callback_query(callback_id)
            if active is not None:
                await self.start_watcher(active)

    async def send_text(
        self,
        message: Mapping[str, object],
        text: str,
        *,
        silent: bool = False,
        ephemeral: bool = False,
    ) -> None:
        chat = _mapping(message.get("chat"))
        chat_id = _int(chat.get("id"))
        sender = _mapping(message.get("from"))
        receiver_user_id = (
            _int(sender.get("id"))
            if ephemeral and chat.get("type") in {"group", "supergroup"}
            else None
        )
        try:
            await self.telegram.send_markdown(
                chat_id,
                text,
                thread_id=_thread_id(message),
                disable_notification=silent,
                receiver_user_id=receiver_user_id,
            )
        except RuntimeError as exc:
            if receiver_user_id is None or "ephemeral" not in str(exc).casefold():
                raise
            await self.telegram.send_markdown(
                chat_id,
                text,
                thread_id=_thread_id(message),
                disable_notification=silent,
            )

    async def send_markup(
        self,
        message: Mapping[str, object],
        text: str,
        markup: dict[str, object],
        *,
        ephemeral: bool = False,
    ) -> None:
        chat = _mapping(message.get("chat"))
        sender = _mapping(message.get("from"))
        receiver_user_id = (
            _int(sender.get("id"))
            if ephemeral and chat.get("type") in {"group", "supergroup"}
            else None
        )
        try:
            await self.telegram.send_markdown(
                _int(chat.get("id")),
                text,
                thread_id=_thread_id(message),
                reply_markup=markup,
                receiver_user_id=receiver_user_id,
            )
        except RuntimeError as exc:
            if receiver_user_id is None or "ephemeral" not in str(exc).casefold():
                raise
            await self.telegram.send_markdown(
                _int(chat.get("id")),
                text,
                thread_id=_thread_id(message),
                reply_markup=markup,
            )

    async def get_session_status(self, session_id: str) -> str:
        return (await self.devin.get_session(session_id)).status_enum

    async def send_session_message(self, session_id: str, text: str) -> None:
        await self.devin.send_message(session_id, text)

    async def get_state(self, session_id: str) -> SessionState:
        return await self.devin.get_session(session_id)

    async def create_forum_topic(self, chat_id: int, name: str) -> int:
        return await self.telegram.create_forum_topic(chat_id, name)

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

    @asynccontextmanager
    async def _lock(self, conv_key: str) -> AsyncIterator[None]:
        lock = self.locks.get(conv_key)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[conv_key] = lock
            self.lock_refs[conv_key] = 0
        self.lock_refs[conv_key] += 1
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            self.lock_refs[conv_key] -= 1
            if self.lock_refs[conv_key] == 0:
                self.lock_refs.pop(conv_key, None)
                self.locks.pop(conv_key, None)

    def _conversation_key(self, message: Mapping[str, object]) -> str:
        chat = _mapping(message.get("chat"))
        return Store.conv_key(
            _int(chat.get("id")),
            _thread_id(message),
            is_forum=is_topic_chat(message),
        )

    async def _report_processing_failure(
        self,
        update: Mapping[str, object],
        exc: Exception,
    ) -> None:
        callback = _mapping(update.get("callback_query"))
        message = _mapping(update.get("message")) or _mapping(
            update.get("channel_post")
        )
        if not message and callback:
            message = _mapping(callback.get("message"))
        chat = _mapping(message.get("chat"))
        chat_id = _int(chat.get("id"))
        if not chat_id:
            return
        reason = _short_reason(exc)
        try:
            await self.telegram.send_message(
                chat_id,
                f"Couldn't process that message: {reason}. Use /retry.",
                thread_id=_thread_id(message),
            )
            message_id = _int(message.get("message_id"))
            if message_id:
                await self.telegram.set_message_reaction(chat_id, message_id, "👎")
        except Exception:
            logger.exception("Failed to report update processing error")

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
            size = largest.get("file_size")
            if isinstance(size, int) and size > 20 * 1024 * 1024:
                raise ValueError("Telegram attachments are limited to 20 MB")
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
        telegram
        or TelegramClient(
            actual_settings.telegram_bot_token,
            rich_enabled=actual_settings.telegram_rich_messages,
        ),
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


def _command_name(text: str) -> str:
    first = text.split(maxsplit=1)[0]
    return first[1:].split("@", 1)[0].casefold().replace("_", "-")


def _short_reason(exc: Exception) -> str:
    text = str(exc).strip().replace("\n", " ")
    return text[:120] or "temporary error"
