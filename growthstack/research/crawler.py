"""Stage 2 "Research them": read a prospect's public website into clean text.

Providers, in order of preference:

* ``Crawl4AIClient`` - our self-hosted Crawl4AI server (``CRAWL4AI_URL``).
* ``FirecrawlClient`` - Firecrawl's hosted API, the backup (``FIRECRAWL_API_KEY``).
* ``BasicFetcher`` - stdlib/httpx fallback for dev and tests, no external service.

House rules: stealth / anti-bot modes stay OFF and robots.txt is always respected
(Crawl4AI checks it server side; for Firecrawl and the basic fetcher we check it
ourselves). Website URLs come from imported CSVs, so every provider's input URL is
checked against private / loopback / link-local targets first (SSRF guard).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from pydantic import SecretStr

from growthstack.config import Settings, get_settings

log = logging.getLogger(__name__)

CRAWL_TIMEOUT = 60.0
ROBOTS_TIMEOUT = 10.0
PAGE_TIMEOUT = 20.0
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_ROBOTS_BYTES = 512 * 1024  # RFC 9309 asks crawlers to parse at least 500 KiB
MAX_REDIRECTS = 5
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"


# --------------------------------------------------------------------------- types


@dataclass
class ResearchResult:
    url: str
    text: str
    provider: str
    title: str | None = None


class CrawlError(Exception):
    """The page could not be read."""


class RobotsDisallowed(CrawlError):
    """robots.txt does not allow us to fetch this page. Never retried elsewhere."""


class UnsafeURL(CrawlError):
    """The URL is not a public http(s) address (SSRF guard). Never retried elsewhere."""


class Crawler(Protocol):
    name: str

    def fetch(self, url: str) -> ResearchResult: ...


def _secret(value: str | SecretStr | None) -> str | None:
    if value is None:
        return None
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    return raw or None


# ------------------------------------------------------------------ URL handling

_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def normalize_url(url_or_domain: str) -> str:
    """Turn ``example.com`` / `` https://example.com/about `` into an absolute
    http(s) URL. Raises CrawlError when the value cannot be a website address."""
    if not isinstance(url_or_domain, str):
        raise CrawlError("URL must be a string")
    value = url_or_domain.strip()
    if not value:
        raise CrawlError("empty URL")
    if any(c.isspace() for c in value):
        raise CrawlError(f"invalid URL: {value!r}")
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        port = parts.port  # raises ValueError on a bad port
    except ValueError as exc:
        raise CrawlError(f"invalid URL: {value!r}") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise CrawlError(f"only http/https URLs are supported, got {scheme!r}")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise CrawlError(f"invalid URL (no host): {value!r}")
    if not _is_ip_literal(host):
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise CrawlError(f"invalid host name: {host!r}") from exc
        labels = ascii_host.split(".")
        if len(labels) < 2 or not all(_HOST_LABEL.match(label) for label in labels):
            raise CrawlError(f"invalid host name: {host!r}")
    if parts.username or parts.password:
        raise CrawlError("URLs with credentials are not supported")
    if port == 0:
        raise CrawlError(f"invalid port in URL: {value!r}")
    return urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:  # embeds an IPv4 address
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return ip.is_global


Resolver = Callable[..., list]


def ensure_public_url(url: str, *, getaddrinfo: Resolver | None = None) -> str:
    """Normalise ``url`` and make sure every address its host resolves to is a
    public one. Raises UnsafeURL otherwise (and CrawlError on an invalid URL)."""
    url = normalize_url(url)
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")
    if _is_ip_literal(host):
        addresses = [host.strip("[]")]
    else:
        resolve = getaddrinfo or socket.getaddrinfo
        try:
            infos = resolve(host, parts.port or (443 if parts.scheme == "https" else 80), 0, socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError) as exc:
            raise CrawlError(f"could not resolve host {host!r}: {exc}") from exc
        addresses = [info[4][0] for info in infos]
        if not addresses:
            raise CrawlError(f"could not resolve host {host!r}")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeURL(f"unexpected address for {host!r}") from exc
        if not _ip_is_public(ip):
            raise UnsafeURL(f"refusing to fetch non-public address for {host!r}")
    return url


# ----------------------------------------------------------------- HTTP helpers


@contextmanager
def _client(http: httpx.Client | None, timeout: float) -> Iterator[httpx.Client]:
    if http is not None:
        yield http
        return
    with httpx.Client(timeout=timeout) as client:
        yield client


@dataclass
class _Fetched:
    url: str
    status_code: int
    content_type: str
    body: bytes
    charset: str | None


def _safe_get(
    http: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    max_bytes: int,
    timeout: float,
) -> _Fetched:
    """GET with redirects followed by hand so that each hop passes the SSRF guard,
    reading at most ``max_bytes`` of the body."""
    current = ensure_public_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        with http.stream("GET", current, headers=headers, timeout=timeout, follow_redirects=False) as resp:
            if resp.is_redirect and "location" in resp.headers:
                current = ensure_public_url(urljoin(current, resp.headers["location"]))
                continue
            body = bytearray()
            for chunk in resp.iter_bytes():
                body.extend(chunk)
                if len(body) >= max_bytes:
                    del body[max_bytes:]
                    break
            return _Fetched(
                url=str(resp.url),
                status_code=resp.status_code,
                content_type=resp.headers.get("content-type", "").split(";", 1)[0].strip().lower(),
                body=bytes(body),
                charset=resp.charset_encoding,
            )
    raise CrawlError(f"too many redirects fetching {url}")


def _decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# ------------------------------------------------------------------- robots.txt


def check_robots(url: str, user_agent: str, http: httpx.Client | None = None) -> None:
    """Raise RobotsDisallowed unless robots.txt lets ``user_agent`` fetch ``url``.

    4xx on robots.txt means "no rules" (allow all); a 5xx or network error means
    we cannot tell, so we stay off the site (RFC 9309 section 2.3.1.4)."""
    url = ensure_public_url(url)
    parts = urlsplit(url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    with _client(http, ROBOTS_TIMEOUT) as client:
        try:
            fetched = _safe_get(
                client,
                robots_url,
                headers={"User-Agent": user_agent},
                max_bytes=MAX_ROBOTS_BYTES,
                timeout=ROBOTS_TIMEOUT,
            )
        except UnsafeURL:
            raise
        except (httpx.HTTPError, CrawlError) as exc:
            raise RobotsDisallowed(f"robots.txt unreachable for {parts.netloc}: {exc}") from exc
    if 400 <= fetched.status_code < 500:
        return
    if not 200 <= fetched.status_code < 300:
        raise RobotsDisallowed(f"robots.txt returned HTTP {fetched.status_code} for {parts.netloc}")
    parser = robotparser.RobotFileParser()
    parser.parse(_decode(fetched.body, fetched.charset).splitlines())
    if not parser.can_fetch(user_agent, url):
        raise RobotsDisallowed(f"robots.txt disallows {url}")


# ------------------------------------------------------------------ HTML -> text

_SKIP_TAGS = {"script", "style", "nav", "footer", "svg", "noscript", "template", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "header", "aside", "ul", "ol", "li", "table",
    "tr", "blockquote", "pre", "hr", "br", "dl", "dt", "dd", "figure", "figcaption", "address",
    "h1", "h2", "h3", "h4", "h5", "h6",
}  # fmt: skip
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.pre_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
            return
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "pre":
            self.pre_depth += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n\n" if tag not in ("br", "li", "tr", "dt", "dd") else "\n")
        if len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("- ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <svg/>, <br/>: no content, so never enters skip mode
        if tag in _SKIP_TAGS or self.skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
            return
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS and self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "pre" and self.pre_depth:
            self.pre_depth -= 1
        if tag in _BLOCK_TAGS and tag not in ("li", "tr", "dt", "dd"):
            self.parts.append("\n\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip_depth:
            return
        # source line breaks are not significant in HTML, except inside <pre>
        self.parts.append(data if self.pre_depth else re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> tuple[str, str | None]:
    """Readable text (headings marked with #, paragraphs separated by a blank
    line) and the page <title>."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # html.parser is lenient; this is belt and braces
        log.debug("html parse error: %s", exc)
    raw = "".join(parser.parts)
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in raw.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # drop heading markers left with no heading text
    text = re.sub(r"^#{1,6}\s*$\n?", "", text, flags=re.MULTILINE).strip()
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip() or None
    return text, title


