from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Self, cast

import httpx
import pytest
from PIL import Image

import app.main as main_module
from app.access import is_allowed, is_topic_chat, should_respond_in_group
from app.clients import DevinClient, DevinMessage, SessionState, TelegramClient
from app.commands import handle_command
from app.config import Settings
from app.formatting import (
    chunk,
    extract_attachments,
    extract_options,
    markdown_to_telegram_markdown_v2,
    normalize_rich_linebreaks,
)
from app.main import Bridge, create_app
from app.store import Store
from app.watcher import SessionWatcher


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": "token-placeholder",
        "telegram_webhook_secret": "secret-placeholder",
        "devin_api_key": "key-placeholder",
        "public_base_url": "http://localhost",
        "database_path": str(tmp_path / "bridge.sqlite3"),
        "telegram_allowed_users": "111",
        "bot_username": "testbot",
        "notify_secret": "notify-placeholder",
        "devin_poll_seconds": 0,
        "devin_watch_timeout_seconds": 1,
        "devin_settle_seconds": 30,
        "telegram_debounce_seconds": 0,
        "telegram_queue_while_busy": False,
    }
    values.update(overrides)
    return Settings(**values)


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def message(
    text: str,
    *,
    chat_id: int = 222,
    user_id: int = 111,
    message_id: int = 7,
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "from": {"id": user_id, "is_bot": False},
        "chat": {"id": chat_id, "type": "private"},
        "text": text,
    }


def test_formatting_and_options() -> None:
    rendered = markdown_to_telegram_markdown_v2(
        "**bold** *em* `code` [link](https://example.test/a) # heading"
    )
    assert r"\*bold\*" not in rendered
    assert "*bold*" in rendered
    assert "_em_" in rendered
    assert "`code`" in rendered
    assert r"\# heading" in rendered
    body, options = extract_options("Choose:\nOPTIONS: Yes | No")
    assert body == "Choose:"
    assert options == ["Yes", "No"]
    assert extract_options("OPTIONS: " + " | ".join(str(i) for i in range(9)))[1] == []


def test_extract_attachments() -> None:
    url = "https://app.devin.ai/attachments/dfae59c0-7722-4087-aa5f-e031dc73205b/bridge_test.txt"
    body, urls = extract_attachments(
        "...text...\n"
        "OPTIONS: All good | Markdown broken | Attachment missing | Buttons missing\n"
        f'ATTACHMENT:{{"url":"{url}","fileSize":56}}'
    )
    assert body.endswith(
        "OPTIONS: All good | Markdown broken | Attachment missing | Buttons missing"
    )
    assert urls == [url]
    assert extract_attachments('before\nATTACHMENT:{"url":}\nafter') == (
        "before\nafter",
        [],
    )
    unchanged = "text without attachment lines\n"
    assert extract_attachments(unchanged) == (unchanged, [])


def test_chunk_preserves_fence_and_suffix() -> None:
    parts = chunk("```python\n" + ("x" * 100) + "\n```\n" + ("y" * 100), 80)
    assert len(parts) > 1
    assert all(len(part) <= 80 for part in parts)
    assert all(part.count("```") % 2 == 0 for part in parts)
    assert all(f"({index}/{len(parts)})" in part for index, part in enumerate(parts, 1))


def test_access_rules() -> None:
    config = settings(Path("/tmp"), telegram_allowed_users="111")
    assert is_allowed(message("hi"), config)
    assert not is_allowed(message("hi", user_id=333), config)
    assert is_allowed(message("hi", user_id=222), config, {222})
    restricted_chat = settings(
        Path("/tmp"),
        telegram_allowed_users="",
        telegram_allowed_chat_ids="-100",
    )
    assert is_allowed(message("hi", user_id=222), restricted_chat, {222})
    group_config = settings(
        Path("/tmp"),
        telegram_allowed_users="",
        telegram_allowed_chat_ids="-777",
    )
    assert not is_allowed(
        {
            **message("hi", chat_id=-888, user_id=222),
            "chat": {"id": -888, "type": "supergroup"},
        },
        group_config,
        {222},
    )
    assert not is_allowed(message("hi", user_id=111), settings(Path("/tmp"), telegram_allowed_users=""))
    assert is_allowed(
        message("hi", user_id=333),
        settings(Path("/tmp"), telegram_allow_all_users=True, telegram_allowed_users=""),
    )
    group = {
        **message("hi"),
        "chat": {"id": -222, "type": "group"},
    }
    assert not should_respond_in_group(group, "testbot", frozenset())
    assert should_respond_in_group(
        {**group, "text": "hi @testbot"},
        "testbot",
        frozenset(),
    )


def test_store_topics_dedupe_and_history(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "store.sqlite3"))
    assert Store.conv_key(222) == "222"
    assert Store.conv_key(222, 9, is_forum=True) == "222:9"
    assert Store.conv_key(222, 9, is_forum=False) == "222"
    assert store.mark_update_seen(4)
    assert not store.mark_update_seen(4)
    store.save_conversation(
        conv_key="222:9",
        chat_id=222,
        thread_id=9,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="first",
    )
    store.add_history(
        conv_key="222:9",
        session_id="s1",
        session_url="https://devin.test/s1",
        title="first",
    )
    assert store.list_history("222:9")[0].session_id == "s1"
    store.update_conversation("222:9", "s1", title="renamed")
    assert store.list_history("222:9")[0].title == "renamed"
    store.close()


def test_private_topic_uses_thread_conversation_key() -> None:
    root = message("root")
    topic = {
        **root,
        "message_thread_id": 9,
        "is_topic_message": True,
    }
    assert not is_topic_chat(root)
    assert is_topic_chat(topic)
    assert Store.conv_key(222) != Store.conv_key(
        222,
        9,
        is_forum=is_topic_chat(topic),
    )


def test_store_migrates_legacy_sessions(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE chat_sessions (
            chat_id INTEGER PRIMARY KEY,
            devin_session_id TEXT NOT NULL,
            last_message_id TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO chat_sessions VALUES (?, ?, ?)",
        (222, "devin-s-old", "event-old"),
    )
    connection.commit()
    connection.close()

    store = Store(str(database_path))
    conversation = store.get_conversation("222")
    assert conversation is not None
    assert conversation.session_id == "devin-s-old"
    assert conversation.session_url.endswith("/sessions/s-old")
    assert conversation.last_event_id == "event-old"
    assert store.list_history("222")[0].session_id == "devin-s-old"
    assert store.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'chat_sessions'"
    ).fetchone() is None
    store.close()


def test_text_link_expansion_handles_emoji_offsets() -> None:
    from app.main import _expand_text_links

    value = {
        "text": "😀See link",
        "entities": [
            {
                "type": "text_link",
                "offset": 6,
                "length": 4,
                "url": "https://example.test",
            }
        ],
    }
    assert _expand_text_links(value, "text") == (
        "😀See link (https://example.test)"
    )


def test_group_bot_mention_is_removed_from_turn_text() -> None:
    from app.access import strip_bot_mention

    assert strip_bot_mention("please ask @TestBot to inspect this", "testbot") == (
        "please ask  to inspect this"
    )


@pytest.mark.asyncio
async def test_clients_use_injected_mock_transports() -> None:
    devin_paths: list[str] = []
    devin_bodies: list[dict[str, object]] = []
    telegram_paths: list[str] = []

    async def devin_handler(request: httpx.Request) -> httpx.Response:
        devin_paths.append(request.url.path)
        if request.url.path == "/v1/sessions":
            devin_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"session_id": "s1", "url": "https://devin.test/s1"},
            )
        if request.url.path == "/v1/sessions/s1":
            return httpx.Response(
                200,
                json={
                    "status_enum": "finished",
                    "title": "one",
                    "pull_request": None,
                    "messages": [],
                },
            )
        return httpx.Response(200, json={})

    async def telegram_handler(request: httpx.Request) -> httpx.Response:
        telegram_paths.append(request.url.path)
        if request.url.path.endswith("/createForumTopic"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_thread_id": 19}},
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(devin_handler),
    )
    telegram = TelegramClient(
        "fake-token",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(telegram_handler),
    )
    assert await devin.create_session("prompt", "title") == (
        "s1",
        "https://devin.test/s1",
    )
    assert "title" in devin_bodies[-1]
    assert await devin.create_session("prompt", None) == (
        "s1",
        "https://devin.test/s1",
    )
    assert "title" not in devin_bodies[-1]
    assert (await devin.get_session("s1")).status_enum == "finished"
    await telegram.send_message(222, "hello")
    assert await telegram.create_forum_topic(222, "New topic") == 19
    assert "/v1/sessions" in devin_paths
    assert any(path.endswith("/sendMessage") for path in telegram_paths)
    await devin.close()
    await telegram.close()


@pytest.mark.asyncio
async def test_telegram_topic_error_includes_description() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "description": "Bad Request: not a forum"},
        )

    telegram = TelegramClient(
        "fake-token",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="not a forum"):
        await telegram.create_forum_topic(222, "New topic")
    await telegram.close()


@pytest.mark.asyncio
async def test_telegram_parse_fallback_preserves_then_drops_markup() -> None:
    bodies: list[dict[str, object]] = []
    attempts = 0

    async def telegram_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        bodies.append(json.loads(request.content))
        if attempts < 3:
            return httpx.Response(400, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {}})

    telegram = TelegramClient(
        "fake-token",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(telegram_handler),
    )
    await telegram.send_message(
        222,
        "*hello*",
        parse_mode="MarkdownV2",
        reply_markup={"inline_keyboard": []},
    )
    assert "parse_mode" not in bodies[1]
    assert "reply_markup" in bodies[1]
    assert "reply_markup" not in bodies[2]
    await telegram.close()


@pytest.mark.asyncio
async def test_watcher_settles_stale_status_and_renders_options() -> None:
    class FakeDevin:
        def __init__(self) -> None:
            self.calls = 0

        async def get_session(self, _: str) -> SessionState:
            self.calls += 1
            messages = (
                [
                    DevinMessage(
                        "devin_message",
                        "event-1",
                        "**Done**\nOPTIONS: Yes | No.",
                        None,
                    )
                ]
                if self.calls == 4
                else []
            )
            status = (
                "working"
                if self.calls == 3
                else "blocked"
            )
            return SessionState(status, "title", None, messages)

    class FakeTelegram:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.reactions: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

        async def send_markdown(
            self, chat_id: int, text: str, **kwargs: object
        ) -> list[dict[str, object]]:
            await self.send_message(chat_id, text, **kwargs)
            return [{**self.sent[-1], "message_id": len(self.sent)}]

        async def send_chat_action(self, *_: object, **__: object) -> None:
            return None

        async def set_message_reaction(
            self, _chat_id: int, _message_id: int, emoji: str
        ) -> None:
            self.reactions.append(emoji)

        async def react(
            self, chat_id: int, message_id: int | None, emoji: str | None
        ) -> bool:
            if message_id is None:
                return False
            try:
                await self.set_message_reaction(chat_id, message_id, emoji)
            except Exception:  # noqa: BLE001 - mirrors client
                return False
            return True

    now = 0.0

    def clock() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += max(seconds, 1.0)

    config = settings(
        Path("/tmp"),
        devin_poll_seconds=1,
        devin_watch_timeout_seconds=20,
        devin_settle_seconds=30,
    )
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = FakeTelegram()
    devin = FakeDevin()
    await SessionWatcher(
        conversation,
        store,
        devin,
        telegram,  # type: ignore[arg-type]
        config,
        clock=clock,
        sleep=sleep,
        trigger_message_id=7,
    ).run()
    assert [item["text"] for item in telegram.sent] == ["**Done**"]
    assert "parse_mode" not in telegram.sent[0]
    markup = cast(dict[str, object], telegram.sent[0]["reply_markup"])
    keyboard = cast(list[list[dict[str, str]]], markup["inline_keyboard"])
    assert [row[0]["text"] for row in keyboard] == ["Yes", "No."]
    assert devin.calls == 4
    assert telegram.reactions == ["👍"]


@pytest.mark.asyncio
async def test_webhook_new_message_watcher_and_duplicate(
    tmp_path: Path,
) -> None:
    state_calls = 0
    telegram_calls: list[tuple[str, dict[str, object]]] = []

    async def devin_handler(request: httpx.Request) -> httpx.Response:
        nonlocal state_calls
        if request.url.path == "/v1/sessions":
            return httpx.Response(
                200,
                json={"session_id": "s1", "url": "https://devin.test/s1"},
            )
        if request.url.path == "/v1/sessions/s1":
            state_calls += 1
            return httpx.Response(
                200,
                json={
                    "status_enum": "finished",
                    "title": "title",
                    "pull_request": None,
                    "messages": [
                        {
                            "type": "devin_message",
                            "event_id": "e1",
                            "message": "first",
                        },
                        {
                            "type": "devin_message",
                            "event_id": "e2",
                            "message": "second",
                        },
                    ],
                },
            )
        return httpx.Response(200, json={})

    async def telegram_handler(request: httpx.Request) -> httpx.Response:
        payload = request.content
        value = json.loads(payload) if payload else {}
        telegram_calls.append((request.url.path, value))
        return httpx.Response(200, json={"ok": True, "result": {}})

    config = settings(tmp_path)
    app = create_app(
        config,
        store=Store(config.database_path),
        devin=DevinClient(
            "fake-key",
            "https://devin.test",
            3,
            transport=httpx.MockTransport(devin_handler),
        ),
        telegram=TelegramClient(
            "fake-token",
            base_url="https://telegram.test/botfake",
            transport=httpx.MockTransport(telegram_handler),
        ),
    )
    runtime = cast(object, app.state.bridge)
    await runtime.startup()  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        update = {"update_id": 1, "message": message("hello")}
        response = await client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret-placeholder"},
            json=update,
        )
        assert response.status_code == 200
        duplicate = await client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret-placeholder"},
            json=update,
        )
        assert duplicate.status_code == 200
        await asyncio.sleep(0.05)
    send_texts = [
        (
            value["text"]
            if path.endswith("/sendMessage")
            else cast(dict[str, object], value["rich_message"])["markdown"]
        )
        for path, value in telegram_calls
        if (
            path.endswith("/sendMessage")
            and isinstance(value.get("text"), str)
        )
        or (
            path.endswith("/sendRichMessage")
            and isinstance(value.get("rich_message"), dict)
        )
    ]
    assert any("Started session" in text for text in send_texts)
    assert "first" in send_texts
    assert "second" in send_texts
    assert state_calls == 1
    await runtime.shutdown()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_notify_auth_and_photo_upload(tmp_path: Path) -> None:
    devin_paths: list[str] = []
    telegram_calls: list[tuple[str, dict[str, object]]] = []

    async def devin_handler(request: httpx.Request) -> httpx.Response:
        devin_paths.append(request.url.path)
        if request.url.path == "/v1/sessions":
            return httpx.Response(
                200,
                json={"session_id": "s1", "url": "https://devin.test/s1"},
            )
        if request.url.path == "/v1/attachments":
            attachment_bodies.append(request.content)
            return httpx.Response(200, json="https://files.test/a")
        if request.url.path == "/v1/sessions/s1":
            return httpx.Response(
                200,
                json={
                    "status_enum": "working",
                    "title": "title",
                    "pull_request": None,
                    "messages": [],
                },
            )
        return httpx.Response(200, json={})

    async def telegram_handler(request: httpx.Request) -> httpx.Response:
        value = json.loads(request.content) if request.content else {}
        telegram_calls.append((request.url.path, value))
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/a.jpg"}})
        if request.url.path.startswith("/file/"):
            return httpx.Response(200, content=b"image")
        return httpx.Response(200, json={"ok": True, "result": {}})

    config = settings(tmp_path)
    attachment_bodies: list[bytes] = []
    app = create_app(
        config,
        store=Store(config.database_path),
        devin=DevinClient(
            "fake-key",
            "https://devin.test",
            3,
            transport=httpx.MockTransport(devin_handler),
        ),
        telegram=TelegramClient(
            "fake-token",
            base_url="https://telegram.test/botfake",
            transport=httpx.MockTransport(telegram_handler),
        ),
    )
    runtime = app.state.bridge
    await runtime.startup()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.post("/notify", json={"text": "x"})
        assert unauthorized.status_code == 403
        notified = await client.post(
            "/notify",
            headers={"Authorization": "Bearer notify-placeholder"},
            json={"text": "x", "chat_id": 222},
        )
        assert notified.json() == {"sent": 1}
        photo = message(
            "look",
            message_id=8,
        )
        photo["photo"] = [{"file_id": "file-1", "width": 1, "height": 1}]
        photo.pop("text")
        response = await client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret-placeholder"},
            json={"update_id": 2, "message": photo},
        )
        assert response.status_code == 200
        await asyncio.sleep(0.03)
        voice = message("listen", message_id=9)
        voice["voice"] = {"file_id": "voice-1", "file_size": 10}
        voice.pop("text")
        response = await client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret-placeholder"},
            json={"update_id": 3, "message": voice},
        )
        assert response.status_code == 200
        await asyncio.sleep(0.03)
    assert "/v1/attachments" in devin_paths
    assert any(b"voice.ogg" in body for body in attachment_bodies)
    assert any(
        value.get("chat_id") == 222
        for path, value in telegram_calls
        if path.endswith("/sendMessage")
    )
    await runtime.shutdown()


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.reactions: list[str] = []
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.markup_edits: list[dict[str, object]] = []
        self.drafts: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []
        self.edited_topics: list[tuple[int, int, str]] = []
        self.created_topics: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self.deleted_topics: list[tuple[int, int]] = []
        self.documents: list[dict[str, object]] = []
        self.photos: list[dict[str, object]] = []
        self.topic_error: Exception | None = None
        self.draft_error: Exception | None = None

    async def send_message(
        self,
        chat_id: int,
        text: str,
        **kwargs: object,
    ) -> dict[str, object]:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return {"message_id": len(self.sent)}

    async def send_markdown(
        self, chat_id: int, text: str, **kwargs: object
    ) -> list[dict[str, object]]:
        await self.send_message(chat_id, text, **kwargs)
        return [{**self.sent[-1], "message_id": len(self.sent)}]

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        **kwargs: object,
    ) -> dict[str, object]:
        self.documents.append(
            {
                "chat_id": chat_id,
                "filename": filename,
                "content": content,
                **kwargs,
            }
        )
        return {"message_id": len(self.sent) + len(self.documents)}

    async def send_photo(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        **kwargs: object,
    ) -> dict[str, object]:
        self.photos.append(
            {
                "chat_id": chat_id,
                "filename": filename,
                "content": content,
                **kwargs,
            }
        )
        return {"message_id": len(self.sent) + len(self.photos)}

    async def create_forum_topic(self, chat_id: int, name: str) -> int:
        if self.topic_error is not None:
            raise self.topic_error
        self.created_topics.append((chat_id, name))
        return 19

    async def send_chat_action(
        self, chat_id: int, **kwargs: object
    ) -> None:
        self.actions.append({"chat_id": chat_id, **kwargs})

    async def send_message_draft(
        self, chat_id: int, draft_id: int, text: str = "", **kwargs: object
    ) -> None:
        if self.draft_error is not None:
            raise self.draft_error
        self.drafts.append(
            {"chat_id": chat_id, "draft_id": draft_id, "text": text, **kwargs}
        )

    async def set_message_reaction(
        self, _chat_id: int, _message_id: int, emoji: str
    ) -> None:
        self.reactions.append(emoji)

    async def react(
        self, chat_id: int, message_id: int | None, emoji: str | None
    ) -> bool:
        if message_id is None:
            return False
        try:
            await self.set_message_reaction(chat_id, message_id, emoji)
        except Exception:  # noqa: BLE001 - mirrors client
            return False
        return True

    async def answer_callback_query(
        self, _callback_id: str, text: str | None = None
    ) -> None:
        if text is not None:
            self.answers.append(text)

    async def edit_message_text(
        self, _chat_id: int, _message_id: int, text: str, **_: object
    ) -> None:
        self.edits.append(text)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    async def delete_forum_topic(self, chat_id: int, thread_id: int) -> None:
        self.deleted_topics.append((chat_id, thread_id))

    async def edit_message_reply_markup(
        self, _chat_id: int, _message_id: int, markup: dict[str, object] | None = None
    ) -> None:
        self.markup_edits.append(markup or {"inline_keyboard": []})

    async def edit_forum_topic(
        self, chat_id: int, thread_id: int, name: str
    ) -> None:
        self.edited_topics.append((chat_id, thread_id, name))

    async def get_me(self) -> dict[str, object]:
        return {}

    async def close(self) -> None:
        return None

    async def get_file(self, file_id: str) -> str:
        return f"path/{file_id}"

    async def download_file(self, _file_path: str) -> bytes:
        return b"audio"


