from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app import doctor
from app.config import Settings
from app.doctor import CheckResult
from app.main import create_app
from app.publish_knowledge import parse_front_matter, publish


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": "real-token-abcdef",
        "telegram_webhook_secret": "real-secret-abcdef",
        "devin_api_key": "apk_user_abcdef123456",
        "public_base_url": "https://devin-bridge.example.ts.net",
        "database_path": str(tmp_path / "bridge.sqlite3"),
        "telegram_allowed_users": "111",
        "notify_secret": "notify-secret-abcdef",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_env_placeholders_fail_and_mask(tmp_path: Path) -> None:
    bad = settings(
        tmp_path,
        telegram_bot_token="replace-with-telegram-bot-token",
        devin_api_key="replace-with-devin-api-key",
        public_base_url="PLACEHOLDER",
    )
    result = doctor.check_env(bad)
    assert result.status == "fail"
    assert "TELEGRAM_BOT_TOKEN" in result.detail
    assert "replace-with-telegram-bot-token" not in result.detail
    assert "replace-with-telegram-bot-token" not in result.hint


def test_env_ok_lists_mode_and_masks(tmp_path: Path) -> None:
    result = doctor.check_env(settings(tmp_path))
    assert result.status == "ok"
    assert "webhook" in result.detail
    assert "real-token-abcdef" not in result.detail
    assert "apk_user_abcdef123456" not in result.detail


def test_env_warns_when_nobody_allowed(tmp_path: Path) -> None:
    result = doctor.check_env(
        settings(tmp_path, telegram_allowed_users="", telegram_allow_all_users=False)
    )
    assert result.status == "warn"


def test_env_warns_on_non_v1_devin_key(tmp_path: Path) -> None:
    result = doctor.check_env(settings(tmp_path, devin_api_key="cog_service_user_token"))
    assert result.status == "warn"
    assert "apk_" in result.detail
    assert "DEVIN_SERVICE_USER_API_KEY" in result.detail
    ok = doctor.check_env(settings(tmp_path, devin_api_key="apk_user_abc123"))
    assert ok.status == "ok"


