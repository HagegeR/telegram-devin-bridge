import asyncio
import sys
import textwrap
from pathlib import Path

import pytest
import pytest_asyncio

from app.local import LocalClient, is_local

FAKE_ACP = textwrap.dedent(
    """
    import json, sys
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
            print(json.dumps({"jsonrpc": "2.0", "id": i,
                              "result": {"sessionId": sid}}), flush=True)
        elif meth == "session/prompt":
            text = m["params"]["prompt"][0]["text"]
            for chunk in ("echo: ", text):
                print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                                  "params": {"sessionId": sid, "update": {
                                      "sessionUpdate": "agent_message_chunk",
                                      "content": {"type": "text", "text": chunk}}}}),
                      flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": i,
                              "result": {"stopReason": "end_turn"}}), flush=True)
        elif meth == "session/delete":
            print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {}}), flush=True)
    """
)


@pytest_asyncio.fixture
async def shell_client(tmp_path: Path):
    fake = tmp_path / "fake_acp.py"
    fake.write_text(FAKE_ACP)
    shim = tmp_path / "devin"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n")
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
async def test_terminate_marks_expired(shell_client: LocalClient) -> None:
    session_id, _ = await shell_client.create_session("bye")
    await shell_client.terminate(session_id)
    state = await shell_client.get_session(session_id)
    assert state.status_enum == "expired"