class _FakeDevin:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.created_titles: list[str | None] = []
        self.created_playbooks: list[str | None] = []
        self.sent: list[tuple[str, str]] = []
        self.terminated: list[str] = []
        self.playbooks: list[tuple[str, str]] = []

    async def create_session(
        self, prompt: str, title: str | None, playbook_id: str | None = None
    ) -> tuple[str, str]:
        self.created.append(prompt)
        self.created_titles.append(title)
        self.created_playbooks.append(playbook_id)
        return "s1", "https://devin.test/s1"

    async def send_message(self, session_id: str, text: str) -> None:
        self.sent.append((session_id, text))

    async def upload_attachment(
        self,
        filename: str,
        _content: bytes,
        _content_type: str,
    ) -> str:
        return f"https://files.test/{filename}"

    async def get_session(self, _session_id: str) -> SessionState:
        return SessionState("working", "title", None, [])

    async def terminate(self, session_id: str) -> None:
        self.terminated.append(session_id)

    async def list_playbooks(self) -> list[object]:
        return []

    async def download_attachment(self, _url: str) -> tuple[bytes, str] | None:
        return None

    async def fetch_github_pr(
        self,
        _url: str,
        _token: str | None = None,
    ) -> dict[str, object] | None:
        return None

    async def session_consumption(
        self,
        _org_id: str,
        _session_id: str,
        _start: object,
        _end: object,
    ) -> dict[str, object]:
        return {"data": []}

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_concurrent_first_messages_share_one_session(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "lock.sqlite3"))
    class UntitledDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("working", None, None, [])

    devin = UntitledDevin()
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, devin, telegram)  # type: ignore[arg-type]
    await asyncio.gather(
        runtime.handle_user_turn(message("one", message_id=1), "one"),
        runtime.handle_user_turn(message("two", message_id=2), "two"),
    )
    assert len(devin.created) == 1
    assert devin.created_titles == [None]
    assert {text for _, text in devin.sent} == {"two"}
    assert "one" in devin.created[0]
    stored = store.get_conversation("222")
    assert stored is not None
    assert stored.title_pending is True
    await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "new_sessions"),
    [("finished", 1), ("blocked", 1), ("expired", 2)],
)
async def test_follow_up_reuses_idle_session(
    tmp_path: Path, status: str, new_sessions: int
) -> None:
    class StatusDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState(status, "title", None, [])

    devin = StatusDevin()
    runtime = Bridge(settings(tmp_path), Store(":memory:"), devin, _FakeTelegram())  # type: ignore[arg-type]
    await runtime.handle_user_turn(message("first", message_id=1), "first")
    await runtime.handle_user_turn(message("second", message_id=2), "second")
    assert len(devin.created) == new_sessions
    assert [text for _, text in devin.sent] == (["second"] if new_sessions == 1 else [])
    await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 410, 500])
async def test_gone_session_starts_new_one_keeping_queue(
    tmp_path: Path, status_code: int
) -> None:
    class RejectingDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("finished", "title", None, [])

        async def send_message(self, session_id: str, text: str) -> None:
            raise httpx.HTTPStatusError(
                "rejected",
                request=httpx.Request("POST", "https://devin.test"),
                response=httpx.Response(status_code),
            )

    devin = RejectingDevin()
    runtime = Bridge(settings(tmp_path), Store(":memory:"), devin, _FakeTelegram())  # type: ignore[arg-type]
    await runtime.handle_user_turn(message("first", message_id=1), "first")
    runtime.queued_turns["222"] = [(message("third", message_id=3), "third", None)]
    runtime.pending_turns["222"] = [(message("fourth", message_id=4), "fourth", None)]
    if status_code == 500:
        with pytest.raises(httpx.HTTPStatusError):
            await runtime.handle_user_turn(message("second", message_id=2), "second")
        assert len(devin.created) == 1
    else:
        await runtime.handle_user_turn(message("second", message_id=2), "second")
        assert len(devin.created) == 2
        assert "second" in devin.created[1]
    assert runtime.queued_count("222") == 2
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_session_instructions_prefix_new_session_prompt(tmp_path: Path) -> None:
    devin = _FakeDevin()
    runtime = Bridge(
        settings(tmp_path, devin_session_instructions="  Org rules here.  "),
        Store(":memory:"),
        devin,
        _FakeTelegram(),  # type: ignore[arg-type]
    )
    await runtime.handle_user_turn(message("hello", message_id=1), "hello")
    assert devin.created[0].startswith("Org rules here.\n\n")
    assert devin.created[0].endswith("hello")
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_topic_command_creates_and_seeds_topic(tmp_path: Path) -> None:
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), Store(":memory:"), _FakeDevin(), telegram)  # type: ignore[arg-type]
    command_message = {
        **message("/topic New topic"),
        "message_thread_id": 7,
        "is_topic_message": True,
    }
    await handle_command(runtime, command_message, "/topic New topic")
    assert telegram.created_topics == [(222, "New topic")]
    assert telegram.sent[0]["thread_id"] == 19
    assert str(telegram.sent[0]["text"]).startswith("📌 New topic")
    assert telegram.sent[1]["text"].startswith("Created topic New topic")
    assert telegram.sent[1]["thread_id"] == 7
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_topic_command_reports_disabled_topics(tmp_path: Path) -> None:
    telegram = _FakeTelegram()
    telegram.topic_error = RuntimeError("Bad Request: not a forum")
    runtime = Bridge(settings(tmp_path), Store(":memory:"), _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(runtime, message("/topic New topic"), "/topic New topic")
    assert len(telegram.sent) == 1
    assert str(telegram.sent[0]["text"]).startswith("Topics aren't enabled here")
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_dispatch_failure_notifies_and_reacts(tmp_path: Path) -> None:
    class FailingDevin(_FakeDevin):
        async def create_session(
            self, prompt: str, title: str | None, playbook_id: str | None = None
        ) -> tuple[str, str]:
            raise RuntimeError("backend unavailable")

    store = Store(str(tmp_path / "failure.sqlite3"))
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path),
        store,
        FailingDevin(),
        telegram,  # type: ignore[arg-type]
    )
    await runtime.handle_update({"update_id": 1, "message": message("hello")})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert telegram.reactions[-1] == "👎"
    assert telegram.sent[0]["text"].startswith("Couldn't process that message:")
    assert "parse_mode" not in telegram.sent[0]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_last_user_text_is_saved_before_send_failure(tmp_path: Path) -> None:
    class FailingSendDevin(_FakeDevin):
        async def send_message(self, _session_id: str, _text: str) -> None:
            raise RuntimeError("send failed")

    store = Store(str(tmp_path / "retry.sqlite3"))
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    runtime = Bridge(
        settings(tmp_path),
        store,
        FailingSendDevin(),
        _FakeTelegram(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError):
        await runtime.handle_user_turn(message("retry me"), "retry me")
    conversation = store.get_conversation("222")
    assert conversation is not None
    assert conversation.last_user_text == "retry me"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_callback_rejects_wrong_chat_and_stale_session(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "callback.sqlite3"))
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s2",
        session_url="https://devin.test/s2",
        title="new",
    )
    store.add_choice("wrong-chat", "222", "s1", 222, "yes")
    store.add_choice("stale", "222", "s1", 222, "yes")
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    callback = {
        "id": "cb",
        "data": "wrong-chat",
        "from": {"id": 111},
        "message": {"message_id": 4, "chat": {"id": 333, "type": "private"}},
    }
    await runtime.handle_callback(callback)
    assert telegram.answers == ["This choice expired"]
    callback["data"] = "stale"
    callback["message"] = {"message_id": 4, "chat": {"id": 222, "type": "private"}}
    await runtime.handle_callback(callback)
    assert telegram.answers == ["This choice expired", "This choice expired"]
    await runtime.shutdown()


def test_guarded_store_updates_and_choices_are_replaced(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "guards.sqlite3"))
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="one",
    )
    store.add_choice("choice", "222", "s1", 222, "yes")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s2",
        session_url="https://devin.test/s2",
        title="two",
    )
    assert store.get_choice("choice") is None
    store.update_conversation("222", "s1", last_event_id="stale")
    assert store.get_conversation("222").last_event_id is None  # type: ignore[union-attr]
    store.clear_conversation("222", "s1")
    assert store.get_conversation("222") is not None
    store.clear_conversation("222", "s2")
    assert store.get_conversation("222") is None
    store.close()


@pytest.mark.asyncio
async def test_option_only_reply_and_pr_url_are_persisted(tmp_path: Path) -> None:
    class WatchDevin:
        calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            return SessionState(
                "finished",
                "title",
                "https://github.test/pr/1",
                [
                    DevinMessage(
                        "devin_message",
                        "event-1",
                        "OPTIONS: Yes | No",
                        None,
                    )
                ] if self.calls == 1 else [],
            )

    store = Store(str(tmp_path / "watch.sqlite3"))
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    devin = WatchDevin()
    config = settings(tmp_path, devin_poll_seconds=0)
    await SessionWatcher(
        conversation,
        store,
        devin,  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        config,
        trigger_message_id=1,
    ).run()
    assert telegram.sent[0]["text"] == "Choose an option:"
    assert store.get_conversation("222").last_pr_url == "https://github.test/pr/1"  # type: ignore[union-attr]
    refreshed = store.get_conversation("222")
    assert refreshed is not None
    await SessionWatcher(
        refreshed,
        store,
        WatchDevin(),
        telegram,  # type: ignore[arg-type]
        config,
    ).run()
    assert sum(
        item["text"] == "PR: https://github.test/pr/1" for item in telegram.sent
    ) == 1


@pytest.mark.asyncio
async def test_unsupported_content_does_not_react_or_create_session(
    tmp_path: Path,
) -> None:
    telegram = _FakeTelegram()
    devin = _FakeDevin()
    runtime = Bridge(settings(tmp_path), Store(":memory:"), devin, telegram)  # type: ignore[arg-type]
    sticker = message("", message_id=8)
    sticker.pop("text")
    sticker["sticker"] = {"file_id": "sticker"}
    await runtime.handle_message(sticker)
    assert telegram.sent[0]["text"].startswith("Unsupported message type")
    assert telegram.reactions == []
    assert devin.created == []
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_forum_topic_service_message_is_ignored(tmp_path: Path) -> None:
    telegram = _FakeTelegram()
    devin = _FakeDevin()
    runtime = Bridge(settings(tmp_path), Store(":memory:"), devin, telegram)  # type: ignore[arg-type]
    service = message("", message_id=8)
    service.pop("text")
    service["forum_topic_created"] = {"name": "New topic"}
    await runtime.handle_message(service)
    assert telegram.sent == []
    assert telegram.reactions == []
    assert devin.created == []
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_download_file_enforces_streamed_size() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/too-large"):
            return httpx.Response(
                200,
                headers={"content-length": str(20 * 1024 * 1024 + 1)},
                content=b"x",
            )
        return httpx.Response(200, content=b"x" * (20 * 1024 * 1024 + 1))

    telegram = TelegramClient(
        "fake-token",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError):
        await telegram.download_file("too-large")
    with pytest.raises(ValueError):
        await telegram.download_file("streamed")
    await telegram.close()


@pytest.mark.asyncio
async def test_group_reply_requires_this_bot(tmp_path: Path) -> None:
    runtime = Bridge(settings(tmp_path), Store(":memory:"), _FakeDevin(), _FakeTelegram())  # type: ignore[arg-type]
    own = {
        **message("reply"),
        "chat": {"id": -222, "type": "group"},
        "reply_to_message": {"from": {"is_bot": True, "username": "TestBot"}},
    }
    other = {
        **own,
        "reply_to_message": {"from": {"is_bot": True, "username": "OtherBot"}},
    }
    assert should_respond_in_group(own, "testbot", frozenset())
    assert not should_respond_in_group(other, "testbot", frozenset())
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_commands_handle_unknown_status_and_pr_url(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "commands.sqlite3"))
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    store.add_history(
        conv_key="222",
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]

    async def failed_status(_: str) -> str:
        raise RuntimeError("status unavailable")

    async def state(_: str) -> SessionState:
        return SessionState("working", "title", "https://github.test/pr/2", [])

    runtime.get_session_status = failed_status  # type: ignore[method-assign]
    runtime.get_state = state  # type: ignore[method-assign]
    command_message = message("/sessions")
    await handle_command(runtime, command_message, "/sessions")
    assert "unknown" in str(telegram.sent[-1]["text"])
    await handle_command(runtime, command_message, "/status")
    assert "PR: https://github.test/pr/2" in str(telegram.sent[-1]["text"])
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_replacing_conversation_cancels_old_watcher(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "replace.sqlite3"))
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="old",
        session_url="https://devin.test/old",
        title="old",
    )
    runtime = Bridge(
        settings(tmp_path),
        store,
        _FakeDevin(),
        _FakeTelegram(),  # type: ignore[arg-type]
    )
    old_task = asyncio.create_task(asyncio.sleep(60))
    runtime.watchers["old"] = old_task
    await runtime.replace_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="new",
        session_url="https://devin.test/new",
        title="new",
    )
    assert old_task.cancelled()
    assert "old" not in runtime.watchers
    await runtime.shutdown()


