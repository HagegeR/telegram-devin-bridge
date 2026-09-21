"""Local speech-to-text via faster-whisper (no API key)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import tempfile
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_models: dict[str, Any] = {}
_lock = asyncio.Lock()
_TIMEOUT = 120
_AUDIO_SUFFIXES = frozenset({
    ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac",
})
# Demuxers ffmpeg may pick when probing untrusted media; excludes playlist-like
# demuxers (hls, concat, ...) that dereference external file:/http: references.
_FFMPEG_FORMATS = "ogg,mp3,mov,mp4,m4a,aac,wav,flac,matroska,webm"

# ponytail: one local transcription (ffmpeg + model process/container) at a
# time, process-wide; a burst of voice notes queues instead of forking N
# memory-heavy children. Make it a setting if a beefier host ever needs more.
_local_slots: asyncio.Semaphore | None = None


def _slot() -> asyncio.Semaphore:
    global _local_slots
    if _local_slots is None:
        _local_slots = asyncio.Semaphore(1)
    return _local_slots


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
    try:
        async with _lock:
            model = _models.get(model_name)
            if model is None:
                model = await asyncio.to_thread(_load, model_name)
                _models[model_name] = model
        text = await asyncio.to_thread(
            _transcribe,
            model,
            content,
            filename,
            language,
        )
    except Exception:
        logger.warning("Local transcription failed", exc_info=True)
        return None
    return text or None


async def _reap(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


async def _run_whispercpp_command(
    *args: str,
    stdin_data: bytes | None = None,
    env: dict[str, str] | None = None,
) -> bytes | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=(
                asyncio.subprocess.PIPE if stdin_data is not None else None
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError:
        logger.warning("command transcription failed", exc_info=True)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin_data)
            if stdin_data is not None
            else process.communicate(),
            _TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("command transcription timed out: %s", args[0])
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
    return (
        await _run_whispercpp_command(
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
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
    try:
        async with _slot():
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
    text = " ".join(stdout.decode(errors="replace").split())
    return text or None


async def transcribe_command(
    content: bytes,
    filename: str,
    command: Sequence[str],
    language: str | None,
) -> str | None:
    if not command:
        return None
    try:
        async with _slot():
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
                    stdin_data=wav_path.read_bytes(),
                    env=env,
                )
                if stdout is None:
                    return None
    except OSError:
        logger.warning("command transcription failed", exc_info=True)
        return None
    text = " ".join(stdout.decode(errors="replace").split())
    if not text:
        logger.warning("command transcription produced no output")
        return None
    return text


def docker_transcription_command(image: str, memory: str) -> list[str]:
    # --pull never: only locally built images; --network none: no network;
    # no volume mounts — audio goes in via stdin only; no capabilities, no
    # privilege escalation, bounded process count.
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
        "--memory",
        memory,
        image,
    ]
