from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import secrets
import shlex
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx
from dotenv import dotenv_values
from dotenv.parser import parse_stream
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

import app.doctor as doctor_module
from app.config import Settings
from app.store import Store

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SAFE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@,+-]*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_NEVER_ENV_KEYS = frozenset(
    {
        "SELF_UPDATE_COMMAND",
        "SELF_UPDATE_BRANCH",
        "SELF_UPDATE_CHANNEL",
        "ADMIN_RESTART_COMMAND",
        "ADMIN_ENV_ALLOWLIST",
        "ADMIN_ENV_PATH",
        "ADMIN_LOG_PATH",
        "ADMIN_SECRET",
    }
)
_RATE_LIMIT = 10  # requests per minute
_AUTH_FAIL_DELAY = 1.0

ACTIONS = (
    "doctor",
    "logs",
    "get-env",
    "set-env",
    "restart",
    "update",
    "db-check",
    "backup",
)


class AdminRuntime(Protocol):
    store: Store

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
Sleep = Callable[[float], Awaitable[None]]


async def _default_spawn(command: str) -> object:
    return await asyncio.create_subprocess_shell(command)


def _sanitize_update_output(text: str) -> str:
    text = _ANSI_RE.sub("", text).replace("`", "'")
    return text[-3000:]


def _quote_env_value(value: str) -> str:
    if value and _SAFE_ENV_VALUE_RE.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_env_key(env_path: Path, key: str, value: str) -> None:
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    out: list[str] = []
    written = False
    serialized_value = _quote_env_value(value)
    for binding in parse_stream(io.StringIO(text)):
        if binding.key == key:
            if not written:
                out.append(f"{key}={serialized_value}\n")
                written = True
            continue
        out.append(binding.original.string)
    if not written:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"{key}={serialized_value}\n")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(env_path.parent), prefix=".env.", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(out))
        if env_path.exists():
            os.chmod(tmp_name, env_path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp_name, 0o600)
        try:
            Settings(_env_file=tmp_name)
        except ValidationError as exc:
            first_error = exc.errors()[0]["msg"]
            raise HTTPException(
                status_code=400,
                detail=f"invalid value for {key}: {first_error}",
            ) from exc
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
        settings.transcription_api_key,
        settings.github_token,
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
    sleep: Sleep = asyncio.sleep,
) -> None:
    request_times: deque[float] = deque()
    auth_fail_times: deque[float] = deque()
    auth_fail_lock = asyncio.Lock()
    env_lock = asyncio.Lock()

    def _check_rate(times: deque[float]) -> None:
        now = clock()
        while times and now - times[0] > 60:
            times.popleft()
        if len(times) >= _RATE_LIMIT:
            raise HTTPException(status_code=429, detail="admin rate limit")
        times.append(now)

    async def _notify_outcome(text: str) -> None:
        try:
            await runtime.notify(
                text,
                chat_id=None,
                thread_id=None,
                silent=True,
                markdown=False,
            )
        except (ValueError, RuntimeError, httpx.HTTPError):
            logger.debug("Failed to notify admin action outcome", exc_info=True)

    @application.post("/admin")
    async def admin(request: Request) -> dict[str, object]:
        if settings.admin_secret is None:
            raise HTTPException(status_code=404, detail="Not found")
        if auth_fail_lock.locked():
            raise HTTPException(status_code=429, detail="admin rate limit")
        client_host = request.client.host if request.client else "-"
        authorization = request.headers.get("authorization", "")
        expected_authorization = f"Bearer {settings.admin_secret}"
        if not secrets.compare_digest(
            authorization.encode(), expected_authorization.encode()
        ):
            now = clock()
            while auth_fail_times and now - auth_fail_times[0] > 60:
                auth_fail_times.popleft()
            auth_fail_times.append(now)
            log = logger.warning if len(auth_fail_times) <= _RATE_LIMIT else logger.debug
            log("admin auth failed from=%s", client_host)
            async with auth_fail_lock:
                await sleep(_AUTH_FAIL_DELAY)
            raise HTTPException(status_code=403, detail="Invalid bearer token")
        action = "-"
        key = "-"
        status = "ok"

        async def _dispatch() -> dict[str, object]:
            nonlocal action, key
            _check_rate(request_times)
            try:
                payload = await request.json()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid JSON") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="JSON object required")
            action_value = payload.get("action")
            key_value = payload.get("key")
            action = action_value if action_value in ACTIONS else "invalid"
            key = (
                key_value
                if (
                    isinstance(key_value, str)
                    and _ENV_KEY_RE.fullmatch(key_value)
                    and len(key_value) <= 64
                )
                else "-"
            )
            if action_value not in ACTIONS:
                raise HTTPException(
                    status_code=400, detail=f"unknown action; one of {list(ACTIONS)}"
                )

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
                if not isinstance(count, int) or isinstance(count, bool):
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
                    {
                        k: v
                        for k, v in dotenv_values(env_path).items()
                        if v is not None
                    }
                    if env_path.is_file()
                    else {}
                )
                return {
                    "env": {
                        k: v
                        for k, v in env.items()
                        if k in settings.admin_env_keys and k not in _NEVER_ENV_KEYS
                    }
                }

            if action == "set-env":
                value = payload.get("value")
                if (
                    not isinstance(key_value, str)
                    or not _ENV_KEY_RE.match(key_value)
                    or key_value in _NEVER_ENV_KEYS
                    or key_value not in settings.admin_env_keys
                ):
                    raise HTTPException(status_code=400, detail="key not allowed")
                if (
                    not isinstance(value, str)
                    or "\n" in value
                    or "\r" in value
                    or len(value) > 2000
                ):
                    raise HTTPException(status_code=400, detail="invalid value")
                if "${" in value:
                    raise HTTPException(
                        status_code=400, detail="interpolation syntax not allowed"
                    )
                async with env_lock:
                    await asyncio.to_thread(
                        _write_env_key, env_path, key_value, value
                    )
                return {"key": key_value, "restart_required": True}

            if action == "restart":
                await spawn_shell(settings.admin_restart_command)
                return {"scheduled": True}

            if action == "db-check":
                problems = await asyncio.to_thread(runtime.store.integrity_check)
                return {"ok": problems == ["ok"], "results": problems}

            if action == "backup":
                database_path = runtime.store.path
                if database_path is None or not database_path.exists():
                    return {
                        "ok": False,
                        "error": "database is not file-backed; nothing to copy",
                    }
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                dest = database_path.with_name(
                    f"{database_path.stem}-backup-{stamp}.sqlite3"
                )
                await asyncio.to_thread(runtime.store.backup_to, dest)
                return {"path": str(dest)}

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
            return {
                "exit_code": exit_code,
                "output": _sanitize_update_output(output),
            }

        try:
            result = await _dispatch()
        except HTTPException as exc:
            status = f"error {exc.status_code}: {exc.detail}"
            raise
        except Exception:
            status = "error 500"
            raise
        finally:
            logger.info(
                "admin action=%s key=%s from=%s status=%s",
                action,
                key,
                client_host,
                status,
            )
            key_suffix = f" {key}" if key != "-" else ""
            asyncio.create_task(
                _notify_outcome(f"Admin API: {action}{key_suffix} — {status}")
            )
        return result
