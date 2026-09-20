from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from app.access import is_allowed, is_topic_chat
from app.config import Settings
from app.devin import Playbook, SessionState
from app.store import Conversation, Store

SYSTEM_PREAMBLE = (
    "You are chatting with a user over Telegram via a bridge. Keep replies concise. "
    "Telegram renders Markdown. When you need the user to pick between a small set "
    "of options, end your message with one line exactly like `OPTIONS: first option | "
    "second option | third option` (max 8, each under 60 chars); the bridge turns it "
    "into buttons. Never ask the user to open a UI; they only see your messages.\n\n"
    "User message: "
)


class CommandRuntime(Protocol):
    settings: Settings
    store: Store
    bot_topics_enabled: bool
    approved_users: set[int]

    async def send_text(
        self,
        message: Mapping[str, object],
        text: str,
        *,
        silent: bool = False,
        ephemeral: bool = False,
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

    def clear_queued_turns(self, conv_key: str) -> None: ...

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

    async def revoke_user(self, message: Mapping[str, object], user_id: int) -> None: ...

    async def self_update(
        self, message: Mapping[str, object], args: str
    ) -> None: ...


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
        help_text = _help_text()
        if (
            _mapping(message.get("chat")).get("type") == "private"
            and not runtime.bot_topics_enabled
        ):
            help_text += (
                "\nTip: enable Topics for this bot to run several Devin sessions "
                "side by side."
            )
        await runtime.send_text(message, help_text, ephemeral=True)
    elif command == "new":
        runtime.clear_queued_turns(conv_key)
        title = args.strip() or "Telegram conversation"
        prompt = (
            f"The user started a new conversation titled '{title}'. "
            "Greet briefly and wait."
        )
        await runtime.create_session_for_message(
            message,
            SYSTEM_PREAMBLE + prompt,
            title,
            playbook_id=runtime.store.get_settings(conv_key).default_playbook,
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
        await runtime.send_text(message, f"Created topic {name}.")
    elif command == "sessions":
        await _sessions(runtime, message, conv_key, conversation)
    elif command == "resume":
        await _resume(runtime, message, conv_key, args)
    elif command == "status":
        if conversation is None:
            await runtime.send_text(message, "No active session.", ephemeral=True)
        else:
            state = await runtime.get_state(conversation.session_id)
            status = state.status_enum
            pr_line = f"\nPR: {state.pr_url}" if state.pr_url else ""
            queued_line = (
                f" · {runtime.queued_count(conv_key)} queued"
                if runtime.queued_count(conv_key)
                else ""
            )
            await runtime.send_text(
                message,
                (
                    f"{conversation.title}\n"
                    f"Status: {status}\n"
                    f"Session: {conversation.session_url}"
                    f"{queued_line}"
                    f"{pr_line}"
                ),
                ephemeral=True,
            )
    elif command == "settings":
        await runtime.settings_menu(message)
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
        await runtime.edit_forum_topic(chat_id, thread_id, args)
        if conversation is not None:
            runtime.store.update_conversation(
                conversation.conv_key,
                conversation.session_id,
                title=args,
                title_pending=False,
            )
            runtime.store.update_history_title(
                conversation.conv_key,
                conversation.session_id,
                args,
            )
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
        await runtime.send_text(message, "This chat is now the notification home.")
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
    for index, entry in enumerate(history, start=1):
        try:
            status = await runtime.get_session_status(entry.session_id)
        except Exception:  # noqa: BLE001
            status = "unknown"
        marker = "*" if conversation is not None and entry.session_id == conversation.session_id else " "
        rows.append(f"{marker}{index}. {entry.title} — {status}")
    await runtime.send_text(message, "\n".join(rows), ephemeral=True)


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
    await runtime.replace_conversation(
        conv_key=entry.conv_key,
        chat_id=_int(_mapping(message.get("chat")).get("id")),
        thread_id=_thread_id(message),
        session_id=entry.session_id,
        session_url=entry.session_url,
        title=entry.title,
        last_event_id=latest,
    )
    await runtime.send_text(message, f"Resumed: {entry.title} {entry.session_url}")


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


def _help_text() -> str:
    return (
        "/new [title]\n/topic <name>\n/close\n/rename <name>\n/sessions\n"
        "/resume <n>\n/status\n/stop (/cancel)\n/playbook [n] [text]\n/retry\n"
        "/steer <text>\n/settings\n/usage\n/whoami\n/sethome\n/users\n/revoke <id>\n"
        "/update [check]\n/help"
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _thread_id(message: Mapping[str, object]) -> int | None:
    value = _mapping(message).get("message_thread_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None