def test_load_settings_or_error_empty_int(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("DEVIN_API_KEY", "x")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x.example")
    loaded = doctor.load_settings_or_error()
    assert isinstance(loaded, CheckResult)
    assert loaded.status == "fail"
    assert "TELEGRAM_HOME_CHANNEL" in loaded.hint


@pytest.mark.asyncio
async def test_dns_all_fail() -> None:
    async def fail(host: str) -> object:
        raise OSError("no dns")

    result = await doctor.check_dns(["a.example", "b.example"], 3, resolve=fail)
    assert result.status == "fail"
    assert "local cache" in result.hint


@pytest.mark.asyncio
async def test_dns_partial_warn() -> None:
    async def flaky(host: str) -> object:
        if host == "bad.example":
            raise OSError("no dns")
        return []

    result = await doctor.check_dns(
        ["bad.example", "good.example"], 2, resolve=flaky
    )
    assert result.status == "warn"


@pytest.mark.asyncio
async def test_dns_ok() -> None:
    async def fine(host: str) -> object:
        return []

    result = await doctor.check_dns(["good.example"], 2, resolve=fine)
    assert result.status == "ok"


def test_resolv_conf_magic_dns_warn(tmp_path: Path) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 100.100.100.100\nsearch tail12b72d.ts.net\n")
    result = doctor.check_resolv_conf(resolv)
    if result.status == "skip":
        pytest.skip("not Linux")
    assert result.status == "warn"
    assert "accept-dns=false" in result.hint


def test_resolv_conf_normal_ok(tmp_path: Path) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.1\noptions timeout:2 attempts:3\n")
    result = doctor.check_resolv_conf(resolv)
    if result.status == "skip":
        pytest.skip("not Linux")
    assert result.status == "ok"
    assert "127.0.0.1" in result.detail


def _route_run(routes: object, links: object):
    async def run(args: list[str]) -> str:
        if "route" in args:
            return json.dumps(routes)
        if "link" in args:
            return json.dumps(links)
        raise AssertionError(args)

    return run


@pytest.mark.asyncio
async def test_routes_two_defaults_warn() -> None:
    run = _route_run(
        [
            {"dst": "default", "gateway": "192.168.68.1", "dev": "eth0"},
            {"dst": "default", "gateway": "192.168.68.1", "dev": "eth1"},
        ],
        [
            {"ifname": "eth0", "mtu": 1500},
            {"ifname": "eth1", "mtu": 1500},
        ],
    )
    result = await doctor.check_network_routes(run=run)
    if result.status == "skip":
        pytest.skip("not Linux / no ip")
    assert result.status == "warn"
    assert "NO_GATEWAY" in result.hint


@pytest.mark.asyncio
async def test_routes_jumbo_mtu_warn() -> None:
    run = _route_run(
        [{"dst": "default", "gateway": "192.168.68.1", "dev": "eth0"}],
        [{"ifname": "eth0", "mtu": 9000}],
    )
    result = await doctor.check_network_routes(run=run)
    if result.status == "skip":
        pytest.skip("not Linux / no ip")
    assert result.status == "warn"
    assert "mtu 1500" in result.hint


@pytest.mark.asyncio
async def test_routes_single_default_ok() -> None:
    run = _route_run(
        [{"dst": "default", "gateway": "192.168.68.1", "dev": "eth0"}],
        [{"ifname": "eth0", "mtu": 1500}],
    )
    result = await doctor.check_network_routes(run=run)
    if result.status == "skip":
        pytest.skip("not Linux / no ip")
    assert result.status == "ok"


_BUSYBOX_ROUTE_OUT = (
    "default via 192.168.68.1 dev eth0  metric 202\n"
    "default via 192.168.68.1 dev eth1  metric 203\n"
)
_BUSYBOX_LINK_OUT = (
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 qdisc pfifo_fast "
    "state UP qlen 1000\n"
    "3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast "
    "state UP qlen 1000\n"
)


def _linux(monkeypatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/sbin/ip")


@pytest.mark.asyncio
async def test_routes_busybox_text_fallback_warn(monkeypatch) -> None:
    _linux(monkeypatch)

    async def run(args: list[str]) -> str:
        if "-j" in args:
            raise RuntimeError("ip -j: exited 1")
        if "route" in args:
            return _BUSYBOX_ROUTE_OUT
        if "link" in args:
            return _BUSYBOX_LINK_OUT
        raise AssertionError(args)

    result = await doctor.check_network_routes(run=run)
    assert result.status == "warn"
    assert "NO_GATEWAY" in result.hint
    assert "mtu 1500" in result.hint
    assert "eth0" in result.detail


@pytest.mark.asyncio
async def test_routes_busybox_text_fallback_ok(monkeypatch) -> None:
    _linux(monkeypatch)

    async def run(args: list[str]) -> str:
        if "-j" in args:
            raise RuntimeError("ip -j: exited 1")
        if "route" in args:
            return "default via 192.168.68.1 dev eth0  metric 202\n"
        if "link" in args:
            return (
                "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 "
                "qdisc pfifo_fast state UP qlen 1000\n"
            )
        raise AssertionError(args)

    result = await doctor.check_network_routes(run=run)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_exceptions_redact_token() -> None:
    token = "123456:secretbot-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPStatusError(
            f"error for https://api.telegram.org/bot{token}/getMe",
            request=request,
            response=httpx.Response(401),
        )

    async with _telegram_client(handler) as client:
        api_result = await doctor.check_telegram_api(client, token)
        hook_result = await doctor.check_webhook(
            client, token, "https://devin-bridge.example.ts.net"
        )
    for result in (api_result, hook_result):
        assert result.status == "fail"
        assert token not in result.detail
        assert token not in result.hint
        assert "123456…" in result.detail


@pytest.mark.asyncio
async def test_devin_exception_redacts_key() -> None:
    api_key = "devin-secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"key {api_key} in url https://api.devin.ai/v1/sessions")

    async with _telegram_client(handler) as client:
        result = await doctor.check_devin_api(client, api_key, "https://api.devin.ai")
    assert result.status == "fail"
    assert api_key not in result.detail


def _telegram_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_telegram_api_ok_and_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "bad-token" in request.url.path:
            return httpx.Response(401, json={"ok": False})
        return httpx.Response(
            200, json={"ok": True, "result": {"username": "bridgebot"}}
        )

    async with _telegram_client(handler) as client:
        ok = await doctor.check_telegram_api(client, "good-token")
        assert ok.status == "ok" and "@bridgebot" in ok.detail
        bad = await doctor.check_telegram_api(client, "bad-token")
        assert bad.status == "fail" and "rejected" in bad.detail


@pytest.mark.asyncio
async def test_devin_api_ok_and_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == "Bearer good-key":
            return httpx.Response(200, json={"sessions": []})
        return httpx.Response(401, json={})

    async with _telegram_client(handler) as client:
        ok = await doctor.check_devin_api(client, "good-key", "https://api.devin.ai")
        assert ok.status == "ok"
        bad = await doctor.check_devin_api(client, "bad-key", "https://api.devin.ai")
        assert bad.status == "fail"


@pytest.mark.asyncio
async def test_webhook_mismatch_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"url": "https://old.example/telegram/webhook"}}
        )

    async with _telegram_client(handler) as client:
        result = await doctor.check_webhook(
            client, "token", "https://devin-bridge.example.ts.net"
        )
    assert result.status == "fail"
    assert "set_webhook" in result.hint


