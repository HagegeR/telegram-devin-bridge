import asyncio
import sys
import textwrap
from pathlib import Path

import pytest
import pytest_asyncio

from app.local import LocalClient, is_local

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
                                  "id": "model",
                                  "currentValue": "swe-2-high",
                                  "options": [
                                      {"value": "swe-2-high"},
                                      {"value": "swe-2-low"},
                                  ]}]}}), flush=True)
        elif meth in ("session/set_mode", "session/set_config_option"):
            print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {}}), flush=True)
        elif meth == "session/prompt":
            text = m["params"]["prompt"][0]["text"]
            chunks = ["echo: ", text]
            if os.environ.get("FAKE_SLOW"):
                chunks = ["echo: \\n", text]
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
            for chunk in chunks:
                if os.environ.get("FAKE_SLOW"):
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


@pytest_asyncio.fixture
async def shell_client(tmp_path: Path):
    fake = tmp_path / "fake_acp.py"
    fake.write_text(FAKE_ACP)
    shim = tmp_path / "devin"
    shim.write_text(
        "#!/bin/sh\n"
        f"exec {sys.executable} {fake} \"$@\"\n"
    )
    shim.chmod(0o755)
    client = LocalClient(cli_command=str(shim), api_key=None)
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


@pytest.mark.asyncio
async def test_create_applies_mode_and_model(shell_client: LocalClient) -> None:
    session_id, _ = await shell_client.create_session(
        "hi", mode="smart", model="swe-2-high"
    )
    await _wait_for(shell_client, session_id, "echo: hi")
    await shell_client.set_mode(session_id, "smart")
    await shell_client.set_model(session_id, "swe-2-low")


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
    assert any("big:" + "Z" * 100 in m.message for m in state.messages)


@pytest.mark.asyncio
async def test_activity_surfaces_in_status_detail(
    shell_client: LocalClient,
) -> None:
    session_id, _ = await shell_client.create_session("hello")
    await _wait_for(shell_client, session_id, "echo: hello")
    state = await shell_client.get_session(session_id)
    assert state.status_detail == "Running pytest"


@pytest.mark.asyncio
async def test_partial_replies_stream_mid_turn(
    shell_client: LocalClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # long turns used to stay silent until stopReason — the flusher emits
    # buffered chunks periodically while the turn is still running
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
    assert state.messages[0].message.strip() == "echo:"
    await _wait_for(shell_client, session_id, "hello")


@pytest.mark.asyncio
async def test_login_turn_suppressed_when_logged_out(tmp_path: Path) -> None:
    # regression: a configured api_key must reach the auth check (the
    # _cli_authed/_cli_logged_in name shadow used to TypeError here), and
    # the /login turn must not surface as a user-visible event
    fake = tmp_path / "fake_acp.py"
    fake.write_text(FAKE_ACP)
    shim = tmp_path / "devin"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "auth" ]; then printf "Logged out\\n"; exit 0; fi\n'
        f"exec {sys.executable} {fake} \"$@\"\n"
    )
    shim.chmod(0o755)
    client = LocalClient(cli_command=str(shim), api_key="sekrit")
    try:
        session_id, _ = await client.create_session("hi")
        await _wait_for(client, session_id, "echo: hi")
        state = await client.get_session(session_id)
        assert [m.message for m in state.messages] == ["echo: hi"]
    finally:
        for sid in list(client.sessions):
            await client.terminate(sid)
