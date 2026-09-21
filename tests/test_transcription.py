import asyncio
import io
import shutil
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import transcription


def _stream(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_transcribe_whispercpp_returns_none_for_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        returncode = 0
        stdout = _stream(b"")
        stderr = _stream(b"")

        async def wait(self) -> int:
            return 0

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

        def __init__(self) -> None:
            self.stdout = _stream(b"  Hello  there\n")
            self.stderr = _stream(b"")

        async def wait(self) -> int:
            return 0

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
    assert "hls" not in calls[0][calls[0].index("-format_whitelist") + 1]
    assert calls[0][calls[0].index("-i") + 1].endswith("input.ogg")
    assert "-l" in calls[1]
    assert calls[1][calls[1].index("-l") + 1] == "auto"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_transcribe_whispercpp_rejects_playlist_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = tmp_path / "secret.mp3"
    secret.write_bytes(b"secret")
    playlist = (
        f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10,\n{secret.as_uri()}\n"
        "#EXT-X-ENDLIST\n"
    ).encode()
    ffmpeg_ran = False
    run = transcription._run_whispercpp_command

    async def spy(*args: str) -> bytes | None:
        nonlocal ffmpeg_ran
        if args[0] != "ffmpeg":
            return b"should not reach whisper"
        ffmpeg_ran = True
        return await run(*args)

    monkeypatch.setattr(transcription, "_run_whispercpp_command", spy)
    assert await transcription.transcribe_whispercpp(
        playlist, "voice.ogg", "whisper-cli", "model.bin", None
    ) is None
    assert ffmpeg_ran


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


@pytest.mark.asyncio
async def test_run_whispercpp_command_kills_grandchildren_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "_TIMEOUT", 0.1)
    assert (
        await transcription._run_whispercpp_command("sh", "-c", "sleep 31; sleep 32")
        is None
    )
    ps = await asyncio.create_subprocess_exec(
        "ps", "-eo", "args", stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await ps.communicate()
    assert b"sleep 31" not in stdout


@pytest.mark.asyncio
async def test_run_whispercpp_command_caps_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "_MAX_OUTPUT", 1000)
    assert await transcription._run_whispercpp_command("yes") is None
    assert await transcription._run_whispercpp_command("echo", "ok") == b"ok\n"


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
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_to_wav16k_caps_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcription, "_MAX_SECONDS", 1)
    src = tmp_path / "long.wav"
    with wave.open(str(src), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 8000 * 5)
    out = tmp_path / "out.wav"
    assert await transcription._to_wav16k(src, out)
    assert transcription._wav_duration_seconds(out) == pytest.approx(1.0, abs=0.05)


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
    monkeypatch.setenv("TRANSCRIPTION_LANGUAGE", "fr")
    command = [
        sys.executable,
        "-c",
        (
            "import os,sys; sys.stdin.buffer.read(); "
            "print(os.environ.get('TRANSCRIPTION_LANGUAGE','unset'))"
        ),
    ]
    assert (
        await transcription.transcribe_command(b"audio", "voice.ogg", command, "en")
        == "en"
    )
    # /lang auto must not leak the deployment default to the child
    assert (
        await transcription.transcribe_command(b"audio", "voice.ogg", command, None)
        == "unset"
    )


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
    with pytest.raises(ValueError, match="malformed"):
        Settings(
            _env_file=None,
            telegram_bot_token="t",
            telegram_webhook_secret="s",
            devin_api_key="k",
            public_base_url="http://x",
            transcription_backend="command",
            transcription_command="docker run 'moonshine-asr",
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


def _config(tmp_path: Path, **overrides: object):
    from app.config import Settings

    values = {
        "telegram_bot_token": "t",
        "telegram_webhook_secret": "s",
        "devin_api_key": "k",
        "public_base_url": "http://x",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "image",
    [
        "moonshine-asr",
        "moonshine-asr:v2",
        "ghcr.io/org/img:1.2",
        "localhost:5000/img",
        "img@sha256:" + "a" * 64,
    ],
)
def test_docker_image_valid(tmp_path: Path, image: str) -> None:
    config = _config(
        tmp_path,
        transcription_backend="docker",
        transcription_docker_image=image,
    )
    assert config.transcription_enabled


@pytest.mark.parametrize(
    "image",
    ["Moonshine", "img; rm -rf /", "img rm", "-img", "img:tag with space"],
)
def test_docker_image_invalid(tmp_path: Path, image: str) -> None:
    with pytest.raises(ValueError, match="docker_image"):
        _config(
            tmp_path,
            transcription_backend="docker",
            transcription_docker_image=image,
        )


@pytest.mark.parametrize("memory", ["400m", "1g", "6291456", " 400m "])
def test_docker_memory_valid(tmp_path: Path, memory: str) -> None:
    config = _config(
        tmp_path,
        transcription_backend="docker",
        transcription_docker_image="moonshine-asr",
        transcription_docker_memory=memory,
    )
    assert config.transcription_docker_memory == memory.strip()


@pytest.mark.parametrize("memory", ["400mb", "-1m", "1 g", "", "512", "5m", "0"])
def test_docker_memory_invalid(tmp_path: Path, memory: str) -> None:
    with pytest.raises(ValueError, match="docker_memory"):
        _config(
            tmp_path,
            transcription_backend="docker",
            transcription_docker_image="moonshine-asr",
            transcription_docker_memory=memory,
        )


def test_docker_backend_requires_image(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="docker_image"):
        _config(tmp_path, transcription_backend="docker")


def test_docker_transcription_command_argv() -> None:
    assert transcription.docker_transcription_command(
        "moonshine-asr", "400m", "transcribe-1"
    ) == [
        "docker",
        "run",
        "--rm",
        "-i",
        "--pull",
        "never",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--env",
        "TRANSCRIPTION_LANGUAGE",
        "--memory",
        "400m",
        "--name",
        "transcribe-1",
        "moonshine-asr",
    ]
    assert transcription.docker_cleanup_command("transcribe-1") == [
        "docker",
        "rm",
        "-f",
        "transcribe-1",
    ]


@pytest.mark.asyncio
async def test_transcribe_command_runs_cleanup_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_ffmpeg(input_path: Path, wav_path: Path) -> bool:
        wav_path.write_bytes(b"")
        return True

    monkeypatch.setattr(transcription, "_to_wav16k", fake_ffmpeg)
    marker = tmp_path / "cleaned"
    assert (
        await transcription.transcribe_command(
            b"audio", "voice.ogg", ["false"], None, cleanup=["touch", str(marker)]
        )
        is None
    )
    assert marker.exists()
    marker.unlink()
    task = asyncio.ensure_future(
        transcription.transcribe_command(
            b"audio", "voice.ogg", ["sleep", "30"], None, cleanup=["touch", str(marker)]
        )
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert marker.exists()


@pytest.mark.asyncio
async def test_transcribe_command_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ffmpeg(input_path: Path, wav_path: Path) -> bool:
        wav_path.write_bytes(b"RIFF")
        return True

    monkeypatch.setattr(transcription, "_to_wav16k", fake_ffmpeg)
    monkeypatch.setattr(transcription, "_slots", asyncio.Semaphore(1))
    active = 0
    peak = 0

    async def fake_run(*args: str, **kwargs: object) -> bytes:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return b"ok"

    monkeypatch.setattr(transcription, "_run_whispercpp_command", fake_run)
    results = await asyncio.gather(
        *(
            transcription.transcribe_command(b"a", "v.ogg", ["x"], None)
            for _ in range(5)
        )
    )
    assert results == ["ok"] * 5
    assert peak == 1


def test_admin_allowlist_excludes_exec_vectors(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert "TRANSCRIPTION_DOCKER_MEMORY" in config.admin_env_keys
    assert "TRANSCRIPTION_DOCKER_IMAGE" not in config.admin_env_keys
    assert "TRANSCRIPTION_COMMAND" not in config.admin_env_keys


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


@pytest.mark.asyncio
async def test_transcribe_local_returns_none_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcription._models.clear()
    monkeypatch.setattr(transcription, "_TIMEOUT", 0.05)

    def slow(*_: object, **__: object) -> str:
        time.sleep(0.5)
        return "late"

    monkeypatch.setattr(transcription, "_load", lambda _name: object())
    monkeypatch.setattr(transcription, "_transcribe", slow)
    assert await transcription.transcribe_local(
        b"audio",
        "voice.ogg",
        "test-timeout",
        "en",
    ) is None
    assert transcription._slots._value == transcription._MAX_CONCURRENT - 1
    await asyncio.sleep(0.6)
    assert transcription._slots._value == transcription._MAX_CONCURRENT


@pytest.mark.asyncio
async def test_transcribe_local_rejects_when_slots_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "_TIMEOUT", 0.05)
    monkeypatch.setattr(transcription, "_slots", asyncio.Semaphore(0))
    monkeypatch.setattr(transcription, "_load", lambda _name: object())
    assert await transcription.transcribe_local(
        b"audio",
        "voice.ogg",
        "test-busy",
        "en",
    ) is None
