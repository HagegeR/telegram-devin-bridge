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
    events: list[DevinMessage] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    next_event: int = 0
    running: bool = False
    dead: bool = False
    suppress_turn: bool = False


class LocalClient:
    """DevinClient-shaped interface backed by `devin acp` on this host."""

    def __init__(
        self,
        cli_command: str = "devin",
        cwd: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.cli_command = cli_command
        self.cwd = cwd
        self.api_key = api_key
        self.sessions: dict[str, _AcpSession] = {}
        self._next_id = 0

    async def create_session(self, prompt: str, title: str | None = None, **_: object) -> tuple[str, str]:
        proc = await asyncio.create_subprocess_exec(
            self.cli_command,
            "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.cwd,
        )
        sess = _AcpSession(proc=proc, acp_id="")
        asyncio.create_task(self._reader(sess))
        await self._request(sess, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "telegram-devin-bridge", "version": "0"},
        })
        created = await self._request(sess, "session/new", {
            "cwd": self.cwd or ".",
            "mcpServers": [],
        })
        sess.acp_id = str(created["sessionId"])
        session_id = f"{LOCAL_PREFIX}{sess.acp_id}"
        self.sessions[session_id] = sess
        if self.api_key:
            # shortcut: silent one-shot /login turn, cheap enough to run per
            # session; upgrade to checking `devin auth status` once when the
            # CLI grows a non-interactive status path
            sess.suppress_turn = True
            await self.send_message(session_id, f"/login {self.api_key}")
        await self.send_message(session_id, prompt)
        return session_id, f"{sess.acp_id} (local CLI on this host — no cloud URL)"

    async def send_message(self, session_id: str, message: str) -> None:
        sess = self.sessions[session_id]
        sess.running = True
        fut = self._send(sess, "session/prompt", {
            "sessionId": sess.acp_id,
            "prompt": [{"type": "text", "text": message}],
        })
        fut.add_done_callback(lambda f: self._turn_done(sess, f))

    def _turn_done(self, sess: _AcpSession, fut: asyncio.Future) -> None:
        sess.running = False
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
            title=sess.acp_id,
            pr_url=None,
            messages=messages if fetch_messages else [],
        )

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

    async def _request(self, sess: _AcpSession, method: str, params: dict) -> dict:
        fut = self._send(sess, method, params)
        result = await asyncio.wait_for(fut, timeout=30)
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
