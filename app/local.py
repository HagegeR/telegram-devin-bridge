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

# cost_tier -> list icon, from `devin models list --format json`;
# dict order doubles as the price sort order (cheapest first)
MODEL_TIER_ICONS = {
    "Free": "🎁",
    "Low cost": "🏷️",
    "Med cost": "💰",
    "High cost": "💎",
}
_MODEL_TIER_RANK = {tier: i for i, tier in enumerate(MODEL_TIER_ICONS)}


def sort_models(
    models: list[str], costs: dict[str, str], rank: dict[str, int]
) -> list[str]:
    """Order by price then novelty: untiered entries lead in input order
    (keeps adaptive, the recommended default, first), then Free -> High
    cost; within a tier newer families first (`models list` emits
    families newest-first, and model_rank() maps each uid to its
    family's index)."""
    unranked = max(rank.values(), default=-1) + 1
    return sorted(
        models,
        key=lambda m: (
            _MODEL_TIER_RANK.get(costs.get(m, ""), -1),
            rank.get(m, unranked) if costs.get(m, "") else 0,
        ),
    )


def is_local(session_id: str) -> bool:
    return session_id.startswith(LOCAL_PREFIX)


# Modes that approve tool calls without ever emitting
# session/request_permission — picking one hands out unattended host
# access, so selecting one is an admin decision.
APPROVAL_SKIPPING_MODES = frozenset({"bypass", "smart"})

# Every mode the local ACP backend offers. The cloud v3 API shares some
# names ('smart', 'ask', 'plan' overlap with devin modes) but never
# accepts an ACP-only mode — cloud creation rejects these even when the
# org's mode list can't be probed.
ACP_SESSION_MODES = frozenset(
    {"accept-edits", "smart", "ask", "plan", "bypass"}
)


@dataclass
class _AcpSession:
    proc: asyncio.subprocess.Process
    acp_id: str
    title: str = ""
    # the prompt head the CLI derives its provisional session title from —
    # a session_info_update title that prefixes it is not a real title
    prompt_head: str = ""
    # a title the user or /new set explicitly — later session_info_update
    # titles must not replace it
    title_explicit: bool = False
    events: list[DevinMessage] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    next_event: int = 0
    # event ids are epoch-ms based so they stay ahead of the watcher's
    # persisted cursor even after a restart swaps in a fresh process
    event_base: int = field(default_factory=lambda: int(time.time() * 1000))
    turns: int = 0
    # turns actually writing to the ACP process right now — a turn that is
    # only queued behind tail doesn't count, so the previous turn's buffer
    # still drains as its own event instead of merging with the next one
    inflight: int = 0
    tail: asyncio.Task | None = None
    flusher: asyncio.Task | None = None
    dead: bool = False
    # the bridge stopped the process itself (shutdown, evict, suspend,
    # wedge respawn, /stop) — mid-turn failures during one are
    # interruptions already queued for replay, not crashes to report
    reaped: bool = False
    # turns in flight that must never surface their reply — each hidden
    # turn (login, preamble) suppresses exactly itself, so a login turn
    # finishing early can't unhide the preamble queued behind it
    suppressed_inflight: int = 0
    # every prompt with a live session/prompt request, in send order —
    # the durable recovery set: pending_prompt tracks the oldest one so
    # a restart replays a turn that is still owed an answer even after
    # the run owner already settled
    inflight_msgs: list[str] = field(default_factory=list)
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
    # drain task handle — awaited at reader exit so the tail is complete
    # before the error detail is built
    stderr_task: asyncio.Task[None] | None = None
    # last stdout byte — the wedge watchdog's liveness signal (stderr and
    # silence don't count: a wedged CLI keeps logging internal RPC)
    last_activity: float = field(default_factory=time.monotonic)
    # in-flight session/list wedge probe — one at a time
    wedge_probe: asyncio.Task | None = None
    # the running turn is a watchdog auto-retry — if it wedges too it is
    # left failed instead of looping; cleared when the turn ends
    replay_turn: bool = False

    @property
    def running(self) -> bool:
        return self.turns > 0


# detached sessions stay addressable for /resume, but bound them: each
# owns a subprocess, so cap the pool and evict the oldest first
_MAX_SESSIONS = 32
# turns can run for a long time; prompts get no deadline (they end on
# stopReason or terminate), everything else uses the control timeout
_CONTROL_TIMEOUT = 30
# session/load replays the persisted history on a fresh process — the
# CLI's session_db work runs well past the control timeout on a busy
# host (observed ~25s/resume at startup, over 30s under contention),
# and a load timeout reads as "session gone" and eats the pending
# prompt. Load gets its own budget.
_LOAD_TIMEOUT = 120
# session/new pays first-use costs the control timeout can't absorb:
# MCP description probes (a broken stdio server retries for ~15-30s)
# and skills-discovery scans. A timeout here kills the subprocess
# mid-handshake, so the turn reads as a hard failure — give it the
# same budget as session/load.
_NEW_TIMEOUT = 120
# _request's default-timeout marker — resolves to _CONTROL_TIMEOUT at
# call time (a def-time default would freeze the constant for tests);
# an explicit None still means "no deadline" for long-running prompts
_CONTROL_DEFAULT = object()
# pause before the single retry a retryable session/prompt error earns —
# instant re-issue can hit the same just-closed agent channel
_RETRY_BACKOFF = 1.0
# a live acp process whose turn emits nothing AND that stops answering
# control requests is wedged inside the CLI (observed: emit(ToolUpdate)
# blocked 40+min on a stalled internal channel) — respawning and reloading
# the persisted session recovers it, waiting recovers nothing. Silence
# alone isn't proof (a quiet tool call can legitimately run for minutes),
# so the probe disambiguates.
_WEDGE_CHECK_SECONDS = 60
_WEDGE_SILENCE_SECONDS = 300
_WEDGE_PROBE_TIMEOUT = 30


