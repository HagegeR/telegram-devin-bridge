from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from app.clients import CAPTION_LIMIT, TelegramClient, _caption
from app.images import PHOTO_MAX_DIMENSION_SUM, fit_photo


def png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_fit_photo_keeps_small_images_untouched() -> None:
    content = png(200, 100)
    assert fit_photo(content) == (content, "image/png")


def test_fit_photo_downscales_tall_pages() -> None:
    fitted = fit_photo(png(1650, 8800))
    assert fitted is not None
    data, content_type = fitted
    assert content_type == "image/jpeg"
    width, height = Image.open(io.BytesIO(data)).size
    assert width + height <= PHOTO_MAX_DIMENSION_SUM
    assert abs(width / height - 1650 / 8800) < 0.01


def test_fit_photo_rejects_extreme_ratio_and_garbage() -> None:
    assert fit_photo(png(100, 3000)) is None
    assert fit_photo(b"not an image") is None


def test_caption_is_cut_to_telegram_limit() -> None:
    assert _caption(None) is None
    assert _caption("short") == "short"
    cut = _caption("x" * 5000)
    assert cut is not None
    assert len(cut) == CAPTION_LIMIT
    assert cut.endswith("\u2026")


def _client(handler) -> TelegramClient:
    return TelegramClient(
        "token",
        base_url="https://api.telegram.org/bottoken",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_send_photo_resizes_and_uses_photo_endpoint() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    client = _client(handler)
    try:
        result = await client.send_photo(
            1, "year_positions.png", png(1650, 8800), caption="c" * 2000
        )
    finally:
        await client.client.aclose()
    assert result == {"message_id": 7}
    assert paths == ["/bottoken/sendPhoto"]


@pytest.mark.asyncio
async def test_send_photo_falls_back_to_document_when_unfit_or_rejected() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/sendPhoto"):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "description": "Bad Request: PHOTO_INVALID_DIMENSIONS",
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})

    client = _client(handler)
    try:
        assert await client.send_photo(1, "strip.png", png(100, 3000)) == {
            "message_id": 8
        }
        assert await client.send_photo(1, "ok.png", png(300, 200)) == {"message_id": 8}
    finally:
        await client.client.aclose()
    assert paths == [
        "/bottoken/sendDocument",
        "/bottoken/sendPhoto",
        "/bottoken/sendDocument",
    ]
