"""Local speech-to-text via faster-whisper (no API key)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_models: dict[str, Any] = {}
_lock = asyncio.Lock()
_TIMEOUT = 120
_FORMATS = {
    ".ogg": "ogg",
    ".oga": "ogg",
    ".opus": "ogg",
    ".mp3": "mp3",
    ".m4a": "mov",
    ".mp4": "mov",
    ".aac": "aac",
    ".wav": "wav",
    ".flac": "flac",
}


def _suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in _FORMATS else ".audio"


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
    stdin: bytes | None = None,
) -> bytes | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.warning("whisper.cpp transcription failed", exc_info=True)
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(stdin), _TIMEOUT)
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


async def transcribe_whispercpp(
    content: bytes,
    filename: str,
    binary: str,
    model_path: str,
    language: str | None,
) -> str | None:
    try:
        suffix = _suffix(filename)
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "audio.wav"
            # Untrusted bytes: force the demuxer from the allowlisted suffix so
            # probing can't pick a playlist demuxer; without a known suffix,
            # stream over a pipe so nested references can't open local files.
            stdin: bytes | None = None
            if suffix in _FORMATS:
                input_path = Path(directory) / f"input{suffix}"
                input_path.write_bytes(content)
                source = (
                    "-protocol_whitelist", "file",
                    "-f", _FORMATS[suffix],
                    "-i", str(input_path),
                )
            else:
                source = ("-protocol_whitelist", "pipe", "-i", "pipe:0")
                stdin = content
            if await _run_whispercpp_command(
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                *source,
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
                stdin=stdin,
            ) is None:
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
