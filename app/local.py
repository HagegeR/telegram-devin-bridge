"""Local CLI backend — drives `devin acp` sessions over ACP JSON-RPC.

Prototype scope: one long-lived `devin acp` subprocess per session on the
host where the bridge runs. Sessions are in-memory (a bridge restart kills
them), there is no cloud URL, attachments are not supported, and settings
like repos/platform/acu don't apply — the CLI's own mode/model config does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

from app.clients import DevinMessage, SessionState

logger = logging.getLogger(__name__)

LOCAL_PREFIX = "local:"


def is_local(session_id: str) -> bool:
    return session_id.startswith(LOCAL_PREFIX)


@dataclass
class _AcpSession:
    proc: asyncio.subprocess.Process
    acp_id: str
    title: str = ""
    events: list[DevinMessage] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    next_event: int = 0
    turns: int = 0
    tail: asyncio.Task | None = None
    dead: bool = False
    suppress_turn: bool = False

    @property
    def running(self) -> bool:
        return self.turns > 0


# detached sessions stay addressable for /resume, but bound them: each
# owns a subprocess, so cap the pool and evict the oldest first
_MAX_SESSIONS = 32
# turns can run for a long time; prompts get no deadline (they end on
# stopReason or terminate), everything else uses the control timeout
_CONTROL_TIMEOUT = 30


class LocalClient:
    """DevinClient-shaped interface backed by `devin acp` on this host."""

    def __init__(
        self,
        cli_command: str = "devin",
        cwd: str | None = None,
        api_key: str | None = None,
        pr_fetcher: object = None,
    ) -> None:
        self.cli_command = cli_command
        # never default into the bridge checkout: agents would get the
        # deployment's .env and app sources as their workspace
        self.cwd = cwd or os.path.join(
            os.path.expanduser("~"), ".devin-local-sessions"
        )
        self.api_key = api_key
        self.pr_fetcher = pr_fetcher
        self._default_model: str | None = None
        self._cli_logged_in: bool | None = None
        self.sessions: dict[str, _AcpSession] = {}
        self._next_id = 0
        self._modes_cache: tuple[float, list[str]] | None = None
        self._models_cache: tuple[float, list[str]] | None = None

    async def modes(self) -> list[str]:
        """ACP session modes (accept-edits/smart/ask/plan/bypass), probed
        once per 5 min by booting a throwaway acp process. Empty on probe
        failure — callers must not treat that as authoritative."""
        cached = self._modes_cache
        if cached is not None and time.monotonic() - cached[0] < 300:
            return cached[1]
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_command,
                "acp",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self._cwd(),
            )
        except OSError:
            return []
        sess = _AcpSession(proc=proc, acp_id="")
        asyncio.create_task(self._reader(sess))
        try:
            await self._request(sess, "initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "telegram-devin-bridge", "version": "0"},
            })
            created = await self._request(sess, "session/new", {
                "cwd": self._cwd(),
                "mcpServers": [],
            })
            modes = [
                str(m["id"])
                for m in created.get("modes", {}).get("availableModes", [])
                if isinstance(m, dict) and m.get("id")
            ]
            for opt in created.get("configOptions", []):
                if isinstance(opt, dict) and opt.get("id") == "model":
                    current = opt.get("currentValue")
                    if current:
                        self._default_model = str(current)
        except (RuntimeError, TimeoutError, KeyError):
            modes = []
        proc.terminate()
        await proc.wait()
        if modes:
            self._modes_cache = (time.monotonic(), modes)
        return modes

    async def models(self) -> list[str]:
        """Model slugs accepted by the `model` config option, parsed from
        `devin models list`. Empty on failure."""
        cached = self._models_cache
        if cached is not None and time.monotonic() - cached[0] < 300:
            return cached[1]
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_command,
                "models",
                "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return []
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
        models = [
            line.split()[0]
            for line in out.decode(errors="replace").splitlines()
            if line.startswith("  ")
            and not line.strip().startswith("aliases:")
            and line.split()
        ]
        if models:
            self._models_cache = (time.monotonic(), models)
        return models

    async def set_model_default(self, session_id: str) -> None:
        if self._default_model:
            await self.set_model(session_id, self._default_model)

    async def set_mode(self, session_id: str, mode: str) -> None:
        sess = self.sessions[session_id]
        await self._request(sess, "session/set_mode", {
            "sessionId": sess.acp_id,
            "modeId": mode,
        })

    async def set_model(self, session_id: str, model: str) -> None:
        sess = self.sessions[session_id]
        await self._request(sess, "session/set_config_option", {
            "sessionId": sess.acp_id,
            "configId": "model",
            "value": model,
        })

    async def create_session(
        self,
        prompt: str,
        title: str | None = None,
        *,
        mode: str | None = None,
        model: str | None = None,
        **_: object,
    ) -> tuple[str, str]:
        cwd = self._cwd()
        os.makedirs(cwd, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            self.cli_command,
            "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )
        sess = _AcpSession(proc=proc, acp_id="")
        asyncio.create_task(self._reader(sess))
        try:
            await self._request(sess, "initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "telegram-devin-bridge", "version": "0"},
            })
            created = await self._request(sess, "session/new", {
                "cwd": cwd,
                "mcpServers": [],
            })
        except (RuntimeError, TimeoutError, asyncio.CancelledError):
            proc.terminate()
            raise
        sess.acp_id = str(created["sessionId"])
        sess.title = title or ""
        session_id = f"{LOCAL_PREFIX}{sess.acp_id}"
        while len(self.sessions) >= _MAX_SESSIONS:
            await self.terminate(next(iter(self.sessions)))
        self.sessions[session_id] = sess
        if model is not None:
            try:
                await self.set_model(session_id, model)
            except RuntimeError as exc:
                self._emit(sess, f"⚠ model {model} rejected: {exc}")
        if mode is not None:
            try:
                await self.set_mode(session_id, mode)
            except RuntimeError as exc:
                self._emit(sess, f"⚠ mode {mode} rejected: {exc}")
        if self.api_key and not await self._cli_logged_in():
            # only login when the CLI isn't already authed — every /login
            # turn leaves the key in the session transcript
            sess.suppress_turn = True
            await self.send_message(session_id, f"/login {self.api_key}")
        await self.send_message(session_id, prompt)
        return session_id, f"{title or sess.acp_id} · local CLI (no cloud URL)"

    async def send_message(self, session_id: str, message: str) -> None:
        sess = self.sessions[session_id]
        # chain turns so two in-flight prompts can't interleave chunks into
        # one merged reply; ACP serializes prompts per session anyway
        sess.turns += 1
        prev = sess.tail

        async def _turn() -> dict:
            if prev is not None:
                try:
                    await prev
                except (RuntimeError, TimeoutError, asyncio.CancelledError) as exc:
                    logger.debug("prior local turn failed: %s", exc)
            return await self._request(sess, "session/prompt", {
                "sessionId": sess.acp_id,
                "prompt": [{"type": "text", "text": message}],
            }, timeout=None)

        sess.tail = asyncio.create_task(_turn())
        sess.tail.add_done_callback(lambda f: self._turn_done(sess, f))

    def _turn_done(self, sess: _AcpSession, fut: asyncio.Future) -> None:
        sess.turns -= 1
        if sess.suppress_turn:
            sess.suppress_turn = False
            sess.buffer.clear()
            return
        try:
            exc = fut.exception()
        except asyncio.CancelledError:
            exc = None
        if exc is not None:
            self._emit(sess, f"⚠ prompt failed: {exc}")
        elif fut.result().get("error"):
            self._emit(sess, f"⚠ {fut.result()['error'].get('message', 'error')}")
        text = "".join(sess.buffer).strip()
        sess.buffer.clear()
        if text:
            self._emit(sess, text)

    def _emit(self, sess: _AcpSession, text: str) -> None:
        sess.next_event += 1
        sess.events.append(
            DevinMessage(
                message_type="devin_message",
                event_id=str(sess.next_event),
                message=text,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        )

    async def get_session(
        self,
        session_id: str,
        *,
        since_event_id: str | None = None,
        fetch_messages: bool = True,
    ) -> SessionState:
        sess = self.sessions.get(session_id)
        if sess is None:
            return SessionState(status_enum="expired", title="", pr_url=None, messages=[])
        messages = sess.events
        if since_event_id is not None:
            messages = [
                m for m in messages if int(m.event_id or 0) > int(since_event_id)
            ]
        status = "expired" if sess.dead else ("working" if sess.running else "blocked")
        return SessionState(
            status_enum=status,
            title=sess.title,
            pr_url=None,
            messages=messages if fetch_messages else [],
        )

    async def aclose(self) -> None:
        for session_id in list(self.sessions):
            await self.terminate(session_id)

    async def terminate(self, session_id: str) -> None:
        sess = self.sessions.pop(session_id, None)
        if sess is None:
            return
        sess.dead = True
        try:
            await self._request(sess, "session/delete", {"sessionId": sess.acp_id})
        except (RuntimeError, TimeoutError, asyncio.CancelledError):
            pass
        sess.proc.terminate()
        try:
            await asyncio.wait_for(sess.proc.wait(), 5)
        except TimeoutError:
            sess.proc.kill()

    async def upload_attachment(self, *_: object, **__: object) -> str:
        raise RuntimeError("attachments are not supported on local sessions")

    async def download_attachment(self, url: str) -> tuple[bytes, str]:
        return b"", "file"

    async def fetch_github_pr(
        self, url: str, token: str | None = None
    ) -> dict[str, object] | None:
        # PR enrichment is cloud metadata, not execution — delegate to the
        # cloud client the bridge injects
        if self.pr_fetcher is None:
            return None
        return await self.pr_fetcher(url, token)

    def _cwd(self) -> str:
        return os.path.abspath(self.cwd)

    async def _cli_logged_in(self) -> bool:
        if self._cli_logged_in is not None:
            return self._cli_logged_in
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_command,
                "auth",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
            self._cli_logged_in = b"Logged in" in out
        except (OSError, TimeoutError):
            self._cli_logged_in = False
        return self._cli_logged_in

    # -- JSON-RPC plumbing -------------------------------------------------

    def _send(self, sess: _AcpSession, method: str, params: dict) -> asyncio.Future:
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        sess.pending[req_id] = fut
        assert sess.proc.stdin is not None
        sess.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode()
            + b"\n"
        )
        return fut

    async def _request(
        self,
        sess: _AcpSession,
        method: str,
        params: dict,
        *,
        timeout: float | None = _CONTROL_TIMEOUT,
    ) -> dict:
        fut = self._send(sess, method, params)
        result = await fut if timeout is None else await asyncio.wait_for(fut, timeout)
        if "error" in result:
            raise RuntimeError(f"{method}: {result['error']}")
        return result.get("result", {})

    async def _reader(self, sess: _AcpSession) -> None:
        assert sess.proc.stdout is not None
        async for line in sess.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            req_id = msg.get("id")
            if req_id is not None and req_id in sess.pending:
                sess.pending.pop(req_id).set_result(msg)
                continue
            if msg.get("method") == "session/request_permission":
                # ACP sends permission prompts as server->client requests;
                # unanswered they stall the turn — pick an allow option so
                # every mode keeps working (bypass-tier access is the
                # prototype's operating mode anyway)
                options = msg.get("params", {}).get("options") or []
                pick = next(
                    (
                        o for o in options
                        if "allow" in str(o.get("kind", "") + o.get("name", "")).lower()
                    ),
                    options[0] if options else None,
                )
                if pick is not None and req_id is not None and sess.proc.stdin is not None:
                    sess.proc.stdin.write(
                        json.dumps({
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"outcome": {
                                "outcome": "selected",
                                "optionId": pick.get("optionId", pick.get("id")),
                            }},
                        }).encode() + b"\n"
                    )
                continue
            if msg.get("method") != "session/update":
                continue
            update = msg.get("params", {}).get("update", {})
            if update.get("sessionUpdate") == "agent_message_chunk":
                text = update.get("content", {}).get("text", "")
                if text:
                    sess.buffer.append(text)
        sess.dead = True
        for fut in sess.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("acp process exited"))
