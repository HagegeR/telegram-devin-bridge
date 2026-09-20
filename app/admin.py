from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request

import app.doctor as doctor_module
from app.config import Settings

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RATE_LIMIT = 10  # requests per minute

ACTIONS = ("doctor", "logs", "get-env", "set-env", "restart", "update")


class AdminRuntime(Protocol):
    async def notify(
        self,
        text: str,
        *,
        chat_id: int | None,
        thread_id: int | None,
        silent: bool,
        markdown: bool,
    ) -> int: ...


RunShell = Callable[[list[str], Path], Awaitable[tuple[int, str]]]
SpawnShell = Callable[[str], Awaitable[object]]
Clock = Callable[[], float]


async def _default_spawn(command: str) -> object:
    return await asyncio.create_subprocess_shell(command)


def _parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


def _write_env_key(env_path: Path, key: str, value: str) -> None:
    lines = (
        env_path.read_text(encoding="utf-8").splitlines()
        if env_path.exists()
        else []
    )
    out: list[str] = []
    written = False
    for line in lines:
        stripped = line.strip()
        if (
            not stripped.startswith("#")
            and stripped.partition("=")[0].strip() == key
        ):
            if not written:
                out.append(f"{key}={value}")
                written = True
            continue
        out.append(line)
    if not written:
        out.append(f"{key}={value}")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(env_path.parent), prefix=".env.", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(out) + "\n")
        if env_path.exists():
            os.chmod(tmp_name, env_path.stat().st_mode & 0o777)
        os.replace(tmp_name, env_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _tail_lines(path: Path, count: int) -> list[str]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\n") for line in deque(handle, maxlen=count)]


def _redact_text(text: str, settings: Settings) -> str:
    secrets = [
        settings.telegram_bot_token,
        settings.devin_api_key,
        settings.devin_service_user_api_key,
        settings.telegram_webhook_secret,
        settings.notify_secret,
        settings.doctor_secret,
        settings.admin_secret,
    ]
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return _BOT_TOKEN_RE.sub("bot***", text)


def register_admin_route(
    application: FastAPI,
    runtime: AdminRuntime,
    settings: Settings,
    *,
    port: int = 8000,
    run_shell: RunShell,
    spawn_shell: SpawnShell = _default_spawn,
    clock: Clock = time.monotonic,
) -> None:
    request_times: deque[float] = deque()

    def _check_rate() -> None:
        now = clock()
        while request_times and now - request_times[0] > 60:
            request_times.popleft()
        if len(request_times) >= _RATE_LIMIT:
            raise HTTPException(status_code=429, detail="admin rate limit")
        request_times.append(now)

    @application.post("/admin")
    async def admin(request: Request) -> dict[str, object]:
        if settings.admin_secret is None:
            raise HTTPException(status_code=404, detail="Not found")
        authorization = request.headers.get("authorization", "")
        if authorization != f"Bearer {settings.admin_secret}":
            raise HTTPException(status_code=403, detail="Invalid bearer token")
        _check_rate()
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        action = payload.get("action")
        key = payload.get("key")
        if action not in ACTIONS:
            raise HTTPException(
                status_code=400, detail=f"unknown action; one of {list(ACTIONS)}"
            )
        client_host = request.client.host if request.client else "-"
        logger.info(
            "admin action=%s key=%s from=%s",
            action,
            key if isinstance(key, str) else "-",
            client_host,
        )
        try:
            await runtime.notify(
                f"Admin API: {action}{' ' + str(key) if key else ''}",
                chat_id=None,
                thread_id=None,
                silent=True,
                markdown=False,
            )
        except (ValueError, RuntimeError, httpx.HTTPError):
            pass

        if action == "doctor":
            async with httpx.AsyncClient() as client:
                results = await doctor_module.run_all(
                    settings, client=client, port=port, attempts=1
                )
            return {
                "results": [asdict(result) for result in results],
                "ok": all(result.status != "fail" for result in results),
            }

        if action == "logs":
            count = payload.get("lines", 100)
            if not isinstance(count, int):
                raise HTTPException(status_code=400, detail="lines must be int")
            count = max(1, min(500, count))
            log_path = Path(settings.admin_log_path)
            if not log_path.is_absolute():
                log_path = _REPO_ROOT / log_path
            if not log_path.is_file():
                raise HTTPException(status_code=404, detail="log not found")
            lines = await asyncio.to_thread(_tail_lines, log_path, count)
            return {"lines": [_redact_text(line, settings) for line in lines]}

        env_path = Path(settings.admin_env_path)
        if not env_path.is_absolute():
            env_path = _REPO_ROOT / env_path

        if action == "get-env":
            env = (
                _parse_env(env_path.read_text(encoding="utf-8"))
                if env_path.is_file()
                else {}
            )
            return {
                "env": {
                    k: v for k, v in env.items() if k in settings.admin_env_keys
                }
            }

        if action == "set-env":
            value = payload.get("value")
            if (
                not isinstance(key, str)
                or not _ENV_KEY_RE.match(key)
                or key not in settings.admin_env_keys
            ):
                raise HTTPException(status_code=400, detail="key not allowed")
            if (
                not isinstance(value, str)
                or "\n" in value
                or "\r" in value
                or len(value) > 2000
            ):
                raise HTTPException(status_code=400, detail="invalid value")
            await asyncio.to_thread(_write_env_key, env_path, key, value)
            return {"key": key, "restart_required": True}

        if action == "restart":
            await spawn_shell(settings.admin_restart_command)
            return {"scheduled": True}

        # action == "update"
        argv = shlex.split(settings.self_update_command)
        script = next(
            (token for token in argv if (_REPO_ROOT / token).is_file()),
            None,
        )
        if (
            script is None
            or not (_REPO_ROOT / ".git").exists()
            or not (_REPO_ROOT / script)
            .resolve()
            .is_relative_to(_REPO_ROOT.resolve())
        ):
            raise HTTPException(status_code=409, detail="not a git checkout")
        exit_code, output = await run_shell(argv, _REPO_ROOT)
        return {"exit_code": exit_code, "output": output[-3000:]}
