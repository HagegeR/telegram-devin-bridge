"""Local speech-to-text via faster-whisper (no API key)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import signal
import tempfile
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_models: dict[str, Any] = {}
_lock = asyncio.Lock()
_TIMEOUT = 120
_MAX_SECONDS = 600
_MAX_OUTPUT = 1 << 20
_MAX_CONCURRENT = 2
_slots = asyncio.Semaphore(_MAX_CONCURRENT)
_AUDIO_SUFFIXES = frozenset({
    ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac",
})
# Demuxers ffmpeg may pick when probing untrusted media; excludes playlist-like
# demuxers (hls, concat, ...) that dereference external file:/http: references.
_FFMPEG_FORMATS = "ogg,mp3,mov,mp4,m4a,aac,wav,flac,matroska,webm"

def _suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in _AUDIO_SUFFIXES else ".audio"


def _load(name: str) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(name, device="cpu", compute_type="int8")


def _transcribe(
    model: Any,
    content: bytes,
    filename: str,
    language: str | None,
) -> str:
    options = {
        "language": language,
        "beam_size": 5,
        "vad_filter": True,
    }
    try:
        segments, _ = model.transcribe(io.BytesIO(content), **options)
    except Exception:  # noqa: BLE001
        with tempfile.NamedTemporaryFile(suffix=_suffix(filename)) as handle:
            handle.write(content)
            handle.flush()
            segments, _ = model.transcribe(handle.name, **options)
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe_local(
    content: bytes,
    filename: str,
    model_name: str,
    language: str | None,
) -> str | None:
    if not await _acquire_slot():
        return None
    job: asyncio.Future[str] | None = None
    try:
        async with _lock:
            model = _models.get(model_name)
            if model is None:
                model = await asyncio.to_thread(_load, model_name)
                _models[model_name] = model
        job = asyncio.ensure_future(
            asyncio.to_thread(_transcribe, model, content, filename, language)
        )
        text = await asyncio.wait_for(asyncio.shield(job), _TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Local transcription timed out")
        return None
    except Exception:
        logger.warning("Local transcription failed", exc_info=True)
        return None
    finally:
        # The thread cannot be interrupted, so the slot stays held until it exits.
        if job is not None and not job.done():
            job.add_done_callback(_release_slot)
        else:
            _slots.release()
    return text or None


async def _acquire_slot() -> bool:
    try:
        await asyncio.wait_for(_slots.acquire(), _TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Transcription rejected: all slots busy")
        return False
    return True


def _release_slot(job: asyncio.Future[str]) -> None:
    if not job.cancelled() and job.exception() is not None:
        logger.warning("Late local transcription failed", exc_info=job.exception())
    _slots.release()


async def _reap(process: asyncio.subprocess.Process) -> None:
    # Children run in their own session, so this reaches sh -c grandchildren.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def _read_capped(
    stream: asyncio.StreamReader, process: asyncio.subprocess.Process
) -> bytes | None:
    # Past the cap: kill, but keep draining so the pipe reaches EOF and wait()
    # can complete.
    buffer = bytearray()
    while chunk := await stream.read(65536):
        if buffer is None:
            continue
        buffer += chunk
        if len(buffer) > _MAX_OUTPUT:
            buffer = None
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    return None if buffer is None else bytes(buffer)


async def _communicate(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    assert process.stdout is not None and process.stderr is not None
    stdout, stderr = await asyncio.gather(
        _read_capped(process.stdout, process), _read_capped(process.stderr, process)
    )
    await process.wait()
    if stdout is None or stderr is None:
        raise ValueError("output limit exceeded")
    return stdout, stderr


async def _run_whispercpp_command(
    *args: str,
    stdin_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> bytes | None:
    try:
        stdin = os.open(stdin_path, os.O_RDONLY) if stdin_path is not None else None
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        finally:
            if stdin is not None:
                os.close(stdin)
    except OSError:
        logger.warning("command transcription failed", exc_info=True)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(_communicate(process), _TIMEOUT)
    except (asyncio.TimeoutError, ValueError) as exc:
        logger.warning("command transcription aborted (%s): %s", exc, args[0])
        await _reap(process)
        return None
    except asyncio.CancelledError:
        await _reap(process)
        raise
    if process.returncode != 0:
        logger.warning(
            "command transcription failed (exit %s): %s",
            process.returncode,
            stderr.decode(errors="replace")[:200],
        )
        return None
    return stdout


async def _to_wav16k(input_path: Path, wav_path: Path) -> bool:
    # -t caps decoded PCM at 32 kB/s * _MAX_SECONDS regardless of input bitrate.
    return (
        await _run_whispercpp_command(
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-t",
            str(_MAX_SECONDS),
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            _FFMPEG_FORMATS,
            "-i",
            str(input_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        )
        is not None
    )


def _wav_duration_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / wav.getframerate()
    except (wave.Error, OSError, EOFError, ZeroDivisionError):
        return None


def whispercpp_args(
    binary: str,
    model_path: str,
    wav_path: Path,
    language: str | None,
    duration_seconds: float | None,
    fast: bool,
    extra_args: Sequence[str],
) -> list[str]:
    args = [
        binary,
        "-m",
        model_path,
        "-f",
        str(wav_path),
        "-nt",
        "-np",
        "-l",
        language or "auto",
    ]
    if fast:
        args += ["-bs", "1", "-bo", "1"]
        # the encoder always processes a 30 s window (audio ctx 1500);
        # shrink it proportionally to the clip so short voice notes
        # skip decoding silence
        if duration_seconds is not None:
            ctx = min(1500, int(duration_seconds / 30 * 1500) + 128)
            if ctx < 1500:
                args += ["-ac", str(ctx)]
    args += list(extra_args)
    return args


async def transcribe_whispercpp(
    content: bytes,
    filename: str,
    binary: str,
    model_path: str,
    language: str | None,
    *,
    fast: bool = True,
    extra_args: Sequence[str] = (),
) -> str | None:
    if not await _acquire_slot():
        return None
    try:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / f"input{_suffix(filename)}"
            wav_path = Path(directory) / "audio.wav"
            input_path.write_bytes(content)
            if not await _to_wav16k(input_path, wav_path):
                return None
            stdout = await _run_whispercpp_command(
                *whispercpp_args(
                    binary,
                    model_path,
                    wav_path,
                    language,
                    _wav_duration_seconds(wav_path),
                    fast,
                    extra_args,
                )
            )
            if stdout is None:
                return None
    except OSError:
        logger.warning("whisper.cpp transcription failed", exc_info=True)
        return None
    finally:
        _slots.release()
    text = " ".join(stdout.decode(errors="replace").split())
    return text or None


async def transcribe_command(
    content: bytes,
    filename: str,
    command: Sequence[str],
    language: str | None,
    cleanup: Sequence[str] = (),
) -> str | None:
    """Run ``command`` with 16 kHz WAV on stdin; ``cleanup`` runs after any failure
    (timeout, kill, non-zero exit) for work the process group kill cannot reach,
    e.g. a daemon-owned docker container."""
    if not command or not await _acquire_slot():
        return None
    try:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / f"input{_suffix(filename)}"
            wav_path = Path(directory) / "audio.wav"
            input_path.write_bytes(content)
            if not await _to_wav16k(input_path, wav_path):
                return None
            env = dict(os.environ)
            env.pop("TRANSCRIPTION_LANGUAGE", None)
            if language:
                env["TRANSCRIPTION_LANGUAGE"] = language
            stdout = await _run_whispercpp_command(
                *command,
                stdin_path=wav_path,
                env=env,
            )
            if stdout is None:
                if cleanup:
                    await _run_whispercpp_command(*cleanup)
                return None
    except OSError:
        logger.warning("command transcription failed", exc_info=True)
        return None
    finally:
        _slots.release()
    text = " ".join(stdout.decode(errors="replace").split())
    if not text:
        logger.warning("command transcription produced no output")
        return None
    return text


def docker_transcription_command(image: str, memory: str, name: str) -> list[str]:
    # --pull never: only locally built images; --network none: no network;
    # no volume mounts — audio goes in via stdin only; no capabilities, no
    # privilege escalation, bounded process count; --env NAME copies the client's
    # TRANSCRIPTION_LANGUAGE only when set (unset for auto).
    return [
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
        memory,
        "--name",
        name,
        image,
    ]


def docker_cleanup_command(name: str) -> list[str]:
    return ["docker", "rm", "-f", name]
