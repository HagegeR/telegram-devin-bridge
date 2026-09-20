from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.admin import register_admin_route
from app.config import Settings


def settings(tmp_path: Path, **overrides: object) -> Settings:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TELEGRAM_ALLOWED_USERS=111\n"
        "# a comment\n"
        "TELEGRAM_BOT_TOKEN=bot123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "DEVIN_MAX_ACU_LIMIT=3\n"
    )
    log_path = tmp_path / "bridge.log"
    log_path.write_text(
        "INFO ok\n"
        "token bot123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA used\n"
        "key apk_user_abcdef123456 here\n"
    )
    values: dict[str, object] = {
        "telegram_bot_token": "bot123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "telegram_webhook_secret": "secret-placeholder",
        "devin_api_key": "apk_user_abcdef123456",
        "public_base_url": "https://bridge.example.ts.net",
        "database_path": str(tmp_path / "bridge.sqlite3"),
        "telegram_allowed_users": "111",
        "notify_secret": "notify-placeholder",
        "admin_secret": "admin-secret-abcdef",
        "admin_env_path": str(env_path),
        "admin_log_path": str(log_path),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_app(tmp_path: Path, **overrides: object):
    spawned: list[str] = []
    ran: list[tuple[str, Path]] = []
    notices: list[str] = []
    clock = {"t": 1000.0}

    async def fake_spawn(command: str) -> object:
        spawned.append(command)
        return None

    async def fake_run(argv: list[str], cwd: Path) -> tuple[int, str]:
        ran.append((" ".join(argv), cwd))
        return 0, "up to date"

    async def fake_notify(text: str, **kwargs) -> int:
        notices.append(text)
        return 0

    runtime = SimpleNamespace(notify=fake_notify)
    app = FastAPI()
    register_admin_route(
        app,
        runtime,  # type: ignore[arg-type]
        settings(tmp_path, **overrides),
        port=8000,
        run_shell=fake_run,
        spawn_shell=fake_spawn,
        clock=lambda: clock["t"],
    )
    return app, spawned, ran, clock, notices


def authed(client: httpx.AsyncClient, body: dict):
    return client.post(
        "/admin",
        json=body,
        headers={"Authorization": "Bearer admin-secret-abcdef"},
    )


@pytest.mark.asyncio
async def test_admin_404_without_secret(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path, admin_secret=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/admin", json={"action": "logs"})
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_403_bad_bearer(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin",
            json={"action": "logs"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_unknown_action(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await authed(client, {"action": "rm-rf"})
        assert response.status_code == 400
        assert "doctor" in response.json()["detail"]


@pytest.mark.asyncio
async def test_admin_set_env_allowed(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await authed(
            client,
            {"action": "set-env", "key": "DEVIN_MAX_ACU_LIMIT", "value": "5"},
        )
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    env_text = (tmp_path / ".env").read_text()
    assert "DEVIN_MAX_ACU_LIMIT=5" in env_text
    assert "# a comment" in env_text
    assert "TELEGRAM_ALLOWED_USERS=111" in env_text


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="chmod mode bits")
async def test_admin_set_env_mode_preserved(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    os.chmod(env_path, 0o600)
    app, *_ = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await authed(
            client,
            {"action": "set-env", "key": "BOT_USERNAME", "value": "mybot"},
        )
    assert env_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_admin_set_env_disallowed_and_bad_value(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await authed(
                client,
                {"action": "set-env", "key": "TELEGRAM_BOT_TOKEN", "value": "x"},
            )
        ).status_code == 400
        assert (
            await authed(
                client,
                {"action": "set-env", "key": "BOT_USERNAME", "value": "a\nb"},
            )
        ).status_code == 400


@pytest.mark.asyncio
async def test_admin_get_env_allowlist_only(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await authed(client, {"action": "get-env"})
    env = response.json()["env"]
    assert env["DEVIN_MAX_ACU_LIMIT"] == "3"
    assert env["TELEGRAM_ALLOWED_USERS"] == "111"
    assert "TELEGRAM_BOT_TOKEN" not in env


@pytest.mark.asyncio
async def test_admin_logs_redacts(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await authed(client, {"action": "logs", "lines": 50})
    assert response.status_code == 200
    text = "\n".join(response.json()["lines"])
    assert "bot123456:AAAA" not in text
    assert "apk_user_abcdef123456" not in text
    assert "***" in text
    assert "INFO ok" in text


@pytest.mark.asyncio
async def test_admin_restart_and_update(tmp_path: Path) -> None:
    app, spawned, ran, _, notices = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await authed(client, {"action": "restart"})
        assert response.status_code == 200
        assert response.json()["scheduled"] is True
        assert len(spawned) == 1

        response = await authed(client, {"action": "update"})
        assert response.status_code == 200
        assert response.json()["exit_code"] == 0
        assert ran and "self-update" in ran[-1][0]
    assert any("Admin API" in n for n in notices)


@pytest.mark.asyncio
async def test_admin_rate_limit(tmp_path: Path) -> None:
    app, *_ = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        codes = [
            (await authed(client, {"action": "get-env"})).status_code
            for _ in range(11)
        ]
    assert codes[:10] == [200] * 10
    assert codes[10] == 429
