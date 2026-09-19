from __future__ import annotations

import re
from collections.abc import Mapping

from app.config import Settings


def is_allowed(
    message: Mapping[str, object],
    settings: Settings,
    approved_users: set[int] | frozenset[int] | None = None,
) -> bool:
    sender = _mapping(message.get("from"))
    chat = _mapping(message.get("chat"))
    if bool(sender.get("is_bot")):
        return False
    user_id = _int(sender.get("id"))
    chat_id = _int(chat.get("id"))
    if settings.telegram_allow_all_users:
        return True
    approved = (
        approved_users is not None
        and user_id in approved_users
        and chat_id == user_id
    )
    if settings.allowed_users and user_id not in settings.allowed_users and not approved:
        return False
    if settings.allowed_chat_ids and chat_id not in settings.allowed_chat_ids:
        return False
    if approved:
        return True
    return bool(settings.allowed_users or settings.allowed_chat_ids) and (
        user_id in settings.allowed_users or chat_id in settings.allowed_chat_ids
    )


def should_respond_in_group(
    message: Mapping[str, object],
    bot_username: str,
    free_response_chats: frozenset[int],
) -> bool:
    chat = _mapping(message.get("chat"))
    if chat.get("type") not in {"group", "supergroup"}:
        return True
    chat_id = _int(chat.get("id"))
    if chat_id in free_response_chats:
        return True
    reply = _mapping(message.get("reply_to_message"))
    reply_sender = _mapping(reply.get("from"))
    reply_username = _text(reply_sender.get("username"))
    if reply_username is not None and reply_username.casefold() == bot_username.casefold():
        return True
    text = _text(message.get("text")) or _text(message.get("caption")) or ""
    return f"@{bot_username.lower()}" in text.lower()


def is_topic_chat(message: Mapping[str, object]) -> bool:
    chat = _mapping(message.get("chat"))
    return bool(chat.get("is_forum")) or bool(message.get("is_topic_message"))


def strip_bot_mention(text: str, bot_username: str) -> str:
    if not bot_username:
        return text.strip()
    return re.sub(
        rf"@{re.escape(bot_username)}\b",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None
