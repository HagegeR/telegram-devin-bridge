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
import shlex
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
    # event ids are epoch-ms based so they stay ahead of the watcher's
    # persisted cursor even after a restart swaps in a fresh process
    event_base: int = field(default_factory=lambda: int(time.time() * 1000))
    turns: int = 0
    tail: asyncio.Task | None = None
    flusher: asyncio.Task | None = None
    dead: bool = False
    suppress_turn: bool = False
    # session/load replays the persisted history as session/update
    # notifications — ignore them until a real turn starts, or the old
    # reply would be re-emitted as if it were new
    replaying: bool = False
    # latest agent activity (tool-call title / thought tail) for the
    # watcher's live status line — cloud exposes nothing this granular
    activity: str = ""
    # bounded rolling tail of the thought stream — enough to find the
    # current line without quadratic rejoins
    thought: str = ""

    @property
    def running(self) -> bool:
        return self.turns > 0


# detached sessions stay addressable for /resume, but bound them: each
# owns a subprocess, so cap the pool and evict the oldest first
_MAX_SESSIONS = 32
# turns can run for a long time; prompts get no deadline (they end on
# stopReason or terminate), everything else uses the control timeout
_CONTROL_TIMEOUT = 30
# ACP replies can carry whole files; stdout is read in chunks and split
# on newlines manually because the default 64KB StreamReader limit used
# to crash the reader and orphan every turn on that session
_STREAM_CHUNK = 65536
# stream interim reply text during a turn — waiting for stopReason alone
# leaves the topic silent for many minutes on long tasks
_FLUSH_INTERVAL = 20


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
        self._model_options: list[str] | None = None
        self._cli_authed: bool | None = None
        self.sessions: dict[str, _AcpSession] = {}
        self._terminated: set[str] = set()
        self._resuming: dict[str, asyncio.Task] = {}
        self._next_id = 0
        self._last_event_base = 0
        self._modes_cache: tuple[float, list[str]] | None = None

    async def modes(self) -> list[str]:
        """ACP session modes (accept-edits/smart/ask/plan/bypass), probed
        once per 5 min by booting a throwaway acp process. Empty on probe
        failure — callers must not treat that as authoritative."""
        cached = self._modes_cache
        if cached is not None and time.monotonic() - cached[0] < 300:
            return cached[1]
        try:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(self.cli_command),
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
        # fresh probe: stale model choices must not survive a failed refresh
        self._model_options = None
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
                    values = [
                        str(o["value"])
                        for o in opt.get("options", [])
                        if isinstance(o, dict) and o.get("value")
                    ]
                    if values:
                        self._model_options = values
        except (RuntimeError, TimeoutError, KeyError):
            modes = []
        proc.terminate()
        await proc.wait()
        if modes:
            self._modes_cache = (time.monotonic(), modes)
        return modes

    async def models(self) -> list[str]:
        """Model values the `model` config option actually accepts, from
        session/new's configOptions. (`devin models list` advertises every
        family — including ones this account can't set, e.g. swe-2-max —
        so it is not a source of truth.) Empty on probe failure."""
        await self.modes()  # the probe populates _model_options
        return self._model_options or []

    async def set_model_default(self, session_id: str) -> None:
        if self._default_model:
            await self.set_model(session_id, self._default_model)

    async def set_mode(self, session_id: str, mode: str) -> None:
        sess = await self._get_or_resume(session_id)
        if sess is None:
            raise RuntimeError(f"local session {session_id} is gone")
        await self._request(sess, "session/set_mode", {
            "sessionId": sess.acp_id,
            "modeId": mode,
        })

    async def set_model(self, session_id: str, model: str) -> None:
        sess = await self._get_or_resume(session_id)
        if sess is None:
            raise RuntimeError(f"local session {session_id} is gone")
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
        sess = await self._spawn()
        try:
            created = await self._request(sess, "session/new", {
                "cwd": cwd,
                "mcpServers": [],
            })
        except (RuntimeError, TimeoutError, asyncio.CancelledError):
            sess.proc.terminate()
            raise
        sess.acp_id = str(created["sessionId"])
        sess.title = title or ""
        session_id = f"{LOCAL_PREFIX}{sess.acp_id}"
        await self._evict()
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
        sess = await self._get_or_resume(session_id)
        if sess is None:
            raise RuntimeError(f"local session {session_id} is gone")
        if sess.dead:
            raise RuntimeError(f"local session {sess.acp_id} is gone")
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
            # reset at the real turn start (after any queued turn): the
            # status line must not show the previous turn's tool/thought
            sess.activity = ""
            sess.thought = ""
            sess.buffer.clear()
            sess.replaying = False
            return await self._request(sess, "session/prompt", {
                "sessionId": sess.acp_id,
                "prompt": [{"type": "text", "text": message}],
            }, timeout=None)

        sess.tail = asyncio.create_task(_turn())
        sess.tail.add_done_callback(lambda f: self._turn_done(sess, f))
        if sess.flusher is None:
            sess.flusher = asyncio.create_task(self._flusher(sess))

    async def _flusher(self, sess: _AcpSession) -> None:
        try:
            while not sess.dead:
                await asyncio.sleep(_FLUSH_INTERVAL)
                if not sess.turns or sess.suppress_turn:
                    continue
                raw = "".join(sess.buffer)
                # only complete lines: markers/fences are line-scoped, and
                # keeping the trailing partial line preserves boundary
                # whitespace + never splits a marker mid-token
                head, sep, tail = raw.rpartition("\n")
                if not sep or not head.strip():
                    continue
                if head.count("```") % 2:
                    # inside an unclosed fence — a fragment would reach
                    # Telegram with broken MarkdownV2
                    continue
                sess.buffer.clear()
                sess.buffer.append(tail)
                self._emit(sess, head)
        except asyncio.CancelledError:
            pass

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
                event_id=str(sess.event_base + sess.next_event),
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
        try:
            # a watcher may outlive the process after a bridge restart —
            # resume so polls keep tracking instead of going expired
            sess = await self._get_or_resume(session_id)
        except RuntimeError:
            sess = None
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
            status_detail=sess.activity or None,
        )

    async def aclose(self) -> None:
        # shutdown kills processes only — the CLI keeps the sessions so the
        # next bridge can resume them; session/delete is for explicit /stop
        for sess in list(self.sessions.values()):
            await self._reap(sess)
        self.sessions.clear()
        self._resuming.clear()

    async def terminate(self, session_id: str) -> None:
        sess = self.sessions.pop(session_id, None)
        self._terminated.add(session_id)
        if sess is None:
            # dormant session: /stop must still delete its persisted record,
            # else a later bridge could resume it
            try:
                sess = await self._spawn()
            except (OSError, RuntimeError, TimeoutError):
                return
            sess.acp_id = session_id.removeprefix(LOCAL_PREFIX)
        sess.dead = True
        try:
            await self._request(sess, "session/delete", {"sessionId": sess.acp_id})
        except (RuntimeError, TimeoutError, asyncio.CancelledError):
            pass
        await self._reap(sess)

    async def _reap(self, sess: _AcpSession) -> None:
        """Stop the acp process without deleting the persisted session."""
        sess.dead = True
        if sess.flusher is not None:
            sess.flusher.cancel()
        sess.proc.terminate()
        try:
            await asyncio.wait_for(sess.proc.wait(), 5)
        except TimeoutError:
            sess.proc.kill()

    async def _evict(self) -> None:
        while len(self.sessions) >= _MAX_SESSIONS:
            # capacity eviction is lifecycle, not /stop — keep the record
            await self._reap(self.sessions.pop(next(iter(self.sessions))))

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

    async def _spawn(self) -> _AcpSession:
        """Spawn a `devin acp` process and complete the ACP handshake."""
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(self.cli_command),
            "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self._cwd(),
        )
        sess = _AcpSession(proc=proc, acp_id="")
        asyncio.create_task(self._reader(sess))
        try:
            await self._request(sess, "initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "telegram-devin-bridge", "version": "0"},
            })
        except (RuntimeError, TimeoutError, asyncio.CancelledError):
            sess.proc.terminate()
            raise
        # strictly increasing bases so a same-ms respawn can't reuse ids
        sess.event_base = max(
            int(time.time() * 1000), self._last_event_base + 1
        )
        self._last_event_base = sess.event_base
        return sess

    async def _get_or_resume(self, session_id: str) -> _AcpSession | None:
        sess = self.sessions.get(session_id)
        if sess is None and session_id not in self._terminated:
            sess = await self._resume(session_id)
        return sess

    async def _resume(self, session_id: str) -> _AcpSession:
        # dedupe concurrent loads (watcher poll + user turn) — a second
        # process would orphan the first one's replies
        task = self._resuming.get(session_id)
        if task is None:
            task = asyncio.create_task(self._resume_once(session_id))
            self._resuming[session_id] = task
        try:
            return await task
        finally:
            if self._resuming.get(session_id) is task:
                self._resuming.pop(session_id)

    async def _resume_once(self, session_id: str) -> _AcpSession:
        """Reload a persisted CLI session into a fresh `devin acp` process.
        Sessions outlive bridge restarts in the CLI's session DB — the
        in-memory map is only the live-process index."""
        existing = self.sessions.get(session_id)
        if existing is not None:
            return existing
        acp_id = session_id.removeprefix(LOCAL_PREFIX)
        sess = await self._spawn()
        try:
            await self._request(sess, "session/load", {
                "sessionId": acp_id,
                "cwd": self._cwd(),
                "mcpServers": [],
            })
        except (RuntimeError, TimeoutError) as exc:
            sess.proc.terminate()
            raise RuntimeError(
                f"local session {acp_id} couldn't resume: {exc}"
            ) from exc
        sess.acp_id = acp_id
        sess.replaying = True
        sess.title = await self._session_title(sess, acp_id)
        await self._evict()
        self.sessions[session_id] = sess
        logger.info("resumed local session %s", acp_id)
        return sess

    async def _session_title(self, sess: _AcpSession, acp_id: str) -> str:
        try:
            listed = await self._request(sess, "session/list", {
                "cwd": self._cwd(),
            })
            for entry in listed.get("sessions", []):
                if entry.get("sessionId") == acp_id:
                    return str(entry.get("title") or "")
        except (RuntimeError, TimeoutError):
            pass
        return ""

    def _cwd(self) -> str:
        path = os.path.abspath(self.cwd)
        # the probe spawns here too, before any session exists
        os.makedirs(path, exist_ok=True)
        return path

    async def _cli_logged_in(self) -> bool:
        if self._cli_authed is not None:
            return self._cli_authed
        try:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(self.cli_command),
                "auth",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
            self._cli_authed = b"Logged in" in out
        except (OSError, TimeoutError):
            self._cli_authed = False
        return self._cli_authed

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
        buf = b""
        while True:
            chunk = await sess.proc.stdout.read(_STREAM_CHUNK)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                try:
                    self._handle(sess, msg)
                except (KeyError, IndexError, TypeError):
                    # a malformed update must not kill the reader — its
                    # pending turns would otherwise hang forever
                    logger.warning("bad acp update skipped: %.200s", raw)
        sess.dead = True
        for fut in sess.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("acp process exited"))

    def _handle(self, sess: _AcpSession, msg: dict) -> None:
        req_id = msg.get("id")
        if req_id is not None and req_id in sess.pending:
            sess.pending.pop(req_id).set_result(msg)
            return
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
            return
        if msg.get("method") != "session/update":
            return
        update = msg.get("params", {}).get("update", {})
        if sess.replaying:
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                sess.buffer.append(text)
            return
        if kind == "agent_thought_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                sess.thought = (sess.thought + text)[-4096:]
                lines = sess.thought.strip().splitlines()
                if lines:
                    sess.activity = lines[-1][-120:]
            return
        if kind in {"tool_call", "tool_call_update"}:
            title = update.get("title") or update.get("kind")
            if title:
                sess.activity = str(title)[:120]
