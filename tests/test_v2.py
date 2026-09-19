from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.access import is_allowed, is_topic_chat, should_respond_in_group
from app.clients import DevinClient, DevinMessage, SessionState, TelegramClient
from app.commands import handle_command
from app.config import Settings
from app.formatting import (
    chunk,
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
    telegram_paths: list[str] = []

    async def devin_handler(request: httpx.Request) -> httpx.Response:
        devin_paths.append(request.url.path)
        if request.url.path == "/v1/sessions":
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


class _FakeDevin:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.terminated: list[str] = []

    async def create_session(
        self, prompt: str, title: str, playbook_id: str | None = None
    ) -> tuple[str, str]:
        self.created.append(prompt)
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

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_concurrent_first_messages_share_one_session(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "lock.sqlite3"))
    devin = _FakeDevin()
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, devin, telegram)  # type: ignore[arg-type]
    await asyncio.gather(
        runtime.handle_user_turn(message("one", message_id=1), "one"),
        runtime.handle_user_turn(message("two", message_id=2), "two"),
    )
    assert len(devin.created) == 1
    assert {text for _, text in devin.sent} == {"two"}
    assert "one" in devin.created[0]
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
            self, prompt: str, title: str, playbook_id: str | None = None
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
    telegram = _FakeTelegram()
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(runtime, message("/close"), "/close")
    assert "This command only works inside a topic." in telegram.sent[-1]["text"]
    topic_message = {**message("/rename New name"), "message_thread_id": 9}
    await handle_command(runtime, topic_message, "/rename New name")
    assert telegram.edited_topics == [(222, 9, "New name")]


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
    watcher.last_status = "working"
    runtime.active_watchers["s1"] = watcher
    task = asyncio.create_task(asyncio.sleep(10))
    runtime.watchers["s1"] = task
    await runtime._queue_turn(message("next", message_id=9), "next", None)
    assert runtime.queued_count("222") == 1
    assert telegram.reactions[-1] == "⏳"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    runtime.watchers.pop("s1", None)
    runtime.active_watchers.pop("s1", None)
    await runtime._drain_queue("222")
    assert runtime.queued_count("222") == 0
    assert telegram.reactions[-1] == "👀"
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


@pytest.mark.asyncio
async def test_get_updates_accepts_list_result() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(cast(dict[str, object], json.loads(request.content)))
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
