import asyncio
from types import SimpleNamespace

import pytest

from app import transcription


@pytest.mark.asyncio
async def test_transcribe_whispercpp_returns_none_for_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def create_process(*args: str, **_: object) -> Process:
        if args[0] == "ffmpeg":
            return Process()
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(
        transcription.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    assert await transcription.transcribe_whispercpp(
        b"not audio",
        "voice.ogg",
        "/nonexistent/whisper-cli",
        "/nonexistent/model.bin",
        None,
    ) is None


@pytest.mark.asyncio
async def test_transcribe_whispercpp_runs_commands_and_normalizes_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"  Hello  there\n", b""

    async def create_process(*args: str, **_: object) -> Process:
        calls.append(args)
        return Process()

    monkeypatch.setattr(
        transcription.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    assert await transcription.transcribe_whispercpp(
        b"audio",
        "voice.ogg",
        "whisper-cli",
        "model.bin",
        None,
    ) == "Hello there"
    assert calls[0][calls[0].index("-protocol_whitelist") + 1] == "file"
    assert calls[0][calls[0].index("-i") + 1].endswith("input.ogg")
    assert "-l" in calls[1]
    assert calls[1][calls[1].index("-l") + 1] == "auto"


def test_suffix_ignores_untrusted_extensions() -> None:
    assert transcription._suffix("voice.OGG") == ".ogg"
    assert transcription._suffix("evil.m3u8") == ".audio"
    assert transcription._suffix("noext") == ".audio"


@pytest.mark.asyncio
async def test_transcribe_local_returns_none_when_loader_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcription._models.clear()

    def load(_name: str) -> object:
        raise ImportError("faster-whisper is not installed")

    monkeypatch.setattr(transcription, "_load", load)
    assert await transcription.transcribe_local(
        b"audio",
        "voice.ogg",
        "test-import-error",
        "en",
    ) is None


@pytest.mark.asyncio
async def test_transcribe_local_joins_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcription._models.clear()

    class Model:
        def transcribe(self, *_: object, **__: object) -> tuple[list[object], None]:
            return [
                SimpleNamespace(text=" hi "),
                SimpleNamespace(text="there"),
            ], None

    monkeypatch.setattr(transcription, "_load", lambda _name: Model())
    assert await transcription.transcribe_local(
        b"audio",
        "voice.ogg",
        "test-segments",
        "en",
    ) == "hi there"


@pytest.mark.asyncio
async def test_run_whispercpp_command_kills_process_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "_TIMEOUT", 0.1)
    assert await transcription._run_whispercpp_command("sleep", "30") is None
    ps = await asyncio.create_subprocess_exec(
        "ps", "-eo", "args", stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await ps.communicate()
    assert b"sleep 30" not in stdout
