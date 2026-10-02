from __future__ import annotations

import asyncio
import datetime
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import cast
from urllib.parse import unquote, urlparse

import httpx

from app.config import Settings
from app.devin import DevinClient, DevinMessage, SessionState
from app.formatting import (
    extract_attachments,
    extract_controls,
    extract_large_code_blocks,
    extract_options,
    markdown_to_telegram_markdown_v2,
    parse_rich_segments,
    split_long_text,
)
from app.images import photo_fits_unchanged
from app.store import Conversation, Store
from app.telegram import TelegramClient

logger = logging.getLogger(__name__)


async def _send_as_photo(mode: str, content: bytes) -> bool:
    if mode == "false":
        return True
    if mode == "true":
        return False
    return await asyncio.to_thread(photo_fits_unchanged, content)

TYPING_REFRESH_SECONDS = 4

ACTIVE_STATUSES = frozenset({
    "working",
    "resumed",
    "resume_requested",
    "resume_requested_frontend",
})

FINISH_NOTICES = {
    "blocked": "💬 Waiting for your reply",
    "finished": "✓ Finished",
    "expired": "⚠ Session expired",
    "suspended": "💤 Session suspended — send a message to resume",
}


def _prepend_buttons(
    markup: dict[str, object] | None, buttons: list[dict[str, str]]
) -> dict[str, object] | None:
    if not buttons:
        return markup
    rows: list[object] = []
    if markup is not None and isinstance(markup.get("inline_keyboard"), list):
        rows = cast(list[object], markup["inline_keyboard"])
    return {"inline_keyboard": [[button] for button in buttons] + rows}


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
        has_queued: Callable[[], bool] | None = None,
        drafts_enabled: bool | None = None,
        status_after_seconds: float | None = None,
        silent: bool = False,
        resume_from: float | None = None,
        trigger_at: float | None = None,
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
        self.has_queued = has_queued
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
        self.resume_from = resume_from
        # Messages emitted before trigger_at belong to a previous turn: they
        # are delivered but must not be attributed to (or close) this one.
        # Set to the moment the user message was forwarded to Devin; left
        # None for recovery watchers (restart), where undelivered downtime
        # replies legitimately answer the persisted trigger.
        self.trigger_at = (
            trigger_at if conversation.last_event_id is not None else None
        )
        self.draft_id = secrets.randbelow(2**31 - 1) + 1
        self.status_message_id: int | None = None
        self.last_status_text: str | None = None
        self.status_sent_at: float | None = None
        self.last_chat_action_at: float | None = None
        self.delivered_count = 0
        self.first_status: str | None = None
        self._closed_gen = -1
        self.delivered = False
        self.draft_used = False
        self.devin_reacted = False
        self.progress_message_id: int | None = None
        self.last_status: str | None = None
        self.started_at = self.clock()
        self.generation = 0
        self.topic_title_stale = False

    def set_trigger(self, message_id: int, *, at: float | None = None) -> None:
        self.trigger_message_id = message_id
        self.trigger_at = (
            (time.time() if at is None else at)
            if self.conversation.last_event_id is not None
            else None
        )
        self.delivered = False
        self.devin_reacted = False
        self.started_at = self.clock()
        self.generation += 1

    async def run(self) -> None:
        self.started_at = self.clock()
        wall_started_at = (
            self.resume_from if self.resume_from is not None else time.time()
        )
        last_pr_url = self.conversation.last_pr_url
        last_event_id = self.conversation.last_event_id
        interval = min(
            max(self.settings.devin_poll_fast_seconds, 0.5),
            self.poll_seconds,
        )
        previous_status: str | None = None
        title_retries_after_finish = 3
        try:
            while True:
                turn_trigger = self.trigger_message_id
                turn_delivered = self.delivered
                gen = self.generation
                if (
                    self.clock() - self.started_at
                    >= self.settings.devin_watch_timeout_seconds
                ):
                    await self._cleanup_transients()
                    if self.generation != gen:
                        continue
                    if previous_status in {"expired", "finished"}:
                        await self._close_turn(
                            previous_status, gen=gen, trigger=turn_trigger
                        )
                    elif not self.delivered:
                        await self.telegram.send_message(
                            self.conversation.chat_id,
                            "⏳ Devin is still working; I'll deliver replies when you next message.",
                            thread_id=self.conversation.thread_id,
                            disable_notification=self.silent,
                        )
                    if self.generation != gen:
                        continue
                    return
                state = await self.devin.get_session(
                    self.conversation.session_id,
                    since_event_id=last_event_id,
                )
                if self.generation != gen:
                    await self.sleep(interval)
                    continue
                if self.conversation.title_pending or self.topic_title_stale:
                    stored = self.store.get_conversation(self.conversation.conv_key)
                    if stored is not None:
                        self.conversation = stored
                if self.topic_title_stale:
                    self.topic_title_stale = not await self._edit_topic(
                        self.conversation.title
                    )
                elif (
                    self.conversation.title_pending
                    and state.title
                    and state.title != self.conversation.title
                ):
                    await self._apply_session_title(state.title)
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
                previous_status = state.status_enum
                self.last_status = state.status_enum
                if self.first_status is None:
                    self.first_status = state.status_enum
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
                    stale = self._pre_trigger(message)
                    if not delivery_delivered and not stale:
                        await self._cleanup_transients()
                    await self._deliver(
                        message,
                        state,
                        reply_to_message_id=(
                            turn_trigger
                            if not delivery_delivered and not stale
                            else None
                        ),
                        react_message_id=(
                            turn_trigger if not stale else None
                        ),
                    )
                    if not stale:
                        delivery_delivered = True
                        self.delivered = True
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
                    self.delivered_count += 1
                if status_changed and self.on_status_change is not None:
                    await self.on_status_change(state.status_enum)
                if state.pr_url is not None and state.pr_url != last_pr_url:
                    await self.telegram.send_message(
                        self.conversation.chat_id,
                        f"🔗 PR: {state.pr_url}",
                        thread_id=self.conversation.thread_id,
                        disable_notification=self.silent,
                    )
                    last_pr_url = state.pr_url
                    self.store.update_conversation(
                        self.conversation.conv_key,
                        self.conversation.session_id,
                        last_pr_url=state.pr_url,
                    )
                if (
                    state.status_enum not in ACTIVE_STATUSES
                    and self._topic_title_outstanding(state)
                    and title_retries_after_finish
                ):
                    title_retries_after_finish -= 1
                    await self.sleep(max(interval, 0.001))
                    continue
                settled = (
                    self.clock() - self.started_at
                    >= self.settings.devin_settle_seconds
                )
                # A user-triggered watcher on a session with delivered history
                # may be watching a resume still in flight: hold the close
                # until a post-trigger reply lands or the settle window
                # expires. Other watchers close as before.
                if state.status_enum in {"expired", "finished"} and (
                    self.trigger_at is None or self.delivered or settled
                ):
                    await self._cleanup_transients()
                    if self.generation != gen:
                        await self.sleep(interval)
                        continue
                    await self._close_turn(
                        state.status_enum, gen=gen, trigger=turn_trigger
                    )
                    if self.generation != gen:
                        await self.sleep(interval)
                        continue
                    return
                if state.status_enum not in ACTIVE_STATUSES:
                    if self.delivered or settled:
                        await self._cleanup_transients()
                        if self.generation != gen:
                            await self.sleep(interval)
                            continue
                        await self._close_turn(
                            state.status_enum, gen=gen, trigger=turn_trigger
                        )
                        if self.generation != gen:
                            await self.sleep(interval)
                            continue
                        return
                else:
                    await self._refresh_progress(self.started_at, state)
                    if self.last_chat_action_at is not None:
                        await self._sleep_keeping_typing(interval)
                        continue
                await self.sleep(max(interval, 0.001))
        except Exception as exc:
            logger.exception("Session watcher failed for conversation %s", self.conversation.conv_key)
            await self._cleanup_transients()
            try:
                await self.telegram.send_message(
                    self.conversation.chat_id,
                    f"⚠ Couldn't reach Devin: {self._short_reason(exc)}",
                    thread_id=self.conversation.thread_id,
                )
                await self._finish_reaction(
                    self.trigger_message_id, expired=True
                )
            except Exception:
                logger.exception("Failed to report watcher error")
        except asyncio.CancelledError:
            await self._cleanup_transients()
            raise

    def _topic_title_outstanding(self, state: SessionState) -> bool:
        conv = self.conversation
        return self.topic_title_stale or bool(
            conv.title_pending
            and state.title
            and state.title != conv.title
            and conv.thread_id is not None
        )
    async def _edit_topic(self, title: str) -> bool:
        conv = self.conversation
        for attempt in range(3):
            try:
                await self.telegram.edit_forum_topic(
                    conv.chat_id, conv.thread_id, title[:128]
                )
                return True
            except (RuntimeError, httpx.HTTPError):
                logger.warning(
                    "Failed to rename topic chat=%s thread=%s",
                    conv.chat_id,
                    conv.thread_id,
                )
                if attempt < 2:
                    await self.sleep(1)
        return False

    async def _apply_session_title(self, title: str) -> None:
        conv = self.conversation
        if conv.thread_id is not None and conv.conv_key == Store.conv_key(
            conv.chat_id, conv.thread_id, is_forum=True
        ):
            if not await self._edit_topic(title):
                return
            stored = self.store.get_conversation(conv.conv_key)
            if stored is None or stored.session_id != conv.session_id:
                return
            if not stored.title_pending:
                self.topic_title_stale = not await self._edit_topic(stored.title)
                self.conversation = stored
                return
        self.store.update_conversation(
            conv.conv_key,
            conv.session_id,
            title=title,
            title_pending=False,
        )
        self.conversation = replace(conv, title=title, title_pending=False)

    async def _refresh_progress(
        self,
        started_at: float,
        state: SessionState,
    ) -> None:
        elapsed = self.clock() - started_at
        if not (self.drafts_ok and self.conversation.chat_id > 0):
            await self._send_chat_action()
        if elapsed < self.status_after_seconds:
            if self.drafts_ok and self.conversation.chat_id > 0:
                await self._send_draft("")
            return
        status_text = self._status_text(elapsed, state)
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

    async def _sleep_keeping_typing(self, interval: float) -> None:
        remaining = max(interval, 0.001)
        while remaining > 0:
            last = self.last_chat_action_at or self.clock()
            step = min(remaining, max(last + TYPING_REFRESH_SECONDS - self.clock(), 0.001))
            await self.sleep(step)
            remaining -= step
            await self._send_chat_action()

    async def _send_chat_action(self) -> None:
        now = self.clock()
        if (
            self.last_chat_action_at is not None
            and now - self.last_chat_action_at < TYPING_REFRESH_SECONDS
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
        react_message_id: int | None = None,
    ) -> None:
        body, attachment_urls = extract_attachments(message.message)
        metadata_urls = set(attachment_urls)
        body, options = extract_options(body)
        body, controls = extract_controls(body)
        if "react" in controls and await self.telegram.react(
            self.conversation.chat_id,
            react_message_id,
            str(controls["react"]),
        ):
            self.devin_reacted = True
        notify_disabled = (
            self.silent
            or "silent" in controls
            or (
                self.settings.telegram_notification_mode == "important"
                and state.status_enum == "working"
            )
        ) and "urgent" not in controls
        if not body and options:
            body = "Choose an option:"
        bare_attachment_urls = re.findall(
            r"https://app\.devin\.ai/attachments/[^/\s)\]]+/[^\s)\]\"'<>]+",
            body,
        )
        attachment_urls.extend(
            url.rstrip(".,;:!?")
            for url in bare_attachment_urls
            if url.rstrip(".,;:!?") not in attachment_urls
        )
        downloads = await asyncio.gather(
            *(self.devin.download_attachment(url) for url in attachment_urls)
        )
        report_buttons: list[dict[str, str]] = []
        for url, downloaded in zip(attachment_urls, downloads, strict=True):
            if downloaded is None:
                if url in metadata_urls and url not in body:
                    body = f"{body}\n\n{url}".strip()
                continue
            content, content_type = downloaded
            filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
            if content_type.startswith("text/html") and self.settings.public_base_url:
                token = secrets.token_urlsafe(16)
                self.store.add_report(
                    token,
                    self.conversation.conv_key,
                    self.conversation.chat_id,
                    content,
                    content_type,
                )
                report_buttons.append({
                    "text": f"Open {filename}",
                    "url": f"{self.settings.public_base_url.rstrip('/')}/r/{token}",
                })
                body = body.replace(f"\n{url}\n", "\n")
                if body == url:
                    body = ""
                continue
            try:
                if (
                    content_type.startswith("image/")
                    and await _send_as_photo(
                        self.settings.telegram_images_as_documents,
                        content,
                    )
                ):
                    result = await self.telegram.send_photo(
                        self.conversation.chat_id,
                        filename,
                        content,
                        thread_id=self.conversation.thread_id,
                        caption=filename,
                        reply_to=reply_to_message_id,
                        content_type=content_type,
                        disable_notification=notify_disabled,
                    )
                else:
                    result = await self.telegram.send_document(
                        self.conversation.chat_id,
                        filename,
                        content,
                        content_type=content_type,
                        thread_id=self.conversation.thread_id,
                        reply_to=reply_to_message_id,
                        disable_notification=notify_disabled,
                    )
            except (httpx.HTTPError, RuntimeError):
                logger.exception("Failed to send attachment %s", filename)
                if url not in body:
                    body = f"{body}\n\n{url}".strip()
                continue
            self._index_outbound(result)
            body = body.replace(f"\n{url}\n", "\n")
            if body == url:
                body = ""
        pr_urls = re.findall(
            r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
            body,
        )
        if state.pr_url and state.pr_url.startswith("https://github.com/"):
            pr_urls.append(state.pr_url)
        pr_url_list = list(dict.fromkeys(pr_urls))
        pr_metadata = await asyncio.gather(
            *(
                self.devin.fetch_github_pr(
                    pr_url,
                    self.settings.github_token,
                )
                for pr_url in pr_url_list
            )
        )
        for metadata in pr_metadata:
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
        if not body.strip() and report_buttons:
            body = "Report ready:"
        if not body.strip() and not options and "poll" not in controls:
            return
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
                disable_notification=notify_disabled,
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
                disable_notification=notify_disabled,
            )
            self._index_outbound(result)
            results = await self.telegram.send_markdown(
                self.conversation.chat_id,
                body[:500],
                thread_id=self.conversation.thread_id,
                reply_markup=_prepend_buttons(None, report_buttons),
                disable_notification=notify_disabled,
                reply_to_message_id=reply_to_message_id,
            )
            self._index_outbound_many(results)
            return
        # Marked replies send as structured segments, so the Show-more
        # split would cut a TABLE:/DETAILS: block in half — skip it there.
        if not options and len(body) > limit and parse_rich_segments(body) is None:
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
        markup = _prepend_buttons(markup, report_buttons)
        delivery_kwargs: dict[str, object] = {
            "thread_id": self.conversation.thread_id,
            "reply_markup": markup,
            "disable_notification": notify_disabled,
        }
        if reply_to_message_id is not None:
            delivery_kwargs["reply_to_message_id"] = reply_to_message_id
        edited = False
        if (
            "progress" in controls
            and self.progress_message_id is not None
            and body.strip()
            and parse_rich_segments(body) is None
        ):
            try:
                await self.telegram.edit_message_text(
                    self.conversation.chat_id,
                    self.progress_message_id,
                    markdown_to_telegram_markdown_v2(body[:4096]),
                    parse_mode="MarkdownV2",
                    reply_markup=markup or {"inline_keyboard": []},
                )
                edited = True
                if not options:
                    self.store.delete_choices(self.conversation.conv_key)
            except (RuntimeError, httpx.HTTPError):
                logger.warning("progress edit failed, sending a new message")
                self.progress_message_id = None
        results = (
            []
            if edited or not body.strip()
            else await self._send_body(body, delivery_kwargs)
        )
        for result in results:
            self._index_outbound(result)
        delivered_id: int | None = self.progress_message_id if edited else None
        if results:
            candidate = results[0].get("message_id")
            if isinstance(candidate, int):
                delivered_id = candidate
        if "progress" in controls:
            self.progress_message_id = delivered_id
        if "pin" in controls and delivered_id is not None:
            try:
                await self.telegram.pin_chat_message(
                    self.conversation.chat_id, delivered_id
                )
            except (RuntimeError, httpx.HTTPError):
                logger.warning("pin_chat_message failed")
        if "poll" in controls:
            poll_parts = cast(list[str], controls["poll"])
            try:
                await self.telegram.send_poll(
                    self.conversation.chat_id,
                    poll_parts[0],
                    poll_parts[1:11],
                    thread_id=self.conversation.thread_id,
                    disable_notification=notify_disabled,
                )
            except (RuntimeError, httpx.HTTPError):
                logger.warning("send_poll failed")

        if options:
            message_id = None
            if results:
                candidate = results[-1].get("message_id")
                if isinstance(candidate, int):
                    message_id = candidate
            elif edited:
                message_id = self.progress_message_id
            for choice_id, option in choice_ids:
                self.store.add_choice(
                    choice_id,
                    self.conversation.conv_key,
                    self.conversation.session_id,
                    self.conversation.chat_id,
                    option,
                    message_id,
                )

    async def _send_body(
        self, body: str, delivery_kwargs: dict[str, object]
    ) -> list[dict[str, object]]:
        segments = parse_rich_segments(body)
        if segments is None:
            return await self.telegram.send_markdown(
                self.conversation.chat_id, body, **delivery_kwargs
            )
        results: list[dict[str, object]] = []
        last = len(segments) - 1
        for index, segment in enumerate(segments):
            kwargs = dict(delivery_kwargs)
            # Buttons/reply threading belong to the outer message edges only.
            if index < last:
                kwargs.pop("reply_markup", None)
            if index:
                kwargs.pop("reply_to_message_id", None)
            if "blocks" in segment and self.telegram.rich_enabled:
                try:
                    results.append(
                        await self.telegram.send_rich_message(
                            self.conversation.chat_id,
                            blocks=cast(list[dict[str, object]], segment["blocks"]),
                            **kwargs,
                        )
                    )
                    continue
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
                            "rich block send failed, using fallback: %s", exc
                        )
            results.extend(
                await self.telegram.send_markdown(
                    self.conversation.chat_id,
                    str(segment.get("markdown") or segment["fallback"]),
                    **kwargs,
                )
            )
        return results

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

    def _status_text(self, elapsed: float, state: SessionState) -> str:
        total_seconds = max(0, int(elapsed))
        minutes, seconds = divmod(total_seconds, 60)
        text = f"⏳ Working… {minutes}:{seconds:02d}"
        if self.delivered_count:
            text += f" · {self.delivered_count} updates"
        activity = self._activity_age(state.updated_at)
        if activity is not None:
            text += f" · {activity}"
        summary = self._structured_summary(state.structured_output)
        if summary:
            text += f"\n{summary}"
        return text

    @staticmethod
    def _activity_age(updated_at: str | None) -> str | None:
        if updated_at is None:
            return None
        try:
            stamp = datetime.datetime.fromisoformat(
                updated_at.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return None
        age = max(0, int(time.time() - stamp))
        if age <= 5:
            return "active now"
        minutes, seconds = divmod(age, 60)
        return f"last activity {minutes}:{seconds:02d} ago"

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

    def _pre_trigger(self, message: DevinMessage) -> bool:
        # True when the message was emitted before the current trigger was
        # set, i.e. it answers an earlier turn, not this one.
        if self.trigger_at is None:
            return False
        value = self._message_ts(message.timestamp)
        return value is not None and value < self.trigger_at - 5

    @staticmethod
    def _message_ts(timestamp: str | None) -> float | None:
        if timestamp is None:
            return None
        try:
            return float(timestamp)
        except ValueError:
            pass
        try:
            return datetime.datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return None

    @classmethod
    def _recent(cls, timestamp: str | None, wall_started_at: float) -> bool:
        value = cls._message_ts(timestamp)
        return value is None or value >= wall_started_at - 5

    async def _send_finish_notice(self, status: str) -> None:
        try:
            await self.telegram.send_message(
                self.conversation.chat_id,
                FINISH_NOTICES.get(status, "✓ Done"),
                thread_id=self.conversation.thread_id,
                disable_notification=self.silent,
            )
        except (RuntimeError, httpx.HTTPError):
            logger.warning(
                "Failed to send finish notice chat=%s status=%s",
                self.conversation.chat_id,
                status,
            )

    async def _close_turn(
        self, status: str, *, gen: int, trigger: int | None
    ) -> None:
        fresh = self.delivered_count > 0 or status != self.first_status
        queued = (
            status in {"blocked", "finished"}
            and self.has_queued is not None
            and self.has_queued()
        )
        if fresh and not queued and self._closed_gen != gen:
            await self._send_finish_notice(status)
        if self.generation != gen:
            return
        self._closed_gen = gen
        await self._finish_reaction(trigger, expired=status == "expired")

    async def _finish_reaction(self, trigger: int | None, *, expired: bool) -> None:
        if self.devin_reacted:
            return
        await self.telegram.react(
            self.conversation.chat_id,
            trigger,
            "👎" if expired else "👍",
        )

    @staticmethod
    def _short_reason(exc: Exception) -> str:
        text = str(exc).strip().replace("\n", " ")
        return text[:120] or "temporary error"
