import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest
import pytest_asyncio

from app.local import (
    LocalClient,
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
    import json, os, sys, time
    sid = "fake-sid"
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
            chunks = ["echo: ", text]
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
            print(json.dumps({"jsonrpc": "2.0", "id": i,
                              "result": {"sessions": [{
                                  "sessionId": sid,
                                  "title": "resumed title",
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
async def test_modes_and_models_probe(shell_client: LocalClient) -> None:
    assert await shell_client.modes() == ["accept-edits", "smart"]
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
