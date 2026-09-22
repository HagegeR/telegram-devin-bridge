from __future__ import annotations

import asyncio
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
        "doctor_secret": "doctor-secret-abcdef",
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


def test_admin_user_ids_fall_back_to_allowed_users(tmp_path: Path) -> None:
    fallback = settings(
        tmp_path,
        telegram_allowed_users="111,222",
        telegram_admin_user_ids="",
    )
    assert fallback.admin_user_ids == frozenset({111, 222})

    explicit = settings(
        tmp_path,
        telegram_allowed_users="111,222",
        telegram_admin_user_ids="42",
    )
    assert explicit.admin_user_ids == frozenset({42})


def test_load_settings_or_error_empty_optional_reads_unset(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("DEVIN_API_KEY", "x")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x.example")
    loaded = doctor.load_settings_or_error()
    assert isinstance(loaded, Settings)
    assert loaded.telegram_home_channel is None


def test_load_settings_or_error_bad_int(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "abc")
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


@pytest.mark.asyncio
async def test_dns_rejects_non_positive_attempts() -> None:
    with pytest.raises(ValueError, match="attempts must be >= 1"):
        await doctor.check_dns(["good.example"], attempts=0)


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
async def test_telegram_api_unexpected_json_shape() -> None:
    async with _telegram_client(
        lambda r: httpx.Response(200, json=[])
    ) as client:
        result = await doctor.check_telegram_api(client, "token")
    assert result == CheckResult(
        "telegram api", "fail", "getMe returned unexpected JSON shape"
    )


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
async def test_webhook_unexpected_json_shape() -> None:
    async with _telegram_client(
        lambda r: httpx.Response(200, json={"result": []})
    ) as client:
        result = await doctor.check_webhook(
            client, "token", "https://devin-bridge.example.ts.net"
        )
    assert result == CheckResult(
        "webhook", "fail", "getWebhookInfo returned unexpected JSON shape"
    )


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

    app_no_secret = create_app(settings=settings(tmp_path, doctor_secret=None))
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
            "/doctor", headers={"Authorization": "Bearer doctor-secret-abcdef"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["results"][0]["name"] == "env"
        again = await client.get(
            "/doctor", headers={"Authorization": "Bearer doctor-secret-abcdef"}
        )
        assert again.status_code == 429


@pytest.mark.asyncio
async def test_doctor_route_cooldown_admits_only_one_concurrent_request(
    tmp_path: Path, monkeypatch
) -> None:
    calls = 0

    async def fake_run_all(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return [CheckResult("env", "ok", "fine")]

    monkeypatch.setattr(doctor, "run_all", fake_run_all)
    app = create_app(settings=settings(tmp_path))
    headers = {"Authorization": "Bearer doctor-secret-abcdef"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            client.get("/doctor", headers=headers),
            client.get("/doctor", headers=headers),
        )
    assert sorted(response.status_code for response in responses) == [200, 429]
    assert calls == 1


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
async def test_publish_put_unsupported_raises() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200, json={"items": [{"id": "kn-9", "name": "bridge-runbook"}]}
            )
        if request.method == "PUT":
            return httpx.Response(405, json={})
        return httpx.Response(200, json={"id": "kn-9"})

    async with _telegram_client(handler) as client:
        with pytest.raises(
            RuntimeError,
            match="knowledge entry 'kn-9' exists but PUT returned 405; update it manually",
        ):
            await publish(
                client,
                "https://api.devin.ai",
                "key",
                name="bridge-runbook",
                body="b",
                trigger_description="d",
            )
    assert calls == ["GET", "PUT"]


@pytest.mark.asyncio
async def test_transport_retry_succeeds_after_flaky() -> None:
    from app.clients import DevinClient

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("dns blip")
        return httpx.Response(200, json=[{"playbook_id": "pb-1", "title": "Deploy"}])

    client = DevinClient(
        "apk_key",
        "https://api.devin.ai",
        1,
        transport=httpx.MockTransport(handler),
    )
    result = await client.list_playbooks()
    assert [(p.playbook_id, p.title) for p in result] == [("pb-1", "Deploy")]
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
async def test_send_photo_retries_connect_error(monkeypatch) -> None:
    import app.clients as clients_mod
    from app.clients import TelegramClient

    monkeypatch.setattr(clients_mod, "RETRY_BACKOFF", (0, 0, 0, 0))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = TelegramClient(
        "token",
        base_url="https://api.telegram.org/bottoken",
        transport=httpx.MockTransport(handler),
    )
    try:
        from tests.test_photo_limits import png

        result = await client.send_photo(1, "photo.png", png(4, 4))
    finally:
        await client.client.aclose()
    assert result == {"message_id": 1}
    assert calls == 2


@pytest.mark.asyncio
async def test_send_document_does_not_retry_read_error() -> None:
    from app.clients import TelegramClient

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadError("mid-flight")

    client = TelegramClient(
        "token",
        base_url="https://api.telegram.org/bottoken",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.ReadError):
            await client.send_document(1, "document.txt", b"text")
    finally:
        await client.client.aclose()
    assert calls == 1


@pytest.mark.asyncio
async def test_download_attachment_retries_read_error(monkeypatch) -> None:
    import app.clients as clients_mod
    from app.clients import DevinClient

    monkeypatch.setattr(clients_mod, "RETRY_BACKOFF", (0, 0, 0, 0))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadError("mid-flight")
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    client = DevinClient(
        "apk_key",
        "https://api.devin.ai",
        1,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.download_attachment(
            "https://app.devin.ai/attachments/1/file.png"
        )
    finally:
        await client.close()
    assert result == (b"png", "image/png")
    assert calls == 2


@pytest.mark.asyncio
async def test_self_update_admin_and_check_arg(tmp_path: Path) -> None:
    from app.main import create_app

    app = create_app(settings=settings(tmp_path, telegram_admin_user_ids="42"))
    runtime = app.state.bridge

    sent: list[str] = []
    send_kwargs: list[dict[str, object]] = []

    async def fake_send(message, text, **kwargs):
        sent.append(text)
        send_kwargs.append(kwargs)
        return 1

    runtime.send_text = fake_send  # type: ignore[assignment]

    commands: list[list[str]] = []

    async def fake_run(command: list[str], cwd, env=None) -> tuple[int, str]:
        commands.append(command)
        return 0, "up to date at abc1234 (main)" + chr(10)

    runtime._run_command = fake_run

    admin_msg = {"from": {"id": 42}, "chat": {"id": 5}}
    await runtime.self_update(admin_msg, "")
    assert commands[-1] == ["sh", "deploy/self-update.sh"]
    assert "abc1234" in sent[-1]
    assert sent[-1].startswith("```")

    await runtime.self_update(admin_msg, "check")
    assert commands[-1] == ["sh", "deploy/self-update.sh", "--check"]

    await runtime.self_update(admin_msg, "stable")
    assert commands[-1] == ["sh", "deploy/self-update.sh", "stable"]

    await runtime.self_update(admin_msg, "check v1.2")
    assert commands[-1] == ["sh", "deploy/self-update.sh", "--check", "v1.2"]

    sent.clear()
    await runtime.self_update(admin_msg, "release+candidate")
    assert commands[-1] == ["sh", "deploy/self-update.sh", "release+candidate"]

    sent.clear()
    await runtime.self_update(admin_msg, "foo..bar")
    assert commands[-1] == ["sh", "deploy/self-update.sh", "release+candidate"]
    assert sent[0].startswith("Usage:")

    sent.clear()
    stranger_msg = {"from": {"id": 7}, "chat": {"id": 5}}
    previous_commands = list(commands)
    await runtime.self_update(stranger_msg, "")
    assert sent == [
        (
            "Admins only. Add your Telegram user id (see /whoami) to "
            "TELEGRAM_ADMIN_USER_IDS (or TELEGRAM_ALLOWED_USERS) and restart "
            "the bridge."
        )
    ]
    assert send_kwargs[-1]["ephemeral"] is True
    assert commands == previous_commands


@pytest.mark.asyncio
async def test_webhook_startup_refreshes_bot_commands(
    tmp_path: Path, monkeypatch
) -> None:
    from app import main as main_module

    app = create_app(settings=settings(tmp_path))
    runtime = app.state.bridge
    configured: list[object] = []

    async def fake_configure(telegram: object) -> None:
        configured.append(telegram)

    async def fake_startup() -> None:
        return None

    async def fake_shutdown() -> None:
        return None

    monkeypatch.setattr(main_module, "configure_bot", fake_configure)
    runtime.startup = fake_startup  # type: ignore[method-assign]
    runtime.shutdown = fake_shutdown  # type: ignore[method-assign]
    async with app.router.lifespan_context(app):
        pass
    assert configured == [runtime.telegram]


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


@pytest.mark.asyncio
async def test_retry_idempotent_gating() -> None:
    import app.clients as clients_mod
    from app.clients import DevinClient

    async def no_sleep(seconds: float) -> None:
        return None

    # ReadError propagates immediately when not idempotent
    calls = 0

    def always_read_err(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadError("mid-flight")

    client = DevinClient(
        "apk_key", "https://api.devin.ai", 1,
        transport=httpx.MockTransport(always_read_err),
    )
    with pytest.raises(httpx.ReadError):
        await client._call("POST", "/v1/sessions")  # non-idempotent
    assert calls == 1

    calls = 0
    with pytest.raises(httpx.ReadError):
        await client._call("GET", "/v1/sessions")  # idempotent
    assert calls == clients_mod.RETRY_ATTEMPTS

    # ConnectError retries in both modes
    calls = 0

    def always_conn_err(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("dns")

    client2 = DevinClient(
        "apk_key", "https://api.devin.ai", 1,
        transport=httpx.MockTransport(always_conn_err),
    )
    with pytest.raises(httpx.ConnectError):
        await client2._call("POST", "/v1/sessions")
    assert calls == clients_mod.RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_getme_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    async with _telegram_client(handler) as client:
        result = await doctor.check_telegram_api(client, "token")
    assert result.status == "fail"
    assert "non-JSON" in result.detail


def test_env_no_warn_when_chat_allowlist(tmp_path: Path) -> None:
    result = doctor.check_env(
        settings(
            tmp_path,
            telegram_allowed_users="",
            telegram_allow_all_users=False,
            telegram_allowed_chat_ids="555",
        )
    )
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_run_all_uses_custom_devin_dns_host(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    async def fake_dns(hosts, attempts=5, **kwargs):
        captured["hosts"] = list(hosts)
        return CheckResult("dns", "ok", "stub")

    monkeypatch.setattr(doctor, "check_dns", fake_dns)
    cfg = settings(tmp_path, devin_api_base_url="https://devin.internal.corp")
    async with _telegram_client(lambda r: httpx.Response(401, json={})) as client:
        await doctor.run_all(cfg, client=client, port=1, attempts=1)
    assert "devin.internal.corp" in captured["hosts"]
    assert "api.devin.ai" not in captured["hosts"]


@pytest.mark.asyncio
async def test_run_all_polling_skips_public_checks_and_host(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, list[str]] = {"dns": [], "requests": []}

    async def fake_dns(hosts, attempts=5, **kwargs):
        captured["dns"] = list(hosts)
        return CheckResult("dns", "ok", "stub")

    async def fake_routes(*args, **kwargs):
        return CheckResult("network routes", "ok", "stub")

    monkeypatch.setattr(doctor, "check_dns", fake_dns)
    monkeypatch.setattr(doctor, "check_network_routes", fake_routes)
    monkeypatch.setattr(
        doctor,
        "check_resolv_conf",
        lambda: CheckResult("resolv.conf", "ok", "stub"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(request.url.host or "")
        if request.url.host == "api.telegram.org":
            return httpx.Response(200, json={"ok": True, "result": {"username": "bot"}})
        if request.url.host == "api.devin.ai":
            return httpx.Response(200, json={"sessions": []})
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(request.url)

    cfg = settings(
        tmp_path,
        telegram_mode="polling",
        public_base_url="https://public-placeholder.example",
    )
    async with _telegram_client(handler) as client:
        results = await doctor.run_all(cfg, client=client, port=8000, attempts=1)
    assert "public-placeholder.example" not in captured["dns"]
    assert "public-placeholder.example" not in captured["requests"]
    assert [(result.name, result.status, result.detail) for result in results if result.name in {
        "tailscale funnel", "webhook", "public health"
    }] == [
        ("tailscale funnel", "skip", "polling mode"),
        ("webhook", "skip", "polling mode"),
        ("public health", "skip", "polling mode"),
    ]
    assert not any(result.status == "fail" for result in results)


def test_publish_dry_run_no_env(tmp_path: Path, monkeypatch, capsys) -> None:
    import sys

    from app import publish_knowledge

    for var in ("TELEGRAM_BOT_TOKEN", "DEVIN_API_KEY", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    doc = tmp_path / "k.md"
    doc.write_text("---\nname: kb\ntrigger_description: d\n---\n\nBody\n")
    monkeypatch.setattr(
        sys, "argv", ["publish_knowledge", "--file", str(doc), "--dry-run"]
    )
    assert publish_knowledge.main() == 0
    out = capsys.readouterr().out
    assert '"name": "kb"' in out


@pytest.mark.asyncio
async def test_self_update_unavailable_without_git(tmp_path: Path) -> None:
    from app.main import create_app

    app = create_app(
        settings=settings(
            tmp_path,
            telegram_admin_user_ids="42",
            self_update_command="sh /nonexistent/self-update.sh",
        )
    )
    runtime = app.state.bridge
    sent: list[str] = []

    async def fake_send(message, text, **kwargs):
        sent.append(text)
        return 1

    runtime.send_text = fake_send  # type: ignore[assignment]
    await runtime.self_update({"from": {"id": 42}, "chat": {"id": 5}}, "")
    assert "unavailable" in sent[-1]


@pytest.mark.asyncio
async def test_self_update_sanitizes_output(tmp_path: Path) -> None:
    from app.main import create_app

    app = create_app(settings=settings(tmp_path, telegram_admin_user_ids="42"))
    runtime = app.state.bridge
    sent: list[str] = []

    async def fake_send(message, text, **kwargs):
        sent.append(text)
        return 1

    runtime.send_text = fake_send  # type: ignore[assignment]

    async def fake_run(command, cwd, env=None):
        return 0, "evil ` injection " + chr(0x1B) + "[31mred" + chr(10)

    runtime._run_command = fake_run
    await runtime.self_update({"from": {"id": 42}, "chat": {"id": 5}}, "")
    body = sent[-1].strip("`").strip()
    assert "`" not in body
    assert chr(0x1B) not in body


@pytest.mark.asyncio
async def test_self_update_rejects_script_outside_repo(
    tmp_path: Path, monkeypatch
) -> None:
    from app import main as main_module
    from app.main import create_app

    evil = tmp_path / "evil.sh"
    evil.write_text("#!/bin/sh\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(main_module, "_REPO_ROOT", repo)
    app = create_app(
        settings=settings(
            tmp_path,
            telegram_admin_user_ids="42",
            self_update_command=f"sh {evil}",
        )
    )
    runtime = app.state.bridge
    sent: list[str] = []
    calls = 0

    async def fake_send(message, text, **kwargs):
        sent.append(text)
        return 1

    async def fake_run(command, cwd, env=None):
        nonlocal calls
        calls += 1
        return 0, ""

    runtime.send_text = fake_send  # type: ignore[assignment]
    runtime._run_command = fake_run
    await runtime.self_update({"from": {"id": 42}, "chat": {"id": 5}}, "")
    assert "unavailable" in sent[-1]
    assert calls == 0
