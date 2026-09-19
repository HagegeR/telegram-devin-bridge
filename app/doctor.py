from __future__ import annotations

import argparse
import asyncio
import json
import platform
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from app.config import Settings

Status = Literal["ok", "warn", "fail", "skip"]
Resolver = Callable[[str], Awaitable[object]]
Runner = Callable[[list[str]], Awaitable[str]]

HTTP_TIMEOUT = 10
_DNS_HINT = (
    "resolver flaky/ISP blocking — run a local cache (dnsmasq, see "
    "docs/deployment-alpine-tailscale.md) and put `nameserver 127.0.0.1` first"
)


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    hint: str = ""


def _mask(value: str | None) -> str:
    if not value:
        return "<unset>"
    return f"{value[:6]}…" if len(value) > 6 else f"{value}…"


def _redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, _mask(secret))
    return text


def _is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    return "replace-with" in value or value == "PLACEHOLDER"


def load_settings_or_error() -> Settings | CheckResult:
    """Load Settings, returning a fail CheckResult instead of raising."""
    try:
        return Settings()
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        return CheckResult(
            "env",
            "fail",
            f"settings failed to load: {detail}",
            "remove empty lines for optional int fields (TELEGRAM_HOME_CHANNEL) "
            "from .env",
        )


def check_env(settings: Settings) -> CheckResult:
    mode = settings.telegram_mode
    missing: list[str] = []
    if _is_placeholder(settings.telegram_bot_token):
        missing.append("TELEGRAM_BOT_TOKEN")
    if _is_placeholder(settings.devin_api_key):
        missing.append("DEVIN_API_KEY")
    if mode == "webhook":
        if _is_placeholder(settings.public_base_url):
            missing.append("PUBLIC_BASE_URL")
        if _is_placeholder(settings.telegram_webhook_secret):
            missing.append("TELEGRAM_WEBHOOK_SECRET")
    if missing:
        return CheckResult(
            "env",
            "fail",
            f"missing or placeholder values: {', '.join(sorted(missing))}",
            "fill real values in .env (see .env.example)",
        )
    public = settings.public_base_url or "<unset>"
    detail = f"mode={mode} public_base_url={public}"
    warnings: list[str] = []
    hints: list[str] = []
    if not settings.devin_api_key.startswith("apk_"):
        warnings.append(
            "DEVIN_API_KEY does not look like a Devin v1 API key "
            "(expected apk_user_… or apk_…); a `cog_` service-user token "
            "only works for DEVIN_SERVICE_USER_API_KEY / v3 usage"
        )
        hints.append("check DEVIN_API_KEY in .env")
    if not settings.telegram_allow_all_users and not settings.allowed_users:
        warnings.append("TELEGRAM_ALLOWED_USERS empty — nobody can use the bot")
        hints.append("set TELEGRAM_ALLOWED_USERS or TELEGRAM_ALLOW_ALL_USERS=true")
    if warnings:
        return CheckResult(
            "env",
            "warn",
            f"{detail}; {'; '.join(warnings)}",
            " | ".join(hints),
        )
    return CheckResult("env", "ok", detail)


async def check_dns(
    hosts: list[str],
    attempts: int = 5,
    *,
    resolve: Resolver | None = None,
) -> CheckResult:
    async def _resolve(host: str) -> object:
        if resolve is not None:
            return await resolve(host)
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.getaddrinfo(host, None), 5)

    failed: dict[str, int] = {}
    for host in hosts:
        misses = 0
        for _ in range(attempts):
            try:
                await _resolve(host)
            except (OSError, asyncio.TimeoutError):
                misses += 1
        if misses:
            failed[host] = misses
    if not failed:
        return CheckResult(
            "dns",
            "ok",
            f"resolved {len(hosts)} host(s), {attempts} attempt(s) each",
        )
    detail = "; ".join(
        f"{host}: {misses}/{attempts} lookups failed"
        for host, misses in failed.items()
    )
    if len(failed) == len(hosts) and all(
        misses == attempts for misses in failed.values()
    ):
        return CheckResult("dns", "fail", detail, _DNS_HINT)
    return CheckResult("dns", "warn", detail, _DNS_HINT)