@pytest.mark.asyncio
async def test_webhook_last_error_warn() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "url": "https://devin-bridge.example.ts.net/telegram/webhook",
                    "pending_update_count": 3,
                    "last_error_message": "Connection refused",
                    "last_error_date": 1758000000,
                },
            },
        )

    async with _telegram_client(handler) as client:
        result = await doctor.check_webhook(
            client, "token", "https://devin-bridge.example.ts.net"
        )
    assert result.status == "warn"
    assert "Connection refused" in result.detail
    assert "pending_update_count=3" in result.detail
    assert "T" in result.detail  # ISO timestamp


@pytest.mark.asyncio
async def test_webhook_polling_skip() -> None:
    async with _telegram_client(lambda r: httpx.Response(500)) as client:
        result = await doctor.check_webhook(
            client, "token", None, telegram_mode="polling"
        )
    assert result.status == "skip"


@pytest.mark.asyncio
async def test_doctor_route_auth(tmp_path: Path, monkeypatch) -> None:
    async def fake_run_all(*args, **kwargs):
        return [CheckResult("env", "ok", "fine")]

    monkeypatch.setattr(doctor, "run_all", fake_run_all)

    app_no_secret = create_app(settings=settings(tmp_path, notify_secret=None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_no_secret), base_url="http://test"
    ) as client:
        assert (await client.get("/doctor")).status_code == 404

    app = create_app(settings=settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/doctor")).status_code == 403
        assert (
            await client.get(
                "/doctor", headers={"Authorization": "Bearer wrong"}
            )
        ).status_code == 403
        response = await client.get(
            "/doctor", headers={"Authorization": "Bearer notify-secret-abcdef"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["results"][0]["name"] == "env"


def test_parse_front_matter() -> None:
    fields, body = parse_front_matter(
        "---\nname: bridge-runbook\ntrigger_description: use when deploying\n---\n\nBody here.\n"
    )
    assert fields == {
        "name": "bridge-runbook",
        "trigger_description": "use when deploying",
    }
    assert body == "Body here."


@pytest.mark.asyncio
async def test_publish_creates_when_missing() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"knowledge": []})
        return httpx.Response(200, json={"id": "kn-new"})

    async with _telegram_client(handler) as client:
        action, entry_id = await publish(
            client,
            "https://api.devin.ai",
            "key",
            name="bridge-runbook",
            body="body",
            trigger_description="desc",
        )
    assert action == "created"
    assert entry_id == "kn-new"
    assert ("POST", "/v1/knowledge") in calls


@pytest.mark.asyncio
async def test_publish_updates_existing() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200, json=[{"id": "kn-1", "name": "bridge-runbook"}]
            )
        return httpx.Response(200, json={"id": "kn-1"})

    async with _telegram_client(handler) as client:
        action, entry_id = await publish(
            client,
            "https://api.devin.ai",
            "key",
            name="bridge-runbook",
            body="body",
            trigger_description="desc",
        )
    assert action == "updated"
    assert entry_id == "kn-1"
    assert ("PUT", "/v1/knowledge/kn-1") in calls


