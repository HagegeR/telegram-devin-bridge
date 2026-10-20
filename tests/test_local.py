import asyncio
import io
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest
import pytest_asyncio

from app.local import (
    LocalClient,
    _AcpSession,
    _clean_reason,
    _exit_detail,
    _kill,
    _scrub_command,
    _split_runons,
    _terminate,
    is_local,
    sort_models,
)

FAKE_ACP = textwrap.dedent(
    """
    import json, os, sys, threading, time
    sid = "fake-sid"
    out_lock = threading.Lock()
    def emit(obj):
        with out_lock:
            print(json.dumps(obj), flush=True)
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if "id" not in m:
            continue
        i, meth = m["id"], m["method"]
        if meth == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": i,
                              "result": {"protocolVersion": 1}}), flush=True)
        elif meth == "session/new":
            if os.environ.get("FAKE_JUNK_LINES"):
                print("X" * 200_000, flush=True)      # under the stream limit
                print("Y" * 5_000_000, flush=True)    # over it — skipped
            print(json.dumps({"jsonrpc": "2.0", "id": i,
                              "result": {"sessionId": sid, "modes": {
                                  "currentModeId": "accept-edits",
                                  "availableModes": [
                                      {"id": "accept-edits"},
                                      {"id": "smart"},
                                  ]}, "configOptions": [{
                                  "id": "mode",
                                  "currentValue": "accept-edits",
                                  "options": [
                                      {"value": "accept-edits",
                                       "name": "Code",
                                       "description": "Write and edit code"},
                                      {"value": "smart",
                                       "name": "Smart",
                                       "description": "Auto-approve safe"},
                                  ]}, {
                                  "id": "model",
                                  "currentValue": "swe-2-high",
                                  "options": [
                                      {"value": "swe-2-high"},
                                      {"value": "swe-2-low"},
                                  ]}, {
                                  "id": "thought_level",
                                  "currentValue": "high",
                                  "options": [
                                      {"value": "medium"},
                                      {"value": "high"},
                                      {"value": "max"},
                                  ]}]}}), flush=True)
            if os.environ.get("FAKE_TOOL_TITLE"):
                # the first activity can also seed the provisional title
                # with serialized tool text, not prompt characters
                print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                                  "params": {"sessionId": sid, "update": {
                                      "sessionUpdate": "session_info_update",
                                      "title": 'functions.execute:0{"command": "ls"}'}}}),
                      flush=True)
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": sid, "update": {
                                  "sessionUpdate": "session_info_update",
                                  # provisional title: the CLI derives it
                                  # from the prompt head — not a real title
                                  "title": os.environ.get(
                                      "FAKE_PROVISIONAL", "hello")}}}),
                  flush=True)
            if "FAKE_PROVISIONAL" not in os.environ:
                print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                                  "params": {"sessionId": sid, "update": {
                                      "sessionUpdate": "session_info_update",
                                      "title": "Generated title"}}}),
                      flush=True)
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": sid, "update": {
                                  "sessionUpdate": "available_commands_update",
                                  "availableCommands": [
                                      {"name": "fast",
                                       "description": "Fastest model",
                                       "input": {"hint": "[prompt]"},
                                       "_meta": {"cognition.ai/category":
                                                 "Session"}},
                                      {"name": "ponytail",
                                       "description": "Lazy dev",
                                       "_meta": {"cognition.ai/category":
                                                 "Skills"}},
                                  ]}}}), flush=True)
        elif meth == "session/set_mode":
            print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {}}), flush=True)
        elif meth == "session/set_config_option":
            print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {}}), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": sid, "update": {
                                  "sessionUpdate": "config_option_update",
                                  "configOptions": [{
                                      "id": m["params"]["configId"],
                                      "currentValue": m["params"]["value"],
                                  }]}}}), flush=True)
        elif meth == "session/prompt":
            text = m["params"]["prompt"][0]["text"]
            if os.environ.get("FAKE_RETRYABLE_ONCE"):
                marker = os.environ["FAKE_RETRYABLE_ONCE"]
                if not os.path.exists(marker):
                    with open(marker, "w") as fh:
                        fh.write("x")
                    print(json.dumps({"jsonrpc": "2.0", "id": i,
                                      "error": {"code": -32603,
                                          "message": "Agent communication"
                                          " channel closed",
                                          "data": {
                                              "cognition.ai/errorKind":
                                              "agent_channel_closed",
                                              "cognition.ai/retryable":
                                              True}}}), flush=True)
                    continue
            if os.environ.get("FAKE_RETRYABLE_ALWAYS"):
                with open(os.environ["FAKE_RETRYABLE_ALWAYS"], "a") as fh:
                    fh.write("x")
                print(json.dumps({"jsonrpc": "2.0", "id": i,
                                  "error": {"code": -32603,
                                      "message": "Agent communication"
                                      " channel closed",
                                      "data": {
                                          "cognition.ai/errorKind":
                                          "agent_channel_closed",
                                          "cognition.ai/retryable":
                                          True}}}), flush=True)
                continue
            if os.environ.get("FAKE_WEDGE"):
                once = os.environ.get("FAKE_WEDGE_ONCE")
                if not once or not os.path.exists(once):
                    if once:
                        with open(once, "w") as fh:
                            fh.write("x")
                    # wedged CLI: no updates, and stdin is ignored so
                    # control requests (the watchdog probe) never answer
                    while True:
                        time.sleep(3600)
            if os.environ.get("FAKE_QUIET"):
                # silent prompt whose control channel stays alive — the
                # wedge probe gets answered, so the turn must not be killed
                import select
                deadline = time.time() + float(os.environ["FAKE_QUIET"])
                while time.time() < deadline:
                    r, _, _ = select.select(
                        [sys.stdin], [], [],
                        max(0, deadline - time.time()),
                    )
                    if not r:
                        break
                    qline = sys.stdin.readline()
                    if not qline:
                        break
                    try:
                        qm = json.loads(qline)
                    except ValueError:
                        continue
                    if qm.get("method") == "session/list":
                        print(json.dumps({"jsonrpc": "2.0", "id": qm["id"],
                                          "result": {"sessions": []}}),
                              flush=True)
            chunks = ["echo: ", text]
            if os.environ.get("FAKE_GOAL"):
                if text.startswith("/goal ") and text != "/goal clear":
                    # a goal run: the response stays open while the run
                    # emits turns on its own — the main loop keeps
                    # reading so a concurrent prompt (a steer, /goal
                    # clear) is answered mid-run like the real CLI
                    def _run(i=i):
                        delay = float(
                            os.environ.get("FAKE_GOAL_DELAY", "0.15"))
                        for n in range(10):
                            emit({"jsonrpc": "2.0",
                                  "method": "session/update",
                                  "params": {"sessionId": sid,
                                             "update": {
                                      "sessionUpdate": "agent_message_chunk",
                                      "content": {"type": "text",
                                                  "text": "tick %d\\n\\n" % n}}}})
                            time.sleep(delay)
                        emit({"jsonrpc": "2.0", "id": i,
                              "result": {"stopReason": "end_turn"}})
                    threading.Thread(target=_run, daemon=True).start()
                    continue
                if text == "/goal clear":
                    # answered while the goal run above is still live
                    emit({"jsonrpc": "2.0", "method": "session/update",
                          "params": {"sessionId": sid, "update": {
                              "sessionUpdate": "agent_message_chunk",
                              "content": {"type": "text",
                                          "text": "Goal cleared."}}}})
                    emit({"jsonrpc": "2.0", "id": i,
                          "result": {"stopReason": "end_turn"}})
                    continue
            if text == "hang steer":
                # a steer that stays in flight past the owner's end —
                # answered on a thread so the main loop keeps reading. The
                # delay must outlive the test body on slow runners; the
                # daemon thread dies with the process at teardown.
                def _hang(i=i):
                    time.sleep(30)
                    emit({"jsonrpc": "2.0", "id": i,
                          "result": {"stopReason": "end_turn"}})
                threading.Thread(target=_hang, daemon=True).start()
                continue
            if os.environ.get("FAKE_PERMISSION"):
                print(json.dumps({"jsonrpc": "2.0", "id": 9000,
                                  "method": "session/request_permission",
                                  "params": {"sessionId": sid, "toolCall": {
                                      "title": "Run rm -rf"},
                                     "options": [
                                         {"optionId": "allow",
                                          "name": "Allow",
                                          "kind": "allow_once"},
                                         {"optionId": "reject",
                                          "name": "Reject",
                                          "kind": "reject_once"},
                                     ]}}), flush=True)
                opt = "?"
                for pline in sys.stdin:      # the turn waits for the decision
                    try:
                        pm = json.loads(pline)
                    except ValueError:
                        continue
                    if pm.get("id") == 9000:
                        opt = pm["result"]["outcome"]["optionId"]
                        break
                chunks = ["granted: " + opt + " | echo: ", text]
            if os.environ.get("FAKE_SLOW"):
                chunks = ["echo: first para\\n\\n", "second", " para tail"]
            if os.environ.get("FAKE_FENCE"):
                chunks = ["intro para\\n\\n", "```python\\ncode ", "line\\n```\\ndone"]
            if os.environ.get("FAKE_JUNK_LINES"):
                # a valid JSON-RPC line larger than the old 64KB limit
                chunks.append("big:" + "Z" * 200_000)
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": sid, "update": {
                                  "sessionUpdate": "agent_thought_chunk",
                                  "content": {"type": "text",
                                              "text": " \\n"}}}}), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": sid, "update": {
                                  "sessionUpdate": "agent_thought_chunk",
                                  "content": {"type": "text",
                                              "text": "musing\\n"}}}}), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": sid, "update": {
                                  "sessionUpdate": "tool_call",
                                  "title": "Running pytest"}}}), flush=True)
            if os.environ.get("FAKE_RTK"):
                print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                                  "params": {"sessionId": sid, "update": {
                                      "sessionUpdate": "tool_call",
                                      "title": "Ran rtk",
                                      "rawInput": {"command": "rtk pytest -q tests/x.py"}}}}), flush=True)
            variant = os.environ.get("FAKE_LONG_THOUGHT")
            if variant:
                long_line = {
                    "midword": ("Orphaned supervise-daemon and uvicorn "
                                "processes, allowing the tracked supervisor "
                                "to restart the correct new instance safely!"),
                    "tab": "a" * 130 + "\tdone",
                    "boundary": "prefix " + "alpha " * 19 + "suffix",
                    "unbroken": "a" * 200,
                }[variant]
                print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                                  "params": {"sessionId": sid, "update": {
                                      "sessionUpdate": "agent_thought_chunk",
                                      "content": {"type": "text",
                                                  "text": long_line + "\\n"}}}}),
                      flush=True)
            for chunk in chunks:
                if os.environ.get("FAKE_SLOW") or os.environ.get("FAKE_FENCE"):
                    time.sleep(0.4)
                print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                                  "params": {"sessionId": sid, "update": {
                                      "sessionUpdate": "agent_message_chunk",
                                      "content": {"type": "text", "text": chunk}}}}),
                      flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": i,
                              "result": {"stopReason": "end_turn"}}), flush=True)
        elif meth == "session/load":
            # the real CLI replays persisted history as session/update
            # notifications during load — those must not be re-emitted
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": sid, "update": {
                                  "sessionUpdate": "agent_message_chunk",
                                  "content": {"type": "text",
                                              "text": "replayed: old"}}}}),
                      flush=True)
            if os.environ.get("FAKE_REPLAY_TITLE"):
                print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                                  "params": {"sessionId": sid, "update": {
                                      "sessionUpdate": "session_info_update",
                                      "title": "replayed provisional"}}}),
                      flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {}}), flush=True)
        elif meth == "session/list":
            delay = os.environ.get("FAKE_LIST_DELAY")
            if delay:
                time.sleep(float(delay))
            print(json.dumps({"jsonrpc": "2.0", "id": i,
                              "result": {"sessions": [{
                                  "sessionId": sid,
                                  "title": os.environ.get(
                                      "FAKE_LIST_TITLE", "resumed title"),
                              }]}}), flush=True)
        elif meth == "session/delete":
            print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {}}), flush=True)
    """
)