# ACP replies can carry whole files; stdout is read in chunks and split
# on newlines manually because the default 64KB StreamReader limit used
# to crash the reader and orphan every turn on that session
_STREAM_CHUNK = 65536
# ...but one boundless JSON-RPC line would still stall the loop (parse
# cost, memory) — drop lines past 16MB; real updates are nowhere near
# that size, and a dropped line that carried a pending response settles
# its turn with an error instead of leaving it waiting
_ACP_MAX_LINE = 1 << 24
_PENDING_ID_RE = re.compile(rb'"id"\s*:\s*(\d+)')


# structured agent logs ("2026-10-12T22:19:57Z INFO target: msg") are
# journal material, not something to relay into the user's chat
# tool-call serializations like functions.execute:0{"command": …} are
# provisional titles the CLI derives from its first activity, not titles
_TOOL_TITLE = re.compile(r"^[\w.]+:\d+\s*\{")
_LOG_LINE = re.compile(
    r"^\S+\s+(?:INFO|DEBUG|TRACE|WARN(?:ING)?|ERROR)\s"
)


# the truncated provisional is ~80 chars; a shorter title that merely
# ends in an ellipsis is a real title that chose that punctuation
_PROVISIONAL_MIN_LEN = 60


def _is_provisional_title(title: str) -> bool:
    # the CLI's provisional title is the prompt/activity head truncated to
    # ~80 chars ending in "..." — suffix plus length marks provisionals
    # the prompt-head prefix check misses (reply-derived, or replayed on
    # a resumed session whose prompt_head is empty)
    normalized = " ".join(title.split())
    return len(normalized) >= _PROVISIONAL_MIN_LEN and normalized.endswith(
        ("...", "…")
    )


class _TurnRescheduledError(Exception):
    """A queued turn's process died — its prompt moved to the resumed
    process; the turn's own failure is not user-visible."""


class _RpcError(RuntimeError):
    """An ACP error response carrying the raw error object — the CLI
    flags transient ones with `cognition.ai/retryable`."""

    def __init__(self, method: str, error: dict) -> None:
        super().__init__(f"{method}: {error}")
        self.error = error

    @property
    def retryable(self) -> bool:
        data = self.error.get("data")
        return (
            isinstance(data, dict)
            and data.get("cognition.ai/retryable") is True
        )


def _clean_reason(exc: BaseException) -> str:
    # object reprs (<WriteUnixTransport ...>) and embedded newlines are
    # debugging noise in chat — collapse to a single capped line
    text = re.sub(r"<[^>\n]+>", "…", str(exc))
    text = " ".join(text.split())
    return text[:160].rstrip() + ("…" if len(text) > 160 else "")


_RATE_LIMIT_CODE = -32010
_RATE_LIMIT_RESET = re.compile(
    r"reset in (\d+) minutes \(at ([0-9]{1,2}:[0-9]{2})", re.IGNORECASE
)


def _rate_limit_options(exc: BaseException) -> str | None:
    # -32010 = free model rate limit — offer the two sane recoveries as
    # buttons instead of making the user retype the intent
    if not (
        isinstance(exc, _RpcError) and exc.error.get("code") == _RATE_LIMIT_CODE
    ):
        return None
    match = _RATE_LIMIT_RESET.search(str(exc))
    wait = (
        f"Wait until {match.group(2)} UTC and retry"
        if match
        else "Wait for the rate limit reset and retry"
    )
    return f"{wait} | Switch to a different model"


def _exit_detail(stderr_tail: str) -> str:
    # the tail is mostly the agent's own structured logs — surface only
    # non-log lines (panics, tracebacks); the first line can be a
    # mid-line fragment of the -400 cut, so drop it when there is more
    lines = stderr_tail[-400:].splitlines()
    plain = [
        line.strip()
        for line in (lines[1:] or lines)
        if line.strip() and not _LOG_LINE.match(line)
    ]
    return f": {plain[-1][-160:]}" if plain else ""


