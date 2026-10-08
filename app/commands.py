from __future__ import annotations

import re
from collections.abc import Mapping
from html import escape
from typing import Protocol

import httpx

from app.access import is_allowed, is_topic_chat
from app.config import Settings
from app.crawlers import CRAWLER_NAMES
from app.devin import DevinClient, Playbook, SessionState
from app.formatting import (
    rich_details,
    rich_paragraph,
    rich_text_bold,
    rich_text_code,
    rich_text_link,
)
from app.store import Conversation, Store

SYSTEM_PREAMBLE = (
    "You are chatting with a user over Telegram via a bridge. Keep replies concise. "
    "Telegram renders Markdown. When you need the user to pick between a small set "
    "of options, end your message with one line exactly like `OPTIONS: first option | "
    "second option | third option` (max 8, each under 60 chars); the bridge turns it "
    "into buttons. For rich or long results (tables, charts, reports), attach a "
    "self-contained .html file; the bridge serves it and adds an Open button. "
    "To collapse a long section inline, use a Telegram expandable quote: first line "
    "prefixed `**>`, the rest prefixed `>`, and end with `||`. Inline `||spoiler||` "
    "hides text until tapped. Markers that render "
    "native blocks: `DETAILS: <summary>` on its own line, then content lines, then "
    "`END DETAILS` gives a tap-to-expand section; `TABLE:` on its own line, then "
    "`| col | col |` pipe rows, then `END TABLE` gives a real table; "
    "`SUGGEST: <what> — <short why>` on its own line, then the proposal body, "
    "then `END SUGGEST` renders a 💡 suggestion card — use it to propose a "
    "skill, knowledge entry, or environment change, and pair it with "
    "`OPTIONS:` (e.g. `OPTIONS: Save it | Skip`) so the user can approve; "
    "apply their choice with your own tools. "
    "Control markers, each on its own line: `REACT: <emoji>` reacts to the "
    "user's message (common emojis — off-set ones degrade to 👍); `PIN:` pins "
    "the reply; `URGENT:`/`SILENT:` override "
    "notification quieting; `PROGRESS:` edits your previous progress message "
    "instead of sending a new one; `POLL: question | a | b` sends a poll. "
    "Never ask the user to open a UI; they only see your messages.\n\n"
    "User message: "
)

_STATUS_LABELS = {
    "working": "⏳ working",
    "resumed": "⏳ resuming",
    "resume_requested": "⏳ resuming",
    "resume_requested_frontend": "⏳ resuming",
    "blocked": "💬 waiting for your reply",
    "finished": "✓ finished",
    "expired": "⚠ expired",
}

# v3 status_detail rendered as a suffix only when it adds information the
# coarse label doesn't already carry ("working"/"waiting_for_user"/
# "finished" are implied by their labels).
_STATUS_DETAIL = {
    "waiting_for_approval": "approval",
    "inactivity": "idle timeout",
    "user_request": "paused on request",
}


