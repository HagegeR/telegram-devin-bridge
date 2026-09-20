from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shlex
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from app.doctor import register_doctor_route
from app.formatting import (
    chunk,
    extract_large_code_blocks,
    markdown_to_telegram_markdown_v2,
    split_long_text,
)
from app.notify import register_notify_route
from app.polling import run_polling
from app.store import Conversation, Store
from app.telegram import TelegramClient
from app.watcher import ACTIVE_STATUSES, SessionWatcher

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _sanitize_update_output(text: str) -> str:
    text = _ANSI_RE.sub("", text).replace("`", "'")
    return text[-3000:]


async def _run_command(argv: list[str], cwd: Path) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 300)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "timed out after 300s"
    return process.returncode or 0, stdout.decode(errors="replace")


Attachment = tuple[str, bytes, str]
TurnFragment = tuple[Mapping[str, object], str, Attachment | None]
QueuedTurn = tuple[Mapping[str, object], str, Attachment | None]


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
        self.active_watchers: dict[str, SessionWatcher] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.lock_refs: dict[str, int] = {}
        self.background_tasks: set[asyncio.Task[None]] = set()
        self.denied_notices: set[tuple[int, int]] = set()
        self._run_command = _run_command
        self.access_prompted: dict[int, float] = {}
        self.transient_messages: dict[str, list[int]] = {}
        self.pending_turns: dict[str, list[TurnFragment]] = {}
        self.debounce_tasks: dict[str, asyncio.Task[None]] = {}
        self.queued_turns: dict[str, list[QueuedTurn]] = {}
        self.draining: set[str] = set()
        self.rate_windows: dict[int, deque[float]] = {}
        self.rate_warnings: dict[int, float] = {}
        self.shutting_down = False
        self.approved_users: set[int] = set()

    async def startup(self) -> None:
        profile = await self.telegram.get_me()
        username = _text(profile.get("username"))
        if not self.bot_username and username is not None:
            self.bot_username = username
        self.bot_topics_enabled = bool(profile.get("has_topics_enabled"))
        self.store.cleanup_long_texts()
        self.store.cleanup_message_index()
        self.approved_users = {
            request.user_id
            for request in self.store.list_access_requests("approved")
        }

    async def shutdown(self) -> None:
        self.shutting_down = True
        for task in self.debounce_tasks.values():
            task.cancel()
        if self.debounce_tasks:
            await asyncio.gather(*self.debounce_tasks.values(), return_exceptions=True)
        conv_keys = set(self.queued_turns) | set(self.pending_turns)
        for conv_key in conv_keys:
            queued = self.queued_turns.pop(conv_key, [])
            for message, text, attachment in queued:
                try:
                    await self.handle_user_turn(
                        message,
                        text,
                        attachment=attachment,
                    )
                except Exception as exc:
                    logger.exception(
                        "Failed to deliver queued Telegram turn for %s",
                        conv_key,
                    )
                    await self._report_processing_failure({"message": message}, exc)
            await self._flush_pending(conv_key)
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
            reaction = _mapping(update.get("message_reaction"))
            if reaction:
                await self.handle_reaction(reaction)
                return
            edited = _mapping(update.get("edited_message"))
            if edited:
                await self.handle_edited_message(edited)
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
        if not is_allowed(message, self.settings, self.approved_users):
            key = (user_id, chat_id)
            if (
                chat.get("type") == "private"
                and self.settings.admin_user_ids
            ):
                prompted_at = self.access_prompted.get(user_id)
                if (
                    prompted_at is None
                    or time.time() - prompted_at >= 86400
                ):
                    self.access_prompted[user_id] = time.time()
                    await self.send_markup(
                        message,
                        "You're not authorized.",
                        {
                            "inline_keyboard": [[
                                {
                                    "text": "Request access",
                                    "callback_data": "acc:req",
                                }
                            ]]
                        },
                    )
            elif key not in self.denied_notices:
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
        if not text.startswith("/"):
            text = self._contextualize_message(message, text)
        if text.startswith("/"):
            command = _command_name(text)
            if command in {
                "new",
                "resume",
                "retry",
                "steer",
                "stop",
                "cancel",
                "playbook",
                "close",
                "rename",
                "settings",
                "usage",
                "users",
                "revoke",
            }:
                async with self._lock(self._conversation_key(message)):
                    await handle_command(self, message, text)
            else:
                await handle_command(self, message, text)
            return
        if self._rate_limited(user_id):
            now = time.monotonic()
            if now - self.rate_warnings.get(user_id, 0) >= 60:
                self.rate_warnings[user_id] = now
                await self.telegram.send_message(
                    chat_id,
                    "Slow down — try again in a moment.",
                    thread_id=_thread_id(message),
                )
            return
        attachment = await self._attachment(message)
        if attachment is not None and self.settings.transcription_api_key:
            transcript = await self._transcribe(message, attachment)
            if transcript is not None:
                text = (
                    f"Voice note transcript:\n{transcript}"
                    + (f"\n\n{text}" if text else "")
                )
                await self.telegram.set_message_reaction(
                    chat_id,
                    _int(message.get("message_id")),
                    "🎙",
                )
                if not self.settings.telegram_attach_voice:
                    attachment = None
        if not text and attachment is None:
            await self.telegram.send_message(
                chat_id,
                "Unsupported message type; send text, a photo, a document, or a voice note.",
                thread_id=_thread_id(message),
            )
            return
        await self._queue_turn(message, text, attachment)

    async def _queue_turn(
        self,
        message: Mapping[str, object],
        text: str,
        attachment: Attachment | None,
    ) -> None:
        conv_key = self._conversation_key(message)
        pending = self.pending_turns.setdefault(conv_key, [])
        if attachment is not None and any(
            fragment_attachment is not None
            for _, _, fragment_attachment in pending
        ):
            await self._flush_pending(conv_key)
            pending = self.pending_turns.setdefault(conv_key, [])
        pending.append((message, text, attachment))
        if self.settings.telegram_debounce_seconds <= 0:
            await self._flush_pending(conv_key)
            return
        existing = self.debounce_tasks.get(conv_key)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(self._debounce_flush(conv_key))
        self.debounce_tasks[conv_key] = task

    async def _debounce_flush(self, conv_key: str) -> None:
        try:
            await asyncio.sleep(self.settings.telegram_debounce_seconds)
            await self._flush_pending(conv_key)
        except asyncio.CancelledError:
            return
        finally:
            current = self.debounce_tasks.get(conv_key)
            if current is asyncio.current_task():
                self.debounce_tasks.pop(conv_key, None)

    async def _flush_pending(self, conv_key: str) -> None:
        fragments = self.pending_turns.pop(conv_key, [])
        if not fragments:
            return
        for fragment_message, _, _ in fragments:
            chat_id = _int(_mapping(fragment_message.get("chat")).get("id"))
            message_id = _int(fragment_message.get("message_id"))
            if message_id:
                self.store.index_message(chat_id, message_id, conv_key)
        message = fragments[-1][0]
        try:
            text = "\n\n".join(value for _, value, _ in fragments if value)
            attachment = next(
                (value for _, _, value in fragments if value is not None),
                None,
            )
            turn = (message, text, attachment)
            if (
                self.settings.telegram_queue_while_busy
                and not self.shutting_down
                and self._conversation_busy(conv_key)
            ):
                self.queued_turns.setdefault(conv_key, []).append(turn)
                await self.react(message, "🤔")
                return
            queued = self.queued_turns.pop(conv_key, [])
            for index, queued_turn in enumerate(queued):
                queued_message, queued_text, queued_attachment = queued_turn
                try:
                    await self.handle_user_turn(
                        queued_message,
                        queued_text,
                        attachment=queued_attachment,
                    )
                except Exception as exc:
                    self.queued_turns[conv_key] = [
                        queued_turn,
                        *queued[index + 1:],
                        turn,
                    ]
                    logger.exception(
                        "Failed to flush queued Telegram turn for %s",
                        conv_key,
                    )
                    await self._report_processing_failure(
                        {"message": queued_message},
                        exc,
                        retryable=True,
                    )
                    return
            try:
                await self.handle_user_turn(message, text, attachment=attachment)
            except Exception:
                self.queued_turns[conv_key] = [turn]
                raise
        except Exception as exc:
            logger.exception("Failed to flush Telegram turn for %s", conv_key)
            await self._report_processing_failure(
                {"message": message},
                exc,
                retryable=True,
            )

    def _conversation_busy(self, conv_key: str) -> bool:
        conversation = self.store.get_conversation(conv_key)
        if conversation is None:
            return False
        task = self.watchers.get(conversation.session_id)
        watcher = self.active_watchers.get(conversation.session_id)
        return (
            task is not None
            and not task.done()
            and watcher is not None
            and (
                watcher.last_status is None
                or watcher.last_status in ACTIVE_STATUSES
            )
        )

    async def _drain_queue(self, conv_key: str) -> None:
        try:
            if conv_key in self.draining or self._conversation_busy(conv_key):
                return
            self.draining.add(conv_key)
            queued = self.queued_turns.get(conv_key)
            if not queued or self._conversation_busy(conv_key):
                return
            message, text, attachment = queued.pop(0)
            if not queued:
                self.queued_turns.pop(conv_key, None)
            if self._conversation_busy(conv_key):
                self.queued_turns.setdefault(conv_key, []).insert(
                    0, (message, text, attachment)
                )
                return
            try:
                await self.handle_user_turn(message, text, attachment=attachment)
            except Exception as exc:
                self.queued_turns.setdefault(conv_key, []).insert(
                    0,
                    (message, text, attachment),
                )
                logger.exception("Failed to drain Telegram turn for %s", conv_key)
                await self._report_processing_failure(
                    {"message": message},
                    exc,
                    retryable=True,
                )
        finally:
            self.draining.discard(conv_key)

    def clear_queued_turns(self, conv_key: str) -> None:
        self.queued_turns.pop(conv_key, None)
        self.pending_turns.pop(conv_key, None)
        task = self.debounce_tasks.pop(conv_key, None)
        if task is not None and not task.done():
            task.cancel()

    def queued_count(self, conv_key: str) -> int:
        return len(self.queued_turns.get(conv_key, [])) + len(
            self.pending_turns.get(conv_key, [])
        )

    def _rate_limited(self, user_id: int) -> bool:
        limit = self.settings.telegram_rate_limit_per_minute
        if limit <= 0 or user_id <= 0:
            return False
        now = time.monotonic()
        window = self.rate_windows.setdefault(user_id, deque())
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) >= limit:
            return True
        window.append(now)
        return False

    def _contextualize_message(
        self,
        message: Mapping[str, object],
        text: str,
    ) -> str:
        pieces: list[str] = []
        reply = _mapping(message.get("reply_to_message"))
        if reply and not any(
            reply.get(field) is not None
            for field in (
                "forum_topic_created",
                "forum_topic_edited",
                "forum_topic_closed",
                "forum_topic_reopened",
            )
        ):
            reply_sender = _mapping(reply.get("from"))
            is_bot_reply = bool(reply_sender.get("is_bot"))
            same_bot = (
                is_bot_reply
                and _text(reply_sender.get("username")) == self.bot_username
            )
            if not is_bot_reply or same_bot:
                quote = _mapping(message.get("quote"))
                quoted = _text(quote.get("text"))
                if quoted is None:
                    quoted = _expand_text_links(reply, "text")
                if not quoted:
                    quoted = _expand_text_links(reply, "caption")
                if quoted:
                    pieces.append(f'> Re: "{quoted[:300]}"')
        forward = _mapping(message.get("forward_origin"))
        if forward:
            forwarded_text = (
                _expand_text_links(forward, "text")
                or _expand_text_links(forward, "caption")
                or text
            )
            pieces.append(f"Forwarded message:\n{forwarded_text}")
            return "\n\n".join(pieces)
        if pieces:
            pieces.append(text)
            return "\n\n".join(pieces)
        return text

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
        if message_id:
            self.store.index_message(chat_id, message_id, conv_key)
        conversation = self.store.get_conversation(conv_key)
        if conversation is not None:
            self.store.update_conversation(
                conv_key,
                conversation.session_id,
                last_user_text=text,
                last_user_message_id=message_id,
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
                playbook_id=self.store.get_settings(conv_key).default_playbook,
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
                last_user_message_id=message_id,
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
        extra = self.settings.devin_session_instructions.strip()
        if extra:
            prompt = f"{extra}\n\n{prompt}"
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
            last_user_message_id=(
                _int(message.get("message_id"))
                if last_user_text is not None
                else None
            ),
        )
        self.store.add_history(
            conv_key=conv_key,
            session_id=session_id,
            session_url=session_url,
            title=title,
        )
        started_id = await self.send_text(
            message,
            f"Started session: {session_url}",
            silent=True,
        )
        if started_id is not None:
            self.transient_messages.setdefault(conv_key, []).append(started_id)
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
        last_user_message_id: int | None = None,
    ) -> Conversation:
        previous = self.store.get_conversation(conv_key)
        if previous is not None and previous.session_id != session_id:
            self.clear_queued_turns(conv_key)
            task = self.watchers.pop(previous.session_id, None)
            self.active_watchers.pop(previous.session_id, None)
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
            last_user_message_id=last_user_message_id,
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
            watcher = self.active_watchers.get(conversation.session_id)
            if watcher is not None and trigger_message_id is not None:
                watcher.set_trigger(trigger_message_id)
            return
        watcher = SessionWatcher(
            conversation,
            self.store,
            self.devin,
            self.telegram,
            self.settings,
            poll_seconds=poll_seconds,
            trigger_message_id=trigger_message_id,
            transient_message_ids=self.transient_messages.pop(
                conversation.conv_key,
                [],
            ),
            drafts_enabled=self._conversation_drafts(conversation.conv_key),
            status_after_seconds=self._conversation_status_after(
                conversation.conv_key,
            ),
            silent=self._conversation_silent(conversation.conv_key),
        )
        task = asyncio.create_task(watcher.run())
        self.watchers[conversation.session_id] = task
        self.active_watchers[conversation.session_id] = watcher

        def watcher_done(_: asyncio.Task[None]) -> None:
            current = self.watchers.get(conversation.session_id) is task
            if current:
                self.watchers.pop(conversation.session_id, None)
                if self.active_watchers.get(conversation.session_id) is watcher:
                    self.active_watchers.pop(conversation.session_id, None)
            if not current or self.shutting_down:
                return
            drain = asyncio.create_task(self._drain_queue(conversation.conv_key))
            self.background_tasks.add(drain)
            drain.add_done_callback(self.background_tasks.discard)

        task.add_done_callback(watcher_done)

    async def handle_callback(self, callback: Mapping[str, object]) -> None:
        callback_id = _text(callback.get("id")) or ""
        sender = _mapping(callback.get("from"))
        callback_message = _mapping(callback.get("message"))
        if any(
            callback_message.get(field) is not None
            for field in (
                "forward_origin",
                "forward_from",
                "forward_from_chat",
                "forward_date",
            )
        ):
            await self.telegram.answer_callback_query(
                callback_id,
                "Not available on forwarded messages",
            )
            return
        authorization_message = {
            "from": sender,
            "chat": callback_message.get("chat", {}),
        }
        data = _text(callback.get("data")) or ""
        if data.startswith("acc:"):
            await self._handle_access_callback(callback, data)
            return
        if not is_allowed(
            authorization_message,
            self.settings,
            self.approved_users,
        ):
            await self.telegram.answer_callback_query(callback_id, "This bot is private.")
            return
        chat = _mapping(callback_message.get("chat"))
        chat_id = _int(chat.get("id"))
        callback_message_id = _int(callback_message.get("message_id"))
        conv_key = self._conversation_key(callback_message)
        async with self._lock(conv_key):
            data = _text(callback.get("data")) or ""
            if data.startswith("cfg:"):
                await self._handle_settings_callback(
                    callback_id,
                    callback_message,
                    conv_key,
                    data,
                )
                return
            if data.startswith("more:"):
                await self._handle_long_text_callback(
                    callback_id,
                    callback_message,
                    conv_key,
                    data.removeprefix("more:"),
                )
                return
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
            choices = self.store.list_choices(conv_key, callback_message_id)
            self.store.delete_choices(conv_key)
            plain_option = not option.startswith("__cmd:")
            if option.startswith("__cmd:terminate:"):
                await self.stop_conversation(active)
                updated = "Session terminated."
                active = None
            elif option == "__cmd:cancel":
                updated = "Cancelled."
            else:
                self.store.update_conversation(
                    conv_key,
                    session_id,
                    last_user_text=option,
                    last_user_message_id=None,
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
                                        "disabled": {},
                                    }
                                ]
                                for choice_id, choice_option in choices
                            ]
                        }
                        if not choices:
                            markup = {
                                "inline_keyboard": [[
                                    {
                                        "text": f"✅ {option}",
                                        "disabled": {},
                                    }
                                ]]
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

    async def handle_reaction(self, reaction: Mapping[str, object]) -> None:
        user = _mapping(reaction.get("user"))
        if not user:
            return
        authorization = {
            "from": user,
            "chat": reaction.get("chat", {}),
        }
        if not is_allowed(authorization, self.settings, self.approved_users):
            return
        chat = _mapping(reaction.get("chat"))
        chat_id = _int(chat.get("id"))
        message_id = _int(reaction.get("message_id"))
        old_emojis = _reaction_emojis(reaction.get("old_reaction"))
        new_emojis = _reaction_emojis(reaction.get("new_reaction"))
        added = new_emojis - old_emojis
        conv_key = self.store.conv_key_for_message(chat_id, message_id)
        if conv_key is None:
            if (
                chat.get("is_forum")
                or self.store.count_conversations_for_chat(chat_id) > 1
            ):
                return
            conversation = self.store.get_conversation_for_chat(chat_id)
        else:
            conversation = self.store.get_conversation(conv_key)
        if conversation is None:
            return
        async with self._lock(conversation.conv_key):
            if "🔁" in added and conversation.last_user_text:
                if message_id:
                    await self.telegram.set_message_reaction(
                        chat_id,
                        message_id,
                        "👀",
                    )
                await self.retry_conversation(
                    conversation,
                    trigger_message_id=message_id,
                )
            elif "🛑" in added:
                await self.stop_conversation(conversation)
                target = {"chat": chat, "from": user}
                if conversation.thread_id is not None:
                    target["message_thread_id"] = conversation.thread_id
                    target["is_topic_message"] = True
                await self.send_text(
                    target,
                    "Stopped session.",
                    silent=True,
                )

    async def handle_edited_message(self, message: Mapping[str, object]) -> None:
        if not is_allowed(message, self.settings, self.approved_users):
            return
        if not should_respond_in_group(
            message,
            self.bot_username,
            self.settings.free_response_chats,
        ):
            return
        text = _expand_text_links(message, "text")
        if text is None:
            text = _expand_text_links(message, "caption")
        if text is None:
            return
        if _mapping(message.get("chat")).get("type") in {"group", "supergroup"}:
            text = strip_bot_mention(text, self.bot_username)
        conv_key = self._conversation_key(message)
        message_id = _int(message.get("message_id"))
        if text.startswith("/"):
            return
        pending = self.pending_turns.get(conv_key, [])
        for index, (pending_message, _, attachment) in enumerate(pending):
            if _int(pending_message.get("message_id")) != message_id:
                continue
            pending[index] = (
                message,
                self._contextualize_message(message, text),
                attachment,
            )
            if message_id:
                await self.telegram.set_message_reaction(
                    _int(_mapping(message.get("chat")).get("id")),
                    message_id,
                    "✏️",
                )
            return
        conversation = self.store.get_conversation(conv_key)
        if conversation is None:
            return
        if conversation.last_user_message_id != message_id:
            return
        async with self._lock(conv_key):
            conversation = self.store.get_conversation(conv_key)
            if conversation is None:
                return
            if conversation.last_user_message_id != message_id:
                return
            if conversation.last_user_text == text:
                return
            self.store.update_conversation(
                conv_key,
                conversation.session_id,
                last_user_text=text,
                last_user_message_id=message_id,
            )
            await self.devin.send_message(
                conversation.session_id,
                f"Correction to my previous message: {text}",
            )
            message_id = _int(message.get("message_id"))
            if message_id:
                await self.telegram.set_message_reaction(
                    conversation.chat_id,
                    message_id,
                    "✏️",
                )
            updated = self.store.get_conversation(conv_key) or conversation
            await self.start_watcher(
                updated,
                trigger_message_id=message_id,
            )

    async def retry_conversation(
        self,
        conversation: Conversation,
        *,
        trigger_message_id: int | None = None,
    ) -> None:
        if not conversation.last_user_text:
            return
        existing = self.watchers.pop(conversation.session_id, None)
        self.active_watchers.pop(conversation.session_id, None)
        if existing is not None and not existing.done():
            existing.cancel()
            await asyncio.gather(existing, return_exceptions=True)
        self.store.update_conversation(
            conversation.conv_key,
            conversation.session_id,
            last_user_text=conversation.last_user_text,
        )
        try:
            await self.devin.send_message(
                conversation.session_id,
                conversation.last_user_text,
            )
        except Exception:
            current = self.store.get_conversation(conversation.conv_key) or conversation
            await self.start_watcher(current)
            raise
        await self.start_watcher(
            conversation,
            trigger_message_id=trigger_message_id,
        )

    async def stop_conversation(self, conversation: Conversation) -> None:
        task = self.watchers.pop(conversation.session_id, None)
        self.active_watchers.pop(conversation.session_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self.devin.terminate(conversation.session_id)
        except Exception:
            current = self.store.get_conversation(conversation.conv_key) or conversation
            await self.start_watcher(current)
            raise
        self.clear_queued_turns(conversation.conv_key)
        self.store.clear_conversation(
            conversation.conv_key,
            conversation.session_id,
        )

    async def _handle_long_text_callback(
        self,
        callback_id: str,
        callback_message: Mapping[str, object],
        conv_key: str,
        token: str,
    ) -> None:
        stored = self.store.get_long_text(token)
        chat = _mapping(callback_message.get("chat"))
        chat_id = _int(chat.get("id"))
        if stored is None or stored[0] != conv_key or stored[1] != chat_id:
            await self.telegram.answer_callback_query(callback_id, "This page expired")
            return
        _, _, remaining, _ = stored
        limit = max(1, self.settings.telegram_long_reply_chars)
        if len(remaining) > 4 * limit:
            document = await self.telegram.send_document(
                chat_id,
                "reply.md",
                remaining.encode(),
                thread_id=_thread_id(callback_message),
                reply_to=_int(callback_message.get("message_id")) or None,
            )
            self._index_outbound(chat_id, conv_key, document)
            results = await self.telegram.send_markdown(
                chat_id,
                remaining[:500],
                thread_id=_thread_id(callback_message),
                reply_to_message_id=_int(callback_message.get("message_id")) or None,
            )
            self._index_outbound_many(chat_id, conv_key, results)
            try:
                await self.telegram.edit_message_reply_markup(
                    chat_id,
                    _int(callback_message.get("message_id")),
                )
            except (RuntimeError, httpx.HTTPError):
                logger.debug("Failed to clear long-text keyboard", exc_info=True)
            finally:
                await self.telegram.answer_callback_query(callback_id)
                self.store.delete_long_text(token)
            return
        body, documents = extract_large_code_blocks(remaining)
        for filename, content in documents:
            document = await self.telegram.send_document(
                chat_id,
                filename,
                content,
                thread_id=_thread_id(callback_message),
            )
            self._index_outbound(chat_id, conv_key, document)
        if len(body) > limit:
            page, rest = split_long_text(body, limit)
        else:
            page, rest = body, ""
        markup: dict[str, object] | None = None
        if rest:
            next_token = secrets.token_urlsafe(12)
            self.store.add_long_text(next_token, conv_key, chat_id, rest)
            markup = {
                "inline_keyboard": [[
                    {"text": "Show more ▾", "callback_data": f"more:{next_token}"}
                ]]
            }
        results = await self.telegram.send_markdown(
            chat_id,
            page,
            thread_id=_thread_id(callback_message),
            reply_markup=markup,
            reply_to_message_id=_int(callback_message.get("message_id")) or None,
        )
        self._index_outbound_many(chat_id, conv_key, results)
        try:
            await self.telegram.edit_message_reply_markup(
                chat_id,
                _int(callback_message.get("message_id")),
            )
        except (RuntimeError, httpx.HTTPError):
            logger.debug("Failed to clear long-text keyboard", exc_info=True)
        finally:
            await self.telegram.answer_callback_query(callback_id)
            self.store.delete_long_text(token)

    async def send_text(
        self,
        message: Mapping[str, object],
        text: str,
        *,
        silent: bool = False,
        ephemeral: bool = False,
    ) -> int | None:
        chat = _mapping(message.get("chat"))
        chat_id = _int(chat.get("id"))
        sender = _mapping(message.get("from"))
        receiver_user_id = (
            _int(sender.get("id"))
            if ephemeral and chat.get("type") in {"group", "supergroup"}
            else None
        )
        try:
            results = await self.telegram.send_markdown(
                chat_id,
                text,
                thread_id=_thread_id(message),
                disable_notification=(
                    silent or self._conversation_silent(self._conversation_key(message))
                ),
                receiver_user_id=receiver_user_id,
            )
            conv_key = self._conversation_key(message)
            for result in results:
                sent_id = result.get("message_id")
                if isinstance(sent_id, int):
                    self.store.index_message(chat_id, sent_id, conv_key)
            return _sent_message_id(results)
        except RuntimeError as exc:
            if receiver_user_id is None or "ephemeral" not in str(exc).casefold():
                raise
            results = await self.telegram.send_markdown(
                chat_id,
                text,
                thread_id=_thread_id(message),
                disable_notification=(
                    silent or self._conversation_silent(self._conversation_key(message))
                ),
            )
            conv_key = self._conversation_key(message)
            for result in results:
                sent_id = result.get("message_id")
                if isinstance(sent_id, int):
                    self.store.index_message(chat_id, sent_id, conv_key)
            return _sent_message_id(results)

    def _index_outbound(
        self,
        chat_id: int,
        conv_key: str,
        result: Mapping[str, object],
    ) -> None:
        message_id = result.get("message_id")
        if isinstance(message_id, int):
            self.store.index_message(chat_id, message_id, conv_key)

    def _index_outbound_many(
        self,
        chat_id: int,
        conv_key: str,
        results: list[dict[str, object]],
    ) -> None:
        for result in results:
            self._index_outbound(chat_id, conv_key, result)

    async def send_markup(
        self,
        message: Mapping[str, object],
        text: str,
        markup: dict[str, object],
        *,
        ephemeral: bool = False,
    ) -> int | None:
        chat = _mapping(message.get("chat"))
        sender = _mapping(message.get("from"))
        receiver_user_id = (
            _int(sender.get("id"))
            if ephemeral and chat.get("type") in {"group", "supergroup"}
            else None
        )
        try:
            results = await self.telegram.send_markdown(
                _int(chat.get("id")),
                text,
                thread_id=_thread_id(message),
                reply_markup=markup,
                receiver_user_id=receiver_user_id,
            )
            return _sent_message_id(results)
        except RuntimeError as exc:
            if receiver_user_id is None or "ephemeral" not in str(exc).casefold():
                raise
            results = await self.telegram.send_markdown(
                _int(chat.get("id")),
                text,
                thread_id=_thread_id(message),
                reply_markup=markup,
            )
            return _sent_message_id(results)

    async def get_session_status(self, session_id: str) -> str:
        return (await self.devin.get_session(session_id)).status_enum

    async def send_session_message(self, session_id: str, text: str) -> None:
        await self.devin.send_message(session_id, text)

    async def react(self, message: Mapping[str, object], emoji: str) -> None:
        try:
            await self.telegram.set_message_reaction(
                _int(_mapping(message.get("chat")).get("id")),
                _int(message.get("message_id")),
                emoji,
            )
        except (RuntimeError, httpx.HTTPError):
            logger.warning("Failed to set Telegram reaction %s", emoji, exc_info=True)

    async def get_state(self, session_id: str) -> SessionState:
        return await self.devin.get_session(session_id)

    async def create_forum_topic(self, chat_id: int, name: str) -> int:
        return await self.telegram.create_forum_topic(chat_id, name)

    async def edit_forum_topic(self, chat_id: int, thread_id: int, name: str) -> None:
        await self.telegram.edit_forum_topic(chat_id, thread_id, name)

    async def delete_forum_topic(self, chat_id: int, thread_id: int) -> None:
        await self.telegram.delete_forum_topic(chat_id, thread_id)

    async def list_playbooks(self) -> list[Playbook]:
        return await self.devin.list_playbooks()

    async def usage(self, message: Mapping[str, object]) -> None:
        if not self.settings.devin_org_id:
            await self.send_text(message, "DEVIN_ORG_ID is required for usage reporting.")
            return
        conversation = self.store.get_conversation(self._conversation_key(message))
        if conversation is None:
            await self.send_text(message, "No Devin session is active in this conversation.")
            return
        end = datetime.now(timezone.utc)
        start = max(
            end - timedelta(days=30),
            datetime.fromtimestamp(
                conversation.created_at,
                timezone.utc,
            ),
        )
        try:
            payload = await self.devin.session_consumption(
                self.settings.devin_org_id,
                conversation.session_id,
                start,
                end,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                await self.send_text(
                    message,
                    "The configured Devin key can't read consumption "
                    "(needs org consumption permission).",
                )
                return
            raise
        total = payload.get("total_acus")
        total_acus = float(total) if isinstance(total, (int, float)) else 0.0
        rows = payload.get("consumption_by_date", [])
        daily: list[tuple[str, float]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                date = row.get("date")
                amount = row.get("acus")
                if isinstance(date, str) and isinstance(amount, (int, float)):
                    daily.append((date, float(amount)))
        daily = daily[-7:]
        lines = [f"Session ACUs: {total_acus:.2f} (last 30 days)"]
        if not daily and total_acus == 0:
            lines.append(
                "No consumption data returned — the Devin API reports consumption "
                "only for Enterprise-plan organizations (service user needs "
                "ViewOrgConsumption)."
            )
        lines.extend(f"{date} · {amount:.2f}" for date, amount in reversed(daily))
        lines.append(
            "Usage is aggregated daily and refreshed roughly hourly — not real-time."
        )
        await self.send_text(message, "\n".join(lines))

    async def list_users(self, message: Mapping[str, object]) -> None:
        sender_id = _int(_mapping(message.get("from")).get("id"))
        if sender_id not in self.settings.admin_user_ids:
            return
        users = self.store.list_access_requests("approved")
        text = "\n".join(
            f"{request.user_id} · {request.first_name or request.username or 'user'}"
            for request in users
        ) or "No approved users."
        await self.send_text(message, text)

    async def self_update(self, message: Mapping[str, object], args: str) -> None:
        sender_id = _int(_mapping(message.get("from")).get("id"))
        if sender_id not in self.settings.admin_user_ids:
            return
        argv = shlex.split(self.settings.self_update_command)
        script = next(
            (token for token in argv if (_REPO_ROOT / token).is_file()),
            None,
        )
        if (
            script is None
            or not (_REPO_ROOT / ".git").exists()
            or not (_REPO_ROOT / script).resolve().is_relative_to(_REPO_ROOT.resolve())
        ):
            await self.send_text(
                message,
                "Self-update is unavailable on this install (not a git checkout).",
            )
            return
        if args.strip() == "check":
            argv.append("--check")
        exit_code, output = await self._run_command(argv, _REPO_ROOT)
        tail = "\n".join(output.strip().splitlines()[-30:]) or "(no output)"
        if exit_code != 0:
            tail = f"exit {exit_code}\n{tail}"
        await self.send_text(
            message, f"```\n{_sanitize_update_output(tail)}\n```"
        )

    async def revoke_user(
        self,
        message: Mapping[str, object],
        user_id: int,
    ) -> None:
        sender_id = _int(_mapping(message.get("from")).get("id"))
        if sender_id not in self.settings.admin_user_ids:
            return
        self.store.decide_access_request(user_id, "denied", sender_id)
        self.approved_users.discard(user_id)
        await self.send_text(message, f"Revoked access for {user_id}.")

    def _conversation_settings(self, conv_key: str):
        return self.store.get_settings(conv_key)

    def _conversation_silent(self, conv_key: str) -> bool:
        return self._conversation_settings(conv_key).silent

    def _conversation_drafts(self, conv_key: str) -> bool:
        value = self._conversation_settings(conv_key).drafts
        return self.settings.telegram_drafts if value is None else value

    def _conversation_status_after(self, conv_key: str) -> float:
        value = self._conversation_settings(conv_key).status_timer
        return self.settings.devin_status_after_seconds if value is not False else float("inf")

    async def settings_menu(
        self,
        message: Mapping[str, object],
        *,
        edit_message_id: int | None = None,
    ) -> None:
        conv_key = self._conversation_key(message)
        current = self.store.get_settings(conv_key)
        drafts = "inherit" if current.drafts is None else ("on" if current.drafts else "off")
        timer = "inherit" if current.status_timer is None else ("on" if current.status_timer else "off")
        markup = {
            "inline_keyboard": [
                [{"text": f"🔔 Notifications: {'silent' if current.silent else 'on'}", "callback_data": f"cfg:silent:{0 if current.silent else 1}"}],
                [{"text": f"✍️ Drafts: {drafts}", "callback_data": "cfg:drafts:menu"}],
                [{"text": f"⏱ Status timer: {timer}", "callback_data": "cfg:status_timer:menu"}],
                [{"text": f"📘 Default playbook: {current.default_playbook or 'none'}", "callback_data": "cfg:playbook:menu"}],
                [{"text": "Close", "callback_data": "cfg:close:1"}],
            ]
        }
        if edit_message_id is not None:
            await self.telegram.edit_message_reply_markup(
                _int(_mapping(message.get("chat")).get("id")),
                edit_message_id,
                markup,
            )
        else:
            await self.send_markup(message, "Conversation settings", markup)

    async def _handle_settings_callback(
        self,
        callback_id: str,
        callback_message: Mapping[str, object],
        conv_key: str,
        data: str,
    ) -> None:
        pieces = data.split(":", 2)
        chat_id = _int(_mapping(callback_message.get("chat")).get("id"))
        message_id = _int(callback_message.get("message_id"))
        if len(pieces) != 3:
            return
        field, value = pieces[1], pieces[2]
        if field == "close":
            await self.telegram.edit_message_reply_markup(chat_id, message_id)
        elif value == "menu":
            if field == "playbook":
                rows = [[{"text": "None", "callback_data": "cfg:playbook:none"}]]
                for playbook in await self.devin.list_playbooks():
                    rows.append([{
                        "text": playbook.title,
                        "callback_data": f"cfg:playbook:{playbook.playbook_id}",
                    }])
                await self.telegram.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    {"inline_keyboard": rows},
                )
            else:
                values = ["inherit", "on", "off"]
                await self.telegram.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    {"inline_keyboard": [[{
                        "text": value,
                        "callback_data": f"cfg:{field}:{value}",
                    }] for value in values]},
                )
        else:
            if field == "silent":
                self.store.update_settings(conv_key, silent=value == "1")
            elif field in {"drafts", "status_timer"}:
                parsed = None if value == "inherit" else value == "on"
                self.store.update_settings(conv_key, **{field: parsed})
            elif field == "playbook":
                self.store.update_settings(
                    conv_key,
                    default_playbook=None if value == "none" else value,
                )
            await self.settings_menu(callback_message, edit_message_id=message_id)
        await self.telegram.answer_callback_query(callback_id)

    async def _handle_access_callback(
        self,
        callback: Mapping[str, object],
        data: str,
    ) -> None:
        sender = _mapping(callback.get("from"))
        sender_id = _int(sender.get("id"))
        if data == "acc:req":
            callback_chat_id = _int(
                _mapping(_mapping(callback.get("message")).get("chat")).get("id")
            )
            if callback_chat_id != sender_id:
                callback_id = _text(callback.get("id"))
                if callback_id:
                    await self.telegram.answer_callback_query(
                        callback_id,
                        "Not allowed",
                    )
                return
            user_id = sender_id
            callback_id = _text(callback.get("id"))
            if self._rate_limited(sender_id):
                if callback_id:
                    await self.telegram.answer_callback_query(
                        callback_id,
                        "Slow down",
                    )
                return
            request = self.store.get_access_request(user_id)
            if request is not None and request.status in {"requested", "approved"}:
                if callback_id:
                    await self.telegram.answer_callback_query(
                        callback_id,
                        "Request already pending"
                        if request.status == "requested"
                        else "Already approved",
                    )
                return
            self.store.save_access_request(
                user_id,
                _text(sender.get("username")),
                _text(sender.get("first_name")),
            )
            for admin_id in self.settings.admin_user_ids:
                await self.telegram.send_message(
                    admin_id,
                    f"Access request from {_text(sender.get('first_name')) or 'user'} "
                    f"(@{_text(sender.get('username')) or 'unknown'}, id {user_id})",
                    reply_markup={"inline_keyboard": [[
                        {"text": "Approve", "callback_data": f"acc:ok:{user_id}"},
                        {"text": "Deny", "callback_data": f"acc:no:{user_id}"},
                    ]]},
                )
            if callback_id:
                await self.telegram.answer_callback_query(callback_id, "Request sent")
            return
        if sender_id not in self.settings.admin_user_ids:
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"ok", "no"}:
            return
        try:
            user_id = int(parts[2])
        except ValueError:
            return
        approved = parts[1] == "ok"
        self.store.decide_access_request(user_id, "approved" if approved else "denied", sender_id)
        if approved:
            self.approved_users.add(user_id)
            await self.telegram.send_message(user_id, "Access approved.")
        else:
            await self.telegram.send_message(user_id, "Access request denied.")
        callback_id = _text(callback.get("id"))
        if callback_id:
            await self.telegram.answer_callback_query(callback_id)

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
        *,
        retryable: bool = False,
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
        suffix = (
            "The turn will be retried with the next message or status change."
            if retryable
            else "Use /retry."
        )
        try:
            await self.telegram.send_message(
                chat_id,
                f"Couldn't process that message: {reason}. {suffix}",
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

    async def _transcribe(
        self,
        message: Mapping[str, object],
        attachment: Attachment,
    ) -> str | None:
        filename, content, content_type = attachment
        if not any(message.get(field) is not None for field in (
            "voice",
            "audio",
            "video_note",
        )):
            return None
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.transcription_base_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {self.settings.transcription_api_key}"
                },
                timeout=30,
            ) as client:
                response = await client.post(
                    "/audio/transcriptions",
                    data={"model": self.settings.transcription_model},
                    files={"file": (filename, content, content_type)},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("Voice transcription failed", exc_info=True)
            return None
        value = payload.get("text") if isinstance(payload, dict) else None
        return value.strip() if isinstance(value, str) and value.strip() else None

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
            service_user_api_key=actual_settings.devin_service_user_api_key,
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
        polling_task: asyncio.Task[None] | None = None
        if actual_settings.telegram_mode == "polling":
            polling_task = asyncio.create_task(
                run_polling(runtime.telegram, runtime.handle_update)
            )
        yield
        if polling_task is not None:
            polling_task.cancel()
            await asyncio.gather(polling_task, return_exceptions=True)
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
        if actual_settings.telegram_mode == "polling":
            raise HTTPException(status_code=404, detail="Polling mode is active")
        if x_telegram_bot_api_secret_token != actual_settings.telegram_webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
        payload = await request.json()
        if isinstance(payload, dict):
            await runtime.handle_update(cast(Mapping[str, object], payload))
        return {"accepted": True}

    register_notify_route(application, runtime, actual_settings)
    register_doctor_route(application, actual_settings)
    application.state.bridge = runtime
    return application


app = create_app()
bridge = cast(Bridge, app.state.bridge)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _sent_message_id(results: list[dict[str, object]]) -> int | None:
    if not results:
        return None
    message_id = results[-1].get("message_id")
    return message_id if isinstance(message_id, int) else None


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


def _reaction_emojis(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    result: set[str] = set()
    for item in value:
        reaction = _mapping(item)
        if reaction.get("type") == "emoji":
            emoji = _text(reaction.get("emoji"))
            if emoji is not None:
                result.add(emoji)
    return result


def _command_name(text: str) -> str:
    first = text.split(maxsplit=1)[0]
    return first[1:].split("@", 1)[0].casefold().replace("_", "-")


def _short_reason(exc: Exception) -> str:
    text = str(exc).strip().replace("\n", " ")
    return text[:120] or "temporary error"