# stream interim reply text during a turn — waiting for stopReason alone
# leaves the topic silent for many minutes on long tasks
_FLUSH_INTERVAL = 20
# but never let a single paragraph-less blob accumulate unbounded — emit
# it anyway rather than holding the turn's whole output back
_STREAM_MAX_BUFFER = 3072
# the activity line posts rawInput.command to Telegram — blank anything
# that looks like a credential before it can leak to the chat. Each token
# is a single greedy run and the keyword test happens on the token in
# Python — the old one-regex approach with adjacent unbounded [\w-]*
# quantifiers backtracked cubically on inputs like "-key-key-…"
_KEYWORD_RE = re.compile(r"(?i)key|token|secret|passw(?:or)?d|credential|auth")
_TOKEN_RE = re.compile(r"--?[\w-]+|[\w-]+")
_SECRET_VALUE_RE = re.compile(r"(?:[\"'][^\"']*[\"']?|\S+)")
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


# code spans can use longer backtick runs (``code``) — capture the whole
# span plus the delimiter length so lone backticks in content stay intact;
# re.split yields [prose, span, delim, prose, …] so spans are every i%3!=0
_INLINE_CODE_RE = re.compile(r"((`+)(?:(?!\2).)+\2)")
_END_BLOCK_RE = re.compile(r"^END (TABLE|DETAILS|SUGGEST)$")


