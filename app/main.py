from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import shlex
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response

from app.access import (
    is_allowed,
    is_topic_chat,
    should_respond_in_group,
    strip_bot_mention,
)
from app.admin import _sanitize_update_output, drain_tasks, register_admin_route
from app.commands import SYSTEM_PREAMBLE, handle_command
from app.config import Settings, get_settings
from app.crawlers import CRAWLER_NAMES, crawl_text
from app.devin import DevinClient, Playbook, SessionState
from app.doctor import register_doctor_route
from app.formatting import (
    chunk,
    extract_large_code_blocks,
    markdown_to_telegram_markdown_v2,
    rich_paragraph,
    split_long_text,
)
from app.local import LocalClient, is_local
from app.notify import register_notify_route
from app.polling import run_polling
from app.set_webhook import configure_bot
from app.store import (
    PLACEHOLDER_TITLE_PREFIX,
    Conversation,
    Store,
)
from app.telegram import TelegramClient
from app.transcription import (
    docker_cleanup_command,
    docker_transcription_command,
    transcribe_command,
    transcribe_local,
    transcribe_whispercpp,
)
from app.watcher import ACTIVE_STATUSES, SessionWatcher

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANNOUNCE_MAX_ATTEMPTS = 3
_ANNOUNCE_RETRY_DELAYS = (30.0, 120.0)
_UPDATE_CONCURRENCY = 8
_UPDATE_QUEUE_SIZE = 256
_MAX_PENDING_FRAGMENTS = 20
_MAX_QUEUED_TURNS = 20
_MAX_TRANSIENT_IDS = 100
_MAX_IMPLICIT_TOPICS = 512
_JANITOR_INTERVAL_SECONDS = 600


async def _run_command(
    argv: list[str],
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # deploy/self-update.sh signals this pid for a supervisor respawn
        env={**os.environ, **(env or {}), "BRIDGE_PID": str(os.getpid())},
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 300)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "timed out after 300s"
    return process.returncode or 0, stdout.decode(errors="replace")


_CHANNEL_CHARS = re.compile(r"[^\s~^:?*[\]\\]+")


