"""Local speech-to-text via faster-whisper (no API key)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import tempfile
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_models: dict[str, Any] = {}
_lock = asyncio.Lock()
_TIMEOUT = 120


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
        suffix = Path(filename).suffix or ".audio"
        with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
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


async def _run_whispercpp_command(*args: str) -> bytes | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.warning("whisper.cpp transcription failed", exc_info=True)
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), _TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("whisper.cpp transcription timed out: %s", args[0])
        await _reap(process)
        return None
    except asyncio.CancelledError:
        await _reap(process)
        raise
    if process.returncode != 0:
        logger.warning("whisper.cpp transcription failed")
        return None
    return stdout


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
        suffix = Path(filename).suffix or ".audio"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / f"input{suffix}"
            wav_path = Path(directory) / "audio.wav"
            input_path.write_bytes(content)
            if await _run_whispercpp_command(
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(input_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ) is None:
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