def _devin_command(
    tmp_path: Path, fake: Path, *, logged_out: bool = False
) -> str | list[str]:
    """A fake `devin` launcher: POSIX sh script, or an argv list for
    `python fake_acp.py` on Windows where create_subprocess_exec cannot
    run batch shims."""
    if os.name == "nt":
        # `auth status` gets no answer from the fake — that IS logged out,
        # so the logged_out flag needs no Windows branch
        return [sys.executable, str(fake)]
    shim = tmp_path / "devin"
    body = "#!/bin/sh\n"
    if logged_out:
        body += 'if [ "$1" = "auth" ]; then printf "Logged out\\n"; exit 0; fi\n'
    body += f'exec "{sys.executable}" "{fake}" "$@"\n'
    shim.write_text(body, encoding="utf-8")
    shim.chmod(0o755)
    return str(shim)


@pytest_asyncio.fixture
async def shell_client(tmp_path: Path):
    fake = tmp_path / "fake_acp.py"
    fake.write_text(FAKE_ACP, encoding="utf-8")
    client = LocalClient(cli_command=_devin_command(tmp_path, fake), api_key=None)
    yield client
    for session_id in list(client.sessions):
        await client.terminate(session_id)


async def _wait_inflight(client: LocalClient, session_id: str, prompt: str) -> object:
    """Wait until `prompt` owns a live request — much earlier than its
    first flushed event, which a short fake goal run would outlast."""
    sess = client.sessions[session_id]
    for _ in range(50):
        if sess.inflight and sess.pending_prompt == prompt:
            return sess
        await asyncio.sleep(0.05)
    raise AssertionError(f"{prompt!r} never went in-flight")


async def _wait_for(client: LocalClient, session_id: str, needle: str) -> None:
    for _ in range(50):
        state = await client.get_session(session_id)
        if any(needle in m.message for m in state.messages):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"never saw {needle!r}")


@pytest.mark.asyncio
async def test_create_session_round_trip(shell_client: LocalClient) -> None:
    session_id, url = await shell_client.create_session("hello")
    assert is_local(session_id)
    assert "local" in url
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_enum == "blocked"
    assert state.messages[-1].message == "echo: hello"


@pytest.mark.asyncio
async def test_send_message_appends_events(shell_client: LocalClient) -> None:
    session_id, _ = await shell_client.create_session("first")
    await _wait_for(shell_client, session_id, "echo: first")
    state = await shell_client.get_session(session_id)
    last = state.messages[-1].event_id
    await shell_client.send_message(session_id, "second")
    await _wait_for(shell_client, session_id, "echo: second")
    state = await shell_client.get_session(session_id, since_event_id=last)
    assert [m.message for m in state.messages] == ["echo: second"]