def test_normalize_rich_linebreaks_preserves_fences_and_tables() -> None:
    text = "one\ntwo\n\n```python\nx\ny\n```\n| a | b |\n| c | d |\nlast"
    assert normalize_rich_linebreaks(text) == (
        "one  \ntwo\n\n```python\nx\ny\n```\n| a | b |\n| c | d |\nlast"
    )


@pytest.mark.asyncio
async def test_rich_message_primary_path_uses_raw_markdown() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = TelegramClient(
        "token-placeholder",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(handler),
    )
    await client.send_markdown(222, "**raw**\nnext")
    assert calls[0][0].endswith("/sendRichMessage")
    assert calls[0][1]["rich_message"] == {"markdown": "**raw**  \nnext"}
    assert "parse_mode" not in calls[0][1]


@pytest.mark.asyncio
async def test_rich_message_400_falls_back_and_unknown_method_latches() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/sendRichMessage"):
            return httpx.Response(400, json={"ok": False, "description": "bad markdown"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = TelegramClient(
        "token-placeholder",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(handler),
    )
    await client.send_markdown(222, "**raw**")
    assert paths == ["/botfake/sendRichMessage", "/botfake/sendMessage"]

    async def unknown_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"ok": False, "description": "Method not found"},
        )

    unknown = TelegramClient(
        "token-placeholder",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(unknown_handler),
    )
    with pytest.raises(RuntimeError):
        await unknown.send_markdown(222, "raw")
    assert unknown.rich_enabled is False


@pytest.mark.asyncio
async def test_private_watcher_draft_falls_back_to_typing(tmp_path: Path) -> None:
    class DraftDevin:
        calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            return SessionState("working" if self.calls == 1 else "finished", "title", None, [])

    store = Store(str(tmp_path / "drafts.sqlite3"))
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    telegram.draft_error = RuntimeError("draft unavailable")
    await SessionWatcher(
        conversation,
        store,
        DraftDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path, telegram_drafts=True),
    ).run()
    assert telegram.drafts == []
    assert telegram.actions


@pytest.mark.asyncio
async def test_implicit_topic_is_renamed_but_explicit_topic_is_not(
    tmp_path: Path,
) -> None:
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), Store(":memory:"), _FakeDevin(), telegram)  # type: ignore[arg-type]
    service = {
        **message(""),
        "message_thread_id": 7,
        "is_topic_message": True,
        "forum_topic_created": {"is_name_implicit": True},
    }
    service.pop("text")
    await runtime.handle_message(service)
    await runtime.handle_message(
        {
            **message("First line\nsecond", message_id=9),
            "message_thread_id": 7,
            "is_topic_message": True,
        }
    )
    assert telegram.edited_topics == [(222, 7, "First line")]
    stored = runtime.store.get_conversation("222:7")
    assert stored is not None
    assert stored.title_pending is True
    await runtime.shutdown()

    explicit_telegram = _FakeTelegram()
    explicit = Bridge(
        settings(tmp_path),
        Store(":memory:"),
        _FakeDevin(),
        explicit_telegram,
    )  # type: ignore[arg-type]
    await explicit.handle_message(
        {
            **message("First line", message_id=10),
            "message_thread_id": 8,
            "is_topic_message": True,
        }
    )
    assert explicit_telegram.edited_topics == []
    await explicit.shutdown()