def check_resolv_conf(path: str | Path = "/etc/resolv.conf") -> CheckResult:
    if platform.system() != "Linux":
        return CheckResult("resolv.conf", "skip", "not Linux")
    resolv = Path(path)
    if not resolv.is_file():
        return CheckResult("resolv.conf", "skip", f"{resolv} missing")
    nameservers = [
        line.split()[1]
        for line in resolv.read_text().splitlines()
        if line.startswith("nameserver") and len(line.split()) > 1
    ]
    if "100.100.100.100" in nameservers:
        return CheckResult(
            "resolv.conf",
            "warn",
            "nameserver 100.100.100.100 — Tailscale MagicDNS took over resolv.conf",
            "tailscale set --accept-dns=false",
        )
    return CheckResult(
        "resolv.conf", "ok", f"nameservers: {', '.join(nameservers) or 'none'}"
    )


async def check_telegram_api(
    client: httpx.AsyncClient, token: str
) -> CheckResult:
    try:
        response = await client.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=HTTP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        return CheckResult(
            "telegram api", "fail", _redact(f"transport error: {exc}", token)
        )
    if response.status_code == 200:
        username = response.json().get("result", {}).get("username", "?")
        return CheckResult("telegram api", "ok", f"getMe ok, bot @{username}")
    if response.status_code == 401:
        return CheckResult(
            "telegram api", "fail", "token rejected (401)", "check TELEGRAM_BOT_TOKEN"
        )
    return CheckResult("telegram api", "fail", f"getMe HTTP {response.status_code}")