@pytest.mark.asyncio
async def test_modes_and_models_probe(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = asyncio.create_subprocess_exec

    async def counted_create_subprocess_exec(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counted_create_subprocess_exec)
    assert await shell_client.modes() == ["accept-edits", "smart"]
    assert await shell_client.modes() == ["accept-edits", "smart"]
    assert calls == 1
    assert await shell_client.models() == ["swe-2-high", "swe-2-low"]
    details = await shell_client.mode_details()
    assert {d["id"] for d in details} == {"accept-edits", "smart"}
    assert details[1]["name"] == "Smart"
    assert details[1]["description"] == "Auto-approve safe"
    assert await shell_client.think_levels() == ["medium", "high", "max"]
    commands = {c["name"]: c for c in await shell_client.commands()}
    assert commands["fast"]["category"] == "Session"
    assert commands["fast"]["hint"] == "[prompt]"
    assert commands["ponytail"]["category"] == "Skills"


@pytest.mark.asyncio
async def test_resume_once_reports_empty_timeout_error(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeSession:
        proc = object()

    async def spawn() -> FakeSession:
        return FakeSession()

    async def fail_load(
        _session: object, _method: str, _params: dict[str, object]
    ) -> dict[str, object]:
        raise TimeoutError

    monkeypatch.setattr(shell_client, "_spawn", spawn)
    monkeypatch.setattr(shell_client, "_request", fail_load)
    monkeypatch.setattr("app.local._terminate", lambda _proc: None)

    with pytest.raises(RuntimeError, match="TimeoutError"):
        await shell_client._resume_once("local:s1")


@pytest.mark.asyncio
async def test_create_applies_mode_and_model(shell_client: LocalClient) -> None:
    session_id, _ = await shell_client.create_session(
        "hi", mode="smart", model="swe-2-high"
    )
    await _wait_for(shell_client, session_id, "echo: hi")
    await shell_client.set_mode(session_id, "smart")
    await shell_client.set_model(session_id, "swe-2-low")


@pytest.mark.asyncio
async def test_thought_level_applies_and_reports_in_status(
    shell_client: LocalClient,
) -> None:
    session_id, _ = await shell_client.create_session("hi", thought_level="max")
    await _wait_for(shell_client, session_id, "echo: hi")
    for _ in range(50):
        state = await shell_client.get_session(session_id)
        if state.local_info and "thought_level max" in state.local_info:
            break
        await asyncio.sleep(0.1)
    assert state.local_info is not None
    assert "thought_level max" in state.local_info
    assert "model swe-2-high" in state.local_info
    await shell_client.set_thought_level(session_id, "medium")


@pytest.mark.asyncio
async def test_terminate_marks_expired(shell_client: LocalClient) -> None:
    session_id, _ = await shell_client.create_session("bye")
    await shell_client.terminate(session_id)
    state = await shell_client.get_session(session_id)
    assert state.status_enum == "expired"


@pytest.mark.asyncio
async def test_send_message_resumes_detached_session(shell_client: LocalClient) -> None:
    # bridge restart drops in-memory sessions; the CLI's session DB still
    # has them — send_message must reload via session/load
    session_id, _ = await shell_client.create_session("first")
    await _wait_for(shell_client, session_id, "echo: first")
    state = await shell_client.get_session(session_id)
    last = state.messages[-1].event_id
    sess = shell_client.sessions.pop(session_id)
    sess.proc.terminate()
    await shell_client.send_message(session_id, "second")
    await _wait_for(shell_client, session_id, "echo: second")
    state = await shell_client.get_session(session_id)
    assert state.title == "resumed title"
    # replayed history must never surface as a new reply
    assert not any("replayed" in m.message for m in state.messages)
    # the watcher's persisted cursor must not filter post-restart replies
    state = await shell_client.get_session(session_id, since_event_id=last)
    assert [m.message for m in state.messages] == ["echo: second"]


@pytest.mark.asyncio
async def test_reader_survives_overlong_lines(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a huge chunk from acp used to kill the reader and orphan every turn
    monkeypatch.setenv("FAKE_JUNK_LINES", "1")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_enum != "expired"
    # "big:ZZZ…" hits the run-on splitter (": "→ ":\n") — assert on the
    # payload itself; the point is the reader survived the huge line
    assert any("Z" * 100 in m.message for m in state.messages)


@pytest.mark.asyncio
async def test_activity_surfaces_in_status_detail(
    shell_client: LocalClient,
) -> None:
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_detail == "Running pytest"


@pytest.mark.asyncio
async def test_tool_call_prefers_raw_command_over_wrapper_title(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "Ran rtk" is the wrapper name — the status line should show the
    # real command from rawInput.command instead
    monkeypatch.setenv("FAKE_RTK", "1")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_detail == "rtk pytest -q tests/x.py"


def test_scrub_command_masks_credentials() -> None:
    # the activity line is posted to the chat — a command that embeds a
    # credential must not publish it
    for raw in (
        "curl -H 'Authorization: Bearer tok123' https://x",
        "export API_KEY=sk-abc123 && pytest",
        'rtk --token "secret value" run',
        "gh auth login --with-token ghp_xyz",
    ):
        scrubbed = _scrub_command(raw)
        for secret in ("tok123", "sk-abc123", "secret", "ghp_xyz"):
            assert secret not in scrubbed, (raw, scrubbed)
    assert _scrub_command("rtk pytest -q") == "rtk pytest -q"


def test_scrub_command_bounds_backtracking() -> None:
    # "-key-key-…" used to force cubic backtracking in the scrub regex —
    # at this size the old pattern needed tens of seconds on the event loop
    start = time.monotonic()
    _scrub_command("-key" * 25_000)
    assert time.monotonic() - start < 2


def test_scrub_command_masks_long_credential_names() -> None:
    # names longer than any fixed suffix bound must still be masked
    raw = "export TOKEN" + "x" * 200 + "=sk-live && pytest"
    assert "sk-live" not in _scrub_command(raw)


@pytest.mark.asyncio
async def test_long_thought_tail_starts_on_word_boundary(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a >120-char thought line tail-slices for the status line — the cut
    # must drop the partial first word rather than show "rphaned…"
    monkeypatch.setenv("FAKE_LONG_THOUGHT", "midword")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_detail is not None
    assert state.status_detail.startswith("supervise-daemon")
    assert "rphaned" not in state.status_detail


@pytest.mark.asyncio
async def test_long_thought_tail_tab_boundary(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tabs count as word boundaries too — a tail of a's then a tab must
    # drop the truncated token and start at the complete word after it
    monkeypatch.setenv("FAKE_LONG_THOUGHT", "tab")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_detail == "done"


@pytest.mark.asyncio
async def test_long_thought_boundary_cut_keeps_first_word(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # when the 120-char cut lands right after a space the tail already
    # starts on a complete word — it must not be discarded
    monkeypatch.setenv("FAKE_LONG_THOUGHT", "boundary")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_detail is not None
    assert state.status_detail.startswith("alpha")


@pytest.mark.asyncio
async def test_long_thought_unbroken_tail_shows_nothing(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a tail with no whitespace at all is one truncated token — better
    # no activity line than a known-mid-word start
    monkeypatch.setenv("FAKE_LONG_THOUGHT", "unbroken")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_detail is None


@pytest.mark.asyncio
async def test_partial_replies_stream_mid_turn(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # long turns used to stay silent until stopReason — the flusher emits
    # buffered chunks periodically, but only at paragraph boundaries so a
    # mid-paragraph cut never fragments the reply into separate messages
    monkeypatch.setattr("app.local._FLUSH_INTERVAL", 0.05)
    monkeypatch.setenv("FAKE_SLOW", "1")
    session_id, _ = await shell_client.create_session("hello")
    state = None
    for _ in range(100):
        state = await shell_client.get_session(session_id)
        if state.messages:
            break
        await asyncio.sleep(0.05)
    assert state is not None and state.messages
    assert state.messages[0].message.strip() == "echo: first para"
    await _wait_for(shell_client, session_id, "second para tail")


@pytest.mark.asyncio
async def test_partial_replies_defer_unclosed_fences(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an unclosed ``` fence mid-turn must not stream to Telegram — a
    # fragment would render broken MarkdownV2; the fence arrives whole
    monkeypatch.setattr("app.local._FLUSH_INTERVAL", 0.05)
    monkeypatch.setenv("FAKE_FENCE", "1")
    session_id, _ = await shell_client.create_session("hi")
    await _wait_for(shell_client, session_id, "done")
    state = await shell_client.get_session(session_id)
    messages = [m.message.strip() for m in state.messages]
    assert messages[0] == "intro para"
    assert len(messages) == 2
    assert "```python" in messages[1] and "done" in messages[1]


@pytest.mark.asyncio
async def test_login_turn_suppressed_when_logged_out(tmp_path: Path) -> None:
    # regression: a configured api_key must reach the auth check (the
    # _cli_authed/_cli_logged_in name shadow used to TypeError here), and
    # the /login turn must not surface as a user-visible event
    fake = tmp_path / "fake_acp.py"
    fake.write_text(FAKE_ACP, encoding="utf-8")
    client = LocalClient(
        cli_command=_devin_command(tmp_path, fake, logged_out=True),
        api_key="sekrit",
    )
    try:
        session_id, _ = await client.create_session("hi")
        await _wait_for(client, session_id, "echo: hi")
        state = await client.get_session(session_id)
        assert [m.message for m in state.messages] == ["echo: hi"]
    finally:
        for sid in list(client.sessions):
            await client.terminate(sid)


@pytest.mark.asyncio
async def test_permission_auto_allowed_without_hook(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no approval hook -> the request is answered with the allow option
    # immediately, so non-bypass modes can't stall the turn
    monkeypatch.setenv("FAKE_PERMISSION", "1")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "granted: allow")


@pytest.mark.asyncio
async def test_permission_routed_to_hook(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # with the hook set the request stays pending for the bridge to
    # answer — answer_permission writes the picked option to the CLI
    monkeypatch.setenv("FAKE_PERMISSION", "1")
    seen: list[tuple[str, dict]] = []
    shell_client.on_permission = lambda sid, msg: seen.append((sid, msg))
    session_id, _ = await shell_client.create_session("hello")
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.1)
    assert seen and seen[0][0] == session_id
    options = LocalClient.permission_options(seen[0][1])
    assert options == [("allow", "Allow"), ("reject", "Reject")]
    assert 9000 in shell_client.sessions[session_id].permissions
    label = shell_client.answer_permission(session_id, 9000, "reject")
    assert label == "Reject"
    await _wait_for(shell_client, session_id, "granted: reject")
    # answered once — a second answer reports the request is gone
    assert shell_client.answer_permission(session_id, 9000, "allow") is None


@pytest.mark.asyncio
async def test_reject_permission_fails_closed(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the bridge's fail-closed path picks the reject option — the CLI
    # sees a real denial, not an implicit allow
    monkeypatch.setenv("FAKE_PERMISSION", "1")
    seen: list[tuple[str, dict]] = []
    shell_client.on_permission = lambda sid, msg: seen.append((sid, msg))
    session_id, _ = await shell_client.create_session("hello")
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.1)
    assert seen
    shell_client.reject_permission(session_id, 9000)
    await _wait_for(shell_client, session_id, "granted: reject")
    # consumed once — a second reject is a no-op
    shell_client.reject_permission(session_id, 9000)


def test_deny_permission_without_reject_option_cancels(tmp_path: Path) -> None:
    # a request whose options carry no reject choice must still fail
    # closed: answer with the ACP cancelled outcome
    proc = type("P", (), {"stdin": io.BytesIO()})()
    sess = _AcpSession(proc=proc, acp_id="x")  # type: ignore[arg-type]
    client = LocalClient(cli_command="devin", cwd=str(tmp_path))
    client._deny_permission(
        sess,
        7,
        {"params": {"options": [{"optionId": "allow", "name": "Allow"}]}},
    )
    proc.stdin.seek(0)
    written = json.loads(proc.stdin.readline())
    assert written["id"] == 7
    assert written["result"]["outcome"] == {"outcome": "cancelled"}


def test_save_attachment_writes_on_host(tmp_path: Path) -> None:
    client = LocalClient(cli_command="devin", cwd=str(tmp_path))
    dest = client.save_attachment("my file!.txt", b"data")
    assert dest.read_bytes() == b"data"
    assert dest.parent.name == "attachments"
    assert dest.name.endswith("-my_file_.txt")


def test_save_attachment_sweeps_old_files(tmp_path: Path) -> None:
    client = LocalClient(cli_command="devin", cwd=str(tmp_path))
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    old = attachments / "ancient.txt"
    old.write_bytes(b"stale")
    os.utime(old, (0, 0))
    dest = client.save_attachment("new.txt", b"data")
    assert dest.exists()
    assert not old.exists()


@pytest.mark.asyncio
async def test_modes_probe_deletes_its_session(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the probe's session/new persists in the CLI's session DB — without a
    # session/delete every /settings or /model call leaks a phantom entry
    seen: list[str] = []
    original = LocalClient._request

    async def record(self, sess, method, params, **kw):  # type: ignore[no-untyped-def]
        seen.append(method)
        return await original(self, sess, method, params, **kw)

    monkeypatch.setattr(LocalClient, "_request", record)
    assert await shell_client.modes() == ["accept-edits", "smart"]
    assert "session/delete" in seen


@pytest.mark.asyncio

async def test_pending_prompt_hook_tracks_turn(shell_client: LocalClient) -> None:
    states: list[tuple[str, str | None]] = []
    shell_client.on_prompt_state = lambda sid, p: states.append((sid, p))
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    assert (session_id, "hello") in states
    assert states[-1] == (session_id, None)
    assert shell_client.sessions[session_id].pending_prompt is None


@pytest.mark.asyncio
async def test_pending_prompt_survives_turn_cancellation(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a bridge shutdown cancels the in-flight turn — the pending marker
    # must stay so the next boot can replay the prompt
    monkeypatch.setenv("FAKE_SLOW", "1")
    states: list[str | None] = []
    shell_client.on_prompt_state = lambda sid, p: states.append(p)
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "second para tail")
    await shell_client.send_message(session_id, "second")
    sess = shell_client.sessions[session_id]
    for _ in range(50):
        if sess.pending_prompt == "second":
            break
        await asyncio.sleep(0.1)
    assert sess.tail is not None
    sess.tail.cancel()
    for _ in range(50):
        if sess.turns == 0:
            break
        await asyncio.sleep(0.1)
    assert sess.pending_prompt == "second"
    assert states[-1] == "second"


@pytest.mark.asyncio
async def test_resume_reapplies_mode_model_and_thought_level(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # session/load restores CLI state, not the chat's configured values —
    # resume must re-apply mode/model/thought_level or e.g. bypass mode
    # silently reverts to asking for approvals
    modes: list[str] = []
    original = LocalClient.set_mode

    async def spy(self, sid, mode):  # type: ignore[no-untyped-def]
        modes.append(mode)
        await original(self, sid, mode)

    monkeypatch.setattr(LocalClient, "set_mode", spy)
    shell_client.resume_config = lambda sid: {
        "mode": "smart",
        "model": "swe-2-low",
        "thought_level": "max",
    }
    session_id, _ = await shell_client.create_session("first")
    await _wait_for(shell_client, session_id, "echo: first")
    sess = shell_client.sessions.pop(session_id)
    sess.proc.terminate()
    await shell_client.send_message(session_id, "second")
    assert "smart" in modes
    sess = shell_client.sessions[session_id]
    for _ in range(50):
        if (
            sess.config.get("model") == "swe-2-low"
            and sess.config.get("thought_level") == "max"
        ):
            break
        await asyncio.sleep(0.1)
    assert sess.config["model"] == "swe-2-low"
    assert sess.config["thought_level"] == "max"


@pytest.mark.asyncio
async def test_reap_keeps_pending_prompt_for_replay(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # shutdown/evict terminates the process mid-turn — the turn was
    # interrupted, not answered, so the marker must survive for the next
    # boot's replay
    monkeypatch.setenv("FAKE_SLOW", "1")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "second para tail")
    await shell_client.send_message(session_id, "second")
    sess = shell_client.sessions[session_id]
    for _ in range(50):
        if sess.pending_prompt == "second":
            break
        await asyncio.sleep(0.1)
    await shell_client._reap(sess)
    for _ in range(50):
        if sess.turns == 0:
            break
        await asyncio.sleep(0.1)
    assert sess.pending_prompt == "second"
    # already reaped — terminate() in teardown would double-kill it
    shell_client.sessions.pop(session_id)


@pytest.mark.asyncio
async def test_reap_mid_turn_reports_no_failure(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a bridge-ordered kill (deploy restart, evict, /stop) interrupts a
    # turn that pending_prompt replays — emitting "prompt failed" for it
    # is a false alarm on every self-update
    monkeypatch.setenv("FAKE_SLOW", "1")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "second para tail")
    await shell_client.send_message(session_id, "second")
    sess = shell_client.sessions[session_id]
    for _ in range(50):
        if sess.pending_prompt == "second":
            break
        await asyncio.sleep(0.1)
    await shell_client._reap(sess)
    for _ in range(50):
        if sess.turns == 0:
            break
        await asyncio.sleep(0.1)
    assert not any("prompt failed" in m.message for m in sess.events)
    shell_client.sessions.pop(session_id)


@pytest.mark.asyncio
async def test_crash_mid_turn_still_reports_failure(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an exit the bridge didn't order (crash, OOM kill) is a real
    # failure — the warning must survive or a dying CLI gaps turns
    # silently
    monkeypatch.setenv("FAKE_SLOW", "1")
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "second para tail")
    await shell_client.send_message(session_id, "second")
    sess = shell_client.sessions[session_id]
    for _ in range(50):
        if sess.pending_prompt == "second":
            break
        await asyncio.sleep(0.1)
    _kill(sess.proc)
    for _ in range(100):
        if any("prompt failed" in m.message for m in sess.events):
            break
        await asyncio.sleep(0.1)
    assert any(
        "prompt failed: acp process exited" in m.message
        for m in sess.events
    )
    shell_client.sessions.pop(session_id)


@pytest.mark.asyncio
async def test_retryable_prompt_error_retries_once(
    shell_client: LocalClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # the CLI flags a dropped agent channel as retryable — one retry
    # keeps the transient blip from surfacing as a failed prompt
    monkeypatch.setattr("app.local._RETRY_BACKOFF", 0)
    monkeypatch.setenv("FAKE_RETRYABLE_ONCE", str(tmp_path / "retried"))
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    # the marker proves the first prompt errored before the retry landed
    assert (tmp_path / "retried").exists()
    sess = shell_client.sessions[session_id]
    assert not any("prompt failed" in m.message for m in sess.events)


@pytest.mark.asyncio
async def test_retryable_prompt_error_fails_after_second_error(
    shell_client: LocalClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # the retry is one-shot: a second retryable failure still reports the
    # turn failed, and exactly two prompts reach the CLI — never a loop
    monkeypatch.setattr("app.local._RETRY_BACKOFF", 0)
    marker = tmp_path / "attempts"
    monkeypatch.setenv("FAKE_RETRYABLE_ALWAYS", str(marker))
    session_id, _ = await shell_client.create_session("hello")
    sess = shell_client.sessions[session_id]
    for _ in range(100):
        if any("prompt failed" in m.message for m in sess.events):
            break
        await asyncio.sleep(0.1)
    assert any("prompt failed" in m.message for m in sess.events)
    assert marker.read_text() == "xx"


def _wedge_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.local._WEDGE_CHECK_SECONDS", 0.05)
    monkeypatch.setattr("app.local._WEDGE_SILENCE_SECONDS", 0.2)
    monkeypatch.setattr("app.local._WEDGE_PROBE_TIMEOUT", 0.3)


@pytest.mark.asyncio
async def test_wedged_session_respawns_and_replays_prompt(
    shell_client: LocalClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a wedged acp (silent stdout, unanswered control requests) used to
    # leave the session "working" forever — the watchdog probes, finds it
    # dead, and respawns it; the in-flight prompt is retried once
    _wedge_constants(monkeypatch)
    monkeypatch.setenv("FAKE_WEDGE", "1")
    monkeypatch.setenv("FAKE_WEDGE_ONCE", str(tmp_path / "wedged-once"))
    session_id, _ = await shell_client.create_session("hello")
    wedged = shell_client.sessions[session_id]
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_enum == "blocked"
    # the resumed session runs on a fresh process
    assert shell_client.sessions[session_id].proc is not wedged.proc
    assert not shell_client.sessions[session_id].dead
    # the kill surfaced on the dead incarnation's event list
    assert any("stopped answering" in m.message for m in wedged.events)


@pytest.mark.asyncio
async def test_wedged_session_replay_is_not_repeated(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a prompt that wedges the fresh process too is reported failed, not
    # retried forever — the second kill leaves the turn visibly dead
    _wedge_constants(monkeypatch)
    monkeypatch.setenv("FAKE_WEDGE", "1")
    session_id, _ = await shell_client.create_session("hi")
    wedged = shell_client.sessions[session_id]
    # first wedge kill → prompt queued for replay on a fresh process
    for _ in range(100):
        sess2 = shell_client.sessions.get(session_id)
        if sess2 is not None and sess2 is not wedged:
            break
        await asyncio.sleep(0.05)
    replayed = shell_client.sessions.get(session_id)
    assert replayed is not None and replayed is not wedged
    # the replayed turn wedges again — killed, but not replayed a third
    # time: the session is left unmapped with nothing owed
    for _ in range(100):
        if shell_client.sessions.get(session_id) is not replayed:
            break
        await asyncio.sleep(0.05)
    assert shell_client.sessions.get(session_id) is None
    assert not shell_client._interrupted.get(session_id)
    # the abandoned prompt says so — a silent reap reads as a lost turn
    assert any(
        "resend your last message" in m.message for m in replayed.events
    )
    # dormant session reloads on next contact; no echo ever arrived
    state = await shell_client.get_session(session_id)
    assert state.status_enum == "blocked"
    assert not any("echo: hi" in m.message for m in state.messages)


@pytest.mark.asyncio
async def test_queued_turn_moves_to_replacement_process(
    shell_client: LocalClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a turn queued behind a wedged one must not write to the reaped
    # process — it transfers to the resumed process, in send order
    _wedge_constants(monkeypatch)
    monkeypatch.setenv("FAKE_WEDGE", "1")
    monkeypatch.setenv("FAKE_WEDGE_ONCE", str(tmp_path / "wedged-once"))
    session_id, _ = await shell_client.create_session("first")
    await shell_client.send_message(session_id, "second")
    await _wait_for(shell_client, session_id, "echo: second")
    sess = shell_client.sessions[session_id]
    messages = [m.message for m in sess.events]
    assert messages.index("echo: first") < messages.index("echo: second")


@pytest.mark.asyncio
async def test_dead_session_replays_interrupted_prompt(
    shell_client: LocalClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a process killed mid-turn outside the watchdog replays its pending
    # prompt on the next contact — the durable marker is not overwritten
    # silently by a later send
    monkeypatch.setenv("FAKE_WEDGE", "1")
    monkeypatch.setenv("FAKE_WEDGE_ONCE", str(tmp_path / "wedged-once"))
    session_id, _ = await shell_client.create_session("hi")
    wedged = shell_client.sessions[session_id]
    for _ in range(50):
        if wedged.pending_prompt == "hi":
            break
        await asyncio.sleep(0.05)
    wedged.proc.kill()
    for _ in range(50):
        if wedged.dead:
            break
        await asyncio.sleep(0.05)
    await _wait_for(shell_client, session_id, "echo: hi")
    assert shell_client.sessions[session_id].proc is not wedged.proc


@pytest.mark.asyncio
async def test_dead_process_resumes_instead_of_staying_gone(
    shell_client: LocalClient,
) -> None:
    # a session whose acp process died outside the watchdog stayed
    # mapped — every send failed 'gone' until a bridge restart; now the
    # next send reloads the persisted session on a fresh process
    session_id, _ = await shell_client.create_session("first")
    await _wait_for(shell_client, session_id, "echo: first")
    dead = shell_client.sessions[session_id]
    dead.proc.kill()
    for _ in range(50):
        if dead.dead:
            break
        await asyncio.sleep(0.1)
    assert dead.dead
    await shell_client.send_message(session_id, "second")
    await _wait_for(shell_client, session_id, "echo: second")
    assert shell_client.sessions[session_id].proc is not dead.proc


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt", reason="select on stdin is POSIX-only in the fake"
)
async def test_quiet_but_answerable_turn_is_not_killed(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # silence alone is not a wedge — a long quiet tool call whose process
    # still answers control requests must survive to completion
    _wedge_constants(monkeypatch)
    monkeypatch.setenv("FAKE_QUIET", "0.6")
    session_id, _ = await shell_client.create_session("hello")
    proc = shell_client.sessions[session_id].proc
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_enum == "blocked"
    assert not any(
        "stopped answering" in m.message for m in state.messages
    )
    assert shell_client.sessions[session_id].proc is proc


@pytest.mark.asyncio
async def test_late_reply_after_timeout_keeps_reader_alive(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a wait_for-cancelled request must drop its pending slot — a late
    # reply resolving the cancelled future raised InvalidStateError out
    # of _handle and killed the reader (dead session on the spot)
    monkeypatch.setenv("FAKE_LIST_DELAY", "0.3")
    session_id, _ = await shell_client.create_session("first")
    await _wait_for(shell_client, session_id, "echo: first")
    sess = shell_client.sessions[session_id]
    with pytest.raises(TimeoutError):
        await shell_client._request(
            sess, "session/list", {"cwd": shell_client._cwd()}, timeout=0.05
        )
    assert not sess.pending
    await asyncio.sleep(0.4)  # the late response lands while reading
    assert not sess.dead
    await shell_client.send_message(session_id, "second")
    await _wait_for(shell_client, session_id, "echo: second")


@pytest.mark.asyncio
async def test_resume_applies_config_on_next_prompt_when_conv_late(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # /resume loads a history session before its conversation is active —
    # the config hook can't see the chat settings yet, so apply them on
    # the first prompt instead of dropping them
    applied: list[dict] = []
    calls = {"n": 0}

    def config(sid):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        cfg = {"mode": "smart", "model": "swe-2-low", "thought_level": "max"}
        applied.append(cfg)
        return cfg

    shell_client.resume_config = config
    session_id, _ = await shell_client.create_session("first")
    await _wait_for(shell_client, session_id, "echo: first")
    sess = shell_client.sessions.pop(session_id)
    sess.proc.terminate()
    await shell_client.send_message(session_id, "second")
    assert applied == [
        {"mode": "smart", "model": "swe-2-low", "thought_level": "max"}
    ]
    await _wait_for(shell_client, session_id, "echo: second")

@pytest.mark.asyncio
async def test_local_events_survive_bridge_restart(tmp_path: Path) -> None:
    # a bridge restart kills the acp process and its in-memory events —
    # persisted rows let a resumed session re-offer the undelivered tail,
    # same as cloud events surviving server-side
    from app.store import Store

    store = Store(str(tmp_path / "bridge.sqlite3"))
    fake = tmp_path / "fake_acp.py"
    fake.write_text(FAKE_ACP, encoding="utf-8")
    cmd = _devin_command(tmp_path, fake)
    first = LocalClient(cli_command=cmd, api_key=None, event_store=store)
    session_id, _ = await first.create_session("hello")
    await _wait_for(first, session_id, "echo: hello")
    first_ids = [
        m.event_id for m in (await first.get_session(session_id)).messages
    ]
    await first.aclose()

    second = LocalClient(cli_command=cmd, api_key=None, event_store=store)
    try:
        state = await second.get_session(session_id)
        assert state.status_enum == "blocked"
        delivered = [m for m in state.messages if m.message == "echo: hello"]
        assert delivered and delivered[0].event_id in first_ids
        # the session/load transcript replay stays suppressed — recovery
        # relies on the persisted rows, not the ACP replay
        assert not any("replayed" in m.message for m in state.messages)
        # the watcher's persisted cursor still filters: only later events
        tail = await second.get_session(
            session_id, since_event_id=first_ids[0]
        )
        assert all(
            int(m.event_id or 0) > int(first_ids[0]) for m in tail.messages
        )
        # janitor-facing count for sessions absent from the live map
        assert store.count_local_events_since(session_id, "0") >= 1
        assert store.count_local_events_since(session_id, first_ids[-1]) == 0
    finally:
        await second.aclose()

    store.delete_local_events(session_id)
    assert store.local_events(session_id) == []



@pytest.mark.asyncio
async def test_model_costs_parses_cli_json_and_caches(tmp_path: Path) -> None:
    fake = tmp_path / "fake_devin.py"
    fake.write_text(
        "import json\n"
        "print(json.dumps({'families': ["
        "{'variants': [{'model_uid': 'free-m', 'cost_tier': 'Free'}]},"
        "{'variants': ["
        "{'model_uid': 'hi-m', 'cost_tier': 'High cost'},"
        "{'model_uid': 'no-tier'}]}]}))",
        encoding="utf-8",
    )
    client = LocalClient(cli_command=[sys.executable, str(fake)], api_key=None)
    costs = await client.model_costs()
    assert costs == {"free-m": "Free", "hi-m": "High cost"}
    # family index is captured from the same probe, tiered or not
    assert client.model_rank() == {"free-m": 0, "hi-m": 1, "no-tier": 1}
    # the 5-min cache serves repeat calls — a now-broken fake must not run
    fake.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    assert await client.model_costs() == costs
    assert client.model_rank()["hi-m"] == 1


def test_sort_models_orders_by_tier_then_family_rank() -> None:
    models = ["hi-old", "free-b", "mid-a", "free-a", "unknown-x"]
    costs = {
        "free-a": "Free",
        "free-b": "Free",
        "mid-a": "Med cost",
        "hi-old": "High cost",
    }
    rank = {"free-a": 0, "free-b": 1, "mid-a": 0, "hi-old": 0}
    assert sort_models(models, costs, rank) == [
        "unknown-x",  # untiered leads (adaptive, the recommended default)
        "free-a",     # same tier: newer family (lower rank) first
        "free-b",
        "mid-a",
        "hi-old",
    ]


def test_sort_models_keeps_untiered_input_order_and_sinks_unranked() -> None:
    # a second untiered entry must not jump ahead of the leading one
    assert sort_models(
        ["adaptive", "other"], {}, {"adaptive": 2, "other": 0}
    ) == ["adaptive", "other"]
    # a tiered model missing from the rank map sorts after ranked ones
    assert sort_models(
        ["ghost", "known"], {"ghost": "Free", "known": "Free"}, {"known": 0}
    ) == ["known", "ghost"]


@pytest.mark.asyncio
async def test_model_costs_empty_on_probe_failure(tmp_path: Path) -> None:
    fake = tmp_path / "fake_devin.py"
    fake.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    client = LocalClient(cli_command=[sys.executable, str(fake)], api_key=None)
    assert await client.model_costs() == {}


class _DeadProc:
    def __init__(self) -> None:
        self.called: list[str] = []

    def terminate(self) -> None:
        self.called.append("terminate")
        raise ProcessLookupError

    def kill(self) -> None:
        self.called.append("kill")
        raise ProcessLookupError


def test_terminate_and_kill_tolerate_exited_process() -> None:
    dead = _DeadProc()
    _terminate(dead)
    _kill(dead)
    assert dead.called == ["terminate", "kill"]


class _AliveProc:
    def __init__(self) -> None:
        self.called: list[str] = []

    def terminate(self) -> None:
        self.called.append("terminate")

    def kill(self) -> None:
        self.called.append("kill")


def test_terminate_and_kill_reach_live_process() -> None:
    alive = _AliveProc()
    _terminate(alive)
    _kill(alive)
    assert alive.called == ["terminate", "kill"]


def test_clean_reason_strips_reprs_and_collapses() -> None:
    exc = RuntimeError(
        "unable to perform operation on "
        "<WriteUnixTransport closed=True reading=False 0x7fe8d57732a0>; "
        "the handler is closed"
    )
    assert _clean_reason(exc) == (
        "unable to perform operation on …; the handler is closed"
    )


def test_exit_detail_drops_structured_log_tail() -> None:
    tail = (
        "erver: chisel_agent::skills_loading: skills discovery: "
        "cwd=/root errors=133\n"
        "2026-10-12T22:19:57.116036Z INFO run_acp_server: "
        "chisel_agent::acp_server: Dispatched SessionEnd "
        "for session parallel-spike\n"
    )
    # the partial first line and the structured INFO line are both noise
    assert _exit_detail(tail) == ""
    # a real crash line still surfaces
    panic = tail + "thread 'main' panicked at 'boom', src/main.rs:42\n"
    assert _exit_detail(panic) == (
        ": thread 'main' panicked at 'boom', src/main.rs:42"
    )
    # a single-line tail is a complete line, not a fragment to drop
    assert _exit_detail("panic: boom\n") == ": panic: boom"


def test_split_runons_restores_lost_line_breaks() -> None:
    text = (
        "resolving in worktrees per branch.The dropped items were done. "
        "recompute mergeable:#2113 is clean. "
        "retrying fetches:⚠ Disk full. "
        "merge:✓ PR #2180 pushed. "
        "staying on v2.0.0 and app.local.py files. "
        "call re.compile(r'(x)') verbatim."
    )
    assert _split_runons(text) == (
        "resolving in worktrees per branch.\nThe dropped items were done. "
        "recompute mergeable:\n#2113 is clean. "
        "retrying fetches:\n⚠ Disk full. "
        "merge:\n✓ PR #2180 pushed. "
        "staying on v2.0.0 and app.local.py files. "
        "call re.compile(r'(x)') verbatim."
    )


def test_split_runons_preserves_markers_and_code() -> None:
    text = (
        "Choose:\nOPTIONS: Yes!Confirm | No\n"
        "```py\nflag:True\nprint('A.B')\n```\n"
        "inline `flag:True` stays.\n"
        "TABLE: a.B | c.D"
    )
    out = _split_runons(text)
    assert "OPTIONS: Yes!Confirm | No" in out
    assert "flag:True" in out
    assert "print('A.B')" in out
    assert "`flag:True`" in out
    assert "TABLE: a.B | c.D" in out


def test_split_runons_splits_prose_around_inline_code() -> None:
    # a fused line that mentions code must still split — backticks used
    # to skip the whole line, which is most progress text
    text = (
        "check — `codecov/project/app` — lands at `cross_module_ci`.While it runs, "
        "verifying merges the old way:Only ci.yml consumes the lcovs."
    )
    assert _split_runons(text) == (
        "check — `codecov/project/app` — lands at `cross_module_ci`.\nWhile it runs, "
        "verifying merges the old way:\nOnly ci.yml consumes the lcovs."
    )


def test_split_runons_keeps_double_backtick_code() -> None:
    text = "Run ``flag:True`` now.Done."
    assert _split_runons(text) == "Run ``flag:True`` now.\nDone."


def test_split_runons_keeps_table_body_rows() -> None:
    text = "TABLE:\n| Name | Value |\n| `config` | a.B |\n| other | fine |\nEND TABLE\nAfter.The end."
    out = _split_runons(text)
    assert "| `config` | a.B |" in out
    assert "After.\nThe end." in out


@pytest.mark.asyncio
async def test_session_info_update_sets_session_title(
    shell_client: LocalClient,
) -> None:
    session_id, _ = await shell_client.create_session("hello")
    for _ in range(50):
        state = await shell_client.get_session(session_id)
        if state.title:
            break
        await asyncio.sleep(0.1)
    assert state.title == "Generated title"


@pytest.mark.asyncio
async def test_session_info_update_never_clobbers_an_explicit_title(
    shell_client: LocalClient,
) -> None:
    session_id, _ = await shell_client.create_session("hello", title="Mine")
    for _ in range(50):
        state = await shell_client.get_session(session_id)
        if state.title:
            break
        await asyncio.sleep(0.1)
    assert state.title == "Mine"


@pytest.mark.asyncio
async def test_ellipsis_truncated_provisional_title_is_ignored(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the real CLI's provisional title is the prompt head with whitespace
    # collapsed, truncated to ~80 chars ending in "..." — it is not a
    # strict prefix of the prompt, so the raw startswith check misses it
    prompt = "boilerplate line one.\n" + "word " * 30
    monkeypatch.setenv(
        "FAKE_PROVISIONAL", " ".join(prompt.split())[:77] + "..."
    )
    session_id, _ = await shell_client.create_session(prompt)
    await _wait_for(shell_client, session_id, "echo:")
    assert (await shell_client.get_session(session_id)).title == ""


@pytest.mark.asyncio
async def test_tool_derived_provisional_title_is_ignored(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TOOL_TITLE", "1")
    session_id, _ = await shell_client.create_session("hello")
    for _ in range(50):
        state = await shell_client.get_session(session_id)
        if state.title:
            break
        await asyncio.sleep(0.1)
    assert state.title == "Generated title"


@pytest.mark.asyncio
async def test_reply_derived_provisional_title_is_ignored(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a provisional can be the head of the agent's own reply rather than
    # the prompt — it never prefixes prompt_head, but the truncation
    # ellipsis marks it either way, so it must not become the subject
    monkeypatch.setenv(
        "FAKE_PROVISIONAL",
        "⚠ I can't open external URLs, so I need the session content "
        "to edit it. Send ...",
    )
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo:")
    assert (await shell_client.get_session(session_id)).title == ""


@pytest.mark.asyncio
async def test_resumed_provisional_list_title_is_ignored(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a session whose generated title never landed keeps the provisional
    # in the CLI DB — a truncated prompt head is not a title, so the
    # resumed session stays untitled and can still pick up a real one
    monkeypatch.setenv(
        "FAKE_LIST_TITLE",
        "prompt head fragment, find matching ones, fix missing, and fo...",
    )
    session_id, _ = await shell_client.create_session("first")
    await _wait_for(shell_client, session_id, "echo: first")
    sess = shell_client.sessions.pop(session_id)
    sess.proc.terminate()
    await shell_client.send_message(session_id, "second")
    await _wait_for(shell_client, session_id, "echo: second")
    assert (await shell_client.get_session(session_id)).title == ""


@pytest.mark.asyncio
async def test_short_ellipsis_title_is_kept(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a short title that merely ends in an ellipsis is a real title —
    # only the ~80-char truncated head marks a provisional
    monkeypatch.setenv("FAKE_PROVISIONAL", "What happens next…")
    session_id, _ = await shell_client.create_session("hello")
    for _ in range(50):
        state = await shell_client.get_session(session_id)
        if state.title:
            break
        await asyncio.sleep(0.1)
    assert state.title == "What happens next…"


@pytest.mark.asyncio
async def test_resumed_ellipsis_title_is_kept(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LIST_TITLE", "What happens next…")
    session_id, _ = await shell_client.create_session("first")
    await _wait_for(shell_client, session_id, "echo: first")
    sess = shell_client.sessions.pop(session_id)
    sess.proc.terminate()
    await shell_client.send_message(session_id, "second")
    await _wait_for(shell_client, session_id, "echo: second")
    assert (await shell_client.get_session(session_id)).title == (
        "What happens next…"
    )


@pytest.mark.asyncio
async def test_replayed_title_cannot_clobber_the_resumed_title(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_REPLAY_TITLE", "1")
    session_id, _ = await shell_client.create_session("first")
    await _wait_for(shell_client, session_id, "echo: first")
    sess = shell_client.sessions.pop(session_id)
    sess.proc.terminate()
    await shell_client.send_message(session_id, "second")
    await _wait_for(shell_client, session_id, "echo: second")
    state = await shell_client.get_session(session_id)
    assert state.title == "resumed title"


@pytest.mark.asyncio
async def test_steer_reaches_a_running_goal_turn(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a goal run keeps its prompt response open for the whole run — a
    # queued /goal clear would wait out every continuation turn. The
    # fake answers a concurrent prompt mid-run, so the clear must land
    # while the run is still ticking, not after it ends.
    monkeypatch.setenv("FAKE_GOAL", "1")
    session_id, _ = await shell_client.create_session("hi")
    await _wait_for(shell_client, session_id, "echo: hi")
    await shell_client.send_message(session_id, "/goal tick forever")
    await _wait_for(shell_client, session_id, "tick 0")
    started = asyncio.get_event_loop().time()
    await shell_client.send_message(session_id, "/goal clear")
    await _wait_for(shell_client, session_id, "Goal cleared.")
    # under turn-chaining the clear lands only after the ~1.5s run ends
    assert asyncio.get_event_loop().time() - started < 1.0
    sess = shell_client.sessions[session_id]
    # the steered turn finishing mid-run must not strand the run's count
    for _ in range(50):
        if not sess.turns:
            break
        await asyncio.sleep(0.1)
    assert sess.turns == 0


@pytest.mark.asyncio
async def test_steer_finish_keeps_goal_owner_and_buffer(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a quick steer can end while the goal request is still open — the
    # next prompt must not read as a clean turn: resetting on tail.done()
    # wiped the running goal's buffer and durable marker. The run is
    # slowed so the steer can settle inside it
    monkeypatch.setenv("FAKE_GOAL", "1")
    monkeypatch.setenv("FAKE_GOAL_DELAY", "0.4")
    session_id, _ = await shell_client.create_session("hi")
    await _wait_for(shell_client, session_id, "echo: hi")
    await shell_client.send_message(session_id, "/goal tick forever")
    sess = await _wait_inflight(
        shell_client, session_id, "/goal tick forever"
    )
    await shell_client.send_message(session_id, "ping")
    # the steer settles mid-run — its echo stays buffered until the owner
    # drains, so watch the request set instead of flushed events
    for _ in range(50):
        if sess.inflight_msgs == ["/goal tick forever"]:
            break
        await asyncio.sleep(0.05)
    assert sess.inflight == 1
    # the durable marker still belongs to the goal that owns the run
    assert sess.pending_prompt == "/goal tick forever"
    # a third prompt now steers into the still-running goal — queued
    # would wait out the remaining ~3s of the run
    await shell_client.send_message(session_id, "hola")
    await asyncio.wait_for(asyncio.shield(sess.tail), 1.0)
    # and the owner is untouched: marker and run still the goal's
    assert sess.pending_prompt == "/goal tick forever"
    assert sess.inflight_msgs == ["/goal tick forever"]
    # and the run's later ticks keep flowing into the same stream
    await _wait_for(shell_client, session_id, "tick 5")


@pytest.mark.asyncio
async def test_crash_replays_goal_owner_before_its_steer(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the steer dead-guards into the interrupted queue before the goal's
    # marker lands — the owner was sent first, so it replays first
    monkeypatch.setenv("FAKE_GOAL", "1")
    session_id, _ = await shell_client.create_session("hi")
    await _wait_for(shell_client, session_id, "echo: hi")
    await shell_client.send_message(session_id, "/goal tick forever")
    await _wait_inflight(shell_client, session_id, "/goal tick forever")
    await shell_client.send_message(session_id, "hang steer")
    sess = shell_client.sessions[session_id]
    for _ in range(50):
        if len(sess.inflight_msgs) == 2:
            break
        await asyncio.sleep(0.05)
    # spy the send order — the drain consumes the queue before the send
    # returns, so the only observable proof is what the resumed process
    # gets fed
    sent: list[str] = []
    orig = shell_client.send_message

    async def spy(sid: str, msg: str, **kw: object) -> None:
        sent.append(msg)
        await orig(sid, msg, **kw)

    shell_client.send_message = spy  # type: ignore[method-assign]
    sess.proc.terminate()
    for _ in range(50):
        if sess.dead:
            break
        await asyncio.sleep(0.05)
    await shell_client.send_message(session_id, "after crash")
    assert "/goal tick forever" in sent and "hang steer" in sent, sent
    assert sent.index("/goal tick forever") < sent.index("hang steer"), sent


@pytest.mark.asyncio
async def test_owner_settle_hands_marker_to_live_steer(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a goal ending while a steer still flies must not clear the durable
    # marker — a bridge restart then would lose the unsettled steer
    monkeypatch.setenv("FAKE_GOAL", "1")
    session_id, _ = await shell_client.create_session("hi")
    await _wait_for(shell_client, session_id, "echo: hi")
    await shell_client.send_message(session_id, "/goal tick forever")
    await _wait_inflight(shell_client, session_id, "/goal tick forever")
    await shell_client.send_message(session_id, "hang steer")
    sess = shell_client.sessions[session_id]
    # the goal run ends (~1.5s) while the steer is still in flight
    for _ in range(60):
        if sess.inflight == 1 and sess.pending_prompt == "hang steer":
            break
        await asyncio.sleep(0.05)
    assert sess.pending_prompt == "hang steer"


@pytest.mark.asyncio
async def test_login_does_not_unhide_preamble_turn(tmp_path: Path) -> None:
    # a required /login turn used to consume the session-level suppress
    # flag before the preamble ran, leaking the hidden preamble's reply
    fake = tmp_path / "fake_acp.py"
    fake.write_text(FAKE_ACP, encoding="utf-8")
    client = LocalClient(
        cli_command=_devin_command(tmp_path, fake, logged_out=True),
        api_key="sekrit",
    )
    try:
        session_id, _ = await client.create_session(
            "the preamble", suppress_first=True
        )
        state = await client.get_session(session_id)
        assert state.messages == []
    finally:
        for sid in list(client.sessions):
            await client.terminate(sid)


@pytest.mark.asyncio
async def test_suppressed_first_turn_is_hidden(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # command-created sessions open with the system preamble as a
    # suppressed turn — its reply must never become a chat message, and
    # the next prompt runs as a clean turn, not a steer into it
    monkeypatch.setenv("FAKE_GOAL", "1")
    session_id, _ = await shell_client.create_session(
        "the preamble", suppress_first=True
    )
    state = await shell_client.get_session(session_id)
    assert not any("the preamble" in m.message for m in state.messages)
    await shell_client.send_message(session_id, "/goal tick forever")
    await _wait_for(shell_client, session_id, "tick 0")