class CommandRuntime(Protocol):
    settings: Settings
    store: Store
    devin: DevinClient
    bot_topics_enabled: bool
    approved_users: set[int]

    async def send_text(
        self,
        message: Mapping[str, object],
        text: str,
        *,
        silent: bool = False,
        ephemeral: bool = False,
        html: bool = False,
        rich: list[dict[str, object]] | None = None,
    ) -> int | None: ...

    async def create_session_for_message(
        self,
        message: Mapping[str, object],
        prompt: str,
        title: str | None,
        *,
        playbook_id: str | None = None,
        start_watcher: bool = True,
    ) -> Conversation: ...

    async def start_watcher(
        self,
        conversation: Conversation,
        *,
        trigger_message_id: int | None = None,
    ) -> None: ...

    async def create_forum_topic(self, chat_id: int, name: str) -> int: ...

    async def delete_forum_topic(self, chat_id: int, thread_id: int) -> None: ...

    async def edit_forum_topic(
        self,
        chat_id: int,
        thread_id: int,
        name: str,
    ) -> None: ...

    async def stop_conversation(self, conversation: Conversation) -> None: ...

    async def detach_conversation(self, conversation: Conversation) -> None: ...

    def clear_queued_turns(self, conv_key: str) -> None: ...

    def crawl_sites(self) -> set[str]: ...

    def queued_count(self, conv_key: str) -> int: ...

    async def retry_conversation(
        self,
        conversation: Conversation,
        *,
        trigger_message_id: int | None = None,
    ) -> None: ...

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
        last_pr_url: str | None = None,
    ) -> Conversation: ...

    async def get_session_status(self, session_id: str) -> str: ...

    async def get_state(self, session_id: str) -> SessionState: ...

    async def send_session_message(self, session_id: str, text: str) -> None: ...

    async def react(self, message: Mapping[str, object], emoji: str) -> None: ...

    async def send_markup(
        self,
        message: Mapping[str, object],
        text: str,
        markup: dict[str, object],
        *,
        ephemeral: bool = False,
    ) -> int | None: ...

    def new_choice_id(self) -> str: ...

    async def list_playbooks(self) -> list[Playbook]: ...

    async def settings_menu(
        self,
        message: Mapping[str, object],
        *,
        edit_message_id: int | None = None,
    ) -> None: ...

    async def usage(self, message: Mapping[str, object]) -> None: ...

    async def list_users(self, message: Mapping[str, object]) -> None: ...

    async def revoke_user(
        self, message: Mapping[str, object], user_id: int
    ) -> None: ...

    async def self_update(self, message: Mapping[str, object], args: str) -> None: ...


