"""Opt-in URL pre-crawlers.

When a chat enables a site via /crawl, incoming messages containing a
matching URL are crawled in the bridge before reaching Devin: the extracted
text is appended to the prompt and media is uploaded as session attachments.
This saves the session from re-discovering crawl logic (and spending tokens)
on every post.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx

_URL_RE = re.compile(r"https?://[^\s<>\"'()\]]+")
_IG_SHORTCODE_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([\w-]+)"
)
_IMG_MAX_BYTES = 10 * 1024 * 1024
_TEXT_MAX_BYTES = 1024 * 1024
# Characters fed to the HTML parser: capped far below _TEXT_MAX_BYTES and
# parsed off-loop with a timeout so a hostile page can't stall the event loop.
_PARSE_MAX_CHARS = 200 * 1024
_PARSE_TIMEOUT = 5.0
_TEXT_MAX_CHARS = 4000
_SKIP_TAGS = {"script", "style", "noscript"}
_MAIN_TAGS = {"article", "main"}
_META_KEYS = {"og:title", "og:description", "og:image", "description"}
_WS_RE = re.compile(r"\s+")
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


async def _resolve_ips(hostname: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    return [info[4][0] for info in infos]


async def _vetted_ip(hostname: str) -> str | None:
    """Resolve a hostname and return an IP only when every address is global."""
    try:
        ips = [ipaddress.ip_address(addr) for addr in await _resolve_ips(hostname)]
    except (OSError, UnicodeError, ValueError):
        return None
    if not ips or not all(ip.is_global for ip in ips):
        return None
    return str(ips[0])


async def _pinned_request(url: str) -> tuple[str, str, str] | None:
    """SSRF guard: return (ip-pinned URL, Host header, SNI name) for a public URL.

    The request connects to the resolved IP while SNI/Host keep the original
    name, so DNS rebinding between the check and the connect cannot redirect
    the fetch to an internal address.
    """
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.hostname
    if host is None:
        return None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            return None
        ip = str(literal)
    else:
        # getaddrinfo also parses the OS-resolver numeric forms
        # (2130706433, 127.1, 0x7f.1), so private spellings of IPv4
        # and names resolving to private addresses are caught here.
        ip = await _vetted_ip(host)
        if ip is None:
            return None
    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    netloc = f"{userinfo}{f'[{ip}]' if ':' in ip else ip}"
    if port is not None:
        netloc += f":{port}"
    default_port = 443 if parsed.scheme == "https" else 80
    host_display = f"[{host}]" if ":" in host else host
    host_header = (
        host_display
        if (port or default_port) == default_port
        else f"{host_display}:{port}"
    )
    return parsed._replace(netloc=netloc).geturl(), host_header, host


async def _fetch(
    url: str,
    client: httpx.AsyncClient,
    max_bytes: int,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response | None:
    """GET with manual redirects (host re-vetted and IP-pinned per hop) + a body cap."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        pinned = await _pinned_request(current)
        if pinned is None:
            return None
        pinned_url, host_header, sni_hostname = pinned
        hop_headers = dict(headers or {})
        hop_headers["Host"] = host_header
        try:
            async with client.stream(
                "GET",
                pinned_url,
                headers=hop_headers,
                params=params,
                follow_redirects=False,
                extensions={"sni_hostname": sni_hostname},
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


class _ArticleParser(HTMLParser):
    """Single-pass (linear) HTML-to-text extractor for ArticleCrawler."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.main_parts: list[str] = []
        self.body_parts: list[str] = []
        self._skip_depth = 0
        self._main_depth = 0
        self._main_done = False
        self._in_title = False
        self._title_done = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _MAIN_TAGS:
            # Only the first article/main block: later blocks would crowd
            # the actual story out of the character cap.
            if not self._main_done:
                self._main_depth += 1
        elif tag == "title" and not self._title_done:
            # First <title> only — body SVG titles are icon labels.
            self._in_title = True
        elif tag == "meta":
            values = dict(attrs)
            key = (values.get("property") or values.get("name") or "").lower()
            if key in _META_KEYS:
                self.meta[key] = values.get("content") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _MAIN_TAGS and self._main_depth:
            self._main_depth -= 1
            if not self._main_depth:
                self._main_done = True
        elif tag == "title":
            self._in_title = False
            self._title_done = True

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        elif self._main_depth and not self._main_done:
            self.main_parts.append(data)
        else:
            self.body_parts.append(data)


def _extract_article(html: str) -> tuple[str, dict[str, str], str]:
    """Parse HTML once, returning (title, meta, body text). Runs off-loop."""
    parser = _ArticleParser()
    parser.feed(html)
    parser.close()
    title = (parser.meta.get("og:title") or " ".join(parser.title_parts)).strip()
    # Prefer the article/main element so nav and banners don't displace
    # the actual content inside the character cap.
    source = parser.main_parts or parser.body_parts
    body = _WS_RE.sub(" ", " ".join(source)).strip()
    return title, parser.meta, body


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
        try:
            title, meta, body = await asyncio.wait_for(
                asyncio.to_thread(_extract_article, response.text[:_PARSE_MAX_CHARS]),
                timeout=_PARSE_TIMEOUT,
            )
        except (TimeoutError, ValueError):
            return None
        description = meta.get("og:description") or meta.get("description") or ""
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
