from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from app.access import is_topic_chat
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

    async def send_text(
        self,
        message: Mapping[str, object],
        text: str,
        *,
        silent: bool = False,
    ) -> None: ...

    async def create_session_for_message(
        self,
        message: Mapping[str, object],
        prompt: str,
        title: str,
        *,
        playbook_id: str | None = None,
        start_watcher: bool = True,
    ) -> Conversation: ...

    async def start_watcher(self, conversation: Conversation) -> None: ...

    async def create_forum_topic(self, chat_id: int, name: str) -> int: ...

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
    ) -> Conversation: ...

    async def get_session_status(self, session_id: str) -> str: ...

    async def get_state(self, session_id: str) -> SessionState: ...

    async def send_session_message(self, session_id: str, text: str) -> None: ...

    async def send_markup(
        self,
        message: Mapping[str, object],
        text: str,
        markup: dict[str, object],
    ) -> None: ...

    def new_choice_id(self) -> str: ...

    async def list_playbooks(self) -> list[Playbook]: ...


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
        await runtime.send_text(message, _help_text())
    elif command == "new":
        title = args.strip() or "Telegram conversation"
        prompt = (
            f"The user started a new conversation titled '{title}'. "
            "Greet briefly and wait."
        )
        await runtime.create_session_for_message(
            message,
            SYSTEM_PREAMBLE + prompt,
            title,
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
                    "Topics aren't enabled here. Private chat: open this chat, "
                    "tap the bot name → enable Topics. Group: group settings → Topics.",
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
            await runtime.send_text(message, "No active session.")
        else:
            state = await runtime.get_state(conversation.session_id)
            status = state.status_enum
            pr_line = f"\nPR: {state.pr_url}" if state.pr_url else ""
            await runtime.send_text(
                message,
                (
                    f"{conversation.title}\n"
                    f"Status: {status}\n"
                    f"Session: {conversation.session_url}"
                    f"{pr_line}"
                ),
            )
    elif command == "stop":
        await _stop(runtime, message, conversation)
    elif command == "playbook":
        await _playbook(runtime, message, args)
    elif command == "retry":
        if conversation is None or not conversation.last_user_text:
            await runtime.send_text(message, "Nothing to retry.")
        else:
            runtime.store.update_conversation(
                conversation.conv_key,
                conversation.session_id,
                last_user_text=conversation.last_user_text,
            )
            await runtime.send_session_message(
                conversation.session_id,
                conversation.last_user_text,
            )
            await runtime.start_watcher(conversation)
    elif command == "whoami":
        await _whoami(runtime, message)
    elif command == "sethome":
        runtime.store.set_setting("home_chat_id", str(chat_id))
        runtime.store.set_setting("home_thread_id", str(thread_id or ""))
        await runtime.send_text(message, "This chat is now the notification home.")
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
        await runtime.send_text(message, "No saved sessions.")
        return
    rows: list[str] = []
    for index, entry in enumerate(history, start=1):
        try:
            status = await runtime.get_session_status(entry.session_id)
        except Exception:  # noqa: BLE001
            status = "unknown"
        marker = "*" if conversation is not None and entry.session_id == conversation.session_id else " "
        rows.append(f"{marker}{index}. {entry.title} — {status}")
    await runtime.send_text(message, "\n".join(rows))


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
        await runtime.send_text(message, "No active session.")
        return
    choice_id = runtime.new_choice_id()
    runtime.store.add_choice(
        choice_id,
        conversation.conv_key,
        conversation.session_id,
        conversation.chat_id,
        f"__cmd:terminate:{conversation.session_id}",
    )
    runtime.store.add_choice(
        f"{choice_id}:cancel",
        conversation.conv_key,
        conversation.session_id,
        conversation.chat_id,
        "__cmd:cancel",
    )
    await runtime.send_markup(
        message,
        "Terminate the active Devin session?",
        {"inline_keyboard": [[
            {"text": "Terminate", "callback_data": choice_id},
            {"text": "Cancel", "callback_data": f"{choice_id}:cancel"},
        ]]},
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
    allowed = runtime.settings.telegram_allow_all_users or (
        user_id in runtime.settings.allowed_users
        or chat_id in runtime.settings.allowed_chat_ids
    )
    home = runtime.store.get_setting("home_chat_id") == str(chat_id)
    await runtime.send_text(
        message,
        (
            f"User ID: {user_id}\nChat ID: {chat_id}\n"
            f"Thread ID: {_thread_id(message) or 'none'}\n"
            f"Allowed: {'yes' if allowed else 'no'}\n"
            f"Home: {'yes' if home else 'no'}"
        ),
    )


def _parse(text: str) -> tuple[str, str]:
    match = re.match(r"^/([A-Za-z0-9_-]+)(?:@\S+)?(?:\s+(.*))?$", text, re.DOTALL)
    if match is None:
        return "", ""
    return match.group(1).lower().replace("_", "-"), (match.group(2) or "").strip()


def _help_text() -> str:
    return (
        "/new [title]\n/topic <name>\n/sessions\n/resume <n>\n/status\n/stop\n"
        "/playbook [n] [text]\n/retry\n/whoami\n/sethome\n/help"
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _thread_id(message: Mapping[str, object]) -> int | None:
    value = _mapping(message).get("message_thread_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None