async def handle_command(
    runtime: CommandRuntime,
    message: Mapping[str, object],
    text: str,
) -> None:
    command, args = _parse(text)
    chat_id = _int(_mapping(message.get("chat")).get("id"))
    thread_id = _thread_id(message)
    conv_key = Store.conv_key(
        chat_id,
        thread_id,
        is_forum=is_topic_chat(message),
    )
    conversation = runtime.store.get_conversation(conv_key)
    if command in {"start", "help"}:
        help_text = _start_text() if command == "start" else _help_html()
        tip = (
            "Tip: enable Topics for this bot to run several Devin sessions "
            "side by side."
        )
        needs_tip = (
            _mapping(message.get("chat")).get("type") == "private"
            and not runtime.bot_topics_enabled
        )
        if needs_tip:
            help_text += f"\n{tip}"
        rich = _help_rich() if command == "help" else None
        if rich is not None and needs_tip:
            rich.append(rich_paragraph(tip))
        await runtime.send_text(
            message,
            help_text,
            ephemeral=True,
            html=command == "help",
            rich=rich,
        )
    elif command == "new":
        runtime.clear_queued_turns(conv_key)
        if conversation is not None:
            await runtime.detach_conversation(conversation)
        title = args.strip() or "Telegram conversation"
        runtime.store.set_setting(f"pending_title:{conv_key}", title)
        await runtime.send_text(
            message,
            f"◆ New conversation: {title}\n"
            "Send your first message to start Devin.",
        )
    elif command == "topic":
        name = args.strip()
        if not name:
            await runtime.send_text(message, "Usage: /topic <name>")
            return
        try:
            thread_id = await runtime.create_forum_topic(chat_id, name)
        except Exception as exc:
            reason = str(exc).casefold()
            if (
                "not a forum" in reason
                or "forums_disabled" in reason
                or "forum" in reason
            ):
                await runtime.send_text(
                    message,
                    "Topics aren't enabled here. Private chat: enable Topics for "
                    "the bot in @BotFather (Bot Settings → Topics) and in this "
                    "chat (tap the bot name → Topics). Group: group settings → Topics.",
                )
            else:
                raise
            return
        seed_message = dict(message)
        seed_message["message_thread_id"] = thread_id
        await runtime.send_text(
            seed_message,
            f"📌 {name} — send a message here to start a Devin session.",
        )
        await runtime.send_text(message, f"✓ Created topic {name}.")
    elif command == "sessions":
        await _sessions(runtime, message, conv_key, conversation)
    elif command == "resume":
        await _resume(runtime, message, conv_key, args)
    elif command == "status":
        if conversation is None:
            await runtime.send_text(message, "No active session.", ephemeral=True)
        else:
            state = await runtime.get_state(conversation.session_id)
            status = _STATUS_LABELS.get(state.status_enum, state.status_enum)
            detail = _STATUS_DETAIL.get(state.status_detail or "")
            if detail:
                status = f"{status} · {detail}"
            queued_line = (
                f" · {runtime.queued_count(conv_key)} queued"
                if runtime.queued_count(conv_key)
                else ""
            )

            def head(title: str) -> str:
                return (
                    f"<b>{escape(title)}</b>\n"
                    f"Status: {escape(status)}\n"
                    f"Session: {escape(conversation.session_url)}"
                    f"{queued_line}"
                )

            # The HTML send path has no chunker, so keep the reply under
            # Telegram's 4096 cap: shrink the user-supplied title first —
            # removing a raw char removes at least one escaped char, so a
            # single proportional cut always fits — then keep as many
            # detail entries as fit inside the expandable block.
            body = head(conversation.title)
            if len(body) > 4000:
                body = head(
                    conversation.title[
                        : max(1, len(conversation.title) - (len(body) - 3900))
                    ]
                )
            details = _status_details(conversation, state)
            while details and len(body + _expandable(details)) > 4000:
                details.pop()
            if details:
                body += _expandable(details)
            await runtime.send_text(
                message,
                body,
                ephemeral=True,
                html=True,
                rich=_status_rich(conversation, state, status, queued_line),
            )
    elif command == "settings":
        await runtime.settings_menu(message)
    elif command == "repos":
        value = args.strip()
        v3_hint = (
            ""
            if runtime.devin.v3_enabled
            else "\n⚠ Ignored until DEVIN_SERVICE_USER_API_KEY + DEVIN_ORG_ID are set"
        )
        if not value:
            current = runtime.store.get_settings(conv_key).repos
            await runtime.send_text(
                message,
                f"Repos: {current or 'all'}\n"
                "/repos owner/repo,org/repo2 to set · /repos all to reset"
                + v3_hint,
                ephemeral=True,
            )
        elif value.casefold() in {"all", "clear", "default", "off"}:
            runtime.store.update_chat_settings(conv_key, repos=None)
            await runtime.send_text(
                message,
                "Repos reset — new sessions see all repos." + v3_hint,
                ephemeral=True,
            )
        else:
            repos = [part.strip() for part in value.split(",") if part.strip()]
            if repos and all(
                re.fullmatch(r"[\w.-]+/[\w.-]+", repo) for repo in repos
            ):
                connected = await runtime.devin.repos()
                # Empty means the fetch failed — let Devin arbitrate
                # rather than block on a probe outage.
                unknown = (
                    [repo for repo in repos if repo not in connected]
                    if connected
                    else []
                )
                if unknown:
                    await runtime.send_text(
                        message,
                        f"Not connected to this org: {', '.join(unknown)}"
                        + v3_hint,
                        ephemeral=True,
                    )
                    return
                runtime.store.update_chat_settings(conv_key, repos=",".join(repos))
                await runtime.send_text(
                    message,
                    f"Repos: {', '.join(repos)} — applies to the next new session."
                    + v3_hint,
                    ephemeral=True,
                )
            else:
                await runtime.send_text(
                    message,
                    "Usage: /repos owner/repo[,org/repo2] · /repos all",
                    ephemeral=True,
                )
    elif command == "platform":
        value = args.strip()
        v3_hint = (
            ""
            if runtime.devin.v3_enabled
            else "\n⚠ Ignored until DEVIN_SERVICE_USER_API_KEY + DEVIN_ORG_ID are set"
        )
        if not value:
            current = runtime.store.get_settings(conv_key).platform
            available = await runtime.devin.platforms()
            options_line = (
                f"\nAvailable: {', '.join(available)}" if available else ""
            )
            await runtime.send_text(
                message,
                f"Platform: {current or 'default'}"
                + options_line
                + "\n/platform <pool-or-label> to set · /platform default to reset"
                + v3_hint,
                ephemeral=True,
            )
        elif value.casefold() in {"default", "cloud", "off", "reset"}:
            runtime.store.update_chat_settings(conv_key, platform=None)
            await runtime.send_text(
                message,
                "Platform reset — new sessions use the org default." + v3_hint,
                ephemeral=True,
            )
        elif re.fullmatch(r"[\w.-]+", value):
            available = await runtime.devin.platforms()
            # An empty list means the probe failed, not that the org has
            # no platforms — accept the name and let Devin arbitrate.
            if available and value not in available:
                await runtime.send_text(
                    message,
                    f"Unknown platform: {value}\n"
                    f"Available: {', '.join(available)}",
                    ephemeral=True,
                )
            else:
                runtime.store.update_chat_settings(conv_key, platform=value)
                await runtime.send_text(
                    message,
                    f"Platform: {value} — applies to the next new session."
                    + v3_hint,
                    ephemeral=True,
                )
        else:
            await runtime.send_text(
                message,
                "Usage: /platform <pool-or-label> · /platform default",
                ephemeral=True,
            )
    elif command == "crawl":
        enabled = sorted(runtime.crawl_sites())
        await runtime.send_text(
            message,
            f"Pre-crawl: {', '.join(enabled) if enabled else 'off'} "
            "(toggle in /settings)\n"
            f"Available: {', '.join(CRAWLER_NAMES)}",
            ephemeral=True,
        )
    elif command == "lang":
        user_id = _int(_mapping(message.get("from")).get("id"))
        key = f"lang:{user_id}"
        value = args.casefold()
        if not user_id:
            text = "/lang needs a sender; not available for channel posts."
        elif not value:
            configured = runtime.store.get_setting(key)
            default = runtime.settings.transcription_language or "auto"
            text = (
                f"Voice language: {configured}"
                if configured is not None
                else f"Voice language: default ({default})"
            )
        elif value in {"off", "default"}:
            runtime.store.delete_setting(key)
            text = "Voice language reset to default."
        elif re.fullmatch(r"[a-z]{2,3}|auto", value):
            runtime.store.set_setting(key, value)
            text = f"Voice language set to {value}."
        else:
            text = "Usage: /lang <code|auto|off> (e.g. /lang he)"
        await runtime.send_text(message, text, ephemeral=True)
    elif command == "usage":
        await runtime.usage(message)
    elif command == "users":
        await runtime.list_users(message)
    elif command == "revoke":
        try:
            user_id = int(args)
        except ValueError:
            await runtime.send_text(message, "Usage: /revoke <id>")
            return
        await runtime.revoke_user(message, user_id)
    elif command == "close":
        if thread_id is None:
            await runtime.send_text(
                message,
                "This command only works inside a topic.",
            )
            return
        if conversation is not None:
            await runtime.stop_conversation(conversation)
        runtime.clear_queued_turns(conv_key)
        try:
            await runtime.delete_forum_topic(chat_id, thread_id)
        except RuntimeError as exc:
            reason = str(exc).strip().replace("\n", " ")[:120] or "temporary error"
            await runtime.send_text(
                message,
                f"Couldn't delete this topic: {reason}",
            )
    elif command == "rename":
        if thread_id is None:
            await runtime.send_text(
                message,
                "This command only works inside a topic.",
            )
            return
        if not args:
            await runtime.send_text(message, "Usage: /rename <name>")
            return
        if conversation is not None:
            runtime.store.update_conversation(
                conversation.conv_key,
                conversation.session_id,
                title=args,
                title_pending=False,
            )
        try:
            await runtime.edit_forum_topic(chat_id, thread_id, args)
        except (RuntimeError, httpx.HTTPError):
            if conversation is not None:
                runtime.store.update_conversation(
                    conversation.conv_key,
                    conversation.session_id,
                    title=conversation.title,
                    title_pending=conversation.title_pending,
                )
            raise
    elif command in {"stop", "cancel"}:
        await _stop(runtime, message, conversation)
    elif command == "playbook":
        await _playbook(runtime, message, args)
    elif command == "retry":
        if conversation is None or not conversation.last_user_text:
            await runtime.send_text(message, "Nothing to retry.")
        else:
            await runtime.retry_conversation(conversation)
    elif command == "steer":
        if conversation is None:
            await runtime.send_text(message, "No active session.")
        elif not args:
            await runtime.send_text(
                message,
                "Usage: /steer <text> — send a message to the running session immediately.",
            )
        else:
            await runtime.send_session_message(conversation.session_id, args)
            await runtime.react(message, "👀")
            await runtime.start_watcher(
                conversation,
                trigger_message_id=_int(message.get("message_id")),
            )
    elif command == "whoami":
        await _whoami(runtime, message)
    elif command == "sethome":
        sender_id = _int(_mapping(message.get("from")).get("id"))
        if sender_id not in runtime.settings.admin_user_ids:
            return
        runtime.store.set_setting("home_chat_id", str(chat_id))
        runtime.store.set_setting("home_thread_id", str(thread_id or ""))
        await runtime.send_text(message, "📌 This chat is now the notification home.")
    elif command == "update":
        await runtime.self_update(message, args)
    else:
        await runtime.send_text(message, "Unknown command; /help")


