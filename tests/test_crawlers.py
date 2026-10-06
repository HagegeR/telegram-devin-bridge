from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.commands import handle_command
from app.main import Bridge
from app.store import Store
from tests.test_v2 import _FakeDevin, _FakeTelegram, message, settings


def _crawl_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oembed" in url:
            return httpx.Response(
                200,
                json={
                    "title": "Test caption",
                    "author_name": "the_coding_wizard",
                    "thumbnail_url": "https://img.test/thumb.jpg",
                },
            )
        if url == "https://img.test/thumb.jpg":
            return httpx.Response(
                200,
                content=b"jpeg-bytes",
                headers={"content-type": "image/jpeg"},
            )
        if request.url.host in {"instagram.com", "www.instagram.com"}:
            return httpx.Response(
                200,
                text="<html><head><title>IG page</title></head><body>x</body></html>",
                headers={"content-type": "text/html"},
            )
        if "example.test" in url:
            return httpx.Response(
                200,
                text=(
                    "<html><head><title>Story</title>"
                    '<meta property="og:description" content="A summary">'
                    "</head><body><script>skip()</script>Body text</body></html>"
                ),
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_crawl_text_instagram_and_article() -> None:
    from app.crawlers import crawl_text

    client = httpx.AsyncClient(transport=_crawl_transport())
    results = await crawl_text(
        "see https://www.instagram.com/p/ABC123/ and https://example.test/x",
        {"instagram", "article"},
        client,
    )
    assert [r.site for r in results] == ["instagram", "article"]
    ig, article = results
    assert "Test caption" in ig.text
    assert "@the_coding_wizard" in ig.text
    assert ig.images and ig.images[0][1] == b"jpeg-bytes"
    assert "Story" in article.text
    assert "A summary" in article.text
    assert "Body text" in article.text
    assert "skip()" not in article.text


@pytest.mark.asyncio
async def test_crawl_text_respects_opt_in() -> None:
    from app.crawlers import crawl_text

    client = httpx.AsyncClient(transport=_crawl_transport())
    assert (
        await crawl_text(
            "https://www.instagram.com/p/ABC123/",
            set(),
            client,
        )
    ) == []
    results = await crawl_text(
        "https://www.instagram.com/p/ABC123/",
        {"article"},
        client,
    )
    # article crawler owns the URL when instagram is not enabled
    assert [r.site for r in results] == ["article"]


@pytest.mark.asyncio
async def test_crawl_command_sets_shows_and_clears(tmp_path: Path) -> None:
    telegram = _FakeTelegram()
    store = Store(":memory:")
    runtime = Bridge(settings(tmp_path), store, _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(runtime, message("/crawl"), "/crawl")
    assert "off" in str(telegram.sent[-1]["text"])
    await handle_command(
        runtime, message("/crawl instagram,article"), "/crawl instagram,article"
    )
    assert store.get_settings("222").crawl_site_list == [
        "instagram",
        "article",
    ]
    await handle_command(runtime, message("/crawl bogus"), "/crawl bogus")
    assert "Unknown" in str(telegram.sent[-1]["text"])
    await handle_command(runtime, message("/crawl off"), "/crawl off")
    assert store.get_settings("222").crawl_sites is None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_crawl_sites_setting_round_trip() -> None:
    store = Store(":memory:")
    store.update_settings("222", crawl_sites="instagram")
    assert store.get_settings("222").crawl_site_list == ["instagram"]
    store.update_settings("222", crawl_sites=None)
    assert store.get_settings("222").crawl_site_list is None


@pytest.mark.asyncio
async def test_crawled_content_appended_to_prompt(tmp_path: Path) -> None:
    devin = _FakeDevin()
    store = Store(":memory:")
    store.update_settings("222", crawl_sites="instagram")
    runtime = Bridge(settings(tmp_path), store, devin, _FakeTelegram())  # type: ignore[arg-type]
    runtime._crawl_client = httpx.AsyncClient(transport=_crawl_transport())
    await runtime.handle_user_turn(
        message("look at https://www.instagram.com/p/ABC123/", message_id=1),
        "look at https://www.instagram.com/p/ABC123/",
    )
    prompt = devin.created[0]
    assert "[Crawled instagram:" in prompt
    assert "Test caption" in prompt
    assert "Attached file: https://files.test/crawl.jpg (crawl.jpg)" in prompt
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_no_crawl_when_disabled(tmp_path: Path) -> None:
    devin = _FakeDevin()
    store = Store(":memory:")
    runtime = Bridge(settings(tmp_path), store, devin, _FakeTelegram())  # type: ignore[arg-type]
    await runtime.handle_user_turn(
        message("look at https://www.instagram.com/p/ABC123/", message_id=1),
        "look at https://www.instagram.com/p/ABC123/",
    )
    assert "[Crawled" not in devin.created[0]
    await runtime.shutdown()