def _valid_channel(token: str) -> bool:
    return (
        bool(_CHANNEL_CHARS.fullmatch(token))
        and ".." not in token
        and "@{" not in token
        and not token.startswith(("-", ".", "/"))
        and not token.endswith(("/", ".lock"))
    )


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
        self.local = LocalClient(
            cli_command=settings.devin_local_cli,
            cwd=settings.devin_local_cwd,
            api_key=settings.devin_service_user_api_key,
            pr_fetcher=self.devin.fetch_github_pr,
        )
        self.telegram = telegram
        self.bot_username = settings.bot_username or ""
        self.bot_topics_enabled = False
        self.implicit_topics: dict[tuple[int, int], float] = {}
        self.watchers: dict[str, asyncio.Task[None]] = {}
        self.active_watchers: dict[str, SessionWatcher] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.lock_refs: dict[str, int] = {}
        self.background_tasks: set[asyncio.Task[None]] = set()
        self._janitor_task: asyncio.Task[None] | None = None
        self.denied_notices: dict[tuple[int, int], float] = {}
        self._update_queue: asyncio.Queue[Mapping[str, object]] = asyncio.Queue(
            maxsize=_UPDATE_QUEUE_SIZE
        )
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._transcription_client: httpx.AsyncClient | None = None
        self._crawl_client: httpx.AsyncClient | None = None
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

    def _session_client(self, session_id: str):
        return self.local if is_local(session_id) else self.devin

    async def startup(self) -> None:
        profile = await self.telegram.get_me()
        username = _text(profile.get("username"))
        if not self.bot_username and username is not None:
            self.bot_username = username
        self.bot_topics_enabled = bool(profile.get("has_topics_enabled"))
        placeholder_fields = (
            ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
            ("DEVIN_API_KEY", "devin_api_key"),
            ("NOTIFY_SECRET", "notify_secret"),
            ("DOCTOR_SECRET", "doctor_secret"),
            ("ADMIN_SECRET", "admin_secret"),
        )
        placeholder_names = [
            env_name
            for env_name, attr in placeholder_fields
            if "replace-with" in (getattr(self.settings, attr) or "")
        ]
        if placeholder_names:
            logger.warning(
                "placeholder secret values still configured: %s — "
                "replace them before production use",
                ", ".join(placeholder_names),
            )
        logger.info(
            "bridge ready: mode=%s bot=%s topics=%s db=%s allowed_users=%d "
            "admins=%d allow_all=%s",
            self.settings.telegram_mode,
            self.bot_username or "?",
            self.bot_topics_enabled,
            self.settings.database_path,
            len(self.settings.allowed_users),
            len(self.settings.admin_user_ids),
            self.settings.telegram_allow_all_users,
        )
        self.store.cleanup_long_texts()
        self.store.cleanup_reports()
        self.store.cleanup_message_index()
        self.approved_users = {
            request.user_id
            for request in self.store.list_access_requests("approved")
        }
        self._janitor_task = asyncio.create_task(self._janitor())
        if not self._worker_tasks:
            self._worker_tasks = [
                asyncio.create_task(self._update_worker())
                for _ in range(_UPDATE_CONCURRENCY)
            ]
        await self._resume_watchers()
        if not await self._announce_update():
            task = asyncio.create_task(self._retry_announce_update())
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)

    async def _resume_watchers(self) -> None:
        # Active sessions can outlive the short timeout now, so recover over
        # the wider window too — each resumed watcher self-selects on first
        # poll (non-active sessions close within the settle window).
        since = time.time() - max(
            self.settings.devin_active_watch_timeout_seconds,
            self.settings.devin_watch_timeout_seconds,
        )
        for conversation in self.store.list_recent_conversations(since):
            await self.start_watcher(
                conversation,
                resume_from=conversation.created_at,
                trigger_message_id=conversation.last_user_message_id,
            )

    async def _retry_announce_update(self) -> None:
        for delay in _ANNOUNCE_RETRY_DELAYS:
            await asyncio.sleep(delay)
            if await self._announce_update():
                return
        await self._announce_update()

    async def _announce_update(self) -> bool:
        """Return True once the pending marker is consumed (sent or given up)."""
        try:
            marker = _REPO_ROOT / ".self-update-pending"
            if not marker.exists():
                return True
            lines = marker.read_text(encoding="utf-8").splitlines()
            old = lines[0].strip() if len(lines) > 0 else "?"
            new = lines[1].strip() if len(lines) > 1 else "?"
            target = lines[2].strip() if len(lines) > 2 else ""
            attempts = int(lines[3]) if len(lines) > 3 and lines[3].strip() else 0
            if attempts >= _ANNOUNCE_MAX_ATTEMPTS:
                marker.unlink()
                logger.warning("giving up on self-update announcement")
                return True
            marker.write_text(
                f"{old}\n{new}\n{target}\n{attempts + 1}\n", encoding="utf-8"
            )
            text = f"Bridge updated {old[:7]} → {new[:7]} and back online."
            try:
                _, log = await self._run_command(
                    ["git", "log", "--oneline", f"{old}..{new}"], _REPO_ROOT
                )
                log_lines = _sanitize_update_output(log.strip()).splitlines()[:10]
                if log_lines:
                    text += "\n```\n" + "\n".join(log_lines) + "\n```"
            except Exception:
                logger.warning("failed to build update changelog", exc_info=True)
            if target:
                chat_part, _, thread_part = target.partition(":")
                await self.telegram.send_markdown(
                    int(chat_part),
                    text,
                    thread_id=int(thread_part) if thread_part else None,
                )
            else:
                try:
                    await self.notify(
                        text,
                        chat_id=None,
                        thread_id=None,
                        silent=True,
                        markdown=True,
                    )
                except ValueError:
                    logger.info("update installed but no home chat configured")
            marker.unlink()
            return True
        except Exception:
            logger.warning("failed to announce self-update", exc_info=True)
            return False

    async def shutdown(self) -> None:
        self.shutting_down = True
        for task in self.debounce_tasks.values():
            task.cancel()
        if self.debounce_tasks:
            await asyncio.gather(*self.debounce_tasks.values(), return_exceptions=True)
        conv_keys = set(self.queued_turns) | set(self.pending_turns)
        for conv_key in conv_keys:
            queued = self.queued_turns.pop(conv_key, [])
            pending = self.pending_turns.pop(conv_key, [])
            for message, text, attachment in self._coalesce_turns(
                [*queued, *pending]
            ):
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
        for task in self.watchers.values():
            task.cancel()
        if self.watchers:
            await asyncio.gather(*self.watchers.values(), return_exceptions=True)
        if self._janitor_task is not None:
            self._janitor_task.cancel()
            await asyncio.gather(self._janitor_task, return_exceptions=True)
            self._janitor_task = None
        for task in self._worker_tasks:
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks = []
        for task in self.background_tasks:
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        await self.local.aclose()
        await self.devin.close()
        await self.telegram.close()
        if self._transcription_client is not None:
            await self._transcription_client.aclose()
        if self._crawl_client is not None:
            await self._crawl_client.aclose()
        self.store.close()

    async def handle_update(self, update: Mapping[str, object]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or not self.store.mark_update_seen(update_id):
            return
        if not self._worker_tasks and not self.shutting_down:
            self._worker_tasks = [
                asyncio.create_task(self._update_worker())
                for _ in range(_UPDATE_CONCURRENCY)
            ]
        # Blocks when the bounded queue is full: webhook callers get
        # backpressure instead of an unbounded pile of pending tasks.
        try:
            await self._update_queue.put(update)
        except asyncio.CancelledError:
            # Cancelled while suspended on a full queue: the update was never
            # enqueued, so release the dedup marker or Telegram's retry is
            # silently dropped.
            self.store.unmark_update_seen(update_id)
            raise

    async def _update_worker(self) -> None:
        while True:
            update = await self._update_queue.get()
            try:
                await self._dispatch_update(update)
            except Exception:
                logger.exception("Telegram update worker dispatch failed")
            finally:
                self._update_queue.task_done()

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
                    if len(self.implicit_topics) < _MAX_IMPLICIT_TOPICS:
                        self.implicit_topics[key] = time.time()
                else:
                    self.implicit_topics.pop(key, None)
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
                self.denied_notices[key] = time.time()
                await self._send_to_ids(
                    chat_id,
                    f"This bot is private. Your user id is {user_id}.",
                )
            logger.warning("Rejected Telegram message user=%s chat=%s", user_id, chat_id)
            return
        self.store.bump_user_stats(user_id, messages=1)
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
                "repos",
                "platform",
                "mode",
                "acu",
                "tags",
                "secrets",
                "knowledge",
                "snapshot",
                "crawl",
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
                    "⏳ Slow down — try again in a moment.",
                    thread_id=_thread_id(message),
                )
            return
        attachment = await self._attachment(message)
        if attachment is not None and self.settings.transcription_enabled:
            transcript = await self._transcribe(message, attachment)
            if transcript is not None:
                text = (
                    f"Voice note transcript:\n{transcript}"
                    + (f"\n\n{text}" if text else "")
                )
                await self._react(
                    chat_id,
                    _int(message.get("message_id")),
                    "✍",
                )
                if not self.settings.telegram_attach_voice:
                    attachment = None
        if not text and attachment is None:
            await self.telegram.send_message(
                chat_id,
                "ℹ Unsupported message type — send text, a photo, a document, or a voice note.",
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
        if len(pending) >= _MAX_PENDING_FRAGMENTS:
            # Install the new list with this fragment first so a concurrent
            # fragment cannot overtake it while the overflow batch flushes.
            overflow = pending
            self.pending_turns[conv_key] = [(message, text, attachment)]
            pending = self.pending_turns[conv_key]
            await self._flush_fragments(conv_key, overflow)
        else:
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
        await self._flush_fragments(
            conv_key, self.pending_turns.pop(conv_key, [])
        )

    async def _flush_fragments(
        self, conv_key: str, fragments: list[TurnFragment]
    ) -> None:
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
                queue = self.queued_turns.setdefault(conv_key, [])
                if len(queue) >= _MAX_QUEUED_TURNS:
                    await self.telegram.send_message(
                        _int(_mapping(message.get("chat")).get("id")),
                        "⚠ Too many queued messages — please wait for the "
                        "current turn to finish.",
                        thread_id=_thread_id(message),
                    )
                else:
                    queue.append(turn)
                    await self.react(message, "🤔")
                return
            queued = self.queued_turns.pop(conv_key, [])
            batches = self._coalesce_turns([*queued, turn])
            while batches:
                batch = batches.pop(0)
                if batches:
                    self.queued_turns[conv_key] = batches
                else:
                    self.queued_turns.pop(conv_key, None)
                batch_message, batch_text, batch_attachment = batch
                try:
                    await self.handle_user_turn(
                        batch_message,
                        batch_text,
                        attachment=batch_attachment,
                    )
                except Exception as exc:
                    self.queued_turns[conv_key] = [
                        batch,
                        *self.queued_turns.get(conv_key, []),
                    ]
                    logger.exception(
                        "Failed to flush queued Telegram turn for %s",
                        conv_key,
                    )
                    await self._report_processing_failure(
                        {"message": batch_message},
                        exc,
                        retryable=True,
                    )
                    return
                batches = self._coalesce_turns(self.queued_turns.pop(conv_key, []))
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

    def _coalesce_turns(self, turns: list[QueuedTurn]) -> list[QueuedTurn]:
        groups: list[list[QueuedTurn]] = []
        for turn in turns:
            group = groups[-1] if groups else []
            if group and turn[2] is not None and any(
                item[2] is not None for item in group
            ):
                group = []
            if not group:
                groups.append(group)
            group.append(turn)
        batches: list[QueuedTurn] = []
        for group in groups:
            for merged_message, _, _ in group[:-1]:
                merged_chat_id = _int(
                    _mapping(merged_message.get("chat")).get("id")
                )
                merged_message_id = _int(merged_message.get("message_id"))
                if merged_message_id:
                    self.store.index_message(
                        merged_chat_id,
                        merged_message_id,
                        self._conversation_key(merged_message),
                    )
            batches.append((
                group[-1][0],
                "\n\n".join(text for _, text, _ in group if text),
                next((value for _, _, value in group if value is not None), None),
            ))
        return batches

    async def _drain_queue(self, conv_key: str) -> None:
        try:
            if conv_key in self.draining or self._conversation_busy(conv_key):
                return
            self.draining.add(conv_key)
            queued = self.queued_turns.pop(conv_key, [])
            if not queued or self._conversation_busy(conv_key):
                if queued:
                    self.queued_turns[conv_key] = queued
                return
            batches = self._coalesce_turns(queued)
            while batches:
                batch = batches.pop(0)
                if batches:
                    self.queued_turns[conv_key] = batches
                else:
                    self.queued_turns.pop(conv_key, None)
                message, text, attachment = batch
                if self._conversation_busy(conv_key):
                    self.queued_turns[conv_key] = [
                        batch,
                        *self.queued_turns.get(conv_key, []),
                    ]
                    return
                try:
                    await self.handle_user_turn(message, text, attachment=attachment)
                except Exception as exc:
                    self.queued_turns[conv_key] = [
                        batch,
                        *self.queued_turns.get(conv_key, []),
                    ]
                    logger.exception(
                        "Failed to drain Telegram turn for %s",
                        conv_key,
                    )
                    await self._report_processing_failure(
                        {"message": message},
                        exc,
                        retryable=True,
                    )
                    return
                batches = self._coalesce_turns(self.queued_turns.pop(conv_key, []))
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

    async def _janitor(self) -> None:
        while True:
            await asyncio.sleep(_JANITOR_INTERVAL_SECONDS)
            try:
                now_monotonic = time.monotonic()
                for user_id, window in list(self.rate_windows.items()):
                    while window and window[0] <= now_monotonic - 60:
                        window.popleft()
                    if not window:
                        self.rate_windows.pop(user_id, None)
                for user_id, warned_at in list(self.rate_warnings.items()):
                    if now_monotonic - warned_at >= 300:
                        self.rate_warnings.pop(user_id, None)
                now_wall = time.time()
                for user_id, prompted_at in list(self.access_prompted.items()):
                    if now_wall - prompted_at >= 86400:
                        self.access_prompted.pop(user_id, None)
                for key, noticed_at in list(self.denied_notices.items()):
                    if now_wall - noticed_at >= 86400:
                        self.denied_notices.pop(key, None)
                for key, created_at in list(self.implicit_topics.items()):
                    if now_wall - created_at >= 86400:
                        self.implicit_topics.pop(key, None)
                self.store.cleanup_message_index()
                self.store.cleanup_long_texts()
                self.store.cleanup_reports()
            except Exception:
                logger.exception("Janitor sweep failed")

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
        await self._react(chat_id, message_id, "👀")
        await self.telegram.send_chat_action(chat_id, thread_id=thread_id)
        enabled_crawls = self.crawl_sites()
        if enabled_crawls:
            text = await self._append_crawled_content(text, enabled_crawls)
        if attachment is not None:
            filename, content, content_type = attachment
            # route by the session's backend, not the current setting — a
            # /platform flip must not misroute files for an existing session
            local_dest = (
                is_local(conversation.session_id)
                if conversation is not None
                else self.store.get_settings(conv_key).platform == "local"
            )
            if local_dest:
                # local sessions can't receive files, and uploading to cloud
                # storage would defeat running locally
                text = f"{text}\n\nAttached file: {filename} (not sent — local sessions can't receive files)".strip()
            else:
                url = await self.devin.upload_attachment(filename, content, content_type)
                text = f"{text}\n\nAttached file: {url} ({filename})".strip()
        if not text:
            text = "Please inspect the attached file."
        sent_at: float | None = None
        if conversation is not None and await self._is_finished(conversation.session_id):
            conversation = None
        if conversation is not None:
            self.store.update_conversation(
                conv_key,
                conversation.session_id,
                last_user_text=text,
                last_user_message_id=message_id,
            )
            try:
                sent_at = time.time()
                await self.send_session_message(conversation.session_id, text)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {404, 410}:
                    raise
                logger.warning(
                    "Session %s is gone (%s); starting a new one",
                    conversation.session_id,
                    exc.response.status_code,
                )
                conversation = None
            else:
                conversation = self.store.get_conversation(conv_key) or conversation
        if conversation is None:
            title = self.store.get_setting(f"pending_title:{conv_key}")
            conversation = await self.create_session_for_message(
                message,
                SYSTEM_PREAMBLE + text,
                title,
                playbook_id=self.store.get_settings(conv_key).default_playbook,
                last_user_text=text,
                start_watcher=False,
                keep_queued=True,
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
                    self.implicit_topics.pop((chat_id, thread_id), None)
        await self.start_watcher(
            conversation, trigger_message_id=message_id, trigger_at=sent_at
        )

    async def _append_crawled_content(
        self,
        text: str,
        enabled: set[str],
    ) -> str:
        """Crawl enabled URLs in a message and append extracted content."""
        if self._crawl_client is None:
            self._crawl_client = httpx.AsyncClient(
                timeout=15.0,
                limits=httpx.Limits(max_connections=10),
            )
        results = await crawl_text(text, enabled, self._crawl_client)
        blocks: list[str] = []
        for result in results:
            lines = [f"[Crawled {result.site}: {result.url}]", result.text]
            for filename, content, content_type in result.images:
                try:
                    url = await self.devin.upload_attachment(
                        filename, content, content_type
                    )
                except (httpx.HTTPError, TypeError, RuntimeError):
                    continue
                lines.append(f"Attached file: {url} ({filename})")
            blocks.append("\n".join(lines))
        if not blocks:
            return text
        return f"{text}\n\n" + "\n\n".join(blocks) if text else "\n\n".join(blocks)

    async def create_session_for_message(
        self,
        message: Mapping[str, object],
        prompt: str,
        title: str | None,
        *,
        playbook_id: str | None = None,
        last_user_text: str | None = None,
        start_watcher: bool = True,
        keep_queued: bool = False,
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
        conv_settings = self.store.get_settings(conv_key)
        if conv_settings.platform == "local":
            # local CLI sessions don't take the cloud session options
            if playbook_id is not None:
                await self.send_text(
                    message,
                    "⚠ playbooks aren't supported on local sessions — starting without it",
                    silent=True,
                )
            session_id, session_url = await self.local.create_session(
                prompt,
                title,
                mode=conv_settings.devin_mode,
                model=conv_settings.local_model,
            )
        else:
            session_id, session_url = await self.devin.create_session(
                prompt,
                title,
                playbook_id,
                devin_mode=conv_settings.devin_mode,
                repos=conv_settings.repo_list,
                platform=conv_settings.platform,
                acu_limit=conv_settings.acu_limit,
                tags=conv_settings.tag_list,
                secret_ids=conv_settings.secret_id_list,
                knowledge_ids=conv_settings.knowledge_id_list,
                snapshot_id=conv_settings.snapshot_id,
                unlisted=conv_settings.unlisted,
                idempotent=conv_settings.idempotent,
            )
        self.store.delete_setting(f"pending_title:{conv_key}")
        stored_title = (
            title
            if title is not None
            else f"{PLACEHOLDER_TITLE_PREFIX}{(last_user_text or prompt)[:60]}"
        )
        conversation = await self.replace_conversation(
            conv_key=conv_key,
            chat_id=chat_id,
            thread_id=thread_id,
            session_id=session_id,
            session_url=session_url,
            title=stored_title,
            title_pending=title is None,
            last_user_text=last_user_text or prompt,
            last_user_message_id=(
                _int(message.get("message_id"))
                if last_user_text is not None
                else None
            ),
            keep_queued=keep_queued,
        )
        self.store.add_history(
            conv_key=conv_key,
            session_id=session_id,
            session_url=session_url,
            title=stored_title,
            title_pending=title is None,
        )
        sender_id = _int(_mapping(message.get("from")).get("id"))
        if sender_id:
            self.store.bump_user_stats(sender_id, sessions=1)
        started_id = await self.send_text(
            message,
            f"◆ Started session\n{session_url}",
            silent=True,
        )
        if started_id is not None:
            transient = self.transient_messages.setdefault(conv_key, [])
            if len(transient) < _MAX_TRANSIENT_IDS:
                transient.append(started_id)
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
        title_pending: bool = False,
        last_event_id: str | None = None,
        last_user_text: str | None = None,
        last_user_message_id: int | None = None,
        last_pr_url: str | None = None,
        keep_queued: bool = False,
    ) -> Conversation:
        previous = self.store.get_conversation(conv_key)
        if previous is not None and previous.session_id != session_id:
            if not keep_queued:
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
            title_pending=title_pending,
            last_event_id=last_event_id,
            last_user_text=last_user_text,
            last_user_message_id=last_user_message_id,
            last_pr_url=last_pr_url,
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
        resume_from: float | None = None,
        trigger_at: float | None = None,
    ) -> None:
        existing = self.watchers.get(conversation.session_id)
        if existing is not None and not existing.done():
            watcher = self.active_watchers.get(conversation.session_id)
            if watcher is not None and trigger_message_id is not None:
                old_trigger = watcher.trigger_message_id
                watcher.set_trigger(trigger_message_id, at=trigger_at)
                if old_trigger is not None and old_trigger != trigger_message_id:
                    await self._react(conversation.chat_id, old_trigger, "👍")
            return
        conv_settings = self._conversation_settings(conversation.conv_key)
        watcher = SessionWatcher(
            conversation,
            self.store,
            self._session_client(conversation.session_id),
            self.telegram,
            self.settings,
            poll_seconds=poll_seconds,
            trigger_message_id=trigger_message_id,
            transient_message_ids=self.transient_messages.pop(
                conversation.conv_key,
                [],
            ),
            drafts_enabled=(
                self.settings.telegram_drafts
                if conv_settings.drafts is None
                else conv_settings.drafts
            ),
            status_after_seconds=(
                self.settings.devin_status_after_seconds
                if conv_settings.status_timer is not False
                else float("inf")
            ),
            silent=conv_settings.silent,
            has_queued=lambda: self.queued_count(conversation.conv_key) > 0,
            resume_from=resume_from,
            trigger_at=trigger_at,
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
        user_id = _int(sender.get("id"))
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
                    user_id=user_id,
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
            if (
                not option.startswith("__cmd:")
                and self._rate_limited(user_id)
            ):
                await self.telegram.answer_callback_query(
                    callback_id, "⏳ Slow down — try again in a moment."
                )
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
                await self._session_client(session_id).send_message(session_id, option)
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
                if self._rate_limited(_int(user.get("id"))):
                    return
                await self._react(chat_id, message_id, "👀")
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
                    "🛑 Stopped session.",
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
            await self._react(
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
            if self._rate_limited(_int(_mapping(message.get("from")).get("id"))):
                return
            self.store.update_conversation(
                conv_key,
                conversation.session_id,
                last_user_text=text,
                last_user_message_id=message_id,
            )
            sent_at = time.time()
            await self._session_client(conversation.session_id).send_message(
                conversation.session_id,
                f"Correction to my previous message: {text}",
            )
            message_id = _int(message.get("message_id"))
            await self._react(conversation.chat_id, message_id, "✏️")
            updated = self.store.get_conversation(conv_key) or conversation
            await self.start_watcher(
                updated,
                trigger_message_id=message_id,
                trigger_at=sent_at,
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
            sent_at = time.time()
            await self._session_client(conversation.session_id).send_message(
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
            trigger_at=sent_at,
        )

    async def detach_conversation(self, conversation: Conversation) -> None:
        task = self.watchers.pop(conversation.session_id, None)
        self.active_watchers.pop(conversation.session_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.clear_queued_turns(conversation.conv_key)
        self.store.clear_conversation(
            conversation.conv_key,
            conversation.session_id,
        )

    async def stop_conversation(self, conversation: Conversation) -> None:
        task = self.watchers.pop(conversation.session_id, None)
        self.active_watchers.pop(conversation.session_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self._session_client(conversation.session_id).terminate(conversation.session_id)
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

    async def _send_parts(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None,
        disable_notification: bool,
        receiver_user_id: int | None,
        html: bool,
        rich: list[dict[str, object]] | None,
    ) -> list[dict[str, object]]:
        if rich is not None and self.telegram.rich_enabled:
            try:
                result = await self.telegram.send_rich_message(
                    chat_id,
                    blocks=rich,
                    thread_id=thread_id,
                    disable_notification=disable_notification,
                    receiver_user_id=receiver_user_id,
                )
                return [result]
            except RuntimeError as exc:
                reason = str(exc).casefold()
                if (
                    "method not found" in reason
                    or ("method" in reason and "not found" in reason)
                    or "unknown method" in reason
                ):
                    self.telegram.rich_enabled = False
                else:
                    logger.warning(
                        "send_rich_message blocks failed, falling back: %s",
                        exc,
                    )
        if html:
            result = await self.telegram.send_message(
                chat_id,
                text,
                thread_id=thread_id,
                parse_mode="HTML",
                disable_notification=disable_notification,
                receiver_user_id=receiver_user_id,
            )
            return [result]
        return await self.telegram.send_markdown(
            chat_id,
            text,
            thread_id=thread_id,
            disable_notification=disable_notification,
            receiver_user_id=receiver_user_id,
        )

    async def send_text(
        self,
        message: Mapping[str, object],
        text: str,
        *,
        silent: bool = False,
        ephemeral: bool = False,
        html: bool = False,
        rich: list[dict[str, object]] | None = None,
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
            results = await self._send_parts(
                chat_id,
                text,
                thread_id=_thread_id(message),
                disable_notification=(
                    silent or self._conversation_silent(self._conversation_key(message))
                ),
                receiver_user_id=receiver_user_id,
                html=html,
                rich=rich,
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
            results = await self._send_parts(
                chat_id,
                text,
                thread_id=_thread_id(message),
                disable_notification=(
                    silent or self._conversation_silent(self._conversation_key(message))
                ),
                receiver_user_id=None,
                html=html,
                rich=rich,
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
        return (
            await self._session_client(session_id).get_session(session_id, fetch_messages=False)
        ).status_enum

    async def send_session_message(self, session_id: str, text: str) -> None:
        await self._session_client(session_id).send_message(session_id, text)

    async def _react(
        self, chat_id: int, message_id: int | None, emoji: str | None
    ) -> None:
        await self.telegram.react(chat_id, message_id, emoji)

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
        return await self._session_client(session_id).get_session(session_id)

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
        rich: list[dict[str, object]] = [rich_paragraph(lines[0])]
        if not daily and total_acus == 0:
            no_data = (
                "No consumption data returned — the Devin API reports consumption "
                "only for Enterprise-plan organizations (service user needs "
                "ViewOrgConsumption)."
            )
            lines.append(no_data)
            rich.append(rich_paragraph(no_data))
        if daily:
            lines.append(
                "<blockquote expandable>"
                + "\n".join(
                    f"{date} · {amount:.2f}" for date, amount in reversed(daily)
                )
                + "</blockquote>"
            )
            rich.append(
                {
                    "type": "table",
                    "is_compact": True,
                    "is_striped": True,
                    "cells": [
                        [
                            {"text": "Date", "is_header": True},
                            {"text": "ACUs", "is_header": True},
                        ],
                        *[
                            [{"text": date}, {"text": f"{amount:.2f}"}]
                            for date, amount in reversed(daily)
                        ],
                    ],
                }
            )
        lines.append(
            "Usage is aggregated daily and refreshed roughly hourly — not real-time."
        )
        rich.append(rich_paragraph(lines[-1]))
        await self.send_text(message, "\n".join(lines), html=True, rich=rich)

    async def list_users(self, message: Mapping[str, object]) -> None:
        sender_id = _int(_mapping(message.get("from")).get("id"))
        if sender_id not in self.settings.admin_user_ids:
            await self.send_text(message, "Admins only.", ephemeral=True)
            return
        entries = [
            (user_id, label or "allowed via .env")
            for user_id, label in sorted(self.settings.allowed_user_labels.items())
        ]
        entries.extend(
            (request.user_id, request.first_name or request.username or "user")
            for request in self.store.list_access_requests("approved")
            if request.user_id not in self.settings.allowed_users
        )
        lines = []
        for user_id, label in entries:
            stats = self.store.get_user_stats(user_id)
            lines.append(
                f"{user_id} · {label} · {stats.sessions} sessions · "
                f"{stats.messages} msgs · last seen {_ago(stats.last_seen_at)}"
            )
        await self.send_text(message, "\n".join(lines) or "No approved users.")

    async def self_update(self, message: Mapping[str, object], args: str) -> None:
        sender_id = _int(_mapping(message.get("from")).get("id"))
        if sender_id not in self.settings.admin_user_ids:
            await self.send_text(
                message,
                "Admins only. Add your Telegram user id (see /whoami) to "
                "TELEGRAM_ADMIN_USER_IDS (or TELEGRAM_ALLOWED_USERS) and restart "
                "the bridge.",
                ephemeral=True,
            )
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
        tokens = args.split()
        if "check" in tokens:
            argv.append("--check")
            tokens.remove("check")
        if len(tokens) > 1 or (tokens and not _valid_channel(tokens[0])):
            await self.send_text(
                message,
                "Usage: /update [check] [channel] — channel is a branch, "
                "stable, vN, vN.N, or vN.N.N.",
                ephemeral=True,
            )
            return
        if tokens:
            argv.append(tokens[0])
        await self.send_text(message, "→ Checking for updates…")
        chat_id = _int(_mapping(message.get("chat")).get("id"))
        notify_target = str(chat_id)
        thread_id = _thread_id(message)
        if thread_id is not None:
            notify_target = f"{notify_target}:{thread_id}"
        exit_code, output = await self._run_command(
            argv, _REPO_ROOT, env={"SELF_UPDATE_NOTIFY": notify_target}
        )
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

    def crawl_sites(self) -> set[str]:
        raw = self.store.get_setting("crawl_sites")
        if raw is None:
            return set(self.settings.crawl_site_set)
        return {
            part.strip().casefold()
            for part in raw.split(",")
            if part.strip()
        } & set(CRAWLER_NAMES)

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
        unlisted = "inherit" if current.unlisted is None else ("on" if current.unlisted else "off")
        idempotent = "inherit" if current.idempotent is None else ("on" if current.idempotent else "off")
        secret_count = len(current.secret_id_list or [])
        rows = [
            [{"text": f"🔔 Notifications: {'silent' if current.silent else 'on'}", "callback_data": f"cfg:silent:{0 if current.silent else 1}"}],
            [{"text": f"✍️ Drafts: {drafts}", "callback_data": "cfg:drafts:menu"}],
            [{"text": f"⏱ Status timer: {timer}", "callback_data": "cfg:status_timer:menu"}],
            [{"text": f"📘 Default playbook: {current.default_playbook or 'none'}", "callback_data": "cfg:playbook:menu"}],
        ]
        if self.devin.v3_enabled or current.platform == "local":
            rows += [
                [{"text": f"🤖 Devin mode: {current.devin_mode or 'org default'}", "callback_data": "cfg:mode:menu"}],
            ] + ([
                [{"text": f"📂 Repos: {current.repos or 'all'}", "callback_data": "cfg:repos:menu"}],
                [{"text": f"⚡ ACU limit: {current.acu_limit if current.acu_limit is not None else 'default'}", "callback_data": "cfg:acu:menu"}],
                [{"text": f"🔑 Secrets: {secret_count or 'none'}", "callback_data": "cfg:secrets:menu"}],
                [{"text": f"👁 Unlisted: {unlisted}", "callback_data": "cfg:unlisted:menu"}],
                [{"text": f"🔁 Idempotent: {idempotent}", "callback_data": "cfg:idempotent:menu"}],
            ] if self.devin.v3_enabled else []) + [
                [{"text": f"🖥 Platform: {current.platform or 'default'}", "callback_data": "cfg:platform:menu"}],
                [{
                    "text": f"🧠 Model: {current.local_model or 'cli default'}",
                    "callback_data": "cfg:model:menu",
                }] if current.platform == "local" else [],
            ]
        crawl = ",".join(sorted(self.crawl_sites())) or "off"
        rows += [
            [{"text": f"🔎 Pre-crawl: {crawl}", "callback_data": "cfg:crawl:menu"}],
            [{"text": "Close", "callback_data": "cfg:close:1"}],
        ]
        markup = {"inline_keyboard": rows}
        if edit_message_id is not None:
            await self.telegram.edit_message_reply_markup(
                _int(_mapping(message.get("chat")).get("id")),
                edit_message_id,
                markup,
            )
        else:
            await self.send_markup(message, "Conversation settings", markup)

    async def _crawl_submenu(
        self,
        callback_message: Mapping[str, object],
        message_id: int,
    ) -> None:
        enabled = self.crawl_sites()
        rows = [[{
            "text": f"{'✓ ' if name in enabled else ''}{name}",
            "callback_data": f"cfg:crawl:{name}",
        }] for name in sorted(CRAWLER_NAMES)]
        rows.append([
            {"text": "all off", "callback_data": "cfg:crawl:off"},
            {"text": "↺ env default", "callback_data": "cfg:crawl:reset"},
        ])
        rows.append([{"text": "‹ back", "callback_data": "cfg:crawl:back"}])
        await self.telegram.edit_message_reply_markup(
            _int(_mapping(callback_message.get("chat")).get("id")),
            message_id,
            {"inline_keyboard": rows},
        )

    async def _handle_settings_callback(
        self,
        callback_id: str,
        callback_message: Mapping[str, object],
        conv_key: str,
        data: str,
        *,
        user_id: int | None = None,
    ) -> None:
        pieces = data.split(":", 2)
        chat_id = _int(_mapping(callback_message.get("chat")).get("id"))
        message_id = _int(callback_message.get("message_id"))
        if len(pieces) != 3:
            return
        field, value = pieces[1], pieces[2]
        toast: str | None = None
        if field == "close":
            await self.telegram.edit_message_reply_markup(chat_id, message_id)
        elif value == "menu" or field == "modelpage":
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
            elif field == "mode":
                modes = (
                    await self.local.modes()
                    if self.store.get_settings(conv_key).platform == "local"
                    else await self.devin.devin_modes()
                )
                await self.telegram.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    {"inline_keyboard": [[{
                        "text": "org default" if mode == "default" else mode,
                        "callback_data": f"cfg:mode:{mode}",
                    }] for mode in ("default", *modes)]},
                )
            elif field == "model" or field == "modelpage":
                # the CLI can offer 100+ models — Telegram rejects oversized
                # keyboards, so page through them
                page = int(value) if field == "modelpage" and value.isdigit() else 0
                models = await self.local.models()
                per_page = 30
                start = page * per_page
                rows = [[{
                    "text": "cli default",
                    "callback_data": "cfg:model:default",
                }]]
                for model in models[start:start + per_page]:
                    callback = f"cfg:model:{model}"
                    if len(callback.encode()) > 64:
                        continue
                    rows.append([{
                        "text": (
                            f"{'✓ ' if model == self.store.get_settings(conv_key).local_model else ''}"
                            f"{model}"
                        ),
                        "callback_data": callback,
                    }])
                nav = []
                if start:
                    nav.append({
                        "text": "‹ prev",
                        "callback_data": f"cfg:modelpage:{page - 1}",
                    })
                if start + per_page < len(models):
                    nav.append({
                        "text": f"next › ({len(models) - start - per_page} more)",
                        "callback_data": f"cfg:modelpage:{page + 1}",
                    })
                if nav:
                    rows.append(nav)
                await self.telegram.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    {"inline_keyboard": rows},
                )
            elif field == "repos":
                selected = set(self.store.get_settings(conv_key).repo_list or [])
                rows = []
                for name in await self.devin.repos():
                    callback = f"cfg:repos:{name}"
                    # Telegram caps callback_data at 64 bytes; oversized
                    # names stay settable via /repos <list>.
                    if len(callback.encode()) > 64:
                        continue
                    rows.append([{
                        "text": f"{'✓ ' if name in selected else ''}{name}",
                        "callback_data": callback,
                    }])
                rows.append([{
                    "text": "all repos (default)",
                    "callback_data": "cfg:repos:all",
                }])
                await self.telegram.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    {"inline_keyboard": rows},
                )
            elif field == "platform":
                current = self.store.get_settings(conv_key).platform
                rows = [[{
                    "text": "org default",
                    "callback_data": "cfg:platform:default",
                }]]
                for name in [*await self.devin.platforms(), "local"]:
                    callback = f"cfg:platform:{name}"
                    # Telegram caps callback_data at 64 bytes; oversized
                    # pool names stay settable via /platform <name>.
                    if len(callback.encode()) > 64:
                        continue
                    label = "local (this host)" if name == "local" else name
                    rows.append([{
                        "text": f"{'✓ ' if name == current else ''}{label}",
                        "callback_data": callback,
                    }])
                await self.telegram.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    {"inline_keyboard": rows},
                )
            elif field == "acu":
                await self.telegram.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    {"inline_keyboard": [[{
                        "text": "default" if preset == "default" else str(preset),
                        "callback_data": f"cfg:acu:{preset}",
                    }] for preset in ("default", 1, 5, 10, 25, 50, 100)]},
                )
            elif field == "secrets":
                selected = set(
                    self.store.get_settings(conv_key).secret_id_list or []
                )
                rows = []
                for key, secret_id in await self.devin.secrets():
                    callback = f"cfg:secrets:{secret_id}"
                    if len(callback.encode()) > 64:
                        continue
                    rows.append([{
                        "text": f"{'✓ ' if secret_id in selected else ''}{key}",
                        "callback_data": callback,
                    }])
                rows.append([{
                    "text": "no secrets (default)",
                    "callback_data": "cfg:secrets:clear",
                }])
                await self.telegram.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    {"inline_keyboard": rows},
                )
            elif field == "crawl":
                await self._crawl_submenu(callback_message, message_id)
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
                self.store.update_chat_settings(conv_key, silent=value == "1")
            elif field in {"drafts", "status_timer", "unlisted", "idempotent"}:
                parsed = None if value == "inherit" else value == "on"
                self.store.update_chat_settings(conv_key, **{field: parsed})
            elif field == "playbook":
                self.store.update_chat_settings(
                    conv_key,
                    default_playbook=None if value == "none" else value,
                )
            elif field == "mode":
                self.store.update_chat_settings(
                    conv_key,
                    devin_mode=None if value == "default" else value,
                )
                conversation = self.store.get_conversation(conv_key)
                if conversation and is_local(conversation.session_id):
                    try:
                        await self.local.set_mode(
                            conversation.session_id,
                            "accept-edits" if value == "default" else value,
                        )
                    except (KeyError, RuntimeError):
                        pass
            elif field == "model":
                self.store.update_chat_settings(
                    conv_key,
                    local_model=None if value == "default" else value,
                )
                conversation = self.store.get_conversation(conv_key)
                if conversation and is_local(conversation.session_id):
                    try:
                        if value == "default":
                            await self.local.set_model_default(conversation.session_id)
                        else:
                            await self.local.set_model(conversation.session_id, value)
                    except (KeyError, RuntimeError):
                        pass
            elif field == "repos":
                if value == "all":
                    self.store.update_chat_settings(conv_key, repos=None)
                else:
                    selected = set(
                        self.store.get_settings(conv_key).repo_list or []
                    )
                    selected ^= {value}
                    self.store.update_chat_settings(
                        conv_key,
                        repos=",".join(sorted(selected)) or None,
                    )
            elif field == "platform":
                self.store.update_chat_settings(
                    conv_key,
                    platform=None if value == "default" else value,
                )
            elif field == "acu":
                self.store.update_chat_settings(
                    conv_key,
                    acu_limit=None if value == "default" else int(value),
                )
            elif field == "secrets":
                # Attaching org secrets exposes them to the session —
                # same admin gate as pre-crawl toggles.
                if user_id not in self.settings.admin_user_ids:
                    await self.telegram.answer_callback_query(
                        callback_id, "Admins only"
                    )
                    return
                if value == "clear":
                    self.store.update_chat_settings(conv_key, secret_ids=None)
                else:
                    selected = set(
                        self.store.get_settings(conv_key).secret_id_list or []
                    )
                    selected ^= {value}
                    self.store.update_chat_settings(
                        conv_key,
                        secret_ids=",".join(sorted(selected)) or None,
                    )
            elif field == "crawl":
                if (
                    value not in {"back", "menu"}
                    and user_id not in self.settings.admin_user_ids
                ):
                    await self.telegram.answer_callback_query(
                        callback_id, "Admins only"
                    )
                    return
                if value == "reset":
                    self.store.delete_setting("crawl_sites")
                elif value == "off":
                    self.store.set_setting("crawl_sites", "")
                elif value in CRAWLER_NAMES:
                    enabled = self.crawl_sites()
                    enabled ^= {value}
                    self.store.set_setting(
                        "crawl_sites", ",".join(sorted(enabled))
                    )
                await self.telegram.answer_callback_query(callback_id, toast)
                if value == "back":
                    await self.settings_menu(
                        callback_message, edit_message_id=message_id
                    )
                else:
                    await self._crawl_submenu(callback_message, message_id)
                return
            await self.settings_menu(callback_message, edit_message_id=message_id)
        await self.telegram.answer_callback_query(callback_id, toast)

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
        html: str | None = None,
        html_name: str = "report.html",
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
        if html is not None and not text.strip():
            text, markdown = f"Report: {html_name}", False
        rendered = markdown_to_telegram_markdown_v2(text) if markdown else text
        markup: dict[str, object] | None = None
        if html is not None and self.settings.public_base_url:
            token = secrets.token_urlsafe(16)
            self.store.add_report(
                token,
                f"notify:{target_chat}",
                target_chat,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
            )
            url = f"{self.settings.public_base_url.rstrip('/')}/r/{token}"
            markup = {"inline_keyboard": [[{"text": f"Open {html_name}", "url": url}]]}
        parts = chunk(rendered)
        sent = 0
        for index, part in enumerate(parts):
            await self.telegram.send_message(
                target_chat,
                part,
                thread_id=target_thread,
                parse_mode="MarkdownV2" if markdown else None,
                disable_notification=silent,
                reply_markup=markup if index == len(parts) - 1 else None,
            )
            sent += 1
        if html is not None and markup is None:
            await self.telegram.send_document(
                target_chat,
                html_name,
                html.encode("utf-8"),
                thread_id=target_thread,
                content_type="text/html",
                disable_notification=silent,
            )
        return sent

    async def _is_finished(self, session_id: str) -> bool:
        # "finished" (idle, awaiting input) and suspended sessions resume when
        # messaged, keeping the conversation's context; only expired ones don't.
        return (
            await self._session_client(session_id).get_session(session_id, fetch_messages=False)
        ).status_enum == "expired"

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
                f"⚠ Couldn't process that message: {reason}. {suffix}",
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
        user_id = _int(_mapping(message.get("from")).get("id"))
        language = (
            (self.store.get_setting(f"lang:{user_id}") if user_id else None)
            or self.settings.transcription_language
            or None
        )
        if language == "auto":
            language = None
        if self.settings.transcription_backend == "whispercpp":
            return await transcribe_whispercpp(
                content,
                filename,
                self.settings.whisper_cpp_bin,
                self.settings.whisper_cpp_model,
                language,
                fast=self.settings.whisper_cpp_fast,
                extra_args=self.settings.whisper_cpp_extra_argv,
            )
        if self.settings.transcription_backend == "command":
            return await transcribe_command(
                content,
                filename,
                shlex.split(self.settings.transcription_command),
                language,
            )
        if self.settings.transcription_backend == "docker":
            name = f"transcribe-{uuid.uuid4().hex}"
            return await transcribe_command(
                content,
                filename,
                docker_transcription_command(
                    self.settings.transcription_docker_image,
                    self.settings.transcription_docker_memory,
                    name,
                ),
                language,
                cleanup=docker_cleanup_command(name),
            )
        if self.settings.transcription_backend == "local":
            model_name = (
                "base"
                if self.settings.transcription_model == "whisper-1"
                else self.settings.transcription_model
            )
            return await transcribe_local(
                content,
                filename,
                model_name,
                language,
            )
        try:
            data = {"model": self.settings.transcription_model}
            if language is not None:
                data["language"] = language
            if self._transcription_client is None:
                self._transcription_client = httpx.AsyncClient(
                    base_url=self.settings.transcription_base_url.rstrip("/"),
                    headers={
                        "Authorization": (
                            f"Bearer {self.settings.transcription_api_key}"
                        )
                    },
                    timeout=30,
                )
            response = await self._transcription_client.post(
                "/audio/transcriptions",
                data=data,
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
            org_id=actual_settings.devin_org_id,
        ),
        telegram
        or TelegramClient(
            actual_settings.telegram_bot_token,
            rich_enabled=actual_settings.telegram_rich_messages,
        ),
    )

    admin_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.startup()
        if actual_settings.telegram_mode != "polling":
            await configure_bot(runtime.telegram)
        polling_task: asyncio.Task[None] | None = None
        if actual_settings.telegram_mode == "polling":
            polling_task = asyncio.create_task(
                run_polling(runtime.telegram, runtime.handle_update)
            )
        yield
        if polling_task is not None:
            polling_task.cancel()
            await asyncio.gather(polling_task, return_exceptions=True)
        # Deliver in-flight admin outcome notices before shutdown closes the
        # Telegram client (a restart self-SIGTERMs ~1s after the response).
        await drain_tasks(admin_tasks, timeout=5)
        await runtime.shutdown()

    application = FastAPI(title="Telegram–Devin Bridge", lifespan=lifespan)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/r/{token}")
    async def report(token: str) -> Response:
        stored = runtime.store.get_report(token)
        if stored is None:
            raise HTTPException(status_code=404, detail="Not found")
        content, content_type = stored
        return Response(
            content,
            media_type=content_type,
            headers={"Content-Security-Policy": "sandbox allow-scripts"},
        )

    @application.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        if actual_settings.telegram_mode == "polling":
            raise HTTPException(status_code=404, detail="Polling mode is active")
        if not hmac.compare_digest(
            x_telegram_bot_api_secret_token or "",
            actual_settings.telegram_webhook_secret or "",
        ):
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
        payload = await request.json()
        if isinstance(payload, dict):
            await runtime.handle_update(cast(Mapping[str, object], payload))
        return {"accepted": True}

    register_notify_route(application, runtime, actual_settings)
    register_doctor_route(application, actual_settings)
    register_admin_route(
        application,
        runtime,
        actual_settings,
        port=8000,
        run_shell=_run_command,
        background_tasks=admin_tasks,
    )
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
    candidates: list[tuple[int, int, str]] = []
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
        end = offset + length
        if end * 2 > len(raw_text):
            continue
        candidates.append((offset, end, url))
    links: list[tuple[int, int, str]] = []
    index = 0  # python index corresponding to unit_cursor
    unit_cursor = 0  # utf-16 units already decoded
    for offset, end, url in sorted(candidates):
        try:
            if offset >= unit_cursor:
                index += len(
                    raw_text[unit_cursor * 2 : offset * 2].decode("utf-16-le")
                )
                start_index = index
            else:
                start_index = len(raw_text[: offset * 2].decode("utf-16-le"))
            end_index = start_index + len(
                raw_text[offset * 2 : end * 2].decode("utf-16-le")
            )
        except UnicodeDecodeError:
            continue
        if end > unit_cursor:
            unit_cursor = end
            index = end_index
        links.append((start_index, end_index, url))
    expanded = text
    for start, end, url in sorted(links, reverse=True):
        expanded = f"{expanded[:end]} ({url}){expanded[end:]}"
    return expanded


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _ago(timestamp: float | None) -> str:
    if timestamp is None:
        return "never"
    seconds = max(0, int(time.time() - timestamp))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


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
