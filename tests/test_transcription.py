from types import SimpleNamespace

import pytest

from app import transcription


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