async def _sessions(
    runtime: CommandRuntime,
    message: Mapping[str, object],
    conv_key: str,
    conversation: Conversation | None,
) -> None:
    history = runtime.store.list_history(conv_key)
    if not history:
        await runtime.send_text(message, "No saved sessions.", ephemeral=True)
        return
    rows: list[str] = []
    details: list[str] = []
    links: list[dict[str, object]] = []
    for index, entry in enumerate(history, start=1):
        try:
            status = await runtime.get_session_status(entry.session_id)
        except Exception:  # noqa: BLE001
            status = "unknown"
        marker = (
            "*"
            if conversation is not None and entry.session_id == conversation.session_id
            else " "
        )
        title = entry.title if len(entry.title) <= 200 else entry.title[:200] + "…"
        rows.append(f"{marker}{index}. {title} — {status}")
        details.append(f"{index}. {escape(entry.session_url)}")
        links.append(
            rich_paragraph(
                [f"{index}. ", rich_text_link(entry.session_url, entry.session_url)]
            )
        )
    # send_message's HTML path has no chunker: keep the rows under the
    # 4096 cap (oldest entries drop first), then keep as many details
    # entries as still fit.
    while len(rows) > 1 and len("\n".join(rows)) > 3900:
        rows.pop()
        details.pop()
        links.pop()
    body = "\n".join(escape(row) for row in rows)
    while details and len(body + _expandable(details)) > 4000:
        details.pop()
        links.pop()
    if details:
        body += _expandable(details)
    rich = [rich_paragraph(row) for row in rows]
    rich.append(rich_details("Links", links))
    await runtime.send_text(message, body, ephemeral=True, html=True, rich=rich)


