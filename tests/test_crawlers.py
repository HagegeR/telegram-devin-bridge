from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.commands import handle_command
from app.main import Bridge
from app.store import Store
from tests.test_v2 import _FakeDevin, _FakeTelegram, message, settings


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch real DNS: public names resolve to a stub public IP,
    localhost names and numeric IPv4 spellings resolve to loopback."""

    async def _fake_resolve(hostname: str) -> list[str]:
        host = hostname.casefold()
        numericish = all(c in "0123456789abcdefx." for c in host) and any(
            c.isdigit() for c in host
        )
        if host in {"localhost", "localhost.localdomain"} or numericish:
            return ["127.0.0.1"]
        return ["93.184.216.34"]

    monkeypatch.setattr("app.crawlers._resolve_ips", _fake_resolve)


def _crawl_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.headers.get("host", "").split(":")[0].strip("[]")
        if "oembed" in url:
            return httpx.Response(
                200,
                json={
                    "title": "Test caption",
                    "author_name": "the_coding_wizard",
                    "thumbnail_url": "https://img.test/thumb.jpg",
                },
            )
        if host == "img.test" and url.endswith("/thumb.jpg"):
            return httpx.Response(
                200,
                content=b"jpeg-bytes",
                headers={"content-type": "image/jpeg"},
            )
        if host in {"instagram.com", "www.instagram.com"}:
            return httpx.Response(
                200,
                text="<html><head><title>IG page</title></head><body>x</body></html>",
                headers={"content-type": "text/html"},
            )
        if host == "example.test":
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
async def test_noncanonical_numeric_hosts_are_not_crawled() -> None:
    from app.crawlers import crawl_text

    client = httpx.AsyncClient(transport=_crawl_transport())
    for url in (
        "http://2130706433/",
        "http://127.1/admin",
        "http://0x7f.0.0.1/",
        "http://0177.0.0.1/",
    ):
        assert await crawl_text(url, {"article"}, client) == []


@pytest.mark.asyncio
async def test_dns_resolving_to_private_ip_is_not_crawled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.crawlers import crawl_text

    async def _private(_hostname: str) -> list[str]:
        return ["10.0.0.1"]

    monkeypatch.setattr("app.crawlers._resolve_ips", _private)
    client = httpx.AsyncClient(transport=_crawl_transport())
    assert await crawl_text("http://internal.attacker.test/", {"article"}, client) == []


@pytest.mark.asyncio
async def test_redirect_to_internal_target_is_not_crawled() -> None:
    from app.crawlers import crawl_text

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host") == "public.test":
            return httpx.Response(
                302, headers={"location": "http://2130706433:8080/admin"}
            )
        return httpx.Response(200, text="internal")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await crawl_text("http://public.test/page", {"article"}, client) == []


@pytest.mark.asyncio
async def test_fetch_pins_connection_to_vetted_ip() -> None:
    from app.crawlers import _pinned_request, crawl_text

    pinned = await _pinned_request("https://alice:secret@public.test:8443/p?q=1")
    assert pinned == (
        "https://alice:secret@93.184.216.34:8443/p?q=1",
        "public.test:8443",
        "public.test",
    )

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url_host"] = request.url.host
        seen["host_header"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            text="<html><body>ok</body></html>",
            headers={"content-type": "text/html"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results = await crawl_text(
        "https://alice:secret@public.test:8443/p", {"article"}, client
    )
    assert [r.site for r in results] == ["article"]
    assert seen["url_host"] == "93.184.216.34"
    assert seen["host_header"] == "public.test:8443"
    assert seen["sni"] == "public.test"
    assert str(seen["auth"]).startswith("Basic ")


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


def test_crawl_sites_db_override_beats_env(tmp_path: Path) -> None:
    store = Store(":memory:")
    runtime = Bridge(
        settings(tmp_path, crawl_sites="instagram"),
        store,
        _FakeDevin(),
        _FakeTelegram(),  # type: ignore[arg-type]
    )
    assert runtime.crawl_sites() == {"instagram"}
    store.set_setting("crawl_sites", "article,bogus")
    assert runtime.crawl_sites() == {"article"}
    store.set_setting("crawl_sites", "")
    assert runtime.crawl_sites() == set()
    store.delete_setting("crawl_sites")
    assert runtime.crawl_sites() == {"instagram"}


@pytest.mark.asyncio
async def test_crawl_settings_callback_toggles(tmp_path: Path) -> None:
    store = Store(":memory:")
    telegram = _FakeTelegram()
    runtime = Bridge(
        settings(
            tmp_path,
            telegram_allowed_users="111,555",
            telegram_admin_user_ids="111",
        ),
        store,
        _FakeDevin(),
        telegram,  # type: ignore[arg-type]
    )
    callback = {
        "id": "cfg-1",
        "data": "cfg:crawl:instagram",
        "from": {"id": 111, "is_bot": False},
        "message": {"message_id": 44, "chat": {"id": 222, "type": "private"}},
    }
    await runtime.handle_callback(callback)
    assert runtime.crawl_sites() == {"instagram"}
    assert store.get_setting("crawl_sites") == "instagram"
    await runtime.handle_callback({**callback, "id": "cfg-2"})
    assert runtime.crawl_sites() == set()
    await runtime.handle_callback({**callback, "id": "cfg-3", "data": "cfg:crawl:article"})
    assert runtime.crawl_sites() == {"article"}
    await runtime.handle_callback({**callback, "id": "cfg-4", "data": "cfg:crawl:reset"})
    assert store.get_setting("crawl_sites") is None
    # Non-admin users can view but not change the bridge-wide set.
    outsider = {**callback, "from": {"id": 555, "is_bot": False}}
    await runtime.handle_callback({**outsider, "id": "cfg-5", "data": "cfg:crawl:instagram"})
    assert runtime.crawl_sites() == set()
    assert store.get_setting("crawl_sites") is None
    assert telegram.answers[-1] == "Admins only"
    await runtime.shutdown()


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