@pytest.mark.asyncio
async def test_publish_put_fallback_to_post() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, json={"items": [{"id": "kn-9", "name": "bridge-runbook"}]}
            )
        if request.method == "PUT":
            return httpx.Response(405, json={})
        return httpx.Response(200, json={"id": "kn-9"})

    async with _telegram_client(handler) as client:
        action, _ = await publish(
            client,
            "https://api.devin.ai",
            "key",
            name="bridge-runbook",
            body="b",
            trigger_description="d",
        )
    assert action == "created"


@pytest.mark.asyncio
async def test_transport_retry_succeeds_after_flaky() -> None:
    from app.clients import DevinClient

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("dns blip")
        return httpx.Response(200, json={"items": []})

    client = DevinClient(
        "apk_key",
        "https://api.devin.ai",
        1,
        transport=httpx.MockTransport(handler),
    )
    result = await client.list_playbooks()
    assert result == []
    assert calls == 3


@pytest.mark.asyncio
async def test_transport_retry_exhausts() -> None:
    import app.clients as clients_mod
    from app.clients import DevinClient

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("always down")

    client = DevinClient(
        "apk_key",
        "https://api.devin.ai",
        1,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(httpx.ConnectError):
        await client._call("GET", "/v1/sessions")
    assert calls == clients_mod.RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_transport_retry_helper_uses_sleep() -> None:
    import app.clients as clients_mod

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    attempts = 0

    async def send() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 5:
            raise httpx.ConnectError("flaky")
        return httpx.Response(200)

    response = await clients_mod._with_transport_retry(send, sleep=fake_sleep)
    assert response.status_code == 200
    assert attempts == 5
    assert slept == list(clients_mod.RETRY_BACKOFF)


@pytest.mark.asyncio
async def test_self_update_admin_and_check_arg(tmp_path: Path) -> None:
    from app.main import create_app

    app = create_app(settings=settings(tmp_path, telegram_admin_user_ids="42"))
    runtime = app.state.bridge

    sent: list[str] = []

    async def fake_send(message, text, **kwargs):
        sent.append(text)
        return 1

    runtime.send_text = fake_send  # type: ignore[assignment]

    commands: list[str] = []

    async def fake_run(command: str, cwd) -> tuple[int, str]:
        commands.append(command)
        return 0, "up to date at abc1234 (main)" + chr(10)

    runtime._run_shell = fake_run

    admin_msg = {"from": {"id": 42}, "chat": {"id": 5}}
    await runtime.self_update(admin_msg, "")
    assert commands[-1] == "sh deploy/self-update.sh"
    assert "abc1234" in sent[-1]
    assert sent[-1].startswith("```")

    await runtime.self_update(admin_msg, "check")
    assert commands[-1] == "sh deploy/self-update.sh --check"

    sent.clear()
    stranger_msg = {"from": {"id": 7}, "chat": {"id": 5}}
    await runtime.self_update(stranger_msg, "")
    assert sent == []


@pytest.mark.asyncio
async def test_timeout_exception_names_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")

    async with _telegram_client(handler) as client:
        local = await doctor.check_local_health(client, 8000)
        public = await doctor.check_public_health(
            client, "https://devin-bridge.example.ts.net"
        )
    assert local.status == "fail" and "ConnectTimeout" in local.detail
    assert public.status == "fail" and "ConnectTimeout" in public.detail
