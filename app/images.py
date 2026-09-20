"""Fit images to Telegram's sendPhoto limits."""

from __future__ import annotations

import io

from PIL import Image, ImageOps

# https://core.telegram.org/bots/api#sendphoto
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_MAX_DIMENSION_SUM = 10000
PHOTO_MAX_RATIO = 20


def fit_photo(content: bytes) -> tuple[bytes, str] | None:
    """Return (bytes, content_type) acceptable to sendPhoto, or None if the image
    cannot be made to fit (extreme aspect ratio or undecodable)."""
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
        image_format = image.format
        image = ImageOps.exif_transpose(image) or image
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    width, height = image.size
    if max(width, height) > PHOTO_MAX_RATIO * min(width, height):
        return None
    if width + height <= PHOTO_MAX_DIMENSION_SUM and len(content) <= PHOTO_MAX_BYTES:
        return (
            None if image_format is None else (content, f"image/{image_format.lower()}")
        )
    scale = min(1.0, PHOTO_MAX_DIMENSION_SUM / (width + height))
    flat = Image.new("RGB", image.size, "white")
    rgba = image.convert("RGBA")
    flat.paste(rgba, mask=rgba.getchannel("A"))
    resized = flat.resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        Image.Resampling.LANCZOS,
    )
    for quality in (90, 75, 60):
        buffer = io.BytesIO()
        resized.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= PHOTO_MAX_BYTES:
            return buffer.getvalue(), "image/jpeg"
    return None
