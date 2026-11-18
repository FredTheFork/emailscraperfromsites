from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import zlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp

from .config import CrawlSettings
from .models import FetchError, FetchResult
from .urls import normalize_url, site_key


class CooldownStore(Protocol):
    def get_cooldown(self, key: str) -> tuple[float, int]: ...
    def put_cooldown(self, key: str, until_at: float, status: int) -> None: ...


Guard = Callable[[str], Awaitable[None]]
RedirectCheck = Callable[[str, str], bool]


def public_address(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return public_address(str(ip.ipv4_mapped))
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
        and str(ip) != "168.63.129.16"
    )


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Filter DNS answers in the actual connector, not in a preflight lookup.

    The connector connects to the checked addresses, preventing a DNS-rebinding
    gap between an application-level check and a second DNS resolution.
    """

    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list:
        records = await self.resolver.resolve(host, port, family)
        if not records or any(not public_address(record["host"]) for record in records):
            raise OSError("DNS resolved to a non-public or reserved network address")
        return records

    async def close(self) -> None:
        await self.resolver.close()


class RateLimiter:
    def __init__(self, delay: float):
        self.default_delay = delay
        self.delays: dict[str, float] = {}
        self.next_at: dict[str, float] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.cooldowns: dict[str, tuple[float, int]] = {}

    def cooldown(self, url: str, seconds: float, status: int) -> None:
        key = site_key(url)
        until = time.monotonic() + seconds
        if until > self.cooldowns.get(key, (0, 0))[0]:
            self.cooldowns[key] = (until, status)
            self.next_at[key] = max(self.next_at.get(key, 0), until)

    def check_cooldown(self, url: str) -> None:
        key = site_key(url)
        until, status = self.cooldowns.get(key, (0, 0))
        remaining = until - time.monotonic()
        if remaining > 0:
            raise FetchError(
                f"Shared website HTTP {status} cooldown; retry later",
                retryable=True,
                retry_after=remaining,
                status=status,
                category="rate_limited" if status == 429 else "http_server",
            )
        self.cooldowns.pop(key, None)

    def set_delay(self, url: str, seconds: float) -> None:
        key = site_key(url)
        previous = self.delays.get(key, self.default_delay)
        self.delays[key] = max(self.default_delay, previous, seconds)
        if key in self.next_at:
            # Newly learned robots delays apply after the robots request too.
            self.next_at[key] += self.delays[key] - previous

    async def wait(self, url: str) -> None:
        key = site_key(url)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            while True:  # noqa: ASYNC110 — deadline pacing, not polling
                self.check_cooldown(url)
                pause = self.next_at.get(key, 0) - time.monotonic()
                if pause <= 0:
                    break
                # A concurrent origin can extend a delay or establish a cooldown.
                await asyncio.sleep(pause)
            self.next_at[key] = time.monotonic() + self.delays.get(key, self.default_delay)


def retry_after_seconds(value: str | None, now: float | None = None) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if seconds < float("inf") else 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0, date.timestamp() - (now if now is not None else datetime.now(UTC).timestamp()))
        except (ValueError, TypeError, OverflowError):
            return 0


class Fetcher:
    def __init__(self, settings: CrawlSettings, *, allow_private: bool = False):
        self.settings = settings
        # This switch exists for loopback test fixtures; it is NOT exposed by any
        # CLI flag, environment variable, import, or web setting.
        self.allow_private = allow_private
        self.limiter = RateLimiter(settings.delay)
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.session: aiohttp.ClientSession | None = None
        self.resolver: PublicResolver | None = None
        self.cooldown_store: CooldownStore | None = None

    async def __aenter__(self) -> Fetcher:
        self.resolver = None if self.allow_private else PublicResolver()
        connector = aiohttp.TCPConnector(
            limit=self.settings.concurrency,
            limit_per_host=2,
            ttl_dns_cache=60,
            resolver=self.resolver,
            enable_cleanup_closed=True,
        )
        self.session = aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(
                total=self.settings.request_timeout, sock_connect=min(10, self.settings.request_timeout)
            ),
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf,text/plain;q=0.9,*/*;q=0.2",
            },
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self.session:
            await self.session.close()
        if self.resolver:
            await self.resolver.close()

    async def _body(self, response: aiohttp.ClientResponse, maximum: int) -> bytes:
        encoding = response.headers.get("Content-Encoding", "").lower().strip()
        if encoding in {"", "identity"}:
            decoder = None
        elif encoding in {"gzip", "x-gzip"}:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decoder = zlib.decompressobj()
        else:
            raise FetchError(f"Unsupported HTTP content encoding: {encoding}", category="encoding")
        pieces, size, wire_size = [], 0, 0
        async for chunk in response.content.iter_chunked(32_768):
            wire_size += len(chunk)
            if wire_size > maximum + 65_536:
                raise FetchError("Response exceeds the download size limit", category="too_large")
            if decoder:
                chunk = decoder.decompress(chunk, maximum + 1 - size)
            size += len(chunk)
            if size > maximum or (decoder and decoder.unconsumed_tail):
                raise FetchError("Response exceeds the decoded size limit", category="too_large")
            pieces.append(chunk)
        if decoder:
            if not decoder.eof:
                raise FetchError("Incomplete compressed response", retryable=True, category="network")
            remaining = decoder.flush(maximum + 1 - size)
            if size + len(remaining) > maximum:
                raise FetchError("Response exceeds the decoded size limit", category="too_large")
            pieces.append(remaining)
        return b"".join(pieces)

    async def fetch(
        self,
        url: str,
        *,
        guard: Guard | None = None,
        redirect_check: RedirectCheck | None = None,
        max_bytes: int | None = None,
    ) -> FetchResult:
        if not self.session:
            raise RuntimeError("Use Fetcher as an async context manager")
        chain: list[str] = []
        for _ in range(7):
            try:
                url = normalize_url(url, allow_private=self.allow_private)
            except ValueError as exc:
                raise FetchError(str(exc), category="unsafe_url") from exc
            if url in chain:
                raise FetchError("Redirect loop", category="redirect")
            if self.cooldown_store:
                seconds, status = self.cooldown_store.get_cooldown(site_key(url))
                if seconds:
                    self.limiter.cooldown(url, seconds, status)
            self.limiter.check_cooldown(url)
            if guard:
                await guard(url)  # Includes robots for EVERY redirect hop.
            chain.append(url)
            try:
                async with self.semaphore:
                    # Pacing happens immediately before the request, inside the
                    # global semaphore, so queued requests cannot bunch up later.
                    await self.limiter.wait(url)
                    async with self.session.get(url, allow_redirects=False, ssl=True) as response:
                        headers = {key.lower(): value for key, value in response.headers.items()}
                        cooldown = retry_after_seconds(headers.get("retry-after"))
                        if response.status == 429 or response.status >= 500 and cooldown:
                            seconds = max(self.settings.retry_base, cooldown)
                            self.limiter.cooldown(url, seconds, response.status)
                            if self.cooldown_store:
                                self.cooldown_store.put_cooldown(
                                    site_key(url), time.time() + seconds, response.status
                                )
                        if response.status in {301, 302, 303, 307, 308} and response.headers.get("Location"):
                            try:
                                target = normalize_url(
                                    response.headers["Location"], url, allow_private=self.allow_private
                                )
                            except ValueError as exc:
                                raise FetchError(str(exc), category="unsafe_redirect") from exc
                            if redirect_check and not redirect_check(url, target):
                                raise FetchError(
                                    "Redirect leaves the permitted website scope", category="offsite_redirect"
                                )
                            url = target
                            continue
                        content_type = response.content_type.lower()
                        maximum = max_bytes or (
                            self.settings.max_pdf_bytes
                            if content_type == "application/pdf"
                            or urlsplit(url).path.lower().endswith(".pdf")
                            else self.settings.max_html_bytes
                        )
                        if response.status >= 400:
                            body = b""  # Error/challenge pages are never mined for emails.
                        else:
                            body = await self._body(response, maximum)
                        return FetchResult(
                            str(response.url),
                            response.status,
                            headers,
                            body,
                            content_type,
                            response.charset or "utf-8",
                            tuple(chain[:-1]),
                        )
            except FetchError:
                raise
            except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError) as exc:
                raise FetchError(
                    "TLS certificate/handshake failed (verification was not disabled)", category="tls"
                ) from exc
            except (aiohttp.ClientError, TimeoutError, OSError, zlib.error) as exc:
                raise FetchError(
                    f"{type(exc).__name__}: {str(exc)[:350]}", category="network", retryable=True
                ) from exc
        raise FetchError("Too many redirects", category="redirect")
