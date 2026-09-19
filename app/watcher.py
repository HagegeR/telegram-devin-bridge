from __future__ import annotations

import asyncio
import datetime
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from urllib.parse import unquote, urlparse

import httpx

from app.config import Settings
from app.devin import DevinClient, DevinMessage, SessionState
from app.formatting import extract_large_code_blocks, extract_options, split_long_text
from app.store import Conversation, Store
from app.telegram import TelegramClient

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = frozenset({
    "working",
    "resumed",
    "resume_requested",
    "resume_requested_frontend",
})


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
        transient_message_ids: list[int] | None = None,
        on_status_change: Callable[[str], Awaitable[None]] | None = None,
        drafts_enabled: bool | None = None,
        status_after_seconds: float | None = None,
        silent: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.conversation = conversation
        self.store = store
        self.devin = devin
        self.telegram = telegram
        self.settings = settings
        self.poll_seconds = max(
            0.5,
            settings.devin_poll_seconds if poll_seconds is None else poll_seconds,
        )
        self.clock = clock
        self.sleep = sleep
        self.trigger_message_id = trigger_message_id
        self.transient_message_ids = transient_message_ids or []
        self.on_status_change = on_status_change
        self.drafts_ok = (
            settings.telegram_drafts
            if drafts_enabled is None
            else drafts_enabled
        )
        self.status_after_seconds = (
            settings.devin_status_after_seconds
            if status_after_seconds is None
            else status_after_seconds
        )
        self.silent = silent
        self.draft_id = secrets.randbelow(2**31 - 1) + 1
        self.status_message_id: int | None = None
        self.last_status_text: str | None = None
        self.status_sent_at: float | None = None
        self.last_chat_action_at: float | None = None
        self.delivered_count = 0
        self.delivered = False
        self.draft_used = False
        self.last_status: str | None = None
        self.started_at = self.clock()

    def set_trigger(self, message_id: int) -> None:
        self.trigger_message_id = message_id
        self.delivered = False
        self.started_at = self.clock()

    async def run(self) -> None:
        self.started_at = self.clock()
        wall_started_at = time.time()
        last_pr_url = self.conversation.last_pr_url
        last_event_id = self.conversation.last_event_id
        interval = min(
            max(self.settings.devin_poll_fast_seconds, 0.5),
            self.poll_seconds,
        )
        previous_status: str | None = None
        try:
            while (
                self.clock() - self.started_at
                < self.settings.devin_watch_timeout_seconds
            ):
                turn_trigger = self.trigger_message_id
                turn_delivered = self.delivered
                state = await self.devin.get_session(self.conversation.session_id)
                new_messages = self._new_messages(
                    state,
                    wall_started_at,
                    last_event_id,
                )
                first_poll = previous_status is None
                status_changed = (
                    previous_status is not None
                    and state.status_enum != previous_status
                )
                if status_changed and self.on_status_change is not None:
                    await self.on_status_change(state.status_enum)
                previous_status = state.status_enum
                self.last_status = state.status_enum
                if first_poll or new_messages or status_changed:
                    interval = min(
                        max(self.settings.devin_poll_fast_seconds, 0.5),
                        self.poll_seconds,
                    )
                else:
                    interval = min(
                        max(interval * 1.5, self.settings.devin_poll_fast_seconds, 0.5),
                        self.poll_seconds,
                    )
                delivery_delivered = turn_delivered
                for message in new_messages:
                    if not delivery_delivered:
                        await self._cleanup_transients()
                    await self._deliver(
                        message,
                        state,
                        reply_to_message_id=(
                            turn_trigger if not delivery_delivered else None
                        ),
                    )
                    delivery_delivered = True
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
                    self.delivered = True
                    self.delivered_count += 1
                if state.pr_url is not None and state.pr_url != last_pr_url:
                    await self.telegram.send_message(
                        self.conversation.chat_id,
                        f"PR: {state.pr_url}",
                        thread_id=self.conversation.thread_id,
                        disable_notification=self.silent,
                    )
                    last_pr_url = state.pr_url
                    self.store.update_conversation(
                        self.conversation.conv_key,
                        self.conversation.session_id,
                        last_pr_url=state.pr_url,
                    )
                if state.status_enum in {"expired", "finished"}:
                    await self._cleanup_transients()
                    await self._finish_reaction(expired=state.status_enum == "expired")
                    return
                if state.status_enum not in ACTIVE_STATUSES:
                    settled = (
                        self.clock() - self.started_at
                        >= self.settings.devin_settle_seconds
                    )
                    if self.delivered or settled:
                        await self._cleanup_transients()
                        await self._finish_reaction(expired=False)
                        return
                else:
                    await self._refresh_progress(self.started_at, state)
                await self.sleep(max(interval, 0.001))
            await self._cleanup_transients()
            if not self.delivered:
                await self.telegram.send_message(
                    self.conversation.chat_id,
                    "Devin is still working; I'll deliver replies when you next message.",
                    thread_id=self.conversation.thread_id,
                    disable_notification=self.silent,
                )
        except Exception as exc:
            logger.exception("Session watcher failed for conversation %s", self.conversation.conv_key)
            await self._cleanup_transients()
            try:
                await self.telegram.send_message(
                    self.conversation.chat_id,
                    f"Couldn't reach Devin: {self._short_reason(exc)}",
                    thread_id=self.conversation.thread_id,
                )
                await self._finish_reaction(expired=True)
            except Exception:
                logger.exception("Failed to report watcher error")
        except asyncio.CancelledError:
            await self._cleanup_transients()
            raise

    async def _refresh_progress(
        self,
        started_at: float,
        state: SessionState,
    ) -> None:
        elapsed = self.clock() - started_at
        if elapsed < self.status_after_seconds:
            if self.drafts_ok and self.conversation.chat_id > 0:
                await self._send_draft("")
            else:
                await self._send_chat_action()
            return
        status_text = self._status_text(elapsed, state.structured_output)
        if self.drafts_ok and self.conversation.chat_id > 0:
            if (
                self.last_status_text != status_text
                or self.status_sent_at is None
                or elapsed - self.status_sent_at >= 10
            ):
                await self._send_draft(status_text)
                self.last_status_text = status_text
                self.status_sent_at = elapsed
            return
        if (
            self.status_message_id is None
            or (
                self.last_status_text != status_text
                and (
                    self.status_sent_at is None
                    or elapsed - self.status_sent_at >= 10
                )
            )
        ):
            if self.status_message_id is None:
                result = await self.telegram.send_message(
                    self.conversation.chat_id,
                    status_text,
                    thread_id=self.conversation.thread_id,
                    disable_notification=self.silent,
                )
                if isinstance(result, dict):
                    value = result.get("message_id")
                    if isinstance(value, int):
                        self.status_message_id = value
            else:
                await self.telegram.edit_message_text(
                    self.conversation.chat_id,
                    self.status_message_id,
                    status_text,
                )
            self.last_status_text = status_text
            self.status_sent_at = elapsed

    async def _send_draft(self, text: str) -> None:
        try:
            await self.telegram.send_message_draft(
                self.conversation.chat_id,
                self.draft_id,
                text,
                thread_id=self.conversation.thread_id,
            )
            self.draft_used = True
        except (RuntimeError, httpx.HTTPError):
            self.drafts_ok = False
            await self._send_chat_action()

    async def _send_chat_action(self) -> None:
        now = self.clock()
        if (
            self.last_chat_action_at is not None
            and now - self.last_chat_action_at < 4
        ):
            return
        self.last_chat_action_at = now
        try:
            await self.telegram.send_chat_action(
                self.conversation.chat_id,
                thread_id=self.conversation.thread_id,
            )
        except (RuntimeError, httpx.HTTPError):
            logger.warning("Failed to send typing action chat=%s", self.conversation.chat_id)

    async def _cleanup_transients(self) -> None:
        message_ids = list(self.transient_message_ids)
        self.transient_message_ids.clear()
        if self.status_message_id is not None:
            message_ids.append(self.status_message_id)
            self.status_message_id = None
        for message_id in message_ids:
            try:
                await self.telegram.delete_message(
                    self.conversation.chat_id,
                    message_id,
                )
            except Exception:
                logger.debug(
                    "Failed to delete transient Telegram message chat=%s message=%s",
                    self.conversation.chat_id,
                    message_id,
                    exc_info=True,
                )
        if self.drafts_ok and self.draft_used and self.conversation.chat_id > 0:
            try:
                await self.telegram.send_message_draft(
                    self.conversation.chat_id,
                    self.draft_id,
                    "",
                    thread_id=self.conversation.thread_id,
                )
            except Exception:
                logger.debug(
                    "Failed to clear draft chat=%s",
                    self.conversation.chat_id,
                    exc_info=True,
                )
            self.draft_used = False

    async def _deliver(
        self,
        message: DevinMessage,
        state: SessionState,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        body, options = extract_options(message.message)
        if not body and options:
            body = "Choose an option:"
        attachment_urls = re.findall(
            r"https://app\.devin\.ai/attachments/[^/\s]+/[^\s]+",
            body,
        )
        for url in attachment_urls:
            downloaded = await self.devin.download_attachment(url)
            if downloaded is None:
                continue
            content, content_type = downloaded
            filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
            if content_type.startswith("image/"):
                await self.telegram.send_photo(
                    self.conversation.chat_id,
                    filename,
                    content,
                    thread_id=self.conversation.thread_id,
                    caption=filename,
                    reply_to=reply_to_message_id,
                    content_type=content_type,
                )
            else:
                await self.telegram.send_document(
                    self.conversation.chat_id,
                    filename,
                    content,
                    content_type=content_type,
                    thread_id=self.conversation.thread_id,
                    reply_to=reply_to_message_id,
                )
            body = body.replace(f"\n{url}\n", "\n")
            if body == url:
                body = ""
        pr_urls = re.findall(
            r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
            body,
        )
        if state.pr_url and state.pr_url.startswith("https://github.com/"):
            pr_urls.append(state.pr_url)
        for pr_url in dict.fromkeys(pr_urls):
            metadata = await self.devin.fetch_github_pr(
                pr_url,
                self.settings.github_token,
            )
            if metadata is None:
                continue
            number = metadata.get("number")
            title = metadata.get("title")
            state_name = metadata.get("state")
            merged = metadata.get("merged")
            additions = metadata.get("additions")
            deletions = metadata.get("deletions")
            base = metadata.get("base")
            head = metadata.get("head")
            if not all(
                isinstance(value, (str, int, bool))
                for value in (number, title, state_name, merged, additions, deletions)
            ):
                continue
            base_name = base.get("ref") if isinstance(base, dict) else ""
            head_name = head.get("ref") if isinstance(head, dict) else ""
            status = "merged" if merged else str(state_name)
            body += (
                f"\n\n🔗 PR #{number} · {title} · {status} · "
                f"+{additions} −{deletions} · {base_name}←{head_name}"
            )
        markup: dict[str, object] | None = None
        choice_ids: list[tuple[str, str]] = []
        if options:
            for message_id in self.store.list_choice_messages(
                self.conversation.conv_key
            ):
                try:
                    await self.telegram.edit_message_reply_markup(
                        self.conversation.chat_id,
                        message_id,
                    )
                except (RuntimeError, httpx.HTTPError):
                    logger.warning(
                        "Failed to clear stale choice keyboard chat=%s message=%s",
                        self.conversation.chat_id,
                        message_id,
                    )
            self.store.delete_choices(self.conversation.conv_key)
            buttons: list[list[dict[str, object]]] = []
            for option in options:
                choice_id = secrets.token_urlsafe(8)
                choice_ids.append((choice_id, option))
                buttons.append([{"text": option, "callback_data": choice_id}])
            markup = {"inline_keyboard": buttons}
        body, documents = extract_large_code_blocks(body)
        for index, (filename, content) in enumerate(documents):
            if filename.endswith(".txt") and "```diff" in message.message:
                filename = filename[:-4] + ".diff"
            result = await self.telegram.send_document(
                self.conversation.chat_id,
                filename,
                content,
                thread_id=self.conversation.thread_id,
                reply_to=reply_to_message_id if index == 0 else None,
            )
            self._index_outbound(result)
        limit = max(1, self.settings.telegram_long_reply_chars)
        if not options and len(body) > 4 * limit:
            result = await self.telegram.send_document(
                self.conversation.chat_id,
                "reply.md",
                body.encode(),
                thread_id=self.conversation.thread_id,
                reply_to=reply_to_message_id,
            )
            self._index_outbound(result)
            results = await self.telegram.send_markdown(
                self.conversation.chat_id,
                body[:500],
                thread_id=self.conversation.thread_id,
                disable_notification=(
                    self.silent
                    or (
                        self.settings.telegram_notification_mode == "important"
                        and state.status_enum == "working"
                    )
                ),
                reply_to_message_id=reply_to_message_id,
            )
            self._index_outbound_many(results)
            return
        if not options and len(body) > limit:
            body, remaining = split_long_text(body, limit)
            if remaining:
                token = secrets.token_urlsafe(12)
                self.store.add_long_text(
                    token,
                    self.conversation.conv_key,
                    self.conversation.chat_id,
                    remaining,
                )
                markup = {
                    "inline_keyboard": [[
                        {
                            "text": "Show more ▾",
                            "callback_data": f"more:{token}",
                        }
                    ]]
                }
        delivery_kwargs: dict[str, object] = {
            "thread_id": self.conversation.thread_id,
            "reply_markup": markup,
            "disable_notification": (
                self.silent
                or (
                    self.settings.telegram_notification_mode == "important"
                    and state.status_enum == "working"
                )
            ),
        }
        if reply_to_message_id is not None:
            delivery_kwargs["reply_to_message_id"] = reply_to_message_id
        results = await self.telegram.send_markdown(
            self.conversation.chat_id,
            body,
            **delivery_kwargs,
        )
        for result in results:
            self._index_outbound(result)
        if options:
            message_id = None
            if results:
                candidate = results[-1].get("message_id")
                if isinstance(candidate, int):
                    message_id = candidate
            for choice_id, option in choice_ids:
                self.store.add_choice(
                    choice_id,
                    self.conversation.conv_key,
                    self.conversation.session_id,
                    self.conversation.chat_id,
                    option,
                    message_id,
                )

    def _index_outbound(self, result: Mapping[str, object]) -> None:
        message_id = result.get("message_id")
        if isinstance(message_id, int):
            self.store.index_message(
                self.conversation.chat_id,
                message_id,
                self.conversation.conv_key,
            )

    def _index_outbound_many(self, results: list[dict[str, object]]) -> None:
        for result in results:
            self._index_outbound(result)

    def _status_text(self, elapsed: float, structured_output: object | None) -> str:
        total_seconds = max(0, int(elapsed))
        minutes, seconds = divmod(total_seconds, 60)
        text = f"👀 Working… {minutes}:{seconds:02d}"
        if self.delivered_count:
            text += f" · {self.delivered_count} messages"
        summary = self._structured_summary(structured_output)
        if summary:
            text += f"\n{summary}"
        return text

    @staticmethod
    def _structured_summary(value: object | None) -> str:
        if isinstance(value, dict):
            pairs = [
                f"{key}: {item}"
                for key, item in value.items()
                if isinstance(key, str)
                and isinstance(item, (str, int, float, bool))
            ][:3]
            return " · ".join(pairs)[:200]
        if isinstance(value, str):
            return value[:200]
        return ""

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
