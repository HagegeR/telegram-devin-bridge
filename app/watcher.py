from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from app.config import Settings
from app.devin import DevinClient, DevinMessage, SessionState
from app.formatting import chunk, extract_options, markdown_to_telegram_markdown_v2
from app.store import Conversation, Store
from app.telegram import TelegramClient

logger = logging.getLogger(__name__)


class SessionWatcher:
    def __init__(
        self,
        conversation: Conversation,
        store: Store,
        devin: DevinClient,
        telegram: TelegramClient,
        settings: Settings,
        *,
        poll_seconds: float | None = None,
        trigger_message_id: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.conversation = conversation
        self.store = store
        self.devin = devin
        self.telegram = telegram
        self.settings = settings
        self.poll_seconds = (
            settings.devin_poll_seconds
            if poll_seconds is None
            else poll_seconds
        )
        self.clock = clock
        self.sleep = sleep
        self.trigger_message_id = trigger_message_id

    async def run(self) -> None:
        started_at = self.clock()
        wall_started_at = time.time()
        delivered = False
        last_pr_url = self.conversation.last_pr_url
        last_event_id = self.conversation.last_event_id
        try:
            while self.clock() - started_at < self.settings.devin_watch_timeout_seconds:
                state = await self.devin.get_session(self.conversation.session_id)
                new_messages = self._new_messages(
                    state,
                    wall_started_at,
                    last_event_id,
                )
                for message in new_messages:
                    await self._deliver(message, state)
                    if message.event_id is not None:
                        last_event_id = message.event_id
                        self.conversation = replace(
                            self.conversation,
                            last_event_id=message.event_id,
                        )
                        self.store.update_conversation(
                            self.conversation.conv_key,
                            self.conversation.session_id,
                            last_event_id=message.event_id,
                        )
                    delivered = True
                if state.pr_url is not None and state.pr_url != last_pr_url:
                    await self.telegram.send_message(
                        self.conversation.chat_id,
                        f"PR: {state.pr_url}",
                        thread_id=self.conversation.thread_id,
                        disable_notification=True,
                    )
                    last_pr_url = state.pr_url
                    self.store.update_conversation(
                        self.conversation.conv_key,
                        self.conversation.session_id,
                        last_pr_url=state.pr_url,
                    )
                if state.status_enum in {"expired", "finished"}:
                    await self._finish_reaction(expired=state.status_enum == "expired")
                    return
                active_statuses = {
                    "working",
                    "resumed",
                    "resume_requested",
                    "resume_requested_frontend",
                }
                if state.status_enum not in active_statuses:
                    settled = self.clock() - started_at >= self.settings.devin_settle_seconds
                    if delivered or settled:
                        await self._finish_reaction(expired=False)
                        return
                else:
                    await self.telegram.send_chat_action(
                        self.conversation.chat_id,
                        thread_id=self.conversation.thread_id,
                    )
                await self.sleep(self.poll_seconds)
            if not delivered:
                await self.telegram.send_message(
                    self.conversation.chat_id,
                    "Devin is still working; I'll deliver replies when you next message.",
                    thread_id=self.conversation.thread_id,
                    disable_notification=True,
                )
        except Exception as exc:
            logger.exception("Session watcher failed for conversation %s", self.conversation.conv_key)
            try:
                await self.telegram.send_message(
                    self.conversation.chat_id,
                    f"Couldn't reach Devin: {self._short_reason(exc)}",
                    thread_id=self.conversation.thread_id,
                )
                await self._finish_reaction(expired=True)
            except Exception:
                logger.exception("Failed to report watcher error")

    async def _deliver(self, message: DevinMessage, state: SessionState) -> None:
        body, options = extract_options(message.message)
        if not body and options:
            body = "Choose an option:"
        rendered = markdown_to_telegram_markdown_v2(body)
        parts = chunk(rendered)
        markup: dict[str, object] | None = None
        if options:
            buttons: list[list[dict[str, str]]] = []
            for option in options:
                choice_id = secrets.token_urlsafe(8)
                self.store.add_choice(
                    choice_id,
                    self.conversation.conv_key,
                    self.conversation.session_id,
                    self.conversation.chat_id,
                    option,
                )
                buttons.append([{"text": option, "callback_data": choice_id}])
            markup = {"inline_keyboard": buttons}
        for index, part in enumerate(parts):
            await self.telegram.send_message(
                self.conversation.chat_id,
                part,
                thread_id=self.conversation.thread_id,
                parse_mode="MarkdownV2",
                reply_markup=markup if index == len(parts) - 1 else None,
                disable_notification=(
                    self.settings.telegram_notification_mode == "important"
                    and state.status_enum == "working"
                ),
            )

    def _new_messages(
        self,
        state: SessionState,
        wall_started_at: float,
        last_event_id: str | None,
    ) -> list[DevinMessage]:
        messages = [
            message
            for message in state.messages
            if message.message_type == "devin_message"
            and message.event_id is not None
        ]
        if last_event_id is not None:
            for index, message in enumerate(messages):
                if message.event_id == last_event_id:
                    return messages[index + 1 :]
            return messages
        return [
            message
            for message in messages
            if self._recent(message.timestamp, wall_started_at)
        ]

    @staticmethod
    def _recent(timestamp: str | None, wall_started_at: float) -> bool:
        if timestamp is None:
            return True
        try:
            value = float(timestamp)
        except ValueError:
            try:
                value = datetime.datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                return True
        return value >= wall_started_at - 5

    async def _finish_reaction(self, *, expired: bool) -> None:
        if self.trigger_message_id is None:
            return
        await self.telegram.set_message_reaction(
            self.conversation.chat_id,
            self.trigger_message_id,
            "👎" if expired else "👍",
        )

    @staticmethod
    def _short_reason(exc: Exception) -> str:
        text = str(exc).strip().replace("\n", " ")
        return text[:120] or "temporary error"