async def _resume(
    runtime: CommandRuntime,
    message: Mapping[str, object],
    conv_key: str,
    args: str,
) -> None:
    try:
        index = int(args.strip()) - 1
    except ValueError:
        await runtime.send_text(message, "Usage: /resume <n>")
        return
    history = runtime.store.list_history(conv_key)
    if index < 0 or index >= len(history):
        await runtime.send_text(message, "Session number not found.")
        return
    entry = history[index]
    state = await runtime.get_state(entry.session_id)
    latest = next(
        (
            item.event_id
            for item in reversed(state.messages)
            if item.message_type == "devin_message" and item.event_id is not None
        ),
        None,
    )
    chat_id = _int(_mapping(message.get("chat")).get("id"))
    thread_id = _thread_id(message)
    title = entry.title
    title_pending = entry.title_pending
    retry_title = False
    if entry.title_pending and state.title:
        try:
            if thread_id is not None:
                await runtime.edit_forum_topic(
                    chat_id, thread_id, state.title[:128]
                )
        except (RuntimeError, httpx.HTTPError):
            retry_title = True
        else:
            title, title_pending = state.title, False
            runtime.store.update_history_title(
                entry.conv_key,
                entry.session_id,
                title,
            )
    conversation = await runtime.replace_conversation(
        conv_key=entry.conv_key,
        chat_id=chat_id,
        thread_id=thread_id,
        session_id=entry.session_id,
        session_url=entry.session_url,
        title=title,
        title_pending=title_pending,
        last_event_id=latest,
        last_pr_url=state.pr_url,
    )
    runtime.store.delete_setting(f"pending_title:{conv_key}")
    if retry_title:
        await runtime.start_watcher(conversation)
    await runtime.send_text(message, f"✓ Resumed: {title}\n{entry.session_url}")