def _split_runons(text: str) -> str:
    out: list[str] = []
    in_fence = False
    in_block = False
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        if in_fence:
            out.append(line)
            continue
        if _END_BLOCK_RE.match(line.strip()):
            in_block = False
            out.append(line)
            continue
        if in_block:
            out.append(line)
            continue
        if (
            stripped == "TABLE:"
            or stripped.startswith(("DETAILS:", "SUGGEST:"))
        ):
            in_block = True
            out.append(line)
            continue
        if _MARKER_LINE_RE.match(line):
            out.append(line)
            continue
        # split only the prose segments — inline `code` spans keep
        # punctuation like `flag:True` intact
        out.append(
            "".join(
                _RUNON_RE.sub("\n", seg) if i % 3 == 0
                else seg if i % 3 == 1
                else ""
                for i, seg in enumerate(_INLINE_CODE_RE.split(line))
            )
        )
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
    # mask the value after `name=`, `name:`, `--name `, and after a
    # bearer/basic scheme word — for any name that contains a credential
    # keyword, however long the name is
    out: list[str] = []
    pos = 0
    for m in _TOKEN_RE.finditer(cmd):
        if m.start() < pos:
            continue  # inside an already-masked value
        name, bare = m.group(0), m.group(0).lstrip("-")
        scheme = bare.lower() in ("bearer", "basic")
        if not (scheme or _KEYWORD_RE.search(bare)):
            continue
        j = m.end()
        while j < len(cmd) and cmd[j].isspace():
            j += 1
        if j < len(cmd) and cmd[j] in "=:":
            j += 1
            while j < len(cmd) and cmd[j].isspace():
                j += 1
            v = _SECRET_VALUE_RE.match(cmd, j)
            if v is not None and v.group(0).lower() in ("bearer", "basic"):
                # "Authorization: Bearer x" — the scheme word goes too
                j2 = v.end()
                while j2 < len(cmd) and cmd[j2].isspace():
                    j2 += 1
                tail = _SECRET_VALUE_RE.match(cmd, j2)
                if tail is not None:
                    out += [cmd[pos : v.start()], "***"]
                    pos = tail.end()
                    continue
        elif name.startswith("-") or scheme:
            # flag or scheme word separated by whitespace (`--token x`,
            # `Bearer x`) — a bare `NAME x` is not treated as a secret
            v = _SECRET_VALUE_RE.match(cmd, j) if j > m.end() else None
        else:
            continue
        if v is not None:
            out += [cmd[pos : v.start()], "***"]
            pos = v.end()
    out.append(cmd[pos:])
    return _USERINFO_RE.sub("***@", "".join(out))


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
        self.on_mode_change: Callable[[str, str], None] | None = None
        self._terminated: set[str] = set()
        self._resuming: dict[str, asyncio.Task] = {}
        self._wedge_task: asyncio.Task | None = None
        # prompts owed to a session whose process died — replayed in send
        # order on the next live process (wedge retry, queued-behind-dead
        # turns, interrupted-turn replay on dead-session reload)
        self._interrupted: dict[str, list[str]] = {}
        # in-flight _drain_interrupted per session — keeps replays ordered
        self._draining: dict[str, asyncio.Task] = {}
        self._next_id = 0
        self._last_event_base = 0
        self._modes_cache: tuple[float, list[str]] | None = None
        self._costs_cache: tuple[float, dict[str, str]] | None = None
        self._model_rank: dict[str, int] = {}
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
            }, timeout=_NEW_TIMEOUT)
            created = await self._request(sess, "session/new", {
                "cwd": self._cwd(),
                "mcpServers": [],
            }, timeout=_NEW_TIMEOUT)
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
            self._model_rank = {
                str(v["model_uid"]): i
                for i, f in enumerate(payload.get("families", []))
                if isinstance(f, dict)
                for v in f.get("variants", [])
                if isinstance(v, dict) and v.get("model_uid")
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

    def model_rank(self) -> dict[str, int]:
        """{model_uid: family index} captured by the model_costs() probe —
        `models list` emits families newest-first, so lower means newer.
        {} until the first successful probe."""
        return self._model_rank

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
        # a stored value can outlive the CLI's accepted set (a model or
        # CLI update removed it) — substitute the CLI's own default with
        # a warning instead of failing the apply with Invalid params
        if config_id in ("model", "thought_level"):
            await self.modes()  # fills the option caches (one probe)
            options, fallback = (
                (self._model_options, self._default_model)
                if config_id == "model"
                else (self._think_options, self._default_thought)
            )
            if options and value not in options and fallback in options:
                self._emit(
                    sess,
                    f"⚠ {config_id} '{value}' is not available — "
                    f"using '{fallback}'",
                )
                value = fallback
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
        suppress_first: bool = False,
        **_: object,
    ) -> tuple[str, str]:
        cwd = self._cwd()
        sess = await self._spawn(init_timeout=_NEW_TIMEOUT)
        # set before session/new: a session_info_update can be read before
        # the response returns, and needs the prompt head for the
        # provisional-title check
        sess.prompt_head = prompt
        try:
            created = await self._request(sess, "session/new", {
                "cwd": cwd,
                "mcpServers": [],
            }, timeout=_NEW_TIMEOUT)
        except (RuntimeError, TimeoutError, asyncio.CancelledError):
            _terminate(sess.proc)
            raise
        sess.acp_id = str(created["sessionId"])
        # don't default "" here — the CLI's generated title can already
        # have arrived via session_info_update and would be clobbered
        if title is not None:
            sess.title = title
            sess.title_explicit = True
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
            await self.send_message(
                session_id, f"/login {self.api_key}", suppress=True
            )
        # the caller's first turn may be context, not content (the system
        # preamble for a command-created session) — suppress drops its
        # reply, and create_session waits for it so the next prompt
        # starts as a clean turn, not a steer into a suppressed one
        await self.send_message(session_id, prompt, suppress=suppress_first)
        if suppress_first and sess.tail is not None:
            try:
                await sess.tail
            except (RuntimeError, TimeoutError, _TurnRescheduledError) as exc:
                # a failed or rescheduled preamble turn isn't fatal — the
                # caller's next prompt surfaces a dead session anyway
                logger.debug("suppressed first turn failed: %s", exc)
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
        if sess.pending_prompt is None and sess.inflight_msgs:
            # the owner settled while steers still fly — the durable
            # marker follows the oldest unsettled prompt so a bridge
            # restart replays a turn that's still owed an answer instead
            # of losing it with the finished owner
            sess.pending_prompt = sess.inflight_msgs[0]
        self._prompt_state(session_id, sess.pending_prompt)

    async def send_message(
        self, session_id: str, message: str, *, suppress: bool = False
    ) -> None:
        sess = await self._get_or_resume(session_id)
        if sess is None:
            raise RuntimeError(f"local session {session_id} is gone")
        if sess.dead:
            raise RuntimeError(f"local session {sess.acp_id} is gone")
        if sess.cfg_pending:
            await self._apply_resume_config(session_id, sess)
        prev = sess.tail
        # order-sensitive sends (history replays, drained interrupted
        # turns, hidden preamble/login turns — or anything sent while one
        # of those is still running) must serialize behind every earlier
        # request. Ordinary messages don't wait: a prompt that reaches
        # the process while a request is open is a steer — the CLI hands
        # it to the running turn (this is how /goal pause|resume|clear
        # and ordinary follow-ups reach a goal mid-run: a goal turn stays
        # open until the goal finishes, so chaining would lock every
        # follow-up out for the whole run).
        queued = (
            suppress
            or sess.replay_turn
            or sess.replaying
            or bool(sess.suppressed_inflight)
        )
        sess.turns += 1

        async def _turn() -> dict:
            if queued:
                if prev is not None:
                    try:
                        await prev
                    except (RuntimeError, TimeoutError, asyncio.CancelledError) as exc:
                        logger.debug("prior local turn failed: %s", exc)
                while sess.inflight:
                    # tail can't promise ordering — a short steer can end
                    # while an earlier request (a goal run) still flies
                    await asyncio.sleep(_FLUSH_INTERVAL)
            if sess.inflight == 0:
                # the first live request owns the run: reset the stream
                # state and take the durable marker. A steer skips both —
                # deciding here instead of on tail.done() keeps a quick
                # steer from making the next prompt look like a clean
                # turn that wipes a still-running goal's buffer/marker
                sess.activity = ""
                sess.thought = ""
                sess.buffer.clear()
                sess.replaying = False
                # the prompt's Telegram row was already consumed; keep it
                # durable until the turn settles so a bridge restart
                # mid-turn replays it instead of losing the user's
                # question
                sess.pending_prompt = None if suppress else message
                self._prompt_state(session_id, sess.pending_prompt)
            if sess.dead:
                # queued behind a turn whose process died — writing to a
                # dead pipe would hang forever; the durable marker above
                # keeps it safe and the resumed process replays it in
                # send order via _deliver_interrupted
                self._interrupted.setdefault(session_id, []).append(message)
                raise _TurnRescheduledError(sess.acp_id)
            sess.inflight += 1
            sess.inflight_msgs.append(message)
            if suppress:
                sess.suppressed_inflight += 1
            try:
                for attempt in range(2):
                    try:
                        result = await self._request(sess, "session/prompt", {
                            "sessionId": sess.acp_id,
                            "prompt": [{"type": "text", "text": message}],
                        }, timeout=None)
                        break
                    except _RpcError as exc:
                        if attempt or sess.dead or not exc.retryable:
                            raise
                        # the CLI marked the failure retryable (e.g. a
                        # dropped agent channel) — one retry keeps a
                        # transient blip from eating the prompt. Log only
                        # the error kind: the raw object can echo the
                        # prompt, which may carry a secret (/login keys)
                        data = exc.error.get("data")
                        kind = (
                            data.get("cognition.ai/errorKind")
                            if isinstance(data, dict)
                            else None
                        )
                        logger.info(
                            "retrying local prompt for %s after %s",
                            sess.acp_id,
                            kind or exc.error.get("code"),
                        )
                        await asyncio.sleep(_RETRY_BACKOFF)
            except asyncio.CancelledError:
                # shutdown mid-turn: keep the pending marker for replay
                raise
            except BaseException:
                # a dead process means the turn was interrupted (shutdown,
                # crash, evict), not answered — keep the marker for replay;
                # only a live-process failure clears it
                if sess.dead:
                    # a steered prompt died in flight — it isn't the
                    # run-owner's marker, so queue it for replay on the
                    # resumed process in send order like a queued turn
                    if sess.pending_prompt != message:
                        self._interrupted.setdefault(
                            session_id, []
                        ).append(message)
                else:
                    self._clear_pending_prompt(sess, session_id, message)
                raise
            finally:
                sess.inflight -= 1
                if suppress:
                    sess.suppressed_inflight -= 1
                if message in sess.inflight_msgs:
                    sess.inflight_msgs.remove(message)
            self._clear_pending_prompt(sess, session_id, message)
            return result

        sess.tail = asyncio.create_task(_turn())
        sess.tail.add_done_callback(
            lambda f, s=suppress: self._turn_done(sess, f, s)
        )
        if sess.flusher is None:
            sess.flusher = asyncio.create_task(self._flusher(sess))
        if self._wedge_task is None or self._wedge_task.done():
            self._wedge_task = asyncio.create_task(self._wedge_watchdog())

    async def _flusher(self, sess: _AcpSession) -> None:
        try:
            while not sess.dead:
                await asyncio.sleep(_FLUSH_INTERVAL)
                if not sess.turns or sess.suppressed_inflight:
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

    def _turn_done(
        self, sess: _AcpSession, fut: asyncio.Future, suppress: bool = False
    ) -> None:
        sess.turns -= 1
        if not sess.turns:
            # clear only once every in-flight turn has ended — a steered
            # turn finishing early must not unmark a replay still running
            sess.replay_turn = False
        if suppress:
            if "".join(sess.buffer).strip():
                logger.warning(
                    "local turn output discarded by a suppressed turn for %s",
                    sess.acp_id,
                )
            sess.buffer.clear()
            return
        try:
            exc = fut.exception()
        except asyncio.CancelledError:
            exc = None
        if isinstance(exc, _TurnRescheduledError):
            # moved to the resumed process — not a user-visible failure
            return
        if exc is not None:
            if sess.reaped:
                # we stopped the process — the prompt is queued for
                # replay, so a ⚠ here reads as a crash on every deploy
                logger.info(
                    "local turn interrupted by reap for %s: %s",
                    sess.acp_id,
                    exc,
                )
            else:
                logger.warning(
                    "local prompt failed for %s: %s", sess.acp_id, exc
                )
                options = _rate_limit_options(exc)
                suffix = f"\nOPTIONS: {options}" if options else ""
                self._emit(
                    sess, f"⚠ prompt failed: {_clean_reason(exc)}{suffix}"
                )
        elif fut.result().get("error"):
            self._emit(sess, f"⚠ {fut.result()['error'].get('message', 'error')}")
        if sess.inflight:
            # a steered turn ended inside a still-running turn — its reply
            # is already in the shared buffer; the last live turn (or the
            # flusher) drains it so no paragraph is cut mid-run
            return
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
    def _pick_reject(msg: dict) -> str | None:
        options = msg.get("params", {}).get("options") or []
        pick = next(
            (
                o for o in options
                if any(
                    word
                    in str(o.get("kind", "") + o.get("name", "")).lower()
                    for word in ("reject", "deny")
                )
            ),
            None,
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

    def _deny_permission(
        self, sess: _AcpSession, req_id: int | str, msg: dict
    ) -> None:
        """Fail closed: answer with the reject option, or the ACP
        cancelled outcome when the request offers none — used when the
        approval prompt cannot reach a decider."""
        option_id = self._pick_reject(msg)
        if option_id is not None:
            self._answer_permission(sess, req_id, option_id)
        elif sess.proc.stdin is not None:
            sess.proc.stdin.write(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                }).encode() + b"\n"
            )

    def reject_permission(self, session_id: str, req_id: int | str) -> None:
        """Deny a pending session/request_permission (fail-closed path).
        No-op when the request or session is already gone."""
        sess = self.sessions.get(session_id)
        if sess is None:
            return
        msg = sess.permissions.pop(req_id, None)
        if msg is not None:
            self._deny_permission(sess, req_id, msg)

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
            # resume so polls keep tracking instead of going expired. A
            # resume deadline propagates as TimeoutError so the watcher
            # retries within its poll-error budget instead of closing
            # the turn on a transiently slow load.
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
        if self._wedge_task is not None:
            self._wedge_task.cancel()
        for sess in list(self.sessions.values()):
            await self._reap(sess)
        self.sessions.clear()
        self._resuming.clear()

    async def suspend(self, session_id: str) -> None:
        """Stop the acp process without deleting the persisted session —
        a detached session stops acting on the host but stays
        resumable, unlike terminate which removes its record."""
        sess = self.sessions.pop(session_id, None)
        if sess is not None:
            await self._reap(sess)

    async def terminate(self, session_id: str) -> None:
        sess = self.sessions.pop(session_id, None)
        self._terminated.add(session_id)
        self._interrupted.pop(session_id, None)
        if sess is None:
            # dormant session: /stop must still delete its persisted record,
            # else a later bridge could resume it
            try:
                sess = await self._spawn()
            except (OSError, RuntimeError, TimeoutError):
                return
            sess.acp_id = session_id.removeprefix(LOCAL_PREFIX)
        sess.dead = True
        sess.reaped = True
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
        sess.reaped = True
        if sess.flusher is not None:
            sess.flusher.cancel()
        _terminate(sess.proc)
        try:
            await asyncio.wait_for(sess.proc.wait(), 5)
        except TimeoutError:
            _kill(sess.proc)

    async def _wedge_watchdog(self) -> None:
        """Probe running turns that go silent; respawn the acp process
        when the probe also goes unanswered — the session reloads from
        the CLI's persisted DB on the fresh process."""
        try:
            while self.sessions:
                await asyncio.sleep(_WEDGE_CHECK_SECONDS)
                now = time.monotonic()
                for session_id, sess in list(self.sessions.items()):
                    if (
                        sess.running
                        and not sess.dead
                        and sess.wedge_probe is None
                        and now - sess.last_activity > _WEDGE_SILENCE_SECONDS
                    ):
                        sess.wedge_probe = asyncio.create_task(
                            self._probe_wedge(session_id, sess)
                        )
        except asyncio.CancelledError:
            pass

    async def _probe_wedge(self, session_id: str, sess: _AcpSession) -> None:
        try:
            try:
                await self._request(
                    sess,
                    "session/list",
                    {"cwd": self._cwd()},
                    timeout=_WEDGE_PROBE_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except (RuntimeError, TimeoutError, OSError):
                pass
            else:
                # answered — a legitimately quiet long-running turn
                sess.last_activity = time.monotonic()
                return
            if sess.dead or not sess.running:
                return
            logger.warning(
                "local acp session %s wedged — respawning", sess.acp_id
            )
            # a prompt that already wedged a replayed process is not
            # retried a second time — it stays failed
            prompt = sess.pending_prompt
            replay = prompt is not None and not sess.replay_turn
            if replay:
                # queued turns append theirs as their dead-guard fires
                # during _reap — the list keeps the original send order
                self._interrupted.setdefault(session_id, []).append(prompt)
            self._emit(
                sess,
                "⚠ the local CLI stopped answering mid-turn — restarting"
                " the session process"
                + (
                    " and retrying the prompt"
                    if replay
                    else "; resend your last message" if prompt else ""
                ),
            )
            # unmap so _get_or_resume spawns a fresh process — a dead
            # session left in the map fails every send as 'gone'
            self.sessions.pop(session_id, None)
            await self._reap(sess)
            try:
                await self._deliver_interrupted(session_id)
            except (RuntimeError, TimeoutError) as exc:
                logger.warning(
                    "wedge replay failed for %s: %s", session_id, exc
                )
        finally:
            sess.wedge_probe = None

    async def _deliver_interrupted(self, session_id: str) -> None:
        """Dedupe entry to the per-session drain — concurrent callers
        (watchdog, status poll, next send) share one drain so list order
        stays send order."""
        task = self._draining.get(session_id)
        if task is asyncio.current_task():
            return
        if task is None or task.done():
            task = asyncio.create_task(self._drain_interrupted(session_id))
            self._draining[session_id] = task
        await task

    async def _drain_interrupted(self, session_id: str) -> None:
        """Replay owed prompts in send order on a live process, marking
        each as a replay turn so a second wedge doesn't loop."""
        queue = self._interrupted.get(session_id)
        while queue:
            sess = self.sessions.get(session_id)
            if sess is None:
                if session_id in self._terminated:
                    self._interrupted.pop(session_id, None)
                    return
                try:
                    sess = await self._resume(session_id)
                except (RuntimeError, TimeoutError, OSError) as exc:
                    logger.warning(
                        "interrupted-turn resume failed for %s: %s",
                        session_id, exc,
                    )
                    return
            if sess.dead:
                return  # leave the queue for the next contact
            sess.replay_turn = True
            prompt = queue[0]
            try:
                await self.send_message(session_id, prompt)
            except (RuntimeError, TimeoutError) as exc:
                logger.warning(
                    "interrupted-turn send failed for %s: %s",
                    session_id, exc,
                )
                return
            queue.pop(0)

    async def _evict(self) -> None:
        while len(self.sessions) >= _MAX_SESSIONS:
            # capacity eviction is lifecycle, not /stop — keep the record
            key = next(iter(self.sessions))
            victim = self.sessions.pop(key)
            if victim.pending_prompt:
                # evicting mid-turn — the prompt survives for replay
                # on next contact, like a watchdog respawn; the owner was
                # sent before any steer already in the queue
                self._interrupted.setdefault(key, []).insert(
                    0, victim.pending_prompt
                )
            await self._reap(victim)

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

    async def _spawn(self, init_timeout: float | None = None) -> _AcpSession:
        """Spawn a `devin acp` process and complete the ACP handshake.

        init_timeout bounds the initialize reply; None keeps the 30s
        control timeout. Create paths pass _NEW_TIMEOUT — the CLI's init
        handshake pays the same first-use costs as session/new on a busy
        host, and a 30s abort here fails the turn before the create
        budget can help."""
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
        sess.stderr_task = asyncio.create_task(self._drain_stderr(sess))
        try:
            await self._request(
                sess,
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {
                        "name": "telegram-devin-bridge",
                        "version": "0",
                    },
                },
                timeout=_CONTROL_TIMEOUT
                if init_timeout is None
                else init_timeout,
            )
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
        if sess is not None and sess.dead:
            # a process that died outside the watchdog stays mapped —
            # unmap it so the session reloads on a fresh process instead
            # of failing every send as 'gone' until a bridge restart
            self.sessions.pop(session_id)
            if sess.pending_prompt:
                # the dead process was mid-turn — replay that prompt on
                # the fresh process instead of letting the next send
                # overwrite the durable marker silently. In-flight steers
                # dead-guarded into the queue first; the owner was sent
                # before them, so it replays first too
                self._interrupted.setdefault(
                    session_id, []
                ).insert(0, sess.pending_prompt)
            sess = None
        if sess is None and session_id not in self._terminated:
            sess = await self._resume(session_id)
        if sess is not None and not sess.dead:
            await self._deliver_interrupted(session_id)
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
        if existing is not None and not existing.dead:
            return existing
        acp_id = session_id.removeprefix(LOCAL_PREFIX)
        sess = await self._spawn()
        try:
            loaded = await self._request(sess, "session/load", {
                "sessionId": acp_id,
                "cwd": self._cwd(),
                "mcpServers": [],
            }, timeout=_LOAD_TIMEOUT)
        except TimeoutError:
            _terminate(sess.proc)
            # propagate unwrapped: a deadline is "not yet", not "gone" —
            # callers retry it (watcher poll budget, next contact) instead
            # of reading the session as expired
            raise
        except RuntimeError as exc:
            _terminate(sess.proc)
            raise RuntimeError(
                "local session "
                f"{acp_id} couldn't resume: {str(exc) or type(exc).__name__}"
            ) from exc
        sess.acp_id = acp_id
        for opt in loaded.get("configOptions", []):
            if isinstance(opt, dict) and opt.get("currentValue") is not None:
                sess.config[str(opt["id"])] = str(opt["currentValue"])
        sess.replaying = True
        # a replayed session_info_update may already have set the title —
        # keep it when session/list has none instead of clobbering to ""
        listed_title = await self._session_title(sess, acp_id)
        if listed_title:
            sess.title = listed_title
            # the persisted title is authoritative — replayed provisional
            # updates (prompt_head is empty on a resumed session) must not
            # replace it
            sess.title_explicit = True
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
            # same startup contention that can stall session/load applies
            # here — a 30s timeout would silently drop the restored title
            listed = await self._request(sess, "session/list", {
                "cwd": self._cwd(),
            }, timeout=_LOAD_TIMEOUT)
            for entry in listed.get("sessions", []):
                if entry.get("sessionId") == acp_id:
                    title = str(entry.get("title") or "")
                    # a persisted provisional is a truncated prompt head,
                    # not a title — leave the session untitled so a real
                    # generated title can still land
                    return "" if _is_provisional_title(title) else title
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

    def _send(
        self, sess: _AcpSession, method: str, params: dict
    ) -> tuple[int, asyncio.Future]:
        self._next_id += 1
        req_id = self._next_id
        if sess.dead:
            # a request sent after the reader settled pending futures would
            # never resolve — fail fast so callers take the dead-session path
            raise RuntimeError("acp process exited")
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        sess.pending[req_id] = fut
        assert sess.proc.stdin is not None
        sess.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode()
            + b"\n"
        )
        return req_id, fut

    async def _request(
        self,
        sess: _AcpSession,
        method: str,
        params: dict,
        *,
        timeout: float | None = _CONTROL_DEFAULT,
    ) -> dict:
        if timeout is _CONTROL_DEFAULT:
            timeout = _CONTROL_TIMEOUT
        req_id, fut = self._send(sess, method, params)
        try:
            result = await fut if timeout is None else await asyncio.wait_for(fut, timeout)
        except BaseException:
            # drop the timed-out slot — a late reply must find no pending
            # future to resolve, not a cancelled one to set_result over
            sess.pending.pop(req_id, None)
            raise
        if "error" in result:
            raise _RpcError(method, result["error"])
        return result.get("result", {})

    async def _reader(self, sess: _AcpSession) -> None:
        assert sess.proc.stdout is not None
        buf = b""
        while True:
            chunk = await sess.proc.stdout.read(_STREAM_CHUNK)
            if not chunk:
                break
            sess.last_activity = time.monotonic()
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if not raw:
                    continue
                if len(raw) > _ACP_MAX_LINE:
                    self._drop_line(sess, raw)
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
            if len(buf) > _ACP_MAX_LINE:
                # an unterminated oversized line: drop it chunk-by-chunk
                # so memory and downstream scrub cost stay bounded
                self._drop_line(sess, buf)
                buf = b""
        sess.dead = True
        # stdout can close before stderr finishes draining — give the
        # drain a moment so the tail is complete for the detail and the
        # journal line below
        if sess.stderr_task is not None:
            await asyncio.wait({sess.stderr_task}, timeout=5)
        detail = _exit_detail(sess.stderr_tail)
        if sess.stderr_tail.strip():
            logger.warning(
                "acp stderr tail for %s: %s",
                sess.acp_id,
                sess.stderr_tail.strip()[-400:],
            )
        for fut in sess.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"acp process exited{detail}"))

    def _mode_update(self, sess: _AcpSession, mode: str) -> None:
        prev = sess.config.get("mode")
        sess.config["mode"] = mode
        # only a mid-turn switch is the user's choice (e.g. the "switch to
        # bypass mode" permission option) — session/new, session/load and
        # the bridge's own set_mode run between turns. Persist it so the
        # resume re-apply restores it instead of reverting the mode.
        if prev == mode or not sess.inflight or self.on_mode_change is None:
            return
        try:
            self.on_mode_change(f"{LOCAL_PREFIX}{sess.acp_id}", mode)
        except Exception:
            logger.exception("on_mode_change hook failed for %s", sess.acp_id)

    def _drop_line(self, sess: _AcpSession, data: bytes) -> None:
        logger.warning("dropping oversized acp line (%d bytes)", len(data))
        # the line may carry the only response a pending request waits
        # on — settle that turn instead of letting it hang
        m = _PENDING_ID_RE.search(data[:4096])
        if m is None:
            return
        fut = sess.pending.pop(int(m.group(1)), None)
        if fut is not None and not fut.done():
            fut.set_exception(
                RuntimeError("acp reply exceeded the line limit and was dropped")
            )

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
            fut = sess.pending.pop(req_id)
            # a cancelled waiter (timeout) must not raise into the reader
            if not fut.done():
                fut.set_result(msg)
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
                            "on_permission hook failed for %s — denying",
                            sess.acp_id,
                        )
                        sess.permissions.pop(req_id, None)
                        self._deny_permission(sess, req_id, msg)
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
            # the CLI first reports a provisional title — the prompt head
            # with whitespace collapsed, truncated to ~80 chars ending in
            # "..." — then a generated one; ignore the former (compare
            # against the normalized prompt head: a raw prefix check
            # misses the ellipsis and the squashed newlines), let the
            # latter replace it, and never clobber an explicit title
            title = update.get("title")
            if not title:
                return
            title = str(title)
            cand = " ".join(
                title.removesuffix("...").removesuffix("…").split()
            )
            if not sess.title_explicit and (
                not (
                    cand
                    and " ".join(sess.prompt_head.split()).startswith(cand)
                )
                and not _TOOL_TITLE.match(title)
                and not _is_provisional_title(title)
            ):
                sess.title = title
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
                    if opt["id"] == "mode":
                        self._mode_update(sess, str(opt["currentValue"]))
                    else:
                        sess.config[str(opt["id"])] = str(opt["currentValue"])
            return
        if kind == "current_mode_update":
            mode = update.get("currentModeId")
            if mode:
                self._mode_update(sess, str(mode))
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
                # truncate before scrubbing — credential scrubbing on a
                # huge command would stall the event loop
                sess.activity = _scrub_command(str(title)[:512])[:120]
