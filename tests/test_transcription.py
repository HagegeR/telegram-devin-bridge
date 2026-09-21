import asyncio
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


def test_whispercpp_args_fast_short_clip() -> None:
    args = transcription.whispercpp_args(
        "whisper-cli",
        "model.bin",
        Path("audio.wav"),
        "en",
        11.0,
        True,
        ("-tr", "--foo"),
    )
    assert args[args.index("-bs") + 1] == "1"
    assert args[args.index("-bo") + 1] == "1"
    # 11s / 30s * 1500 + 128 = 678
    assert args[args.index("-ac") + 1] == "678"
    assert args[-2:] == ["-tr", "--foo"]


def test_whispercpp_args_fast_long_clip_no_ac() -> None:
    args = transcription.whispercpp_args(
        "whisper-cli", "model.bin", Path("audio.wav"), None, 40.0, True, ()
    )
    assert "-bs" in args and "-bo" in args
    assert "-ac" not in args


def test_whispercpp_args_fast_unknown_duration_no_ac() -> None:
    args = transcription.whispercpp_args(
        "whisper-cli", "model.bin", Path("audio.wav"), None, None, True, ()
    )
    assert "-bs" in args and "-bo" in args
    assert "-ac" not in args


def test_whispercpp_args_not_fast() -> None:
    args = transcription.whispercpp_args(
        "whisper-cli",
        "model.bin",
        Path("audio.wav"),
        "en",
        11.0,
        False,
        ("-tr",),
    )
    assert "-bs" not in args and "-bo" not in args and "-ac" not in args
    assert args[-1] == "-tr"


def test_wav_duration_seconds(tmp_path: Path) -> None:
    import wave

    wav_path = tmp_path / "clip.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000 * 2)
    assert transcription._wav_duration_seconds(wav_path) == pytest.approx(2.0)

    bad_path = tmp_path / "clip.bin"
    bad_path.write_bytes(b"not a wav")
    assert transcription._wav_duration_seconds(bad_path) is None


def test_whisper_cpp_fast_settings(tmp_path: Path, monkeypatch) -> None:
    from app.config import Settings

    monkeypatch.setenv("WHISPER_CPP_FAST", "false")
    monkeypatch.setenv("WHISPER_CPP_EXTRA_ARGS", "-tr --foo")
    config = Settings(_env_file=None)
    assert config.whisper_cpp_fast is False
    assert config.whisper_cpp_extra_args == "-tr --foo"
    assert config.whisper_cpp_extra_argv == ["-tr", "--foo"]


@pytest.mark.parametrize(
    "extra_args",
    ["-tr \"", "-tr -f /etc/passwd", "-m x", "-otxt", "--output-file x"],
)
def test_whisper_cpp_extra_args_rejected(monkeypatch, extra_args: str) -> None:
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv("WHISPER_CPP_EXTRA_ARGS", extra_args)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
