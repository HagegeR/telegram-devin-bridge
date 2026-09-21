"""Local speech-to-text via faster-whisper (no API key)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import tempfile
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


async def transcribe_whispercpp(
    content: bytes,
    filename: str,
    binary: str,
    model_path: str,
    language: str | None,
) -> str | None:
    try:
        suffix = Path(filename).suffix or ".audio"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / f"input{suffix}"
            wav_path = Path(directory) / "audio.wav"
            input_path.write_bytes(content)
            if not await _to_wav16k(input_path, wav_path):
                return None
            stdout = await _run_whispercpp_command(
                binary,
                "-m",
                model_path,
                "-f",
                str(wav_path),
                "-nt",
                "-np",
                "-l",
                language or "auto",
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
        suffix = Path(filename).suffix or ".audio"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / f"input{suffix}"
            wav_path = Path(directory) / "audio.wav"
            input_path.write_bytes(content)
            if not await _to_wav16k(input_path, wav_path):
                return None
            env = dict(os.environ)
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