async def _stop(
    runtime: CommandRuntime,
    message: Mapping[str, object],
    conversation: Conversation | None,
) -> None:
    if conversation is None:
        runtime.clear_queued_turns(
            Store.conv_key(
                _int(_mapping(message.get("chat")).get("id")),
                _thread_id(message),
                is_forum=is_topic_chat(message),
            )
        )
        await runtime.send_text(message, "No active session.")
        return
    choice_id = runtime.new_choice_id()
    message_id = await runtime.send_markup(
        message,
        "Terminate the active Devin session?",
        {"inline_keyboard": [[
            {"text": "Terminate", "callback_data": choice_id, "style": "danger"},
            {
                "text": "Cancel",
                "callback_data": f"{choice_id}:cancel",
                "style": "primary",
            },
        ]]},
    )
    runtime.store.add_choice(
        choice_id,
        conversation.conv_key,
        conversation.session_id,
        conversation.chat_id,
        f"__cmd:terminate:{conversation.session_id}",
        message_id,
    )
    runtime.store.add_choice(
        f"{choice_id}:cancel",
        conversation.conv_key,
        conversation.session_id,
        conversation.chat_id,
        "__cmd:cancel",
        message_id,
    )


async def _playbook(
    runtime: CommandRuntime,
    message: Mapping[str, object],
    args: str,
) -> None:
    playbooks = await runtime.list_playbooks()
    if not args.strip():
        if not playbooks:
            await runtime.send_text(message, "No playbooks available.")
        else:
            await runtime.send_text(
                message,
                "\n".join(
                    f"{index}. {playbook.title}"
                    for index, playbook in enumerate(playbooks, start=1)
                ),
            )
        return
    pieces = args.split(maxsplit=1)
    try:
        index = int(pieces[0]) - 1
    except ValueError:
        await runtime.send_text(message, "Usage: /playbook <n> [text]")
        return
    if index < 0 or index >= len(playbooks):
        await runtime.send_text(message, "Playbook number not found.")
        return
    playbook = playbooks[index]
    prompt = pieces[1] if len(pieces) == 2 else "Run this playbook."
    await runtime.create_session_for_message(
        message,
        SYSTEM_PREAMBLE + prompt,
        playbook.title,
        playbook_id=playbook.playbook_id,
    )


async def _whoami(runtime: CommandRuntime, message: Mapping[str, object]) -> None:
    sender = _mapping(message.get("from"))
    chat = _mapping(message.get("chat"))
    user_id = _int(sender.get("id"))
    chat_id = _int(chat.get("id"))
    allowed = is_allowed(message, runtime.settings, runtime.approved_users)
    home = runtime.store.get_setting("home_chat_id") == str(chat_id)
    await runtime.send_text(
        message,
        (
            f"User ID: {user_id}\nChat ID: {chat_id}\n"
            f"Thread ID: {_thread_id(message) or 'none'}\n"
            f"Allowed: {'yes' if allowed else 'no'}\n"
            f"Home: {'yes' if home else 'no'}"
        ),
        ephemeral=True,
    )


def _parse(text: str) -> tuple[str, str]:
    match = re.match(r"^/([A-Za-z0-9_-]+)(?:@\S+)?(?:\s+(.*))?$", text, re.DOTALL)
    if match is None:
        return "", ""
    return match.group(1).lower().replace("_", "-"), (match.group(2) or "").strip()


