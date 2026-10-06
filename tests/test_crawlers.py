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
async def test_crawl_quota_counts_only_owned_urls() -> None:
    from app.crawlers import crawl_text

    client = httpx.AsyncClient(transport=_crawl_transport())
    results = await crawl_text(
        "https://a.test/1 https://b.test/2 https://c.test/3 "
        "https://www.instagram.com/p/ABC123/",
        {"instagram"},
        client,
    )
    # the three non-matching URLs don't consume the 3-URL quota
    assert [r.site for r in results] == ["instagram"]


@pytest.mark.asyncio
async def test_private_urls_are_not_crawled() -> None:
    from app.crawlers import crawl_text

    client = httpx.AsyncClient(transport=_crawl_transport())
    for url in (
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest",
        "http://192.168.1.1/",
        "http://localhost:8080/",
    ):
        assert await crawl_text(url, {"article"}, client) == []


@pytest.mark.asyncio
async def test_crawl_command_shows_global_config(tmp_path: Path) -> None:
    telegram = _FakeTelegram()
    store = Store(":memory:")
    runtime = Bridge(
        settings(tmp_path, crawl_sites="instagram,article"),
        store,
        _FakeDevin(),
        telegram,  # type: ignore[arg-type]
    )
    await handle_command(runtime, message("/crawl"), "/crawl")
    text = str(telegram.sent[-1]["text"])
    assert "article" in text and "instagram" in text
    runtime2 = Bridge(settings(tmp_path), Store(":memory:"), _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(runtime2, message("/crawl"), "/crawl")
    assert "off" in str(telegram.sent[-1]["text"])
    await runtime.shutdown()
    await runtime2.shutdown()


def test_crawl_sites_env_parsing_and_validation(tmp_path: Path) -> None:
    assert settings(tmp_path, crawl_sites="instagram, ARTICLE").crawl_site_set == frozenset(
        {"instagram", "article"}
    )
    assert settings(tmp_path).crawl_site_set == frozenset()
    with pytest.raises(ValueError, match="unknown crawlers"):
        settings(tmp_path, crawl_sites="bogus")


@pytest.mark.asyncio
async def test_crawled_content_appended_to_prompt(tmp_path: Path) -> None:
    devin = _FakeDevin()
    runtime = Bridge(
        settings(tmp_path, crawl_sites="instagram"),
        Store(":memory:"),
        devin,
        _FakeTelegram(),  # type: ignore[arg-type]
    )
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