@pytest.mark.asyncio
async def test_watcher_renames_topic_to_session_title(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None
    telegram = _FakeTelegram()

    class TitledDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("finished", "A useful session title", None, [])

    await SessionWatcher(
        conversation,
        store,
        TitledDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
    ).run()
    assert telegram.edited_topics == [(222, 7, "A useful session title")]
    assert store.get_conversation("222:7").title == "A useful session title"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_watcher_keeps_manual_rename_during_auto_title(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None

    class ManualTelegram(_FakeTelegram):
        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            self.edited_topics.append((chat_id, thread_id, name))
            store.update_conversation(
                "222:7",
                "s1",
                title="Manual",
                title_pending=False,
            )

    class TitledDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("finished", "A useful session title", None, [])

    telegram = ManualTelegram()
    await SessionWatcher(
        conversation,
        store,
        TitledDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
    ).run()
    stored = store.get_conversation("222:7")
    assert stored is not None
    assert stored.title == "Manual"
    assert stored.title_pending is False
    assert telegram.edited_topics[-1] == (222, 7, "Manual")


@pytest.mark.asyncio
async def test_watcher_retries_manual_title_after_failed_reconcile(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None

    class FlakyManualTelegram(_FakeTelegram):
        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            self.edited_topics.append((chat_id, thread_id, name))
            if name == "A useful session title":
                store.update_conversation(
                    "222:7", "s1", title="Manual", title_pending=False
                )
            elif self.edited_topics.count((chat_id, thread_id, name)) <= 3:
                raise httpx.ReadTimeout("temporary failure")

    class TitledDevin(_FakeDevin):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            return SessionState(
                "working" if self.calls == 1 else "finished",
                "A useful session title",
                None,
                [],
            )

    async def no_sleep(_: float) -> None:
        return None

    telegram = FlakyManualTelegram()
    await SessionWatcher(
        conversation,
        store,
        TitledDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
        sleep=no_sleep,
    ).run()
    names = [name for _, _, name in telegram.edited_topics]
    assert names == ["A useful session title"] + ["Manual"] * 4
    stored = store.get_conversation("222:7")
    assert stored is not None
    assert stored.title == "Manual"


@pytest.mark.asyncio
async def test_watcher_retries_topic_rename_on_terminal_poll(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None

    class FailingTelegram(_FakeTelegram):
        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            self.edited_topics.append((chat_id, thread_id, name))
            if len(self.edited_topics) <= 3:
                raise httpx.ReadTimeout("temporary failure")

    class FinishedDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("finished", "A useful session title", None, [])

    async def no_sleep(_: float) -> None:
        return None

    telegram = FailingTelegram()
    await SessionWatcher(
        conversation,
        store,
        FinishedDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
        sleep=no_sleep,
    ).run()
    assert len(telegram.edited_topics) == 4
    stored = store.get_conversation("222:7")
    assert stored is not None
    assert stored.title == "A useful session title"
    assert stored.title_pending is False


@pytest.mark.asyncio
async def test_resume_keeps_title_pending(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "resume.sqlite3"))
    store.add_history(
        conv_key="222",
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    assert store.list_history("222")[0].title_pending is True
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), _FakeTelegram())  # type: ignore[arg-type]

    async def state(_: str) -> SessionState:
        return SessionState("finished", None, None, [])

    runtime.get_state = state  # type: ignore[method-assign]
    await handle_command(runtime, message("/resume 1"), "/resume 1")
    stored = store.get_conversation("222")
    assert stored is not None
    assert stored.session_id == "s1"
    assert stored.title_pending is True
    store.update_history_title("222", "s1", "Generated")
    assert store.list_history("222")[0].title_pending is False
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_resume_adopts_generated_title(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "resume.sqlite3"))
    store.add_history(
        conv_key="222:9",
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]

    async def state(_: str) -> SessionState:
        return SessionState("finished", "Generated", None, [])

    runtime.get_state = state  # type: ignore[method-assign]
    topic_message = {
        **message("/resume 1"),
        "message_thread_id": 9,
        "is_topic_message": True,
    }
    await handle_command(runtime, topic_message, "/resume 1")
    assert telegram.edited_topics == [(222, 9, "Generated")]
    stored = store.get_conversation("222:9")
    assert stored is not None
    assert stored.title == "Generated"
    assert stored.title_pending is False
    entry = store.list_history("222:9")[0]
    assert entry.title == "Generated"
    assert entry.title_pending is False
    assert telegram.sent[-1]["text"] == "Resumed: Generated https://devin.test/s1"

    store.add_history(
        conv_key="222:9",
        session_id="s2",
        session_url="https://devin.test/s2",
        title="Manual title",
        title_pending=False,
    )
    await handle_command(runtime, topic_message, "/resume 1")
    assert telegram.edited_topics == [(222, 9, "Generated")]
    stored = store.get_conversation("222:9")
    assert stored is not None
    assert stored.title == "Manual title"
    assert stored.title_pending is False
    assert telegram.sent[-1]["text"] == "Resumed: Manual title https://devin.test/s2"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_watcher_preserves_manual_topic_title(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: Manual topic title",
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None
    telegram = _FakeTelegram()

    class TitledDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("finished", "A useful session title", None, [])

    await SessionWatcher(
        conversation,
        store,
        TitledDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
    ).run()
    assert telegram.edited_topics == []
    assert (
        store.get_conversation("222:7").title
        == "Telegram: Manual topic title"
    )  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_watcher_persists_session_title_without_topic(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()

    class TitledDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("finished", "A useful session title", None, [])

    await SessionWatcher(
        conversation,
        store,
        TitledDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
    ).run()
    assert telegram.edited_topics == []
    assert store.get_conversation("222").title == "A useful session title"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_history_title_pending_survives_rename_and_resume(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    topic_message = {
        **message("hello", message_id=1),
        "message_thread_id": 9,
        "is_topic_message": True,
    }
    await runtime.handle_message(topic_message)
    conv_key = "222:9"
    assert store.list_history(conv_key)[0].title_pending is True
    await handle_command(
        runtime,
        {
            **message("/rename New name", message_id=2),
            "message_thread_id": 9,
            "is_topic_message": True,
        },
        "/rename New name",
    )
    assert store.list_history(conv_key)[0].title_pending is False
    store.add_history(
        conv_key=conv_key,
        session_id="s2",
        session_url="https://devin.test/s2",
        title="pending session",
        title_pending=True,
    )
    await handle_command(
        runtime,
        {
            **message("/resume 1", message_id=3),
            "message_thread_id": 9,
            "is_topic_message": True,
        },
        "/resume 1",
    )
    stored = store.get_conversation(conv_key)
    assert stored is not None
    assert stored.session_id == "s2"
    assert stored.title == "title"
    assert stored.title_pending is False
    entry = store.list_history(conv_key)[0]
    assert entry.title == "title"
    assert entry.title_pending is False
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_watcher_retries_title_past_finished_status(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None

    class FlakyTelegram(_FakeTelegram):
        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            self.edited_topics.append((chat_id, thread_id, name))
            if len(self.edited_topics) <= 3:
                raise RuntimeError("temporary failure")

    class FinishedDevin(_FakeDevin):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            return SessionState("finished", "A useful session title", None, [])

    async def no_sleep(_: float) -> None:
        return None

    telegram = FlakyTelegram()
    devin = FinishedDevin()
    await SessionWatcher(
        conversation,
        store,
        devin,  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
        sleep=no_sleep,
        trigger_message_id=7,
    ).run()
    assert telegram.edited_topics[-1] == (222, 7, "A useful session title")
    stored = store.get_conversation("222:7")
    assert stored is not None
    assert stored.title == "A useful session title"
    assert stored.title_pending is False
    assert devin.calls == 2
    assert telegram.reactions == ["👍"]


@pytest.mark.asyncio
async def test_watcher_exits_after_bounded_title_retries(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None

    class FailingTelegram(_FakeTelegram):
        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            self.edited_topics.append((chat_id, thread_id, name))
            raise httpx.ReadTimeout("t")

    class FinishedDevin(_FakeDevin):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            return SessionState("finished", "A useful session title", None, [])

    async def no_sleep(_: float) -> None:
        return None

    telegram = FailingTelegram()
    devin = FinishedDevin()
    await SessionWatcher(
        conversation,
        store,
        devin,  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
        sleep=no_sleep,
        trigger_message_id=7,
    ).run()
    stored = store.get_conversation("222:7")
    assert stored is not None
    assert stored.title_pending is True
    assert devin.calls <= 4
    assert telegram.reactions == ["👍"]


@pytest.mark.asyncio
async def test_watcher_terminal_cleanup_after_title_retry_deadline(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None

    class FailingTelegram(_FakeTelegram):
        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            self.edited_topics.append((chat_id, thread_id, name))
            raise httpx.ReadTimeout("t")

    class FinishedDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("finished", "A useful session title", None, [])

    now = 0.0

    def clock() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += max(seconds, 1.0)

    telegram = FailingTelegram()
    await SessionWatcher(
        conversation,
        store,
        FinishedDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path, devin_watch_timeout_seconds=2),
        clock=clock,
        sleep=sleep,
        trigger_message_id=7,
    ).run()
    assert telegram.reactions == ["👍"]
    assert not any(
        "still working" in str(item["text"]) for item in telegram.sent
    )


@pytest.mark.asyncio
async def test_watcher_retries_topic_rename_after_failure(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    store.add_history(
        conv_key="222:7",
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None

    class FailingTelegram(_FakeTelegram):
        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            self.edited_topics.append((chat_id, thread_id, name))
            if len(self.edited_topics) == 1:
                raise RuntimeError("temporary failure")

    class RetryingDevin(_FakeDevin):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            return SessionState(
                "working" if self.calls == 1 else "finished",
                "A useful session title",
                None,
                [],
            )

    async def no_sleep(_: float) -> None:
        return None

    telegram = FailingTelegram()
    await SessionWatcher(
        conversation,
        store,
        RetryingDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
        sleep=no_sleep,
    ).run()
    stored = store.get_conversation("222:7")
    assert stored is not None
    assert telegram.edited_topics == [
        (222, 7, "A useful session title"),
        (222, 7, "A useful session title"),
    ]
    assert stored.title == "A useful session title"
    assert stored.title_pending is False
    assert store.list_history("222:7")[0].title == "A useful session title"


@pytest.mark.asyncio
async def test_watcher_tolerates_topic_rename_transport_errors(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:7",
        chat_id=222,
        thread_id=7,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    conversation = store.get_conversation("222:7")
    assert conversation is not None

    class TransportErrorTelegram(_FakeTelegram):
        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            self.edited_topics.append((chat_id, thread_id, name))
            raise httpx.ReadTimeout("t")

    class ReplyDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState(
                "finished",
                "A useful session title",
                None,
                [DevinMessage("devin_message", "e1", "reply", None)],
            )

    async def no_sleep(_: float) -> None:
        return None

    telegram = TransportErrorTelegram()
    await SessionWatcher(
        conversation,
        store,
        ReplyDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
        sleep=no_sleep,
    ).run()
    stored = store.get_conversation("222:7")
    assert stored is not None
    assert stored.title_pending is True
    assert len(telegram.edited_topics) == 12
    assert [item["text"] for item in telegram.sent].count("reply") == 1


@pytest.mark.asyncio
async def test_option_callback_rebuilds_disabled_markup(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    store.add_choice("yes-id", "222", "s1", 222, "Yes", 4)
    store.add_choice("no-id", "222", "s1", 222, "No", 4)
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    await runtime.handle_callback(
        {
            "id": "callback",
            "data": "yes-id",
            "from": {"id": 111},
            "message": {
                "message_id": 4,
                "chat": {"id": 222, "type": "private"},
            },
        }
    )
    buttons = telegram.markup_edits[-1]["inline_keyboard"]
    assert buttons == [
        [{"text": "✅ Yes", "disabled": {}}],
        [{"text": "No", "disabled": {}}],
    ]
    assert telegram.edits == []
    await runtime.shutdown()


def test_pending_choices_migrate_message_id_column(tmp_path: Path) -> None:
    database_path = tmp_path / "pending.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE pending_choices (
            choice_id TEXT PRIMARY KEY,
            conv_key TEXT NOT NULL,
            session_id TEXT NOT NULL,
            option_text TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        INSERT INTO pending_choices(
            choice_id, conv_key, session_id, option_text, created_at
        ) VALUES ('legacy', '222', 's1', 'Legacy', 1);
        """
    )
    connection.commit()
    connection.close()

    store = Store(str(database_path))
    columns = {
        str(row["name"])
        for row in store.connection.execute("PRAGMA table_info(pending_choices)")
    }
    assert "message_id" in columns
    assert store.list_choice_messages("222") == []
    store.add_choice("new", "222", "s1", 222, "New", 17)
    assert store.list_choices("222", 17) == [("new", "New")]
    assert store.list_choice_messages("222") == [17]
    store.close()


@pytest.mark.asyncio
async def test_option_callback_scopes_keyboard_to_tapped_message(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    store.add_choice("old-a", "222", "s1", 222, "Old A", 11)
    store.add_choice("old-b", "222", "s1", 222, "Old B", 11)
    store.add_choice("new-a", "222", "s1", 222, "New A", 22)
    store.add_choice("new-b", "222", "s1", 222, "New B", 22)
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    await runtime.handle_callback(
        {
            "id": "callback",
            "data": "new-b",
            "from": {"id": 111},
            "message": {
                "message_id": 22,
                "chat": {"id": 222, "type": "private"},
            },
        }
    )
    assert telegram.markup_edits[-1] == {
        "inline_keyboard": [
            [{"text": "New A", "disabled": {}}],
            [{"text": "✅ New B", "disabled": {}}],
        ]
    }
    assert telegram.markup_edits[-1]["inline_keyboard"]  # type: ignore[index]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_second_options_message_clears_first_keyboard(tmp_path: Path) -> None:
    class OptionsDevin:
        calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            messages = [
                DevinMessage(
                    "devin_message",
                    "event-1",
                    "First?\nOPTIONS: First A | First B",
                    None,
                )
            ]
            if self.calls >= 2:
                messages.append(
                    DevinMessage(
                        "devin_message",
                        "event-2",
                        "Second?\nOPTIONS: Second A | Second B",
                        None,
                    )
                )
            return SessionState(
                "working" if self.calls == 1 else "finished",
                "title",
                None,
                messages,
            )

    store = Store(str(tmp_path / "watch-options.sqlite3"))
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    await SessionWatcher(
        conversation,
        store,
        OptionsDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path, devin_poll_seconds=0),
    ).run()
    assert {"inline_keyboard": []} in telegram.markup_edits
    assert store.list_choice_messages("222") == [2]


@pytest.mark.asyncio
async def test_stop_keyboard_uses_bot_api_button_styles(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(runtime, message("/stop"), "/stop")
    markup = telegram.sent[-1]["reply_markup"]
    assert markup["inline_keyboard"][0][0]["style"] == "danger"  # type: ignore[index]
    assert markup["inline_keyboard"][0][1]["style"] == "primary"  # type: ignore[index]
    assert len(store.list_choices("222", 1)) == 2
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_whoami_group_send_includes_ephemeral_receiver(tmp_path: Path) -> None:
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), Store(":memory:"), _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(
        runtime,
        {**message("/whoami"), "chat": {"id": -222, "type": "supergroup"}},
        "/whoami",
    )
    assert telegram.sent[-1]["receiver_user_id"] == 111
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_whoami_reports_approved_user_allowed(tmp_path: Path) -> None:
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path, telegram_allowed_users="111"),
        Store(":memory:"),
        _FakeDevin(),
        telegram,
    )  # type: ignore[arg-type]
    runtime.approved_users.add(222)
    await handle_command(runtime, message("/whoami", user_id=222), "/whoami")
    assert "Allowed: yes" in str(telegram.sent[-1]["text"])
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_flush_pending_indexes_every_fragment(tmp_path: Path) -> None:
    store = Store(":memory:")
    runtime = Bridge(
        settings(tmp_path, telegram_debounce_seconds=10),
        store,
        _FakeDevin(),
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    await runtime._queue_turn(message("one", message_id=1), "one", None)
    await runtime._queue_turn(message("two", message_id=2), "two", None)
    await runtime._flush_pending("222")
    assert store.conv_key_for_message(222, 1) == "222"
    assert store.conv_key_for_message(222, 2) == "222"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_devin_send_message_accepts_non_object_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/message"):
            return httpx.Response(200, content=b"null")
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={})

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    await devin.send_message("devin-1", "hello")
    await devin.terminate("devin-1")
    await devin.close()


@pytest.mark.asyncio
async def test_download_attachment_rejects_non_https_redirect() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((
            str(request.url),
            request.headers.get("authorization", ""),
        ))
        return httpx.Response(
            302,
            headers={"Location": "http://bucket.s3.amazonaws.com/x?sig=1"},
        )

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    assert (
        await devin.download_attachment(
            "https://app.devin.ai/attachments/1/file.txt"
        )
        is None
    )
    assert requests == [
        (
            "https://devin.test/v1/attachments/1/file.txt",
            "Bearer fake-key",
        )
    ]
    await devin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://app.devin.ai/attachments/../sessions",
        "https://app.devin.ai/attachments/%2e%2e/sessions",
        "https://app.devin.ai/attachments/%252e%252e/sessions",
        "https://app.devin.ai/attachments/1/..",
        "https://app.devin.ai/attachments/./sessions",
        "https://app.devin.ai/attachments/1%2f..%2f..%2fsessions/x",
    ],
)
async def test_download_attachment_rejects_dot_segments(url: str) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=b"{}")

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    assert await devin.download_attachment(url) is None
    assert requests == []
    await devin.close()


@pytest.mark.asyncio
async def test_download_attachment_stream_limits_body() -> None:
    limit = 20 * 1024 * 1024

    class Chunks(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.reads = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.reads += 1
            yield b"x" * (limit + 1)
            self.reads += 1
            yield b"should not be read"

        async def aclose(self) -> None:
            return None

    chunks = Chunks()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://devin.test/v1/attachments/1/file.txt"
        assert request.headers["authorization"] == "Bearer fake-key"
        return httpx.Response(200, stream=chunks)

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    assert (
        await devin.download_attachment(
            "https://app.devin.ai/attachments/1/file.txt"
        )
        is None
    )
    assert chunks.reads == 1
    await devin.close()


@pytest.mark.asyncio
async def test_download_attachment_redirect_uses_public_client(monkeypatch) -> None:
    import app.clients as clients_mod

    async def public_host(_: str) -> bool:
        return True

    monkeypatch.setattr(clients_mod, "_is_public_host", public_host)
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((
            str(request.url),
            request.headers.get("authorization", ""),
        ))
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={"Location": "https://bucket.s3.amazonaws.com/x?sig=1"},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"ok",
        )

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    assert await devin.download_attachment(
        "https://app.devin.ai/attachments/1/file.txt"
    ) == (b"ok", "text/plain")
    assert requests == [
        (
            "https://devin.test/v1/attachments/1/file.txt",
            "Bearer fake-key",
        ),
        (
            "https://bucket.s3.amazonaws.com/x?sig=1",
            "",
        ),
    ]
    await devin.close()


@pytest.mark.asyncio
async def test_download_attachment_rejects_non_public_redirect(monkeypatch) -> None:
    import app.clients as clients_mod

    async def private_host(_: str) -> bool:
        return False

    monkeypatch.setattr(clients_mod, "_is_public_host", private_host)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://169.254.169.254/x"},
        )

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    assert await devin.download_attachment(
        "https://app.devin.ai/attachments/1/file.txt"
    ) is None
    assert requests == ["https://devin.test/v1/attachments/1/file.txt"]
    await devin.close()


@pytest.mark.asyncio
async def test_is_public_host_rejects_loopback() -> None:
    from app.clients import _is_public_host

    assert not await _is_public_host("127.0.0.1")


@pytest.mark.asyncio
async def test_download_attachment_rejects_invalid_path() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"unexpected")

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    assert await devin.download_attachment(
        "https://app.devin.ai/sessions/x"
    ) is None
    assert not called
    await devin.close()


@pytest.mark.asyncio
async def test_github_pr_fetch_rejects_invalid_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    assert await devin.fetch_github_pr(
        "https://github.com/org/repo/pull/1"
    ) is None
    await devin.close()


@pytest.mark.asyncio
async def test_github_pr_fetch_does_not_send_devin_token() -> None:
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"number": 1, "title": "Example"})

    devin = DevinClient(
        "devin-secret",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    result = await devin.fetch_github_pr("https://github.com/org/repo/pull/1")
    assert result == {"number": 1, "title": "Example"}
    assert observed["authorization"] == ""
    await devin.close()


@pytest.mark.asyncio
async def test_github_pr_fetch_rejects_non_github_url() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"number": 1})

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    assert await devin.fetch_github_pr("https://example.com/org/repo/pull/1") is None
    assert calls == 0
    await devin.close()


@pytest.mark.asyncio
async def test_devin_session_consumption_uses_unix_time_params() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 2, tzinfo=timezone.utc)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params.multi_items())
        return httpx.Response(200, json={"total_acus": 0, "consumption_by_date": []})

    devin = DevinClient(
        "fake-key",
        "https://devin.test",
        3,
        transport=httpx.MockTransport(handler),
    )
    await devin.session_consumption("org-1", "session-1", start, end)
    assert seen["time_after"] == str(int(start.timestamp()))
    assert seen["time_before"] == str(int(end.timestamp()))
    assert "start_time" not in seen
    assert "end_time" not in seen
    await devin.close()


@pytest.mark.asyncio
async def test_watcher_adaptive_polling_resets_after_delivery(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "adaptive.sqlite3"))
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    states = [
        SessionState("working", "title", None, []),
        SessionState("working", "title", None, []),
        SessionState(
            "working",
            "title",
            None,
            [DevinMessage("devin_message", "e1", "hello", None)],
        ),
        SessionState("working", "title", None, [DevinMessage("devin_message", "e1", "hello", None)]),
        SessionState("finished", "title", None, [DevinMessage("devin_message", "e1", "hello", None)]),
    ]

    class AdaptiveDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return states.pop(0)

    sleeps: list[float] = []
    now = 0.0

    def clock() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += max(seconds, 1)

    await SessionWatcher(
        conversation,
        store,
        AdaptiveDevin(),  # type: ignore[arg-type]
        _FakeTelegram(),  # type: ignore[arg-type]
        settings(
            tmp_path,
            devin_poll_fast_seconds=1,
            devin_poll_seconds=3,
            devin_watch_timeout_seconds=20,
        ),
        clock=clock,
        sleep=sleep,
    ).run()
    assert sleeps[:3] == [1, 1.5, 1]


@pytest.mark.asyncio
async def test_watcher_status_callback_runs_after_delivery_and_persist(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    events: list[object] = []

    async def on_status_change(status: str) -> None:
        current = store.get_conversation("222")
        assert current is not None
        events.append(("status", status, current.last_event_id))

    class FinishedDevin(_FakeDevin):
        calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            if self.calls == 1:
                return SessionState("working", "title", None, [])
            return SessionState(
                "finished",
                "title",
                None,
                [DevinMessage("devin_message", "e1", "done", None)],
            )

    watcher = SessionWatcher(
        conversation,
        store,
        FinishedDevin(),  # type: ignore[arg-type]
        _FakeTelegram(),  # type: ignore[arg-type]
        settings(tmp_path),
        on_status_change=on_status_change,
    )
    original_deliver = watcher._deliver

    async def deliver(*args: object, **kwargs: object) -> None:
        events.append("deliver")
        await original_deliver(*args, **kwargs)

    watcher._deliver = deliver  # type: ignore[method-assign]
    await watcher.run()
    assert events == ["deliver", ("status", "finished", "e1")]


@pytest.mark.asyncio
async def test_blocked_status_does_not_drain_until_watcher_finishes(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    blocked = asyncio.Event()
    release = asyncio.Event()

    class StatusDevin(_FakeDevin):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            status = (
                "working"
                if self.calls == 1
                else "blocked"
                if self.calls == 2
                else "finished"
                if self.calls == 3
                else "working"
            )
            return SessionState(status, "title", None, [])

    async def sleep(_seconds: float) -> None:
        if not blocked.is_set():
            blocked.set()
            await release.wait()

    devin = StatusDevin()
    runtime = Bridge(
        settings(tmp_path, devin_settle_seconds=100),
        store,
        devin,
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    conversation = store.get_conversation("222")
    assert conversation is not None
    runtime.queued_turns["222"] = [
        (message("queued", message_id=8), "queued", None),
    ]
    drained = asyncio.Event()
    original_handle = runtime.handle_user_turn

    async def handle(
        queued_message: Mapping[str, object],
        text: str,
        *,
        attachment: object = None,
    ) -> None:
        await original_handle(queued_message, text, attachment=attachment)
        drained.set()

    runtime.handle_user_turn = handle  # type: ignore[method-assign]
    await runtime.start_watcher(conversation)
    runtime.active_watchers["s1"].sleep = sleep
    await blocked.wait()
    assert runtime.queued_count("222") == 1
    assert devin.sent == []
    release.set()
    await runtime.watchers["s1"]
    await drained.wait()
    assert runtime.queued_count("222") == 0
    assert ("s1", "queued") in devin.sent
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_watcher_generation_change_uses_new_reply_anchor(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    watcher: SessionWatcher

    class TriggerDuringPollDevin(_FakeDevin):
        calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            if self.calls == 1:
                watcher.set_trigger(22)
                return SessionState(
                    "working",
                    "title",
                    None,
                    [DevinMessage("devin_message", "e1", "old", None)],
                )
            return SessionState(
                "finished",
                "title",
                None,
                [DevinMessage("devin_message", "e1", "old", None)],
            )

    telegram = _FakeTelegram()
    watcher = SessionWatcher(
        conversation,
        store,
        TriggerDuringPollDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
        trigger_message_id=11,
    )
    await watcher.run()
    assert telegram.sent[0]["reply_to_message_id"] == 22
    assert watcher.delivered is True


@pytest.mark.asyncio
async def test_watcher_status_and_cleanup(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    states = [
        SessionState(
            "working",
            "title",
            None,
            [],
            {"phase": "build", "count": 2, "ignored": ["x"]},
        ),
        SessionState(
            "working",
            "title",
            None,
            [],
            {"phase": "build", "count": 3},
        ),
        SessionState(
            "working",
            "title",
            None,
            [],
            {"phase": "build"},
        ),
        SessionState(
            "working",
            "title",
            None,
            [DevinMessage("devin_message", "e1", "done", None)],
            {"phase": "build"},
        ),
    ]

    class StatusDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return states.pop(0) if states else SessionState("finished", "title", None, [])

    telegram = _FakeTelegram()
    now = 0.0

    def clock() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += max(seconds, 10)

    await SessionWatcher(
        conversation,
        store,
        StatusDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(
            tmp_path,
            devin_status_after_seconds=8,
            devin_poll_fast_seconds=1,
            devin_poll_seconds=1,
            devin_watch_timeout_seconds=40,
        ),
        transient_message_ids=[99],
        clock=clock,
        sleep=sleep,
    ).run()
    assert any("Working" in str(item["text"]) for item in telegram.sent)
    assert any("phase: build" in text for text in telegram.edits)
    assert (222, 99) in telegram.deleted
    assert any(message_id != 99 for _, message_id in telegram.deleted)


@pytest.mark.asyncio
async def test_cancelled_watcher_cleans_transient_status_messages(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    release_sleep = asyncio.Event()

    class WorkingDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("working", "title", None, [])

    async def sleep(_seconds: float) -> None:
        await release_sleep.wait()

    task = asyncio.create_task(
        SessionWatcher(
            conversation,
            store,
            WorkingDevin(),  # type: ignore[arg-type]
            telegram,  # type: ignore[arg-type]
            settings(
                tmp_path,
                devin_status_after_seconds=0,
                devin_poll_seconds=5,
            ),
            transient_message_ids=[99],
            sleep=sleep,
        ).run()
    )
    for _ in range(10):
        await asyncio.sleep(0)
        if telegram.sent:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (222, 99) in telegram.deleted
    assert any(message_id != 99 for _, message_id in telegram.deleted)


@pytest.mark.asyncio
async def test_watcher_private_draft_uses_status_text(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    now = 0.0

    class DraftDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return (
                SessionState("working", "title", None, [])
                    if now <= 10
                else SessionState("finished", "title", None, [])
            )

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += max(seconds, 10)

    await SessionWatcher(
        conversation,
        store,
        DraftDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(
            tmp_path,
            telegram_drafts=True,
            devin_status_after_seconds=8,
            devin_poll_fast_seconds=1,
            devin_poll_seconds=1,
            devin_watch_timeout_seconds=40,
        ),
        clock=lambda: now,
        sleep=sleep,
    ).run()
    assert any("Working" in str(item["text"]) for item in telegram.drafts)


@pytest.mark.asyncio
async def test_reaction_retry_stop_and_edited_message(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:9",
        chat_id=222,
        thread_id=9,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_user_text="old",
        last_user_message_id=12,
    )
    telegram = _FakeTelegram()
    devin = _FakeDevin()
    runtime = Bridge(settings(tmp_path), store, devin, telegram)  # type: ignore[arg-type]
    await runtime.handle_reaction(
        {
            "user": {"id": 111},
            "chat": {"id": 222, "type": "private"},
            "message_id": 8,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "🔁"}],
        }
    )
    assert devin.sent == [("s1", "old")]
    await runtime.handle_reaction(
        {
            "user": {"id": 111},
            "chat": {"id": 222, "type": "private"},
            "message_id": 8,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "🛑"}],
        }
    )
    assert devin.terminated == ["s1"]
    stopped = [item for item in telegram.sent if item["text"] == "Stopped session."]
    assert stopped
    assert stopped[-1]["thread_id"] == 9


@pytest.mark.asyncio
async def test_retry_send_failure_restarts_watcher(tmp_path: Path) -> None:
    class FailingDevin(_FakeDevin):
        async def send_message(self, _session_id: str, _text: str) -> None:
            raise RuntimeError("send failed")

    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_user_text="retry me",
    )
    runtime = Bridge(
        settings(tmp_path),
        store,
        FailingDevin(),
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    conversation = store.get_conversation("222")
    assert conversation is not None
    with pytest.raises(RuntimeError, match="send failed"):
        await runtime.retry_conversation(conversation, trigger_message_id=8)
    assert "s1" in runtime.watchers
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_resumes_watchers_for_recent_conversations(
    tmp_path: Path,
) -> None:
    class BlockedDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState(
                "blocked",
                "title",
                None,
                [
                    DevinMessage("devin_message", "event-0", "Old", None),
                    DevinMessage("devin_message", "event-1", "Done", None),
                ],
            )

    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_event_id="event-0",
    )
    store.save_conversation(
        conv_key="333",
        chat_id=333,
        thread_id=None,
        session_id="s2",
        session_url="https://devin.test/s2",
        title="title",
        created_at=time.time() - 100,
    )
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path),
        store,
        BlockedDevin(),
        telegram,
    )  # type: ignore[arg-type]
    await runtime.startup()
    assert "s1" in runtime.watchers
    assert "s2" not in runtime.watchers
    await asyncio.gather(*runtime.watchers.values(), return_exceptions=True)
    delivered = [str(item["text"]) for item in telegram.sent]
    assert sum("Done" in text for text in delivered) == 1
    assert not any("Old" in text for text in delivered)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_resumes_watcher_without_cursor_delivers_downtime_reply(
    tmp_path: Path,
) -> None:
    downtime_reply_at = datetime.fromtimestamp(
        time.time() - 30, timezone.utc
    ).isoformat()

    class BlockedDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState(
                "blocked",
                "title",
                None,
                [
                    DevinMessage(
                        "devin_message", "event-1", "Done", downtime_reply_at
                    )
                ],
            )

    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        created_at=time.time() - 60,
    )
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path, devin_watch_timeout_seconds=120),
        store,
        BlockedDevin(),
        telegram,
    )  # type: ignore[arg-type]
    await runtime.startup()
    assert "s1" in runtime.watchers
    await asyncio.gather(*runtime.watchers.values(), return_exceptions=True)
    delivered = [str(item["text"]) for item in telegram.sent]
    assert sum("Done" in text for text in delivered) == 1
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_edited_message_rechecks_target_after_lock(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_user_text="old",
        last_user_message_id=12,
    )
    runtime = Bridge(
        settings(tmp_path),
        store,
        _FakeDevin(),
        _FakeTelegram(),
    )  # type: ignore[arg-type]

    class ReloadLock:
        async def __aenter__(self) -> None:
            store.update_conversation(
                "222",
                "s1",
                last_user_message_id=13,
            )

        async def __aexit__(self, *_: object) -> None:
            return None

    runtime._lock = lambda _conv_key: ReloadLock()  # type: ignore[method-assign]
    await runtime.handle_edited_message({**message("new"), "message_id": 12})
    assert not runtime.devin.sent
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_edited_message_correction_and_noop(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_user_text="old",
        last_user_message_id=12,
    )
    devin = _FakeDevin()
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, devin, telegram)  # type: ignore[arg-type]
    edited = {**message("new"), "message_id": 12}
    await runtime.handle_edited_message(edited)
    assert devin.sent == [("s1", "Correction to my previous message: new")]
    await runtime.handle_edited_message({**edited, "text": "new"})
    assert devin.sent == [("s1", "Correction to my previous message: new")]


@pytest.mark.asyncio
async def test_historical_edit_is_ignored(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_user_text="old",
        last_user_message_id=12,
    )
    devin = _FakeDevin()
    runtime = Bridge(
        settings(tmp_path),
        store,
        devin,
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    await runtime.handle_edited_message({**message("new"), "message_id": 11})
    assert not devin.sent


@pytest.mark.asyncio
async def test_forum_reaction_uses_message_index(tmp_path: Path) -> None:
    store = Store(":memory:")
    for key, thread, session in (("222:9", 9, "s1"), ("222:10", 10, "s2")):
        store.save_conversation(
            conv_key=key,
            chat_id=222,
            thread_id=thread,
            session_id=session,
            session_url=f"https://devin.test/{session}",
            title="title",
            last_user_text=session,
        )
    store.index_message(222, 99, "222:10")
    devin = _FakeDevin()
    runtime = Bridge(
        settings(tmp_path),
        store,
        devin,
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    await runtime.handle_reaction(
        {
            "user": {"id": 111},
            "chat": {"id": 222, "type": "supergroup", "is_forum": True},
            "message_id": 99,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "🔁"}],
        }
    )
    assert devin.sent == [("s2", "s2")]


@pytest.mark.asyncio
async def test_unindexed_forum_reaction_is_ignored(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:9",
        chat_id=222,
        thread_id=9,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_user_text="old",
    )
    devin = _FakeDevin()
    runtime = Bridge(
        settings(tmp_path),
        store,
        devin,
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    await runtime.handle_reaction(
        {
            "user": {"id": 111},
            "chat": {"id": 222, "type": "supergroup", "is_forum": True},
            "message_id": 8,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "🔁"}],
        }
    )
    assert not devin.sent


@pytest.mark.asyncio
async def test_watcher_cleanup_can_delete_later_status_and_clear_draft(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    watcher = SessionWatcher(
        conversation,
        store,
        _FakeDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path, telegram_drafts=True),
    )
    watcher.status_message_id = 10
    watcher.draft_used = True
    await watcher._cleanup_transients()
    watcher.status_message_id = 11
    await watcher._cleanup_transients()
    assert (222, 10) in telegram.deleted
    assert (222, 11) in telegram.deleted
    assert [draft["text"] for draft in telegram.drafts] == [""]


def test_watcher_set_trigger_resets_delivery(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    watcher = SessionWatcher(
        conversation,
        store,
        _FakeDevin(),  # type: ignore[arg-type]
        _FakeTelegram(),  # type: ignore[arg-type]
        settings(tmp_path),
        trigger_message_id=1,
    )
    watcher.delivered = True
    watcher.set_trigger(2)
    assert watcher.trigger_message_id == 2
    assert watcher.delivered is False


@pytest.mark.asyncio
async def test_topic_close_and_rename_commands(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:9",
        chat_id=222,
        thread_id=9,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    store.add_history(
        conv_key="222:9",
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(runtime, message("/close"), "/close")
    assert "This command only works inside a topic." in telegram.sent[-1]["text"]
    topic_message = {
        **message("/rename New name"),
        "message_thread_id": 9,
        "is_topic_message": True,
    }
    await handle_command(runtime, topic_message, "/rename New name")
    assert telegram.edited_topics == [(222, 9, "New name")]
    assert store.get_conversation("222:9").title == "New name"  # type: ignore[union-attr]
    assert store.list_history("222:9")[0].title == "New name"
    assert store.get_conversation("222:9").title_pending is False  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_rename_rolls_back_when_topic_edit_fails(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:9",
        chat_id=222,
        thread_id=9,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )
    store.add_history(
        conv_key="222:9",
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )

    class FailingTelegram(_FakeTelegram):
        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            raise httpx.ReadTimeout("t")

    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), FailingTelegram())  # type: ignore[arg-type]
    topic_message = {
        **message("/rename New name"),
        "message_thread_id": 9,
        "is_topic_message": True,
    }
    with pytest.raises(httpx.ReadTimeout):
        await handle_command(runtime, topic_message, "/rename New name")
    stored = store.get_conversation("222:9")
    assert stored is not None
    assert stored.title == "Telegram: prompt"
    assert stored.title_pending is True
    entry = store.list_history("222:9")[0]
    assert entry.title == "Telegram: prompt"
    assert entry.title_pending is True
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_resume_survives_failed_topic_edit(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "resume.sqlite3"))
    store.add_history(
        conv_key="222:9",
        session_id="s1",
        session_url="https://devin.test/s1",
        title="Telegram: prompt",
        title_pending=True,
    )

    class FailingTelegram(_FakeTelegram):
        def __init__(self) -> None:
            super().__init__()
            self.edits: list[str] = []

        async def edit_forum_topic(
            self, chat_id: int, thread_id: int, name: str
        ) -> None:
            self.edits.append(name)
            if len(self.edits) == 1:
                raise httpx.ReadTimeout("t")

    class FinishedDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState("finished", "Generated", "https://gh.test/pr/1", [])

    telegram = FailingTelegram()
    runtime = Bridge(settings(tmp_path), store, FinishedDevin(), telegram)  # type: ignore[arg-type]
    topic_message = {
        **message("/resume 1"),
        "message_thread_id": 9,
        "is_topic_message": True,
    }
    await handle_command(runtime, topic_message, "/resume 1")
    stored = store.get_conversation("222:9")
    assert stored is not None
    assert stored.session_id == "s1"
    assert stored.title == "Telegram: prompt"
    assert stored.title_pending is True
    assert store.list_history("222:9")[0].title_pending is True
    assert telegram.sent[-1]["text"] == "Resumed: Telegram: prompt https://devin.test/s1"
    await runtime.watchers["s1"]
    assert telegram.edits == ["Generated", "Generated"]
    stored = store.get_conversation("222:9")
    assert stored is not None
    assert stored.title == "Generated"
    assert stored.title_pending is False
    assert stored.last_pr_url == "https://gh.test/pr/1"
    assert not any("PR:" in sent["text"] for sent in telegram.sent)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_close_clears_pending_turn_without_creating_session(
    tmp_path: Path,
) -> None:
    devin = _FakeDevin()
    runtime = Bridge(
        settings(tmp_path, telegram_debounce_seconds=10),
        Store(":memory:"),
        devin,
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    topic_message = {
        **message("hello", message_id=1),
        "message_thread_id": 9,
        "is_topic_message": True,
    }
    await runtime.handle_message(topic_message)
    assert runtime.queued_count("222:9") == 1
    await runtime.handle_message({
        **message("/close", message_id=2),
        "message_thread_id": 9,
        "is_topic_message": True,
    })
    assert runtime.queued_count("222:9") == 0
    assert devin.created == []
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_watcher_anchors_only_first_reply_and_cleans_transient(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None

    class TwoMessageDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState(
                "finished",
                "title",
                None,
                [
                    DevinMessage("devin_message", "e1", "one", None),
                    DevinMessage("devin_message", "e2", "two", None),
                ],
            )

    telegram = _FakeTelegram()
    await SessionWatcher(
        conversation,
        store,
        TwoMessageDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
        trigger_message_id=44,
        transient_message_ids=[43],
    ).run()
    assert telegram.sent[0]["reply_to_message_id"] == 44
    assert "reply_to_message_id" not in telegram.sent[1]
    assert (222, 43) in telegram.deleted


@pytest.mark.asyncio
async def test_options_reply_does_not_paginate(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None

    class OptionsDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState(
                "finished",
                "title",
                None,
                [
                    DevinMessage(
                        "devin_message",
                        "e1",
                        "Choose:\n" + ("x" * 600) + "\nOPTIONS: Yes | No",
                        None,
                    )
                ],
            )

    telegram = _FakeTelegram()
    await SessionWatcher(
        conversation,
        store,
        OptionsDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path, telegram_long_reply_chars=100),
    ).run()
    assert not telegram.documents
    assert telegram.sent[-1]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    assert store.list_choices("222", 1)


@pytest.mark.asyncio
async def test_queue_drains_when_watcher_status_becomes_blocked(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    telegram = _FakeTelegram()

    class StatusDevin(_FakeDevin):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_session(self, _session_id: str) -> SessionState:
            self.calls += 1
            status = "working" if self.calls == 1 else "blocked"
            return SessionState(status, "title", None, [])

    devin = StatusDevin()
    runtime = Bridge(
        settings(tmp_path, devin_settle_seconds=0),
        store,
        devin,
        telegram,
    )  # type: ignore[arg-type]
    conversation = store.get_conversation("222")
    assert conversation is not None
    runtime.queued_turns["222"] = [(message("queued", message_id=8), "queued", None)]
    await runtime.start_watcher(conversation)
    watcher_task = runtime.watchers["s1"]
    await watcher_task
    await asyncio.sleep(0)
    assert runtime.queued_count("222") == 0
    assert ("s1", "queued") in devin.sent
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_debounce_joins_fragments_and_uses_last_message(tmp_path: Path) -> None:
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path, telegram_debounce_seconds=0.01),
        Store(":memory:"),
        _FakeDevin(),
        telegram,
    )  # type: ignore[arg-type]
    first = message("one", message_id=1)
    second = message("two", message_id=2)
    await runtime._queue_turn(first, "one", None)
    await runtime._queue_turn(second, "two", None)
    await asyncio.sleep(0.02)
    assert runtime.store.get_conversation("222") is not None
    assert runtime.store.get_conversation("222").last_user_text == "one\n\ntwo"  # type: ignore[union-attr]
    assert telegram.reactions[0] == "👀"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_flushes_pending_turns(tmp_path: Path) -> None:
    devin = _FakeDevin()
    runtime = Bridge(
        settings(tmp_path, telegram_debounce_seconds=10),
        Store(":memory:"),
        devin,
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    await runtime._queue_turn(message("pending"), "pending", None)
    assert runtime.queued_count("222") == 1
    await runtime.shutdown()
    assert any("pending" in prompt for prompt in devin.created)


@pytest.mark.asyncio
async def test_shutdown_delivers_pending_turn_for_busy_conversation(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    devin = _FakeDevin()
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, devin, telegram)  # type: ignore[arg-type]
    conversation = store.get_conversation("222")
    assert conversation is not None
    watcher = SessionWatcher(
        conversation,
        store,
        devin,  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
    )
    watcher.last_status = "working"
    runtime.active_watchers["s1"] = watcher
    runtime.watchers["s1"] = asyncio.create_task(asyncio.sleep(10))
    runtime.pending_turns["222"] = [(message("pending"), "pending", None)]
    runtime.queued_turns["222"] = [(message("queued"), "queued", None)]
    await runtime.shutdown()
    assert devin.sent[-2:] == [("s1", "queued"), ("s1", "pending")]
    assert not runtime.queued_turns


@pytest.mark.asyncio
async def test_second_attachment_flushes_previous_turn(tmp_path: Path) -> None:
    devin = _FakeDevin()
    runtime = Bridge(
        settings(tmp_path, telegram_debounce_seconds=10),
        Store(":memory:"),
        devin,
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    attachment = ("a.txt", b"a", "text/plain")
    await runtime._queue_turn(message("a", message_id=1), "a", attachment)
    await runtime._queue_turn(message("b", message_id=2), "b", attachment)
    assert len(devin.created) == 1
    assert "a" in devin.created[0]
    assert runtime.pending_turns["222"][0][1] == "b"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_busy_turn_is_queued_and_drained(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path, telegram_queue_while_busy=True),
        store,
        _FakeDevin(),
        telegram,
    )  # type: ignore[arg-type]
    conversation = store.get_conversation("222")
    assert conversation is not None
    watcher = SessionWatcher(
        conversation,
        store,
        _FakeDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
    )
    runtime.active_watchers["s1"] = watcher
    task = asyncio.create_task(asyncio.sleep(10))
    runtime.watchers["s1"] = task
    await runtime._queue_turn(message("next", message_id=9), "next", None)
    assert runtime.queued_count("222") == 1
    assert telegram.reactions[-1] == "🤔"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    runtime.watchers.pop("s1", None)
    runtime.active_watchers.pop("s1", None)
    await runtime._drain_queue("222")
    assert runtime.queued_count("222") == 0
    assert telegram.reactions[-1] == "👀"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_busy_turn_queue_survives_reaction_failure(tmp_path: Path) -> None:
    class FailingReactionTelegram(_FakeTelegram):
        async def set_message_reaction(
            self, _chat_id: int, _message_id: int, _emoji: str
        ) -> None:
            raise RuntimeError("reaction invalid")

        async def react(
            self, chat_id: int, message_id: int | None, emoji: str | None
        ) -> bool:
            if message_id is None:
                return False
            try:
                await self.set_message_reaction(chat_id, message_id, emoji)
            except Exception:  # noqa: BLE001 - mirrors client
                return False
            return True

    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    telegram = FailingReactionTelegram()
    runtime = Bridge(
        settings(tmp_path, telegram_queue_while_busy=True),
        store,
        _FakeDevin(),
        telegram,
    )  # type: ignore[arg-type]
    conversation = store.get_conversation("222")
    assert conversation is not None
    runtime.active_watchers["s1"] = SessionWatcher(
        conversation,
        store,
        _FakeDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path),
    )
    task = asyncio.create_task(asyncio.sleep(10))
    runtime.watchers["s1"] = task
    await runtime._queue_turn(message("next", message_id=9), "next", None)
    assert runtime.queued_count("222") == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    runtime.watchers.pop("s1", None)
    runtime.active_watchers.pop("s1", None)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_steer_sends_immediately_without_queueing(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_user_text="previous",
    )
    devin = _FakeDevin()
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path, telegram_queue_while_busy=True),
        store,
        devin,
        telegram,
    )  # type: ignore[arg-type]
    start_calls: list[tuple[str, int | None]] = []

    async def start_watcher(
        conversation: object,
        *,
        trigger_message_id: int | None = None,
    ) -> None:
        start_calls.append(
            (conversation.session_id, trigger_message_id)  # type: ignore[attr-defined]
        )

    runtime.start_watcher = start_watcher  # type: ignore[method-assign]
    await handle_command(
        runtime,
        message("/steer hurry up", message_id=19),
        "/steer hurry up",
    )
    assert devin.sent == [("s1", "hurry up")]
    assert runtime.queued_count("222") == 0
    assert telegram.reactions[-1] == "👀"
    assert start_calls == [("s1", 19)]
    assert store.get_conversation("222").last_user_text == "previous"  # type: ignore[union-attr]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_react_swallows_http_error(tmp_path: Path) -> None:
    class FailingReactionTelegram(_FakeTelegram):
        async def set_message_reaction(
            self, _chat_id: int, _message_id: int, _emoji: str
        ) -> None:
            raise httpx.HTTPError("reaction transport failed")

        async def react(
            self, chat_id: int, message_id: int | None, emoji: str | None
        ) -> bool:
            if message_id is None:
                return False
            try:
                await self.set_message_reaction(chat_id, message_id, emoji)
            except Exception:  # noqa: BLE001 - mirrors client
                return False
            return True

    runtime = Bridge(
        settings(tmp_path),
        Store(":memory:"),
        _FakeDevin(),
        FailingReactionTelegram(),
    )  # type: ignore[arg-type]
    await runtime.react(message("steer"), "👀")
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_steer_without_args_sends_usage(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(runtime, message("/steer"), "/steer")
    assert telegram.sent[-1]["text"] == (
        "Usage: /steer <text> — send a message to the running session immediately."
    )
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_steer_without_conversation_reports_no_active_session(
    tmp_path: Path,
) -> None:
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), Store(":memory:"), _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(runtime, message("/steer x"), "/steer x")
    assert telegram.sent[-1]["text"] == "No active session."
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_long_reply_extracts_document_and_pages(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None

    class LongDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState(
                "finished",
                "title",
                None,
                [DevinMessage("devin_message", "e1", "```python\n" + "x" * 1600 + "\n```", None)],
            )

    telegram = _FakeTelegram()
    await SessionWatcher(
        conversation,
        store,
        LongDevin(),  # type: ignore[arg-type]
        telegram,  # type: ignore[arg-type]
        settings(tmp_path, telegram_long_reply_chars=500),
        trigger_message_id=7,
    ).run()
    assert telegram.documents[0]["filename"] == "snippet-1.python"
    assert "📎 snippet-1.python" in str(telegram.sent[0]["text"])


def test_reply_and_forward_context(tmp_path: Path) -> None:
    runtime = Bridge(
        settings(tmp_path),
        Store(":memory:"),
        _FakeDevin(),
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    quoted = {
        **message("new"),
        "reply_to_message": {
            "from": {"id": 111, "is_bot": False},
            "text": "quoted text",
        },
        "quote": {"text": "quoted text"},
        "forward_origin": {"text": "forwarded"},
    }
    assert runtime._contextualize_message(quoted, "new") == (
        '> Re: "quoted text"\n\nForwarded message:\nforwarded'
    )


def test_settings_validate_polling_mode(tmp_path: Path) -> None:
    config = settings(
        tmp_path,
        telegram_mode="polling",
        public_base_url=None,
        telegram_webhook_secret=None,
    )
    assert config.telegram_mode == "polling"
    with pytest.raises(ValueError):
        settings(tmp_path, telegram_mode="invalid")


def test_settings_validate_image_delivery_mode(tmp_path: Path) -> None:
    config = settings(tmp_path, telegram_images_as_documents="AUTO")
    assert config.telegram_images_as_documents == "auto"
    with pytest.raises(ValueError, match="telegram_images_as_documents"):
        settings(tmp_path, telegram_images_as_documents="sometimes")


def test_settings_validate_transcription_backend(tmp_path: Path) -> None:
    local = settings(tmp_path, transcription_backend="LOCAL")
    assert local.transcription_backend == "local"
    assert local.transcription_enabled
    whispercpp = settings(tmp_path, transcription_backend="WHISPERCPP")
    assert whispercpp.transcription_backend == "whispercpp"
    assert whispercpp.transcription_enabled
    assert not settings(tmp_path, transcription_backend="api").transcription_enabled
    with pytest.raises(ValueError, match="transcription_backend"):
        settings(tmp_path, transcription_backend="foo")


@pytest.mark.asyncio
async def test_get_updates_accepts_list_result() -> None:
    requests: list[dict[str, object]] = []
    timeouts: list[dict[str, int]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(cast(dict[str, object], json.loads(request.content)))
        timeout = request.extensions["timeout"]
        assert isinstance(timeout, dict)
        timeouts.append(cast(dict[str, int], timeout))
        return httpx.Response(
            200,
            json={"ok": True, "result": [{"update_id": 4, "message": {}}]},
        )

    telegram = TelegramClient(
        "token",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(handler),
    )
    updates = await telegram.get_updates(4, 50, ["message"])
    assert updates == [{"update_id": 4, "message": {}}]
    assert requests == [{"offset": 4, "timeout": 50, "allowed_updates": ["message"]}]
    assert timeouts == [{"connect": 60, "read": 60, "write": 60, "pool": 60}]
    await telegram.close()


@pytest.mark.asyncio
async def test_polling_mode_disables_webhook_route(tmp_path: Path) -> None:
    config = settings(
        tmp_path,
        telegram_mode="polling",
        public_base_url=None,
        telegram_webhook_secret=None,
    )
    app = create_app(
        config,
        store=Store(":memory:"),
        devin=_FakeDevin(),  # type: ignore[arg-type]
        telegram=_FakeTelegram(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/telegram/webhook", json={})
        health = await client.get("/health")
    assert response.status_code == 404
    assert health.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_conversation_settings_callbacks_and_watcher_effects(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "conversation-settings.sqlite3"))
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path, telegram_drafts=True), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    callback = {
        "id": "cfg-1",
        "data": "cfg:silent:1",
        "from": {"id": 111, "is_bot": False},
        "message": {"message_id": 44, "chat": {"id": 222, "type": "private"}},
    }
    await runtime.handle_callback(callback)
    assert store.get_settings("222").silent
    await runtime.handle_callback({**callback, "id": "cfg-2", "data": "cfg:drafts:off"})
    await runtime.handle_callback({**callback, "id": "cfg-3", "data": "cfg:status_timer:off"})
    store.update_settings("222", default_playbook="pb-1")
    assert runtime._conversation_drafts("222") is False
    assert runtime._conversation_status_after("222") == float("inf")
    assert runtime._conversation_silent("222")
    assert store.get_settings("222").default_playbook == "pb-1"
    await runtime.send_text(message("reply"), "quiet")
    assert telegram.sent[-1]["disable_notification"] is True
    await runtime.shutdown()

    fresh_devin = _FakeDevin()
    fresh_store = Store(":memory:")
    fresh_store.update_settings("222", default_playbook="pb-1")
    fresh_runtime = Bridge(settings(tmp_path), fresh_store, fresh_devin, _FakeTelegram())  # type: ignore[arg-type]
    await fresh_runtime.handle_user_turn(message("first"), "first")
    assert fresh_devin.created_playbooks == ["pb-1"]
    await fresh_runtime.shutdown()


@pytest.mark.asyncio
async def test_forwarded_settings_callback_is_unavailable(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.update_settings("222", silent=False)
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path),
        store,
        _FakeDevin(),
        telegram,
    )  # type: ignore[arg-type]
    await runtime.handle_callback({
        "id": "forwarded",
        "data": "cfg:silent:1",
        "from": {"id": 111, "is_bot": False},
        "message": {
            "message_id": 44,
            "chat": {"id": 222, "type": "private"},
            "forward_origin": {"type": "user"},
        },
    })
    assert not store.get_settings("222").silent
    assert telegram.answers[-1] == "Not available on forwarded messages"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_access_request_admin_approval_denial_and_non_admin(tmp_path: Path) -> None:
    config = settings(tmp_path, telegram_admin_user_ids="900", telegram_allowed_users="")
    store = Store(str(tmp_path / "access.sqlite3"))
    telegram = _FakeTelegram()
    runtime = Bridge(config, store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    request = {
        "id": "request",
        "data": "acc:req",
        "from": {"id": 222, "username": "alice", "first_name": "Alice"},
        "message": {"message_id": 1, "chat": {"id": 222, "type": "private"}},
    }
    await runtime.handle_callback(request)
    assert store.get_access_request(222) is not None
    assert telegram.sent[-1]["chat_id"] == 900
    approve = {
        "id": "approve",
        "data": "acc:ok:222",
        "from": {"id": 900},
        "message": {"message_id": 2, "chat": {"id": 900, "type": "private"}},
    }
    await runtime.handle_callback(approve)
    assert 222 in runtime.approved_users
    assert is_allowed(message("hi", user_id=222), config, runtime.approved_users)
    denied_request = {
        **request,
        "id": "request-2",
        "from": {"id": 333, "username": "bob", "first_name": "Bob"},
        "message": {
            "message_id": 3,
            "chat": {"id": 333, "type": "private"},
        },
    }
    await runtime.handle_callback(denied_request)
    await runtime.handle_callback({
        **approve, "id": "deny", "data": "acc:no:333",
    })
    denied = store.get_access_request(333)
    assert denied is not None and denied.status == "denied"
    assert any(item["chat_id"] == 333 for item in telegram.sent)
    await runtime.handle_callback({
        **approve, "id": "deny", "data": "acc:no:222", "from": {"id": 223},
    })
    assert 222 in runtime.approved_users
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_users_lists_env_and_approved(tmp_path: Path) -> None:
    config = settings(
        tmp_path,
        telegram_admin_user_ids="900",
        telegram_allowed_users="222,900",
    )
    store = Store(str(tmp_path / "users.sqlite3"))
    store.save_access_request(333, "bob", "Bob")
    store.decide_access_request(333, "approved", 900)
    telegram = _FakeTelegram()
    runtime = Bridge(config, store, _FakeDevin(), telegram)  # type: ignore[arg-type]

    await runtime.handle_message(message("/users", user_id=222, chat_id=222))
    assert telegram.sent[-1]["text"] == "Admins only."

    await runtime.handle_message(message("/users", user_id=900, chat_id=900))
    assert telegram.sent[-1]["text"] == (
        "222 · allowed via .env\n900 · allowed via .env\n333 · Bob"
    )
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_sethome_requires_admin(tmp_path: Path) -> None:
    config = settings(
        tmp_path,
        telegram_admin_user_ids="900",
        telegram_allowed_users="222,900",
    )
    store = Store(str(tmp_path / "sethome.sqlite3"))
    telegram = _FakeTelegram()
    runtime = Bridge(config, store, _FakeDevin(), telegram)  # type: ignore[arg-type]

    await runtime.handle_message(message("/sethome", user_id=222, chat_id=222))
    assert store.get_setting("home_chat_id") is None
    assert not any(
        item["text"] == "This chat is now the notification home."
        for item in telegram.sent
    )

    await runtime.handle_message(message("/sethome", user_id=900, chat_id=900))
    assert store.get_setting("home_chat_id") == "900"
    assert telegram.sent[-1]["text"] == "This chat is now the notification home."
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_access_request_prompt_and_retry_after_denial(tmp_path: Path) -> None:
    config = settings(
        tmp_path,
        telegram_admin_user_ids="900",
        telegram_allowed_users="",
    )
    store = Store(":memory:")
    telegram = _FakeTelegram()
    runtime = Bridge(config, store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    unauthorized = message("hello", user_id=222)
    await runtime.handle_message(unauthorized)
    assert store.get_access_request(222) is None
    assert telegram.sent[-1]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    await runtime.handle_message(unauthorized)
    assert len(telegram.sent) == 1

    callback = {
        "id": "request-1",
        "data": "acc:req",
        "from": {"id": 222, "username": "alice"},
        "message": {"message_id": 1, "chat": {"id": 222, "type": "private"}},
    }
    await runtime.handle_callback(callback)
    assert len(telegram.sent) == 2
    assert store.get_access_request(222).status == "requested"  # type: ignore[union-attr]
    await runtime.handle_callback({**callback, "id": "request-2"})
    assert len(telegram.sent) == 2
    await runtime.handle_callback({
        **callback,
        "id": "deny",
        "from": {"id": 900},
        "data": "acc:no:222",
        "message": {"message_id": 2, "chat": {"id": 900, "type": "private"}},
    })
    assert store.get_access_request(222).status == "denied"  # type: ignore[union-attr]
    await runtime.handle_callback({**callback, "id": "request-3"})
    assert len(telegram.sent) == 4
    assert store.get_access_request(222).status == "requested"  # type: ignore[union-attr]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_access_request_callback_requires_requester_chat(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path, telegram_admin_user_ids="900")
    store = Store(":memory:")
    telegram = _FakeTelegram()
    runtime = Bridge(config, store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    await runtime.handle_callback({
        "id": "request",
        "data": "acc:req",
        "from": {"id": 222, "username": "alice"},
        "message": {"message_id": 1, "chat": {"id": 999, "type": "private"}},
    })
    assert store.get_access_request(222) is None
    assert telegram.answers[-1] == "Not allowed"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_access_request_duplicate_does_not_notify_admin(
    tmp_path: Path,
) -> None:
    config = settings(
        tmp_path,
        telegram_admin_user_ids="900",
        telegram_allowed_users="",
    )
    telegram = _FakeTelegram()
    runtime = Bridge(config, Store(":memory:"), _FakeDevin(), telegram)  # type: ignore[arg-type]
    callback = {
        "id": "request-1",
        "data": "acc:req",
        "from": {"id": 222, "username": "alice"},
        "message": {"message_id": 1, "chat": {"id": 222, "type": "private"}},
    }
    await runtime.handle_callback(callback)
    sent_count = len(telegram.sent)
    await runtime.handle_callback({**callback, "id": "request-2"})
    assert len(telegram.sent) == sent_count
    assert telegram.answers[-1] == "Request already pending"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_attachment_photo_document_and_download_fallback(tmp_path: Path) -> None:
    class ArtifactDevin(_FakeDevin):
        async def download_attachment(self, url: str) -> tuple[bytes, str] | None:
            if "small" in url:
                return _png(800, 600), "image/png"
            if "large" in url:
                return _png(1650, 3000), "image/png"
            if "missing" in url:
                return None
            return b"zip", "application/zip"

    store = Store(str(tmp_path / "artifacts.sqlite3"))
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    small_image_url = "https://app.devin.ai/attachments/1/small.png"
    large_image_url = "https://app.devin.ai/attachments/2/large.png"
    doc_url = "https://app.devin.ai/attachments/2/archive.zip"
    hd_watcher = SessionWatcher(
        conversation, store, ArtifactDevin(), telegram, settings(tmp_path),  # type: ignore[arg-type]
    )
    await hd_watcher._deliver(
        DevinMessage("devin_message", "0", small_image_url, None),
        SessionState("finished", "title", None, []),
    )
    assert telegram.photos[0]["filename"] == "small.png"
    await hd_watcher._deliver(
        DevinMessage("devin_message", "1", large_image_url, None),
        SessionState("finished", "title", None, []),
    )
    assert telegram.documents[0]["filename"] == "large.png"
    await hd_watcher._deliver(
        DevinMessage("devin_message", "2", doc_url, None),
        SessionState("finished", "title", None, []),
    )
    assert telegram.documents[1]["filename"] == "archive.zip"
    watcher = SessionWatcher(
        conversation, store, ArtifactDevin(), telegram,  # type: ignore[arg-type]
        settings(tmp_path, telegram_images_as_documents="true"),
    )
    await watcher._deliver(
        DevinMessage("devin_message", "3", small_image_url, None),
        SessionState("finished", "title", None, []),
    )
    assert telegram.documents[2]["filename"] == "small.png"
    false_watcher = SessionWatcher(
        conversation, store, ArtifactDevin(), telegram,  # type: ignore[arg-type]
        settings(tmp_path, telegram_images_as_documents="false"),
    )
    await false_watcher._deliver(
        DevinMessage("devin_message", "4", large_image_url, None),
        SessionState("finished", "title", None, []),
    )
    assert telegram.photos[1]["filename"] == "large.png"
    assert store.conv_key_for_message(222, 1) == "222"
    sent_before = len(telegram.sent)
    await watcher._deliver(
        DevinMessage(
            "devin_message",
            "attachment-only",
            small_image_url,
            None,
        ),
        SessionState("finished", "title", None, []),
    )
    assert len(telegram.sent) == sent_before
    missing = "https://app.devin.ai/attachments/3/missing.txt"
    await watcher._deliver(DevinMessage("devin_message", "3", missing, None), SessionState("finished", "title", None, []))
    assert missing in str(telegram.sent[-1]["text"])


@pytest.mark.asyncio
async def test_attachment_url_matching_strips_markdown_punctuation(
    tmp_path: Path,
) -> None:
    class URLDevin(_FakeDevin):
        def __init__(self) -> None:
            super().__init__()
            self.urls: list[str] = []

        async def download_attachment(self, url: str) -> tuple[bytes, str] | None:
            self.urls.append(url)
            return None

    store = Store(":memory:")
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    devin = URLDevin()
    watcher = SessionWatcher(
        conversation,
        store,
        devin,
        _FakeTelegram(),
        settings(tmp_path),
    )
    clean = "https://app.devin.ai/attachments/1/report.pdf"
    await watcher._deliver(
        DevinMessage("devin_message", "1", f"[report]({clean})", None),
        SessionState("finished", "title", None, []),
    )
    await watcher._deliver(
        DevinMessage("devin_message", "2", f"{clean}.", None),
        SessionState("finished", "title", None, []),
    )
    await watcher._deliver(
        DevinMessage("devin_message", "3", f'see "{clean}" now', None),
        SessionState("finished", "title", None, []),
    )
    assert devin.urls == [clean, clean, clean]


@pytest.mark.asyncio
async def test_attachment_line_delivers_document_and_options(tmp_path: Path) -> None:
    class AttachmentDevin(_FakeDevin):
        async def download_attachment(self, _url: str) -> tuple[bytes, str] | None:
            return b"data", "text/plain"

    store = Store(str(tmp_path / "attachment-line.sqlite3"))
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    watcher = SessionWatcher(
        conversation,
        store,
        AttachmentDevin(),
        telegram,
        settings(tmp_path),
    )
    await watcher._deliver(
        DevinMessage(
            "devin_message",
            "attachment-line",
            'Here is the file\nOPTIONS: Yes | No\n'
            'ATTACHMENT:{"url":"https://app.devin.ai/attachments/1/report.txt","fileSize":5}',
            None,
        ),
        SessionState("finished", "title", None, []),
    )
    assert telegram.documents[0]["filename"] == "report.txt"
    sent_text = str(telegram.sent[-1]["text"])
    assert "Here is the file" in sent_text
    assert "ATTACHMENT:" not in sent_text
    assert "OPTIONS:" not in sent_text
    markup = cast(dict[str, object], telegram.sent[-1]["reply_markup"])
    keyboard = cast(list[list[dict[str, str]]], markup["inline_keyboard"])
    assert len(keyboard) == 2


@pytest.mark.asyncio
async def test_attachment_link_fallback_when_download_fails(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "attachment-fallback.sqlite3"))
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    watcher = SessionWatcher(
        conversation,
        store,
        _FakeDevin(),
        telegram,
        settings(tmp_path),
    )
    url = "https://app.devin.ai/attachments/9/big.zip"
    await watcher._deliver(
        DevinMessage(
            "devin_message",
            "metadata-only",
            f'ATTACHMENT:{{"url":"{url}","fileSize":1}}',
            None,
        ),
        SessionState("finished", "title", None, []),
    )
    assert url in str(telegram.sent[-1]["text"])
    assert not telegram.documents
    await watcher._deliver(
        DevinMessage(
            "devin_message",
            "text-options-metadata",
            f'Text\nOPTIONS: A | B\nATTACHMENT:{{"url":"{url}","fileSize":1}}',
            None,
        ),
        SessionState("finished", "title", None, []),
    )
    sent_text = str(telegram.sent[-1]["text"])
    assert "Text" in sent_text
    assert url in sent_text
    assert "ATTACHMENT:" not in sent_text
    markup = cast(dict[str, object], telegram.sent[-1]["reply_markup"])
    keyboard = cast(list[list[dict[str, str]]], markup["inline_keyboard"])
    assert len(keyboard) == 2


@pytest.mark.asyncio
async def test_pr_card_rendering_and_failure_fallback(tmp_path: Path) -> None:
    class PRDevin(_FakeDevin):
        async def fetch_github_pr(self, _url: str, _token: str | None = None) -> dict[str, object] | None:
            return {
                "number": 7, "title": "Fix bridge", "state": "open", "merged": False,
                "additions": 4, "deletions": 2,
                "base": {"ref": "main"}, "head": {"ref": "feature"},
            }

    store = Store(str(tmp_path / "pr.sqlite3"))
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = _FakeTelegram()
    watcher = SessionWatcher(conversation, store, PRDevin(), telegram, settings(tmp_path))  # type: ignore[arg-type]
    url = "https://github.com/acme/repo/pull/7"
    await watcher._deliver(DevinMessage("devin_message", "1", url, None), SessionState("finished", "title", None, []))
    assert "PR #7" in str(telegram.sent[-1]["text"])
    failed = SessionWatcher(conversation, store, _FakeDevin(), telegram, settings(tmp_path))  # type: ignore[arg-type]
    await failed._deliver(DevinMessage("devin_message", "2", url, None), SessionState("finished", "title", None, []))
    assert url in str(telegram.sent[-1]["text"])


@pytest.mark.asyncio
async def test_voice_transcription_success_and_failure_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ResponseClient:
        def __init__(self, response: httpx.Response) -> None:
            self.response = response
        async def __aenter__(self) -> Self:
            return self
        async def __aexit__(self, *_: object) -> None:
            return None
        async def post(self, *_: object, **__: object) -> httpx.Response:
            return self.response

    voice = {**message("caption"), "text": None, "voice": {"file_id": "voice-1"}}
    telegram = _FakeTelegram()
    devin = _FakeDevin()
    runtime = Bridge(
        settings(tmp_path, transcription_api_key="transcribe", telegram_attach_voice=False),
        Store(":memory:"), devin, telegram,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_: ResponseClient(
            httpx.Response(
                200,
                json={"text": "hello"},
                request=httpx.Request("POST", "https://transcribe.test"),
            )
        ),
    )
    await runtime.handle_message(voice)
    assert "Voice note transcript:" in devin.created[0]
    assert "hello" in devin.created[0]
    assert "✍" in telegram.reactions
    await runtime.shutdown()

    failed_devin = _FakeDevin()
    failed_runtime = Bridge(
        settings(tmp_path, transcription_api_key="transcribe", database_path=str(tmp_path / "voice-failure.sqlite3")),
        Store(str(tmp_path / "voice-failure.sqlite3")), failed_devin, _FakeTelegram(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_: ResponseClient(
            httpx.Response(
                500,
                request=httpx.Request("POST", "https://transcribe.test"),
            )
        ),
    )
    await failed_runtime.handle_message(voice)
    assert "Attached file" in failed_devin.created[0]
    await failed_runtime.shutdown()


@pytest.mark.asyncio
async def test_local_transcription_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = {**message("caption"), "text": None, "voice": {"file_id": "voice-1"}}

    async def transcribe(_content: bytes, _filename: str, _model: str, _language: str | None) -> str | None:
        return "hello local"

    telegram = _FakeTelegram()
    devin = _FakeDevin()
    runtime = Bridge(
        settings(
            tmp_path,
            transcription_backend="local",
            telegram_attach_voice=False,
        ),
        Store(":memory:"),
        devin,
        telegram,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(main_module, "transcribe_local", transcribe)
    await runtime.handle_message(voice)
    assert "Voice note transcript:" in devin.created[0]
    assert "hello local" in devin.created[0]
    assert "✍" in telegram.reactions
    await runtime.shutdown()

    failed_devin = _FakeDevin()

    async def unavailable(
        _content: bytes,
        _filename: str,
        _model: str,
        _language: str | None,
    ) -> None:
        return None

    monkeypatch.setattr(main_module, "transcribe_local", unavailable)
    failed_runtime = Bridge(
        settings(
            tmp_path,
            transcription_backend="local",
            telegram_attach_voice=False,
            database_path=str(tmp_path / "local-failure.sqlite3"),
        ),
        Store(str(tmp_path / "local-failure.sqlite3")),
        failed_devin,
        _FakeTelegram(),  # type: ignore[arg-type]
    )
    await failed_runtime.handle_message(voice)
    assert "Attached file" in failed_devin.created[0]
    await failed_runtime.shutdown()


@pytest.mark.asyncio
async def test_whispercpp_transcription_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = {**message("caption"), "text": None, "voice": {"file_id": "voice-1"}}

    async def transcribe(
        _content: bytes,
        _filename: str,
        _binary: str,
        _model: str,
        _language: str | None,
        **_: object,
    ) -> str | None:
        return "hello whispercpp"

    telegram = _FakeTelegram()
    devin = _FakeDevin()
    runtime = Bridge(
        settings(
            tmp_path,
            transcription_backend="whispercpp",
            telegram_attach_voice=False,
        ),
        Store(":memory:"),
        devin,
        telegram,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(main_module, "transcribe_whispercpp", transcribe)
    await runtime.handle_message(voice)
    assert "Voice note transcript:" in devin.created[0]
    assert "hello whispercpp" in devin.created[0]
    assert "✍" in telegram.reactions
    await runtime.shutdown()

    failed_devin = _FakeDevin()

    async def unavailable(
        _content: bytes,
        _filename: str,
        _binary: str,
        _model: str,
        _language: str | None,
        **_: object,
    ) -> None:
        return None

    monkeypatch.setattr(main_module, "transcribe_whispercpp", unavailable)
    failed_runtime = Bridge(
        settings(
            tmp_path,
            transcription_backend="whispercpp",
            telegram_attach_voice=False,
            database_path=str(tmp_path / "whispercpp-failure.sqlite3"),
        ),
        Store(str(tmp_path / "whispercpp-failure.sqlite3")),
        failed_devin,
        _FakeTelegram(),  # type: ignore[arg-type]
    )
    await failed_runtime.handle_message(voice)
    assert "Attached file" in failed_devin.created[0]
    await failed_runtime.shutdown()


@pytest.mark.asyncio
async def test_per_user_voice_transcription_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = {**message("caption"), "text": None, "voice": {"file_id": "voice-1"}}
    languages: list[str | None] = []

    async def transcribe(
        _content: bytes,
        _filename: str,
        _binary: str,
        _model: str,
        language: str | None,
        **_: object,
    ) -> str:
        languages.append(language)
        return "hello"

    store = Store(":memory:")
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(
            tmp_path,
            transcription_backend="whispercpp",
            transcription_language="en",
            telegram_attach_voice=False,
        ),
        store,
        _FakeDevin(),
        telegram,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(main_module, "transcribe_whispercpp", transcribe)

    await handle_command(runtime, message("/lang he"), "/lang he")
    assert store.get_setting("lang:111") == "he"
    assert telegram.sent[-1]["text"] == "Voice language set to he."
    await runtime.handle_message(voice)
    assert languages[-1] == "he"

    await handle_command(runtime, message("/lang"), "/lang")
    assert telegram.sent[-1]["text"] == "Voice language: he"

    await handle_command(runtime, message("/lang auto"), "/lang auto")
    await runtime.handle_message(voice)
    assert languages[-1] is None

    await handle_command(runtime, message("/lang off"), "/lang off")
    assert store.get_setting("lang:111") is None
    await runtime.handle_message(voice)
    assert languages[-1] == "en"

    await handle_command(runtime, message("/lang english!"), "/lang english!")
    assert telegram.sent[-1]["text"] == (
        "Usage: /lang <code|auto|off> (e.g. /lang he)"
    )
    assert store.get_setting("lang:111") is None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_usage_formatting_missing_org_and_forbidden(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "usage.sqlite3"))
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path, devin_org_id="org"), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    async def consumption(*_: object, **__: object) -> dict[str, object]:
        return {
            "total_acus": 12.345,
            "consumption_by_date": [
                {"date": "2025-09-18", "acus": 2.5},
                {"date": "2025-09-19", "acus": 9.845},
            ],
        }
    runtime.devin.session_consumption = consumption  # type: ignore[method-assign]
    await runtime.usage(message("/usage"))
    usage_text = str(telegram.sent[-1]["text"])
    assert "Session ACUs: 12.35" in usage_text
    assert "2025-09-19 · 9.85" in usage_text
    assert "Usage is aggregated daily" in usage_text
    async def empty_consumption(*_: object, **__: object) -> dict[str, object]:
        return {"total_acus": 0, "consumption_by_date": []}
    runtime.devin.session_consumption = empty_consumption  # type: ignore[method-assign]
    await runtime.usage(message("/usage"))
    assert "No consumption data returned" in str(telegram.sent[-1]["text"])
    runtime.settings.devin_org_id = None  # type: ignore[misc]
    await runtime.usage(message("/usage"))
    assert "DEVIN_ORG_ID is required" in str(telegram.sent[-1]["text"])

    runtime.settings.devin_org_id = "org"  # type: ignore[misc]
    async def forbidden(*_: object, **__: object) -> dict[str, object]:
        request = httpx.Request("GET", "https://devin.test")
        raise httpx.HTTPStatusError("forbidden", request=request, response=httpx.Response(403, request=request))
    runtime.devin.session_consumption = forbidden  # type: ignore[method-assign]
    await runtime.usage(message("/usage"))
    assert "can't read consumption" in str(telegram.sent[-1]["text"])
    await runtime.shutdown()

@pytest.mark.asyncio
async def test_poll_reuses_fastapi_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.poll as poll_module

    assert poll_module.bridge is main_module.bridge
    store_calls = 0
    original_store = main_module.Store

    def counted_store(*args: object, **kwargs: object) -> Store:
        nonlocal store_calls
        store_calls += 1
        return original_store(*args, **kwargs)

    async def startup() -> None:
        return None

    async def shutdown() -> None:
        return None

    async def run_polling(*_: object) -> None:
        return None

    monkeypatch.setattr(main_module, "Store", counted_store)
    monkeypatch.setattr(poll_module.bridge, "startup", startup)
    monkeypatch.setattr(poll_module.bridge, "shutdown", shutdown)
    monkeypatch.setattr(poll_module, "run_polling", run_polling)
    await poll_module.main()
    assert store_calls == 0


@pytest.mark.asyncio
async def test_concurrent_queue_drains_serialize(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), _FakeTelegram())  # type: ignore[arg-type]
    runtime.queued_turns["222"] = [
        (message("one", message_id=1), "one", None),
        (message("two", message_id=2), "two", None),
    ]
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def handle_turn(_: Mapping[str, object], text: str, *, attachment: object = None) -> None:
        calls.append(text)
        started.set()
        await release.wait()

    runtime.handle_user_turn = handle_turn  # type: ignore[method-assign]
    first = asyncio.create_task(runtime._drain_queue("222"))
    await started.wait()
    second = asyncio.create_task(runtime._drain_queue("222"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert calls == ["one"]
    assert runtime.queued_count("222") == 1
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_queue_drain_failure_keeps_turn_for_retry(tmp_path: Path) -> None:
    runtime = Bridge(
        settings(tmp_path),
        Store(":memory:"),
        _FakeDevin(),
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    turn = (message("queued", message_id=8), "queued", None)
    runtime.queued_turns["222"] = [turn]

    async def fail(
        _message: Mapping[str, object],
        _text: str,
        *,
        attachment: object = None,
    ) -> None:
        raise RuntimeError("send failed")

    runtime.handle_user_turn = fail  # type: ignore[method-assign]
    await runtime._drain_queue("222")
    assert runtime.queued_turns["222"] == [turn]
    assert "retried with the next message or status change" in str(
        runtime.telegram.sent[-1]["text"]
    )
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_flush_failure_retries_before_next_pending_turn(tmp_path: Path) -> None:
    runtime = Bridge(
        settings(tmp_path, telegram_debounce_seconds=10),
        Store(":memory:"),
        _FakeDevin(),
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    calls: list[str] = []

    async def handle(
        _message: Mapping[str, object],
        text: str,
        *,
        attachment: object = None,
    ) -> None:
        calls.append(text)
        if text == "first" and calls.count("first") == 1:
            raise RuntimeError("send failed")

    runtime.handle_user_turn = handle  # type: ignore[method-assign]
    await runtime._queue_turn(message("first", message_id=1), "first", None)
    await runtime._flush_pending("222")
    assert runtime.queued_turns["222"][0][1] == "first"
    await runtime._queue_turn(message("second", message_id=2), "second", None)
    await runtime._flush_pending("222")
    assert calls == ["first", "first", "second"]
    assert not runtime.queued_turns
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_rate_limit_does_not_block_commands(tmp_path: Path) -> None:
    runtime = Bridge(settings(tmp_path), Store(":memory:"), _FakeDevin(), _FakeTelegram())  # type: ignore[arg-type]
    runtime._rate_limited = lambda _user_id: True  # type: ignore[method-assign]
    await runtime.handle_message(message("/status"))
    assert "Slow down" not in str(runtime.telegram.sent[-1]["text"])
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_rate_limit_blocks_reaction_retry(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_user_text="old",
        last_user_message_id=12,
    )
    devin = _FakeDevin()
    runtime = Bridge(settings(tmp_path), store, devin, _FakeTelegram())  # type: ignore[arg-type]
    runtime._rate_limited = lambda _user_id: True  # type: ignore[method-assign]
    await runtime.handle_reaction(
        {
            "user": {"id": 111},
            "chat": {"id": 222, "type": "private"},
            "message_id": 12,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "🔁"}],
        }
    )
    assert devin.sent == []
    assert not runtime.watchers
    await runtime.handle_reaction(
        {
            "user": {"id": 111},
            "chat": {"id": 222, "type": "private"},
            "message_id": 12,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "🛑"}],
        }
    )
    assert devin.terminated == ["s1"]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_rate_limit_blocks_edited_message_correction(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
        last_user_text="old",
        last_user_message_id=12,
    )
    devin = _FakeDevin()
    runtime = Bridge(settings(tmp_path), store, devin, _FakeTelegram())  # type: ignore[arg-type]
    runtime._rate_limited = lambda _user_id: True  # type: ignore[method-assign]
    await runtime.handle_edited_message({**message("new"), "message_id": 12})
    assert devin.sent == []
    conversation = store.get_conversation("222")
    assert conversation is not None
    assert conversation.last_user_text == "old"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_rate_limit_blocks_choice_callback(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    store.add_choice("pick", "222", "s1", 222, "yes", message_id=4)
    store.add_choice("cancel", "222", "s1", 222, "__cmd:cancel", message_id=4)
    telegram = _FakeTelegram()
    devin = _FakeDevin()
    runtime = Bridge(settings(tmp_path), store, devin, telegram)  # type: ignore[arg-type]
    runtime._rate_limited = lambda _user_id: True  # type: ignore[method-assign]
    await runtime.handle_callback(
        {
            "id": "cb",
            "data": "pick",
            "from": {"id": 111},
            "message": {"message_id": 4, "chat": {"id": 222, "type": "private"}},
        }
    )
    assert devin.sent == []
    assert store.get_choice("pick") is not None
    assert telegram.answers == ["Slow down — try again in a moment."]
    await runtime.handle_callback(
        {
            "id": "cb2",
            "data": "cancel",
            "from": {"id": 111},
            "message": {"message_id": 4, "chat": {"id": 222, "type": "private"}},
        }
    )
    assert store.get_choice("cancel") is None
    assert "Cancelled." in telegram.edits
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_stop_cancel_keeps_queued_turns(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    runtime.queued_turns["222"] = [(message("queued"), "queued", None)]
    await handle_command(runtime, message("/stop"), "/stop")
    choices = store.list_choices("222", 1)
    cancel_id = next(choice_id for choice_id, option in choices if option == "__cmd:cancel")
    await runtime.handle_callback({
        "id": "cancel", "data": cancel_id,
        "from": {"id": 111},
        "message": {"message_id": 1, "chat": {"id": 222, "type": "private"}},
    })
    assert runtime.queued_count("222") == 1
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_confirmed_stop_clears_queued_turns(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    devin = _FakeDevin()
    runtime = Bridge(settings(tmp_path), store, devin, _FakeTelegram())  # type: ignore[arg-type]
    runtime.queued_turns["222"] = [(message("queued"), "queued", None)]
    await handle_command(runtime, message("/stop"), "/stop")
    terminate_id = next(
        choice_id
        for choice_id, option in store.list_choices("222", 1)
        if option.startswith("__cmd:terminate:")
    )
    await runtime.handle_callback({
        "id": "terminate",
        "data": terminate_id,
        "from": {"id": 111},
        "message": {"message_id": 1, "chat": {"id": 222, "type": "private"}},
    })
    assert runtime.queued_count("222") == 0
    assert store.get_conversation("222") is None
    assert not devin.created
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_stop_terminate_failure_preserves_local_state(tmp_path: Path) -> None:
    class FailingDevin(_FakeDevin):
        async def terminate(self, _session_id: str) -> None:
            raise RuntimeError("terminate failed")

    store = Store(":memory:")
    store.save_conversation(
        conv_key="222", chat_id=222, thread_id=None, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    runtime = Bridge(
        settings(tmp_path),
        store,
        FailingDevin(),
        _FakeTelegram(),
    )  # type: ignore[arg-type]
    conversation = store.get_conversation("222")
    assert conversation is not None
    watcher = SessionWatcher(
        conversation,
        store,
        runtime.devin,  # type: ignore[arg-type]
        runtime.telegram,  # type: ignore[arg-type]
        settings(tmp_path),
    )
    task = asyncio.create_task(asyncio.sleep(10))
    runtime.watchers["s1"] = task
    runtime.active_watchers["s1"] = watcher
    runtime.queued_turns["222"] = [(message("queued"), "queued", None)]
    runtime.pending_turns["222"] = [(message("pending"), "pending", None)]
    with pytest.raises(RuntimeError, match="terminate failed"):
        await runtime.stop_conversation(conversation)
    assert runtime.queued_turns["222"]
    assert runtime.pending_turns["222"]
    assert task.cancelled()
    assert runtime.watchers["s1"] is not task
    assert runtime.active_watchers["s1"] is not watcher
    assert store.get_conversation("222") is not None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_forum_reaction_resolves_document_message_index(tmp_path: Path) -> None:
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222:9", chat_id=222, thread_id=9, session_id="s1",
        session_url="https://devin.test/s1", title="title",
    )
    conversation = store.get_conversation("222:9")
    assert conversation is not None
    telegram = _FakeTelegram()

    class DocumentDevin(_FakeDevin):
        async def get_session(self, _session_id: str) -> SessionState:
            return SessionState(
                "finished", "title", None,
                [DevinMessage("devin_message", "e1", "```python\n" + "x" * 1600 + "\n```", None)],
            )

    await SessionWatcher(
        conversation, store, DocumentDevin(), telegram, settings(tmp_path),  # type: ignore[arg-type]
    ).run()
    await Bridge(settings(tmp_path), store, _FakeDevin(), telegram).handle_reaction({
        "user": {"id": 111},
        "chat": {"id": 222, "type": "supergroup", "is_forum": True},
        "message_id": 1,
        "old_reaction": [],
        "new_reaction": [{"type": "emoji", "emoji": "🔁"}],
    })
    assert store.conv_key_for_message(222, 1) == "222:9"


@pytest.mark.asyncio
async def test_unindexed_private_topic_reaction_does_not_use_chat_fallback(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    for thread_id in (1, 2):
        store.save_conversation(
            conv_key=f"222:{thread_id}",
            chat_id=222,
            thread_id=thread_id,
            session_id=f"s{thread_id}",
            session_url=f"https://devin.test/s{thread_id}",
            title="title",
            last_user_text="work",
        )
    devin = _FakeDevin()
    runtime = Bridge(settings(tmp_path), store, devin, _FakeTelegram())  # type: ignore[arg-type]
    await runtime.handle_reaction({
        "user": {"id": 111},
        "chat": {"id": 222, "type": "private"},
        "message_id": 99,
        "old_reaction": [],
        "new_reaction": [{"type": "emoji", "emoji": "🛑"}],
    })
    assert devin.terminated == []
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_long_text_token_survives_send_failure(tmp_path: Path) -> None:
    class FailingTelegram(_FakeTelegram):
        async def send_markdown(self, *_: object, **__: object) -> list[dict[str, object]]:
            raise RuntimeError("send failed")

    store = Store(":memory:")
    store.add_long_text("token", "222", 222, "remaining text")
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), FailingTelegram())  # type: ignore[arg-type]
    callback_message = {"message_id": 4, "chat": {"id": 222, "type": "private"}}
    with pytest.raises(RuntimeError):
        await runtime._handle_long_text_callback("cb", callback_message, "222", "token")
    assert store.get_long_text("token") is not None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_long_text_token_consumed_when_markup_edit_fails(tmp_path: Path) -> None:
    class MarkupFailTelegram(_FakeTelegram):
        async def edit_message_reply_markup(
            self,
            _chat_id: int,
            _message_id: int,
            markup: dict[str, object] | None = None,
        ) -> None:
            raise RuntimeError("edit failed")

    store = Store(":memory:")
    store.add_long_text("token", "222", 222, "remaining text")
    telegram = MarkupFailTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    callback_message = {"message_id": 4, "chat": {"id": 222, "type": "private"}}
    await runtime._handle_long_text_callback("cb", callback_message, "222", "token")
    assert store.get_long_text("token") is None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_edit_pending_debounce_turn_uses_edited_text(tmp_path: Path) -> None:
    devin = _FakeDevin()
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path, telegram_debounce_seconds=10),
        Store(":memory:"), devin, telegram,  # type: ignore[arg-type]
    )
    original = message("original", message_id=12)
    await runtime._queue_turn(original, "original", None)
    await runtime.handle_edited_message({**original, "text": "edited"})
    assert runtime.pending_turns["222"][0][1] == "edited"
    assert telegram.reactions[-1] == "✏️"
    await runtime._flush_pending("222")
    assert "edited" in devin.created[0]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_voice_transcription_reaction_failure_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    voice = {**message("caption"), "text": None, "voice": {"file_id": "voice-1"}}

    async def transcribe(
        _content: bytes,
        _filename: str,
        _binary: str,
        _model: str,
        _language: str | None,
        **_: object,
    ) -> str:
        return "hello"

    class FailingReactionTelegram(_FakeTelegram):
        async def set_message_reaction(self, _chat_id, _message_id, emoji):
            raise RuntimeError("Bad Request: REACTION_INVALID")

        async def react(self, chat_id, message_id, emoji):
            if message_id is None:
                return False
            try:
                await self.set_message_reaction(chat_id, message_id, emoji)
            except Exception as exc:  # noqa: BLE001 - mirrors client
                logging.getLogger("app.clients").warning(
                    "reaction %r failed: %s", emoji, exc
                )
                return False
            return True

    telegram = FailingReactionTelegram()
    devin = _FakeDevin()
    runtime = Bridge(
        settings(
            tmp_path,
            transcription_backend="whispercpp",
            telegram_attach_voice=False,
        ),
        Store(":memory:"),
        devin,
        telegram,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(main_module, "transcribe_whispercpp", transcribe)
    with caplog.at_level(logging.WARNING):
        await runtime.handle_message(voice)
    assert "Voice note transcript:\nhello" in devin.created[0]
    assert any("REACTION_INVALID" in record.message for record in caplog.records)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_voice_transcription_uses_writing_reaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = {**message("caption"), "text": None, "voice": {"file_id": "voice-1"}}

    async def transcribe(
        _content: bytes,
        _filename: str,
        _binary: str,
        _model: str,
        _language: str | None,
        **_: object,
    ) -> str:
        return "hello"

    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(
            tmp_path,
            transcription_backend="whispercpp",
            telegram_attach_voice=False,
        ),
        Store(":memory:"),
        _FakeDevin(),
        telegram,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(main_module, "transcribe_whispercpp", transcribe)
    await runtime.handle_message(voice)
    assert "✍" in telegram.reactions
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_react_noops_without_message_id(tmp_path: Path) -> None:
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(tmp_path),
        Store(":memory:"),
        _FakeDevin(),
        telegram,  # type: ignore[arg-type]
    )
    await runtime._react(222, None, "👀")
    assert telegram.reactions == []


@pytest.mark.asyncio
async def test_telegram_client_react() -> None:
    calls: list[dict[str, object]] = []

    async def ok_handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    telegram = TelegramClient(
        "fake-token",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(ok_handler),
    )
    assert await telegram.react(222, None, "👍") is False
    assert calls == []
    assert await telegram.react(222, 7, "👍") is True
    assert calls[-1]["message_id"] == 7
    await telegram.close()


@pytest.mark.asyncio
async def test_telegram_client_react_failure_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"ok": False, "description": "REACTION_INVALID"}
        )

    telegram = TelegramClient(
        "fake-token",
        base_url="https://telegram.test/botfake",
        transport=httpx.MockTransport(fail_handler),
    )
    with caplog.at_level(logging.WARNING):
        assert await telegram.react(222, 7, "👍") is False
    assert any("reaction" in record.message for record in caplog.records)
    await telegram.close()


@pytest.mark.asyncio
async def test_watcher_finish_reaction_failure_is_not_reported_as_devin_error(
    tmp_path: Path,
) -> None:
    class FakeDevin:
        async def get_session(self, _: str) -> SessionState:
            return SessionState(
                "finished",
                "title",
                None,
                [DevinMessage("devin_message", "event-1", "Done", None)],
            )

    class FakeTelegram:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send_message(
            self, chat_id: int, text: str, **kwargs: object
        ) -> None:
            self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

        async def send_markdown(
            self, chat_id: int, text: str, **kwargs: object
        ) -> list[dict[str, object]]:
            await self.send_message(chat_id, text, **kwargs)
            return [{**self.sent[-1], "message_id": len(self.sent)}]

        async def send_chat_action(self, *_: object, **__: object) -> None:
            return None

        async def set_message_reaction(
            self, _chat_id: int, _message_id: int, _emoji: str
        ) -> None:
            raise RuntimeError("Bad Request: REACTION_INVALID")

        async def react(
            self, chat_id: int, message_id: int | None, emoji: str | None
        ) -> bool:
            if message_id is None:
                return False
            try:
                await self.set_message_reaction(chat_id, message_id, emoji)
            except Exception:  # noqa: BLE001 - mirrors client
                return False
            return True

    now = 0.0

    def clock() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += max(seconds, 1.0)

    config = settings(
        tmp_path,
        devin_poll_seconds=1,
        devin_watch_timeout_seconds=20,
        devin_settle_seconds=30,
    )
    store = Store(":memory:")
    store.save_conversation(
        conv_key="222",
        chat_id=222,
        thread_id=None,
        session_id="s1",
        session_url="https://devin.test/s1",
        title="title",
    )
    conversation = store.get_conversation("222")
    assert conversation is not None
    telegram = FakeTelegram()
    await SessionWatcher(
        conversation,
        store,
        FakeDevin(),
        telegram,  # type: ignore[arg-type]
        config,
        clock=clock,
        sleep=sleep,
        trigger_message_id=7,
    ).run()
    texts = [item["text"] for item in telegram.sent]
    assert not any("Couldn't reach Devin" in text for text in texts)