def _start_text() -> str:
    return (
        "👋 I'm your bridge to Devin.\n\n"
        "Just send a message and it becomes a Devin session in this chat — "
        "replies stream back here, with buttons when Devin asks a question. "
        "Photos, documents, and voice notes are sent along as attachments.\n\n"
        "First steps:\n"
        "• /new <title> — start a session explicitly\n"
        "• /status — what Devin is doing right now\n"
        "• /settings — notifications, drafts, defaults\n"
        "• /help — every command\n\n"
        "Reactions: 🔁 on your message retries it, 🛑 stops the session."
    )


def _expandable(lines: list[str]) -> str:
    return (
        "\n<blockquote expandable>"
        + "\n".join(lines)
        + "</blockquote>"
    )


def _status_details(conversation: Conversation, state: SessionState) -> list[str]:
    lines = [f"id: <code>{escape(conversation.session_id)}</code>"]
    if state.status_detail:
        lines.append(f"detail: {escape(state.status_detail)}")
    if state.acus_consumed:
        lines.append(f"ACUs: {state.acus_consumed:g}")
    if state.updated_at:
        lines.append(f"updated: {escape(state.updated_at)}")
    lines.extend(
        f"PR: {escape(url)}"
        for url in (state.pr_urls or ((state.pr_url,) if state.pr_url else ()))
    )
    return lines


def _status_rich(
    conversation: Conversation,
    state: SessionState,
    status: str,
    queued_line: str,
) -> list[dict[str, object]]:
    detail_blocks: list[dict[str, object]] = [
        rich_paragraph(["id: ", rich_text_code(conversation.session_id)])
    ]
    if state.status_detail:
        detail_blocks.append(rich_paragraph(f"detail: {state.status_detail}"))
    if state.acus_consumed:
        detail_blocks.append(rich_paragraph(f"ACUs: {state.acus_consumed:g}"))
    if state.updated_at:
        detail_blocks.append(rich_paragraph(f"updated: {state.updated_at}"))
    detail_blocks.extend(
        rich_paragraph(["PR: ", rich_text_link(url, url)])
        for url in (state.pr_urls or ((state.pr_url,) if state.pr_url else ()))
    )
    return [
        rich_paragraph(rich_text_bold(conversation.title)),
        rich_paragraph(f"Status: {status}"),
        rich_paragraph(f"Session: {conversation.session_url}{queued_line}"),
        rich_details("Details", detail_blocks),
    ]


_HELP_SECTIONS = (
    (
        "Sessions",
        [
            "/new [title] — start a fresh session on your next message",
            "/status — what Devin is doing",
            "/stop (/cancel) — terminate the session",
            "/retry — resend your last message",
            "/steer <text> — inject into the running session",
            "/sessions · /resume <n> — history and switching",
        ],
    ),
    (
        "Topics",
        [
            "/topic <name> — new topic with its own session",
            "/rename <name> — rename this topic",
            "/close — close this topic's session",
        ],
    ),
    (
        "Setup and admin",
        [
            "/playbook [n] [text] — list or run a playbook",
            "/settings — notifications, drafts, mode, defaults",
            "/repos [a/b,c/d] — restrict sessions to repos",
            "/platform [name] — run sessions on an outpost pool or VM platform",
            "/crawl — show which sites get pre-crawled (toggle in /settings)",
            "/lang [code] — voice-note language",
            "/usage — Devin ACU usage",
            "/whoami — your IDs and access",
            "/sethome — route notifications here",
            "/users · /revoke <id> — approved users (admin)",
            "/update [check] [channel] — self-update (admin)",
        ],
    ),
)


def _help_html() -> str:
    parts = [
        f"<b>{title}</b>" + _expandable([escape(item) for item in items])
        for title, items in _HELP_SECTIONS
    ]
    parts.append("Reactions: 🔁 retry · 🛑 stop")
    return "\n".join(parts)


def _help_rich() -> list[dict[str, object]]:
    blocks = [
        rich_details(
            rich_text_bold(title),
            [rich_paragraph(item) for item in items],
        )
        for title, items in _HELP_SECTIONS
    ]
    blocks.append(rich_paragraph("Reactions: 🔁 retry · 🛑 stop"))
    return blocks


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _thread_id(message: Mapping[str, object]) -> int | None:
    value = _mapping(message).get("message_thread_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None