# -------------------------------------------------------------------- providers


class Crawl4AIClient:
    """Self-hosted Crawl4AI server (``POST /crawl``)."""

    name = "crawl4ai"

    def __init__(self, base_url: str, token: str | SecretStr | None = None, http: httpx.Client | None = None):
        if not base_url:
            raise ValueError("Crawl4AI base_url is required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._http = http

    def _payload(self, url: str) -> dict[str, Any]:
        return {
            "urls": [url],
            "browser_config": {
                "type": "BrowserConfig",
                "params": {"headless": True, "enable_stealth": False},
            },
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {
                    "check_robots_txt": True,
                    "magic": False,
                    # explicit: the server's config.yml turns simulate_user on when absent
                    "simulate_user": False,
                    "override_navigator": False,
                    "cache_mode": "bypass",
                },
            },
        }

    def fetch(self, url: str) -> ResearchResult:
        url = ensure_public_url(url)
        headers = {"Content-Type": "application/json"}
        token = _secret(self._token)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with _client(self._http, CRAWL_TIMEOUT) as client:
            try:
                resp = client.post(
                    f"{self.base_url}/crawl", json=self._payload(url), headers=headers, timeout=CRAWL_TIMEOUT
                )
            except httpx.HTTPError as exc:
                raise CrawlError(f"Crawl4AI request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise CrawlError(f"Crawl4AI returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise CrawlError("Crawl4AI returned invalid JSON") from exc
        results = data.get("results") or []
        if not results:
            raise CrawlError(f"Crawl4AI returned no results (success={data.get('success')})")
        result = results[0] or {}
        error = str(result.get("error_message") or "")
        if not result.get("success") or not data.get("success", True):
            if "robots" in error.lower():
                raise RobotsDisallowed(f"robots.txt disallows {url}")
            raise CrawlError(f"Crawl4AI could not crawl {url}: {error[:300] or 'unknown error'}")
        status = result.get("status_code")
        if isinstance(status, int) and status >= 400:
            raise CrawlError(f"{url} returned HTTP {status}")
        markdown = result.get("markdown")
        if isinstance(markdown, dict):
            text = markdown.get("fit_markdown") or markdown.get("raw_markdown") or ""
        else:
            text = markdown or ""
        text = str(text).strip()
        if not text:
            raise CrawlError(f"Crawl4AI returned no text for {url}")
        # The headless browser follows redirects on its own; make sure it didn't end up
        # somewhere private (e.g. a cloud metadata address) before using the text.
        final_url = result.get("redirected_url") or result.get("url") or url
        if final_url != url:
            try:
                ensure_public_url(final_url)
            except CrawlError as exc:
                raise UnsafeURL(f"{url} redirected to a non-public address") from exc
        metadata = result.get("metadata") or {}
        return ResearchResult(
            url=final_url,
            text=text,
            provider=self.name,
            title=(metadata.get("title") or None) if isinstance(metadata, dict) else None,
        )


class FirecrawlClient:
    """Firecrawl hosted API (backup). Firecrawl does not honour robots.txt for
    single-page scrapes, so we check it ourselves before calling it."""

    name = "firecrawl"

    def __init__(
        self,
        api_key: str | SecretStr,
        http: httpx.Client | None = None,
        *,
        user_agent: str | None = None,
    ):
        if not _secret(api_key):
            raise ValueError("Firecrawl api_key is required")
        self._api_key = api_key
        self._http = http
        self.user_agent = user_agent or get_settings().crawler_user_agent

    def fetch(self, url: str) -> ResearchResult:
        url = ensure_public_url(url)
        with _client(self._http, CRAWL_TIMEOUT) as client:
            check_robots(url, self.user_agent, client)
            try:
                resp = client.post(
                    FIRECRAWL_SCRAPE_URL,
                    json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                    headers={"Authorization": f"Bearer {_secret(self._api_key)}"},
                    timeout=CRAWL_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise CrawlError(f"Firecrawl request failed: {type(exc).__name__}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or not data.get("success", False):
            detail = str(data.get("error") or "")[:300] if isinstance(data, dict) else ""
            raise CrawlError(f"Firecrawl returned HTTP {resp.status_code}: {detail or 'no detail'}")
        payload = data.get("data") or {}
        text = str(payload.get("markdown") or "").strip()
        if not text:
            raise CrawlError(f"Firecrawl returned no text for {url}")
        metadata = payload.get("metadata") or {}
        status = metadata.get("statusCode")
        if isinstance(status, int) and status >= 400:
            raise CrawlError(f"{url} returned HTTP {status}")
        return ResearchResult(
            url=metadata.get("sourceURL") or metadata.get("url") or url,
            text=text,
            provider=self.name,
            title=metadata.get("title") or None,
        )


class BasicFetcher:
    """No-service fallback: robots.txt check, plain GET, HTML -> text. Does not run
    JavaScript, so single-page apps come back thin."""

    name = "basic"

    def __init__(self, user_agent: str, http: httpx.Client | None = None):
        self.user_agent = user_agent
        self._http = http

    def fetch(self, url: str) -> ResearchResult:
        url = ensure_public_url(url)
        with _client(self._http, PAGE_TIMEOUT) as client:
            check_robots(url, self.user_agent, client)
            try:
                fetched = _safe_get(
                    client,
                    url,
                    headers={"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"},
                    max_bytes=MAX_PAGE_BYTES,
                    timeout=PAGE_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise CrawlError(f"fetch failed for {url}: {type(exc).__name__}") from exc
        if fetched.status_code >= 400:
            raise CrawlError(f"{url} returned HTTP {fetched.status_code}")
        if fetched.content_type not in ("text/html", "application/xhtml+xml"):
            raise CrawlError(f"{url} is not an HTML page ({fetched.content_type or 'no content type'})")
        text, title = html_to_text(_decode(fetched.body, fetched.charset))
        if not text:
            raise CrawlError(f"no readable text on {url}")
        return ResearchResult(url=fetched.url, text=text, provider=self.name, title=title)


# ----------------------------------------------------------------------- facade


def get_crawler(settings: Settings | None = None) -> Crawler:
    settings = settings or get_settings()
    if settings.crawl4ai_url:
        return Crawl4AIClient(settings.crawl4ai_url, token=settings.crawl4ai_token)
    if _secret(settings.firecrawl_api_key):
        return FirecrawlClient(settings.firecrawl_api_key, user_agent=settings.crawler_user_agent)  # type: ignore[arg-type]
    return BasicFetcher(settings.crawler_user_agent)


def fetch_site_text(url: str, settings: Settings | None = None) -> ResearchResult:
    """Read ``url`` with the configured provider, falling back from Crawl4AI to
    Firecrawl on ordinary failures. Never falls back when robots.txt says no or
    the URL is unsafe. Text is truncated to ``research_max_chars``."""
    settings = settings or get_settings()
    url = ensure_public_url(url)
    crawler = get_crawler(settings)
    try:
        result = crawler.fetch(url)
    except (RobotsDisallowed, UnsafeURL):
        raise
    except CrawlError as exc:
        if not (isinstance(crawler, Crawl4AIClient) and _secret(settings.firecrawl_api_key)):
            raise
        log.warning("crawl4ai failed, falling back to firecrawl: %s", exc)
        backup = FirecrawlClient(settings.firecrawl_api_key, user_agent=settings.crawler_user_agent)  # type: ignore[arg-type]
        result = backup.fetch(url)
    limit = settings.research_max_chars
    if limit and len(result.text) > limit:
        result.text = result.text[:limit].rstrip()
    log.info("research fetched", extra={"provider": result.provider, "chars": len(result.text)})
    return result
