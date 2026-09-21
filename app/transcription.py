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
    await _slots.acquire()
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


def _release_slot(job: asyncio.Future[str]) -> None:
    if not job.cancelled() and job.exception() is not None:
        logger.warning("Late local transcription failed", exc_info=job.exception())
    _slots.release()


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


async def transcribe_whispercpp(
    content: bytes,
    filename: str,
    binary: str,
    model_path: str,
    language: str | None,
) -> str | None:
    try:
        async with _slots:
            with tempfile.TemporaryDirectory() as directory:
                input_path = Path(directory) / f"input{_suffix(filename)}"
                wav_path = Path(directory) / "audio.wav"
                input_path.write_bytes(content)
                if await _run_whispercpp_command(
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