async def check_devin_api(
    client: httpx.AsyncClient, api_key: str, base_url: str
) -> CheckResult:
    url = f"{base_url.rstrip('/')}/v1/sessions"
    try:
        response = await client.get(
            url,
            params={"limit": 1},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return CheckResult(
            "devin api", "fail", _redact(f"transport error: {exc}", api_key)
        )
    if response.status_code == 200:
        return CheckResult("devin api", "ok", "list sessions ok")
    if response.status_code in {401, 403}:
        return CheckResult(
            "devin api",
            "fail",
            f"key rejected ({response.status_code})",
            "check DEVIN_API_KEY",
        )
    return CheckResult(
        "devin api", "fail", f"list sessions HTTP {response.status_code}"
    )


async def check_local_health(
    client: httpx.AsyncClient, port: int = 8000
) -> CheckResult:
    try:
        response = await client.get(
            f"http://127.0.0.1:{port}/health", timeout=HTTP_TIMEOUT
        )
        ok = response.json() == {"status": "ok"}
    except (httpx.HTTPError, ValueError):
        ok = False
    if ok:
        return CheckResult("local health", "ok", f"127.0.0.1:{port}/health ok")
    return CheckResult(
        "local health",
        "fail",
        f"127.0.0.1:{port}/health not healthy",
        "service not running: rc-service telegram-devin-bridge status; "
        "tail /var/log/telegram-devin-bridge.log",
    )


async def check_public_health(
    client: httpx.AsyncClient, public_base_url: str
) -> CheckResult:
    url = f"{public_base_url.rstrip('/')}/health"
    try:
        response = await client.get(url, timeout=HTTP_TIMEOUT)
        ok = response.json() == {"status": "ok"}
    except (httpx.HTTPError, ValueError):
        ok = False
    if ok:
        return CheckResult("public health", "ok", f"{url} ok")
    return CheckResult(
        "public health",
        "fail",
        f"{url} not healthy",
        "tunnel/funnel down: `tailscale funnel status`, or DNS/TLS for the "
        "public host",
    )


async def check_webhook(
    client: httpx.AsyncClient,
    token: str,
    public_base_url: str | None,
    telegram_mode: str = "webhook",
) -> CheckResult:
    if telegram_mode == "polling":
        return CheckResult("webhook", "skip", "polling mode, no webhook")
    expected = f"{public_base_url.rstrip('/')}/telegram/webhook"
    try:
        response = await client.get(
            f"https://api.telegram.org/bot{token}/getWebhookInfo",
            timeout=HTTP_TIMEOUT,
        )
        payload = response.json().get("result", {})
    except (httpx.HTTPError, ValueError) as exc:
        return CheckResult(
            "webhook", "fail", _redact(f"getWebhookInfo failed: {exc}", token)
        )
    actual = payload.get("url", "")
    if actual != expected:
        return CheckResult(
            "webhook",
            "fail",
            f"registered url {actual or '<none>'} != {expected}",
            "run `python -m app.set_webhook`",
        )
    last_error = payload.get("last_error_message")
    pending = payload.get("pending_update_count", 0)
    if last_error or pending:
        detail_parts = [f"pending_update_count={pending}"]
        if last_error:
            detail_parts.append(f"last_error={last_error}")
            error_date = payload.get("last_error_date")
            if error_date:
                stamp = datetime.fromtimestamp(
                    int(error_date), tz=timezone.utc
                ).isoformat()
                detail_parts.append(f"at {stamp}")
        return CheckResult(
            "webhook",
            "warn",
            "; ".join(detail_parts),
            "Telegram is failing to deliver updates to the webhook URL",
        )
    return CheckResult("webhook", "ok", f"webhook -> {expected}")


async def _run_command(args: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} exited {process.returncode}")
    return stdout.decode(errors="replace")


_LINK_MTU_RE = re.compile(r"^\d+:\s+([^:@]+)[@:].*?\bmtu\s+(\d+)")


def _routes_from_json(
    routes: object, links: object
) -> tuple[list[str], dict[str, int]]:
    route_devs = [
        entry["dev"]
        for entry in routes
        if isinstance(entry, dict) and entry.get("dev")
    ]
    mtu_by_dev = {
        link["ifname"]: int(link["mtu"])
        for link in links
        if isinstance(link, dict) and link.get("ifname") and link.get("mtu")
    }
    return route_devs, mtu_by_dev


def _routes_from_text(
    route_output: str, link_output: str
) -> tuple[list[str], dict[str, int]]:
    route_devs: list[str] = []
    for line in route_output.splitlines():
        tokens = line.split()
        if tokens and tokens[0] == "default" and "dev" in tokens:
            route_devs.append(tokens[tokens.index("dev") + 1])
    mtu_by_dev: dict[str, int] = {}
    for line in link_output.splitlines():
        match = _LINK_MTU_RE.match(line)
        if match:
            mtu_by_dev[match.group(1)] = int(match.group(2))
    return route_devs, mtu_by_dev


async def check_network_routes(*, run: Runner | None = None) -> CheckResult:
    if platform.system() != "Linux":
        return CheckResult("network routes", "skip", "not Linux")
    if shutil.which("ip") is None:
        return CheckResult("network routes", "skip", "`ip` command not found")
    run = run or _run_command
    try:
        routes = json.loads(await run(["ip", "-j", "route", "show", "default"]))
        links = json.loads(await run(["ip", "-j", "link", "show"]))
        route_devs, mtu_by_dev = _routes_from_json(routes, links)
    except (OSError, RuntimeError, json.JSONDecodeError):
        # busybox `ip` has no -j; fall back to plain text output
        try:
            route_devs, mtu_by_dev = _routes_from_text(
                await run(["ip", "route", "show", "default"]),
                await run(["ip", "link", "show"]),
            )
        except (OSError, RuntimeError) as exc:
            return CheckResult(
                "network routes", "skip", f"cannot inspect routes: {exc}"
            )
    warnings: list[tuple[str, str]] = []
    devs = set(route_devs)
    if len(route_devs) > 1:
        warnings.append(
            (
                f"{len(route_devs)} default routes via {', '.join(sorted(devs))}",
                (
                    "a second NIC with a non-routable gateway blackholes "
                    'traffic — set NO_GATEWAY="ethX" in /etc/udhcpc/udhcpc.conf'
                ),
            )
        )
    big_mtu = sorted(
        dev for dev in devs if dev and (mtu_by_dev.get(dev) or 0) > 1500
    )
    if big_mtu:
        mtu_list = ", ".join(f"{dev}={mtu_by_dev.get(dev)}" for dev in big_mtu)
        warnings.append(
            (
                f"jumbo MTU on default-route interface(s): {mtu_list}",
                (
                    "jumbo MTU dropped frames on this LAN; "
                    "`ip link set dev ethX mtu 1500` and persist with post-up "
                    "in /etc/network/interfaces"
                ),
            )
        )
    if warnings:
        detail = "; ".join(text for text, _ in warnings)
        hint = " | ".join(hint for _, hint in warnings)
        return CheckResult("network routes", "warn", detail, hint)
    return CheckResult(
        "network routes",
        "ok",
        f"{len(route_devs)} default route(s) via {', '.join(sorted(devs))}",
    )


async def check_tailscale_funnel(
    public_base_url: str | None, *, run: Runner | None = None
) -> CheckResult:
    if not public_base_url:
        return CheckResult("tailscale funnel", "skip", "no public base url")
    if shutil.which("tailscale") is None:
        return CheckResult(
            "tailscale funnel", "skip", "tailscale binary not found"
        )
    host = urlparse(public_base_url).hostname or public_base_url
    run = run or _run_command
    try:
        output = await run(["tailscale", "funnel", "status"])
    except (OSError, RuntimeError) as exc:
        return CheckResult(
            "tailscale funnel",
            "fail",
            f"funnel status failed: {exc}",
            "`tailscale funnel --bg 8000`; needs HTTPS certs enabled and the "
            "`funnel` nodeAttr in the ACL (see docs)",
        )
    if host in output:
        return CheckResult(
            "tailscale funnel", "ok", f"funnel serving {host}"
        )
    return CheckResult(
        "tailscale funnel",
        "fail",
        f"funnel status does not mention {host}",
        "`tailscale funnel --bg 8000`; needs HTTPS certs enabled and the "
        "`funnel` nodeAttr in the ACL (see docs)",
    )


async def run_all(
    settings: Settings,
    *,
    client: httpx.AsyncClient,
    port: int = 8000,
    attempts: int = 5,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(check_env(settings))
    hosts = ["api.telegram.org", "api.devin.ai"]
    if settings.public_base_url:
        public_host = urlparse(settings.public_base_url).hostname
        if public_host:
            hosts.append(public_host)
    results.append(await check_dns(hosts, attempts=attempts))
    results.append(check_resolv_conf())
    results.append(await check_network_routes())
    results.append(await check_tailscale_funnel(settings.public_base_url))
    if _is_placeholder(settings.telegram_bot_token):
        results.append(
            CheckResult("telegram api", "skip", "TELEGRAM_BOT_TOKEN placeholder")
        )
        results.append(CheckResult("webhook", "skip", "no bot token"))
    else:
        results.append(
            await check_telegram_api(client, settings.telegram_bot_token)
        )
        if settings.public_base_url:
            results.append(
                await check_webhook(
                    client,
                    settings.telegram_bot_token,
                    settings.public_base_url,
                    settings.telegram_mode,
                )
            )
        else:
            results.append(CheckResult("webhook", "skip", "no public base url"))
    if _is_placeholder(settings.devin_api_key):
        results.append(
            CheckResult("devin api", "skip", "DEVIN_API_KEY placeholder")
        )
    else:
        results.append(
            await check_devin_api(
                client, settings.devin_api_key, settings.devin_api_base_url
            )
        )
    results.append(await check_local_health(client, port))
    if settings.public_base_url:
        results.append(await check_public_health(client, settings.public_base_url))
    else:
        results.append(CheckResult("public health", "skip", "no public base url"))
    return results


def register_doctor_route(
    application: FastAPI, settings: Settings, *, port: int = 8000
) -> None:
    @application.get("/doctor")
    async def doctor(request: Request) -> dict[str, object]:
        if settings.notify_secret is None:
            raise HTTPException(status_code=404, detail="Not found")
        authorization = request.headers.get("authorization", "")
        if authorization != f"Bearer {settings.notify_secret}":
            raise HTTPException(status_code=403, detail="Invalid bearer token")
        async with httpx.AsyncClient() as client:
            results = await run_all(
                settings, client=client, port=port, attempts=1
            )
        return {
            "results": [asdict(result) for result in results],
            "ok": all(result.status != "fail" for result in results),
        }


def _format(result: CheckResult) -> str:
    tag = result.status.upper()
    line = f"[{tag}] {result.name} — {result.detail}"
    if result.hint:
        line += f"\n    hint: {result.hint}"
    return line


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.doctor",
        description="Diagnose a telegram-devin-bridge deployment",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--public-url", default=None, help="override PUBLIC_BASE_URL")
    args = parser.parse_args()

    loaded = load_settings_or_error()
    results: list[CheckResult] = []

    async def gather() -> list[CheckResult]:
        async with httpx.AsyncClient() as client:
            if isinstance(loaded, CheckResult):
                collected = [loaded]
                hosts = ["api.telegram.org", "api.devin.ai"]
                public_host = (
                    urlparse(args.public_url).hostname if args.public_url else None
                )
                if public_host:
                    hosts.append(public_host)
                collected.append(await check_dns(hosts, attempts=args.attempts))
                collected.append(check_resolv_conf())
                collected.append(await check_network_routes())
                collected.append(await check_local_health(client, args.port))
                return collected
            settings = loaded
            if args.public_url:
                settings = settings.model_copy(
                    update={"public_base_url": args.public_url}
                )
            return await run_all(
                settings, client=client, port=args.port, attempts=args.attempts
            )

    results = asyncio.run(gather())
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            print(_format(result))
    return 1 if any(result.status == "fail" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
