"""Opt-in URL pre-crawlers.

When a chat enables a site via /crawl, incoming messages containing a
matching URL are crawled in the bridge before reaching Devin: the extracted
text is appended to the prompt and media is uploaded as session attachments.
This saves the session from re-discovering crawl logic (and spending tokens)
on every post.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx

_URL_RE = re.compile(r"https?://[^\s<>\"'()\]]+")
_IG_SHORTCODE_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([\w-]+)"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_MAIN_CONTENT_RE = re.compile(
    r"<(article|main)\b[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE
)
_META_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_META_RE = re.compile(r"<meta\s[^>]*>", re.IGNORECASE)
_META_ATTR_RE = re.compile(r'(\w[\w:-]*)="([^"]*)"')
_IMG_MAX_BYTES = 10 * 1024 * 1024
_TEXT_MAX_BYTES = 1024 * 1024
_TEXT_MAX_CHARS = 4000
_MAX_URLS_PER_MESSAGE = 3
_MAX_REDIRECTS = 5
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class CrawlResult:
    site: str
    url: str
    text: str
    images: list[tuple[str, bytes, str]] = field(default_factory=list)
    """(filename, content, content_type) — caller uploads to Devin."""


class Crawler(Protocol):
    name: str

    def matches(self, url: str) -> bool: ...

    async def fetch(
        self, url: str, client: httpx.AsyncClient
    ) -> CrawlResult | None: ...


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;!?)")
        if url not in urls:
            urls.append(url)
    return urls


def _is_public_url(url: str) -> bool:
    """Reject loopback/private/link-local hosts (SSRF guard).

    Hostnames that *resolve* to private IPs are not caught — the bridge runs
    on the user's own network and only allowlisted senders can enable this.
    """
    host = urlparse(url).hostname
    if host is None:
        return False
    if host.casefold() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


async def _fetch(
    url: str,
    client: httpx.AsyncClient,
    max_bytes: int,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response | None:
    """GET with manual redirects (host-validated per hop) + a body cap."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not _is_public_url(current):
            return None
        try:
            async with client.stream(
                "GET",
                current,
                headers=headers,
                params=params,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        return None
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                return response
        except httpx.HTTPError:
            return None
    return None


async def _download_image(
    url: str, client: httpx.AsyncClient
) -> tuple[str, bytes, str] | None:
    response = await _fetch(url, client, _IMG_MAX_BYTES)
    if response is None:
        return None
    content_type = (
        response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    )
    if not content_type.startswith("image/"):
        return None
    ext = content_type.split("/")[-1].replace("jpeg", "jpg")
    return (f"crawl.{ext}", response.content, content_type)


class InstagramCrawler:
    """Public Instagram posts via the oEmbed endpoint (no login).

    Returns the full caption + the post's cover image. Carousel slides beyond
    the cover need a headless browser, which the bridge intentionally avoids.
    """

    name = "instagram"

    def matches(self, url: str) -> bool:
        return _IG_SHORTCODE_RE.match(url) is not None

    async def fetch(
        self, url: str, client: httpx.AsyncClient
    ) -> CrawlResult | None:
        shortcode = _IG_SHORTCODE_RE.match(url).group(1)
        response = await _fetch(
            "https://www.instagram.com/api/v1/oembed/",
            client,
            _TEXT_MAX_BYTES,
            headers={"User-Agent": _USER_AGENT},
            params={"url": f"https://www.instagram.com/p/{shortcode}/"},
        )
        if response is None:
            return None
        data = response.json()
        if not isinstance(data, dict):
            return None
        parts = [str(data.get("title") or "").strip()]
        author = str(data.get("author_name") or "").strip()
        if author:
            parts.append(f"Author: @{author.lstrip('@')}")
        result = CrawlResult(
            site=self.name,
            url=url,
            text="\n".join(part for part in parts if part),
        )
        thumbnail = data.get("thumbnail_url")
        if isinstance(thumbnail, str):
            image = await _download_image(thumbnail, client)
            if image is not None:
                result.images.append(image)
        return result


class ArticleCrawler:
    """Generic article/page extractor: title, og:description, body text."""

    name = "article"

    def matches(self, url: str) -> bool:
        return url.startswith("http")

    async def fetch(
        self, url: str, client: httpx.AsyncClient
    ) -> CrawlResult | None:
        response = await _fetch(
            url, client, _TEXT_MAX_BYTES, headers={"User-Agent": _USER_AGENT}
        )
        if response is None:
            return None
        if "html" not in response.headers.get("content-type", ""):
            return None
        html = response.text
        meta: dict[str, str] = {}
        for tag in _META_RE.findall(html):
            attrs = dict(_META_ATTR_RE.findall(tag))
            key = attrs.get("property") or attrs.get("name") or ""
            if key.lower() in {
                "og:title",
                "og:description",
                "og:image",
                "description",
            }:
                meta[key.lower()] = unescape(attrs.get("content", ""))
        title = meta.get("og:title") or (
            unescape(_META_TITLE_RE.search(html).group(1).strip())
            if _META_TITLE_RE.search(html)
            else ""
        )
        description = meta.get("og:description") or meta.get("description") or ""
        # Prefer the article/main element so nav and banners don't displace
        # the actual content inside the character cap.
        main = _MAIN_CONTENT_RE.search(html)
        source = main.group(2) if main else html
        body = _TAG_RE.sub(" ", _SCRIPT_STYLE_RE.sub(" ", source))
        body = re.sub(r"\s+", " ", unescape(body)).strip()
        text = "\n\n".join(
            part
            for part in (title, description, body[:_TEXT_MAX_CHARS])
            if part
        )
        result = CrawlResult(site=self.name, url=url, text=text)
        og_image = meta.get("og:image", "")
        if og_image:
            image = await _download_image(urljoin(url, og_image), client)
            if image is not None:
                result.images.append(image)
        return result


CRAWLERS: tuple[Crawler, ...] = (InstagramCrawler(), ArticleCrawler())
CRAWLER_NAMES: tuple[str, ...] = tuple(c.name for c in CRAWLERS)


async def crawl_text(
    text: str,
    enabled: set[str],
    client: httpx.AsyncClient,
) -> list[CrawlResult]:
    """Crawl URLs in a message for every enabled site type.

    The per-message cap counts only URLs an enabled crawler owns, so
    unrelated links never consume the crawl quota.
    """
    results: list[CrawlResult] = []
    for url in extract_urls(text):
        if len(results) >= _MAX_URLS_PER_MESSAGE:
            break
        for crawler in CRAWLERS:
            if crawler.name not in enabled or not crawler.matches(url):
                continue
            try:
                result = await crawler.fetch(url, client)
            except (httpx.HTTPError, ValueError, KeyError, AttributeError):
                result = None
            if result is not None and result.text:
                results.append(result)
            break  # first matching enabled crawler owns the URL
    return results
