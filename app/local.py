"""Local CLI backend — drives `devin acp` sessions over ACP JSON-RPC.

Prototype scope: one long-lived `devin acp` subprocess per session on the
host where the bridge runs. Sessions are in-memory (a bridge restart kills
them), there is no cloud URL, attachments are saved on the host and
handed to the agent by path, and settings like repos/acu don't apply —
the CLI's own mode/model config does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.clients import DevinMessage, SessionState

logger = logging.getLogger(__name__)

LOCAL_PREFIX = "local:"

# cost_tier -> list icon, from `devin models list --format json`
MODEL_TIER_ICONS = {
    "Free": "🎁",
    "Low cost": "🏷️",
    "Med cost": "💰",
    "High cost": "💎",
}


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
    # live config the CLI reports (mode/model/thought_level -> current value)
    config: dict[str, str] = field(default_factory=dict)
    # available_commands_update arrives after session/new; None until then
    commands: list[dict] | None = None
    # session/load replays the persisted history as session/update
    # notifications — ignore them until a real turn starts, or the old
    # reply would be re-emitted as if it were new
    replaying: bool = False
    # session/request_permission requests awaiting a Telegram decision —
    # answered via answer_permission() from the choice callback
    permissions: dict[int | str, dict] = field(default_factory=dict)
    # user text currently in flight: the bridge persists it so a bridge
    # restart mid-turn can replay it; cleared when the turn settles
    pending_prompt: str | None = None
    # resume loaded the session before its conversation was active (e.g.
    # /resume of a history entry) — apply the chat config on the next
    # prompt, when the conv lookup can see it
    cfg_pending: bool = False
    # latest agent activity (tool-call title / thought tail) for the
    # watcher's live status line — cloud exposes nothing this granular
    activity: str = ""
    # bounded rolling tail of the thought stream — enough to find the
    # current line without quadratic rejoins
    thought: str = ""
    # child's stderr tail — included in 'acp process exited' errors so a
    # dead spawn isn't a silent black box
    stderr_tail: str = ""

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
# but never let a single paragraph-less blob accumulate unbounded — emit
# it anyway rather than holding the turn's whole output back
_STREAM_MAX_BUFFER = 3072
# the activity line posts rawInput.command to Telegram — blank anything
# that looks like a credential before it can leak to the chat
_SECRET_RE = re.compile(
    r"(?i)("
    r"(?:key|token|secret|passw(?:or)?d|credential|auth)[\w-]*\s*[=:]"
    r"\s*(?:(?:bearer|basic)\s+)?"
    r"|(?:bearer|basic)\s+"
    r"|--?[\w-]*(?:key|token|secret|passw(?:or)?d|auth)[\w-]*\s*[= ]"
    r")\s*(?:[\"'][^\"']*[\"']?|\S+)"
)
_USERINFO_RE = re.compile(r"(?<=://)[^\s/:]+:[^\s@]+@")
# the CLI's chunk stream can drop line separators between streamed
# progress updates — sentence punctuation immediately followed by a
# capital letter or a status marker is a lost line break. Digits and
# lowercase are excluded so versions ("v2.0.0") and "word.ext" survive.
_RUNON_RE = re.compile(r"(?<=[.!?:;])(?=[A-Z⚠✓◆→#])")
# reply markers the watcher parses line-wise (OPTIONS: labels, TABLE:/
# DETAILS:/SUGGEST: bodies, control markers) break if a split lands
# inside them — marker-prefixed lines are left alone
_MARKER_LINE_RE = re.compile(r"^[A-Z][A-Z_]{2,}:")


def _split_runons(text: str) -> str:
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if in_fence or "`" in line or _MARKER_LINE_RE.match(line):
            out.append(line)
        else:
            out.append(_RUNON_RE.sub("\n", line))
    return "\n".join(out)


def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        pass  # already exited on its own


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass  # already exited on its own


def _scrub_command(cmd: str) -> str:
    scrubbed = _SECRET_RE.sub(lambda m: m.group(1) + "***", cmd)
    return _USERINFO_RE.sub("***@", scrubbed)


class LocalClient:
    """DevinClient-shaped interface backed by `devin acp` on this host."""

    def __init__(
        self,
        # a plain string is ONE executable path (may contain spaces);
        # a sequence is an argv prefix for an interpreter-style command
        cli_command: str | list[str] = "devin",
        cwd: str | None = None,
        api_key: str | None = None,
        pr_fetcher: object = None,
        # optional durability hook (the bridge's Store): cloud events live
        # server-side and survive restarts; local emits vanish with the
        # process unless persisted, so every emitted event is mirrored
        event_store: object | None = None,
    ) -> None:
        self._argv = (
            [cli_command] if isinstance(cli_command, str) else list(cli_command)
        )
        # never default into the bridge checkout: agents would get the
        # deployment's .env and app sources as their workspace
        self.cwd = cwd or os.path.join(
            os.path.expanduser("~"), ".devin-local-sessions"
        )
        self.api_key = api_key
        self.pr_fetcher = pr_fetcher
        self.event_store = event_store
        self._default_model: str | None = None
        self._model_options: list[str] | None = None
        self._cli_authed: bool | None = None
        self.sessions: dict[str, _AcpSession] = {}
        # the bridge hooks this to restart delivery when a turn outlives
        # its watcher — local turns emit events with no cloud counterpart
        # polling for them, so an emit is the earliest recovery signal
        self.on_emit: Callable[[str], None] | None = None
        # bridge hooks this to surface ACP permission prompts as Telegram
        # approve/reject buttons; None -> every request is auto-allowed
        self.on_permission: Callable[[str, dict], None] | None = None
        # bridge hooks these to (a) persist a prompt while its turn is in
        # flight so a restart can replay it, and (b) supply the desired
        # mode/model/thought_level when a session resumes into a fresh
        # process (session/load restores the CLI state, not the chat's
        # configured values)
        self.on_prompt_state: Callable[[str, str | None], None] | None = None
        self.resume_config: Callable[[str], dict | None] | None = None
        self._terminated: set[str] = set()
        self._resuming: dict[str, asyncio.Task] = {}
        self._next_id = 0
        self._last_event_base = 0
        self._modes_cache: tuple[float, list[str]] | None = None
        self._costs_cache: tuple[float, dict[str, str]] | None = None
        self._mode_details: list[dict] = []
        self._think_options: list[str] | None = None
        self._default_thought: str | None = None
        self._commands: list[dict] = []

    async def modes(self) -> list[str]:
        """ACP session modes (accept-edits/smart/ask/plan/bypass), probed
        once per 5 min by booting a throwaway acp process. Empty on probe
        failure — callers must not treat that as authoritative."""
        cached = self._modes_cache
        if cached is not None and time.monotonic() - cached[0] < 300:
            return cached[1]
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
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
        probe_id: str | None = None
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
            probe_id = str(created.get("sessionId") or "") or None
            modes = [
                str(m["id"])
                for m in created.get("modes", {}).get("availableModes", [])
                if isinstance(m, dict) and m.get("id")
            ]
            for opt in created.get("configOptions", []):
                if not isinstance(opt, dict):
                    continue
                values = [
                    str(o["value"])
                    for o in opt.get("options", [])
                    if isinstance(o, dict) and o.get("value")
                ]
                current = opt.get("currentValue")
                if opt.get("id") == "model":
                    if current:
                        self._default_model = str(current)
                    if values:
                        self._model_options = values
                elif opt.get("id") == "mode":
                    details = [
                        {
                            "id": str(o["value"]),
                            "name": str(o.get("name") or o["value"]),
                            "description": str(o.get("description") or ""),
                        }
                        for o in opt.get("options", [])
                        if isinstance(o, dict) and o.get("value")
                    ]
                    if details:
                        self._mode_details = details
                elif opt.get("id") == "thought_level":
                    if current:
                        self._default_thought = str(current)
                    if values:
                        self._think_options = values
            # available_commands_update is pushed asynchronously; give the
            # CLI a moment — long enough for first discovery, short once a
            # list is cached so /mode and /model don't pay for it
            deadline = time.monotonic() + (6 if not self._commands else 1.5)
            while sess.commands is None and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if sess.commands:
                self._commands = sess.commands
        except (RuntimeError, TimeoutError, KeyError):
            modes = []
        finally:
            # the probe's session/new lands in the CLI's session DB like
            # any real session — delete it or every /settings and /model
            # leaves a phantom entry in `devin list`. finally/finally so a
            # cancelled probe still deletes and still reaps the process
            try:
                if probe_id is not None:
                    try:
                        await self._request(sess, "session/delete", {
                            "sessionId": probe_id,
                        })
                    except (RuntimeError, TimeoutError) as exc:
                        logger.warning(
                            "probe session %s not deleted: %s", probe_id, exc
                        )
            finally:
                _terminate(proc)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    _kill(proc)
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

    async def model_costs(self) -> dict[str, str]:
        """{model_uid: cost_tier} from `devin models list --format json`
        (Free / Low cost / Med cost / High cost). ACP configOptions carry
        names but not pricing, so this probes the CLI once per 5 min.
        Empty on probe failure — callers must tolerate missing tiers."""
        cached = self._costs_cache
        if cached is not None and time.monotonic() - cached[0] < 300:
            return cached[1]
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
                "models",
                "list",
                "--format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return {}
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
            payload = json.loads(out)
            costs = {
                str(v["model_uid"]): str(v["cost_tier"])
                for f in payload.get("families", [])
                if isinstance(f, dict)
                for v in f.get("variants", [])
                if isinstance(v, dict)
                and v.get("model_uid")
                and v.get("cost_tier")
            }
        except (TimeoutError, ValueError):
            costs = {}
        finally:
            _terminate(proc)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                _kill(proc)
        if costs:
            self._costs_cache = (time.monotonic(), costs)
        return costs

    async def mode_details(self) -> list[dict]:
        """[{id, name, description}] from the `mode` config option —
        richer than bare ids (Smart auto-approves, Plan refuses edits)."""
        await self.modes()
        return self._mode_details

    async def think_levels(self) -> list[str]:
        """Values the `thought_level` config option accepts."""
        await self.modes()
        return self._think_options or []

    async def commands(self) -> list[dict]:
        """Slash commands the CLI advertises via available_commands_update,
        normalized to {name, description, category, hint}. Empty on probe
        failure."""
        await self.modes()
        return self._commands

    async def list_sessions(self) -> list[dict]:
        """Sessions persisted in the CLI's own DB (`devin list`), via a
        throwaway acp process — includes dormant sessions the bridge can
        still resume. Empty on probe failure. session/list is a query, so
        unlike modes() nothing lands in the session DB to clean up."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
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
            listed = await self._request(sess, "session/list", {
                "cwd": self._cwd(),
            })
            return [
                entry for entry in listed.get("sessions", [])
                if isinstance(entry, dict)
            ]
        except (RuntimeError, TimeoutError):
            return []
        finally:
            _terminate(proc)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                _kill(proc)

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
        await self._set_config(session_id, "model", model)

    async def set_thought_level(self, session_id: str, level: str) -> None:
        await self._set_config(session_id, "thought_level", level)

    async def set_thought_default(self, session_id: str) -> None:
        await self.modes()  # populate _default_thought on a fresh process
        if self._default_thought:
            await self.set_thought_level(session_id, self._default_thought)

    async def _set_config(
        self, session_id: str, config_id: str, value: str
    ) -> None:
        sess = await self._get_or_resume(session_id)
        if sess is None:
            raise RuntimeError(f"local session {session_id} is gone")
        result = await self._request(sess, "session/set_config_option", {
            "sessionId": sess.acp_id,
            "configId": config_id,
            "value": value,
        })
        # the CLI only emits config_option_update once at session/new —
        # apply the change from the response, which echoes currentValue
        for opt in result.get("configOptions", []):
            if isinstance(opt, dict) and opt.get("currentValue") is not None:
                sess.config[str(opt["id"])] = str(opt["currentValue"])

    async def create_session(
        self,
        prompt: str,
        title: str | None = None,
        *,
        mode: str | None = None,
        model: str | None = None,
        thought_level: str | None = None,
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
            _terminate(sess.proc)
            raise
        sess.acp_id = str(created["sessionId"])
        # don't default "" here — the CLI's generated title can already
        # have arrived via session_info_update and would be clobbered
        if title is not None:
            sess.title = title
        session_id = f"{LOCAL_PREFIX}{sess.acp_id}"
        await self._evict()
        self.sessions[session_id] = sess
        for opt in created.get("configOptions", []):
            if isinstance(opt, dict) and opt.get("currentValue") is not None:
                sess.config[str(opt["id"])] = str(opt["currentValue"])
        if model is not None:
            try:
                await self.set_model(session_id, model)
            except RuntimeError as exc:
                self._emit(sess, f"⚠ model {model} rejected: {exc}")
        if thought_level is not None:
            try:
                await self.set_thought_level(session_id, thought_level)
            except RuntimeError as exc:
                self._emit(sess, f"⚠ thought_level {thought_level} rejected: {exc}")
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

    def _prompt_state(self, session_id: str, prompt: str | None) -> None:
        hook = self.on_prompt_state
        if hook is None:
            return
        try:
            hook(session_id, prompt)
        except Exception:
            logger.exception("on_prompt_state hook failed")

    def _clear_pending_prompt(
        self, sess: _AcpSession, session_id: str, message: str
    ) -> None:
        # only the turn that set it clears it — a queued turn may already
        # have written its own prompt over the slot
        if sess.pending_prompt == message:
            sess.pending_prompt = None
            self._prompt_state(session_id, None)

    async def send_message(self, session_id: str, message: str) -> None:
        sess = await self._get_or_resume(session_id)
        if sess is None:
            raise RuntimeError(f"local session {session_id} is gone")
        if sess.dead:
            raise RuntimeError(f"local session {sess.acp_id} is gone")
        if sess.cfg_pending:
            await self._apply_resume_config(session_id, sess)
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
            # the prompt's Telegram row was already consumed; keep it
            # durable until the turn settles so a bridge restart mid-turn
            # replays it instead of losing the user's question
            sess.pending_prompt = None if sess.suppress_turn else message
            self._prompt_state(session_id, sess.pending_prompt)
            try:
                result = await self._request(sess, "session/prompt", {
                    "sessionId": sess.acp_id,
                    "prompt": [{"type": "text", "text": message}],
                }, timeout=None)
            except asyncio.CancelledError:
                # shutdown mid-turn: keep the pending marker for replay
                raise
            except BaseException:
                # a dead process means the turn was interrupted (shutdown,
                # crash, evict), not answered — keep the marker for replay;
                # only a live-process failure clears it
                if not sess.dead:
                    self._clear_pending_prompt(sess, session_id, message)
                raise
            self._clear_pending_prompt(sess, session_id, message)
            return result

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
                # emit whole paragraphs, not arbitrary line batches — every
                # emitted event becomes its own Telegram message, so a
                # mid-paragraph cut fragments the reply into fragments
                boundary = raw.rfind("\n\n")
                if boundary != -1:
                    emit = raw[:boundary]
                    if emit.count("```") % 2:
                        # last ``` opens an unclosed fence — emit only the
                        # paragraphs before it, else defer (a fragment
                        # would reach Telegram with broken MarkdownV2)
                        cut = emit.rfind("\n\n", 0, emit.rfind("```"))
                        if cut == -1:
                            continue
                        emit = emit[:cut]
                    keep = raw[len(emit) + 2 :]
                else:
                    # no paragraph boundary: fall back to complete lines
                    # once the buffer is over the cap so memory stays
                    # bounded (a single never-ending line bounds on raw)
                    if "\n" in raw:
                        emit, _, keep = raw.rpartition("\n")
                    else:
                        emit, keep = raw, ""
                    if (
                        not emit.strip()
                        or emit.count("```") % 2
                        or len(emit) < _STREAM_MAX_BUFFER
                    ):
                        continue
                sess.buffer.clear()
                sess.buffer.append(keep)
                if emit.strip():
                    self._emit(sess, emit)
        except asyncio.CancelledError:
            pass

    def _turn_done(self, sess: _AcpSession, fut: asyncio.Future) -> None:
        sess.turns -= 1
        if sess.suppress_turn:
            sess.suppress_turn = False
            if "".join(sess.buffer).strip():
                logger.warning(
                    "local turn output discarded by suppress_turn for %s",
                    sess.acp_id,
                )
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
        text = _split_runons(text)
        sess.next_event += 1
        msg = DevinMessage(
            message_type="devin_message",
            event_id=str(sess.event_base + sess.next_event),
            message=text,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        sess.events.append(msg)
        if self.event_store is not None:
            try:
                self.event_store.append_local_event(
                    f"{LOCAL_PREFIX}{sess.acp_id}",
                    str(msg.event_id),
                    msg.timestamp,
                    msg.message,
                )
            except Exception:
                # a broken store must not kill the reader — delivery keeps
                # working from memory, it just loses restart durability
                logger.exception("local event persist failed for %s", sess.acp_id)
        if self.on_emit is not None:
            try:
                self.on_emit(f"{LOCAL_PREFIX}{sess.acp_id}")
            except Exception:
                # a store/scheduling error must not escape into the ACP
                # reader — that would end the session's output for good
                logger.exception("on_emit hook failed for %s", sess.acp_id)

    @staticmethod
    def _pick_allow(msg: dict) -> str | None:
        options = msg.get("params", {}).get("options") or []
        pick = next(
            (
                o for o in options
                if "allow" in str(o.get("kind", "") + o.get("name", "")).lower()
            ),
            options[0] if options else None,
        )
        if pick is None:
            return None
        opt = pick.get("optionId", pick.get("id"))
        return str(opt) if opt is not None else None

    @staticmethod
    def permission_options(msg: dict) -> list[tuple[str, str]]:
        """(optionId, label) pairs for the Telegram approval keyboard."""
        out = []
        for opt in msg.get("params", {}).get("options") or []:
            if not isinstance(opt, dict):
                continue
            option_id = opt.get("optionId", opt.get("id"))
            label = opt.get("name") or opt.get("kind") or option_id
            if option_id is not None and label is not None:
                out.append((str(option_id), str(label)))
        return out

    @staticmethod
    def _answer_permission(
        sess: _AcpSession, req_id: int | str, option_id: str | None
    ) -> None:
        if option_id is None or sess.proc.stdin is None:
            return
        sess.proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"outcome": {
                    "outcome": "selected",
                    "optionId": option_id,
                }},
            }).encode() + b"\n"
        )

    def answer_permission(
        self, session_id: str, req_id: int | str, option_id: str | None
    ) -> str | None:
        """Reply to a pending session/request_permission; returns the
        picked option's label (option_id=None picks the allow option —
        the no-hook fallback). None when the request is already gone:
        expired choice, dead session."""
        sess = self.sessions.get(session_id)
        if sess is None:
            return None
        msg = sess.permissions.pop(req_id, None)
        if msg is None:
            return None
        option_id = option_id or self._pick_allow(msg)
        self._answer_permission(sess, req_id, option_id)
        return next(
            (
                label
                for oid, label in self.permission_options(msg)
                if oid == option_id
            ),
            option_id,
        )

    def save_attachment(self, filename: str, content: bytes) -> Path:
        """Persist a Telegram file on the host — ACP has no file-transfer
        channel, but a local session can read host paths."""
        safe = re.sub(r"[^\w.-]", "_", Path(filename).name)[:80] or "file"
        attachments = Path(self.cwd).expanduser() / "attachments"
        attachments.mkdir(parents=True, exist_ok=True)
        # unbounded growth would silently fill the host disk — retain a
        # week (agent-read prompts reference the path within the turn,
        # so days of slack is plenty)
        cutoff = time.time() - 7 * 86400
        for old in attachments.iterdir():
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        dest = attachments / f"{int(time.time() * 1000)}-{safe}"
        dest.write_bytes(content)
        return dest

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
        config_bits = [
            f"{key} {value}"
            for key in ("mode", "model", "thought_level")
            if (value := sess.config.get(key))
        ]
        return SessionState(
            status_enum=status,
            title=sess.title,
            pr_url=None,
            messages=messages if fetch_messages else [],
            status_detail=sess.activity or None,
            local_info=" · ".join(config_bits) or None,
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
        if self.event_store is not None:
            try:
                self.event_store.delete_local_events(session_id)
            except Exception:
                logger.exception(
                    "local event store cleanup failed for %s", session_id
                )
        await self._reap(sess)

    async def _reap(self, sess: _AcpSession) -> None:
        """Stop the acp process without deleting the persisted session."""
        sess.dead = True
        if sess.flusher is not None:
            sess.flusher.cancel()
        _terminate(sess.proc)
        try:
            await asyncio.wait_for(sess.proc.wait(), 5)
        except TimeoutError:
            _kill(sess.proc)

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
            *self._argv,
            "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd(),
        )
        sess = _AcpSession(proc=proc, acp_id="")
        asyncio.create_task(self._reader(sess))
        asyncio.create_task(self._drain_stderr(sess))
        try:
            await self._request(sess, "initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "telegram-devin-bridge", "version": "0"},
            })
        except (RuntimeError, TimeoutError, asyncio.CancelledError):
            _terminate(sess.proc)
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
            loaded = await self._request(sess, "session/load", {
                "sessionId": acp_id,
                "cwd": self._cwd(),
                "mcpServers": [],
            })
        except (RuntimeError, TimeoutError) as exc:
            _terminate(sess.proc)
            raise RuntimeError(
                f"local session {acp_id} couldn't resume: {exc}"
            ) from exc
        sess.acp_id = acp_id
        for opt in loaded.get("configOptions", []):
            if isinstance(opt, dict) and opt.get("currentValue") is not None:
                sess.config[str(opt["id"])] = str(opt["currentValue"])
        sess.replaying = True
        sess.title = await self._session_title(sess, acp_id)
        await self._evict()
        self.sessions[session_id] = sess
        if self.event_store is not None:
            # re-offer events a dead incarnation emitted but the watcher
            # never polled — the store's event ids slot straight into the
            # persisted last_event_id dedup like cloud's server-side log.
            # A broken store must not block resuming the session at all.
            try:
                for event_id, ts, text in self.event_store.local_events(
                    session_id
                ):
                    sess.events.append(
                        DevinMessage(
                            message_type="devin_message",
                            event_id=event_id,
                            message=text,
                            timestamp=ts,
                        )
                    )
            except Exception:
                logger.exception(
                    "local event store read failed for %s", acp_id
                )
        logger.info("resumed local session %s", acp_id)
        # session/load restores the CLI's state, not the chat's configured
        # mode/model/thought_level — re-apply them so e.g. bypass mode
        # survives a bridge restart instead of silently reverting. When
        # resume runs before the conversation is active (/resume of a
        # history entry) the hook can't see the settings yet — keep the
        # flag so the next prompt applies them instead.
        sess.cfg_pending = True
        await self._apply_resume_config(session_id, sess)
        return sess

    async def _apply_resume_config(
        self, session_id: str, sess: _AcpSession
    ) -> None:
        cfg = self.resume_config(session_id) if self.resume_config else None
        if not cfg:
            return
        sess.cfg_pending = False
        mode = cfg.get("mode")
        if mode:
            try:
                await self.set_mode(session_id, mode)
            except RuntimeError as exc:
                self._emit(sess, f"⚠ mode {mode} rejected: {exc}")
        for config_id in ("model", "thought_level"):
            value = cfg.get(config_id)
            if value:
                try:
                    await self._set_config(session_id, config_id, value)
                except RuntimeError as exc:
                    self._emit(sess, f"⚠ {config_id} {value} rejected: {exc}")

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
                *self._argv,
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
        detail = f": {sess.stderr_tail[-400:].strip()}" if sess.stderr_tail else ""
        for fut in sess.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"acp process exited{detail}"))

    async def _drain_stderr(self, sess: _AcpSession) -> None:
        if sess.proc.stderr is None:
            return
        tail = b""
        while chunk := await sess.proc.stderr.read(4096):
            tail = (tail + chunk)[-4096:]
        sess.stderr_tail = tail.decode(errors="replace")

    def _handle(self, sess: _AcpSession, msg: dict) -> None:
        req_id = msg.get("id")
        if req_id is not None and req_id in sess.pending:
            sess.pending.pop(req_id).set_result(msg)
            return
        if msg.get("method") == "session/request_permission":
            # ACP sends permission prompts as server->client requests;
            # unanswered they stall the turn. With no approval hook every
            # request is auto-allowed (bypass-tier access is the
            # prototype's operating mode); with one the bridge surfaces
            # the options in Telegram and answer_permission replies.
            if req_id is not None:
                if self.on_permission is None:
                    self._answer_permission(sess, req_id, self._pick_allow(msg))
                else:
                    sess.permissions[req_id] = msg
                    try:
                        self.on_permission(f"{LOCAL_PREFIX}{sess.acp_id}", msg)
                    except Exception:
                        logger.exception(
                            "on_permission hook failed for %s — auto-allowing",
                            sess.acp_id,
                        )
                        sess.permissions.pop(req_id, None)
                        self._answer_permission(sess, req_id, self._pick_allow(msg))
            return
        if msg.get("method") != "session/update":
            return
        update = msg.get("params", {}).get("update", {})
        kind = update.get("sessionUpdate")
        # replayed history is only reply content — control updates
        # (commands, config, mode, plan) still apply during replay so
        # resume-time re-configuration isn't silently dropped
        if sess.replaying and kind not in (
            "available_commands_update",
            "config_option_update",
            "current_mode_update",
            "plan",
            "session_info_update",
        ):
            return
        if kind == "session_info_update":
            # the CLI generates the session title after session/new —
            # without it an untitled topic never gets its rename
            title = update.get("title")
            if title and not sess.title:
                sess.title = str(title)
            return
        if kind == "available_commands_update":
            commands = update.get("availableCommands")
            if isinstance(commands, list):
                sess.commands = [
                    {
                        "name": str(cmd.get("name") or ""),
                        "description": str(cmd.get("description") or ""),
                        "category": str(
                            cmd.get("_meta", {}).get("cognition.ai/category") or ""
                        ),
                        "hint": str(cmd.get("input", {}).get("hint") or "")
                        if isinstance(cmd.get("input"), dict)
                        else "",
                    }
                    for cmd in commands
                    if isinstance(cmd, dict) and cmd.get("name")
                ]
            return
        if kind == "config_option_update":
            for opt in update.get("configOptions") or []:
                if isinstance(opt, dict) and opt.get("currentValue") is not None:
                    sess.config[str(opt["id"])] = str(opt["currentValue"])
            return
        if kind == "current_mode_update":
            mode = update.get("currentModeId")
            if mode:
                sess.config["mode"] = str(mode)
            return
        if kind == "agent_message_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                sess.buffer.append(text)
            return
        if kind == "agent_thought_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                # thought chunks arrive one complete line per update —
                # joining them raw would fuse the narration into run-ons
                sess.thought = (sess.thought + "\n" + text if sess.thought else text)[-4096:]
                lines = sess.thought.strip().splitlines()
                if lines:
                    line = lines[-1]
                    if len(line) > 120:
                        tail = line[-120:]
                        # drop the first token only when the cut fell
                        # inside a word: the char before the tail and the
                        # tail's first char are both non-whitespace
                        if not (line[-121].isspace() or tail[:1].isspace()):
                            parts = tail.split(None, 1)
                            tail = parts[1] if len(parts) > 1 else ""
                        sess.activity = tail.lstrip()
                    else:
                        sess.activity = line
            return
        if kind in {"tool_call", "tool_call_update"}:
            # rawInput.command holds the full command ("rtk --version")
            # while title is just "Ran rtk" — prefer the real command
            raw = update.get("rawInput")
            cmd = raw.get("command") if isinstance(raw, dict) else None
            title = cmd or update.get("title") or update.get("kind")
            if title:
                sess.activity = _scrub_command(str(title))[:120]
