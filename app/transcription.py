"""Local speech-to-text via faster-whisper (no API key)."""

from __future__ import annotations

import asyncio
import io
import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_models: dict[str, Any] = {}
_lock = asyncio.Lock()


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
    except ImportError:
        logger.warning("Local transcription failed", exc_info=True)
        return None
    except Exception:
        logger.warning("Local transcription failed", exc_info=True)
        return None
    return text or None
