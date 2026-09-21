import asyncio
import io
import sys
import wave
from pathlib import Path
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
    assert "-l" in calls[1]
    assert calls[1][calls[1].index("-l") + 1] == "auto"


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


@pytest.mark.asyncio
async def test_transcribe_command_pipes_wav_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_ffmpeg(input_path: Path, wav_path: Path) -> bool:
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 1600)
        return True

    monkeypatch.setattr(transcription, "_to_wav16k", fake_ffmpeg)
    text = await transcription.transcribe_command(
        b"audio",
        "voice.ogg",
        [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"],
        None,
    )
    assert text is not None
    assert int(text) > 1600 * 2  # wav bytes incl. header reached stdin


@pytest.mark.asyncio
async def test_transcribe_command_passes_language_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ffmpeg(input_path: Path, wav_path: Path) -> bool:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 160)
        wav_path.write_bytes(buffer.getvalue())
        return True

    monkeypatch.setattr(transcription, "_to_wav16k", fake_ffmpeg)
    text = await transcription.transcribe_command(
        b"audio",
        "voice.ogg",
        [
            sys.executable,
            "-c",
            (
                "import os,sys; sys.stdin.buffer.read(); "
                "print(os.environ.get('TRANSCRIPTION_LANGUAGE',''))"
            ),
        ],
        "en",
    )
    assert text == "en"


@pytest.mark.asyncio
async def test_transcribe_command_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_ffmpeg(input_path: Path, wav_path: Path) -> bool:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 160)
        wav_path.write_bytes(buffer.getvalue())
        return True

    monkeypatch.setattr(transcription, "_to_wav16k", fake_ffmpeg)
    assert await transcription.transcribe_command(
        b"audio", "voice.ogg", [sys.executable, "-c", "import sys; sys.exit(1)"], None
    ) is None
    assert await transcription.transcribe_command(
        b"audio", "voice.ogg", [sys.executable, "-c", "pass"], None
    ) is None


def test_command_backend_config() -> None:
    from app.config import Settings

    with pytest.raises(ValueError, match="transcription_command"):
        Settings(
            _env_file=None,
            telegram_bot_token="t",
            telegram_webhook_secret="s",
            devin_api_key="k",
            public_base_url="http://x",
            transcription_backend="command",
        )
    config = Settings(
        _env_file=None,
        telegram_bot_token="t",
        telegram_webhook_secret="s",
        devin_api_key="k",
        public_base_url="http://x",
        transcription_backend="command",
        transcription_command="docker run --rm -i moonshine-asr",
    )
    assert config.transcription_enabled
    # exec vector: never admin-editable
    assert "TRANSCRIPTION_COMMAND" not in config.admin_env_keys
