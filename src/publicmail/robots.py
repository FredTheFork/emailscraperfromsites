from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

from protego import Protego

from .http import Fetcher, retry_after_seconds
from .models import FetchError
from .urls import origin, same_site


@dataclass
class Policy:
    parser: Protego
    expires: float
    sitemaps: list[str]
    size: int
    error: FetchError | None = None


class Robots:
    def __init__(self, client: Fetcher):
        self.client = client
        self.policies: OrderedDict[str, Policy] = OrderedDict()
        self.locks: dict[str, asyncio.Lock] = {}
        self.cache_bytes = 0

    def _remember(self, key: str, policy: Policy) -> None:
        previous = self.policies.pop(key, None)
        self.cache_bytes -= previous.size if previous else 0
        self.policies[key] = policy
        self.cache_bytes += policy.size
        # Bound both raw-policy-equivalent bytes and entry overhead across 33k+
        # websites. The parser has additional object overhead, not retained HTML.
        while len(self.policies) > 1024 or self.cache_bytes > 8_000_000:
            _, evicted = self.policies.popitem(last=False)
            self.cache_bytes -= evicted.size

    async def policy(self, url: str) -> Policy:
        key = origin(url)
        current = self.policies.get(key)
        if current and current.expires > time.monotonic():
            self.policies.move_to_end(key)
            return current
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = self.policies.get(key)
            if current and current.expires > time.monotonic():
                self.policies.move_to_end(key)
                return current
            source = ""
            error = None
            ttl = 3600
            try:
                result = await self.client.fetch(
                    key + "/robots.txt", redirect_check=same_site, max_bytes=512_000
                )
                if result.status in {404, 410}:
                    source = "User-agent: *\nAllow: /"
                elif result.status in {401, 403}:
                    error = FetchError(
                        "robots.txt denied access", category="robots_denied", status=result.status
                    )
                elif result.status == 429 or result.status >= 500:
                    pause = retry_after_seconds(result.headers.get("retry-after"))
                    ttl = max(60, pause)
                    error = FetchError(
                        f"robots.txt unavailable (HTTP {result.status}); deferring instead of ignoring robots",
                        category="robots_unavailable",
                        retryable=True,
                        retry_after=pause,
                        status=result.status,
                    )
                elif result.status != 200:
                    error = FetchError(
                        f"robots.txt returned HTTP {result.status}",
                        category="robots_unavailable",
                        status=result.status,
                    )
                else:
                    source = result.text()
            except FetchError as exc:
                ttl = max(60, exc.retry_after)
                error = FetchError(
                    f"Could not establish robots policy: {exc}",
                    category="robots_unavailable",
                    retryable=exc.retryable,
                    retry_after=exc.retry_after,
                    status=exc.status,
                )
            parser = Protego.parse(source)
            agent = self.client.settings.robot_name
            delay = parser.crawl_delay(agent) or 0
            rate = parser.request_rate(agent)
            if rate and rate.requests:
                delay = max(delay, rate.seconds / rate.requests)
            self.client.limiter.set_delay(url, delay)
            policy = Policy(
                parser, time.monotonic() + ttl, list(parser.sitemaps), max(256, len(source.encode())), error
            )
            self._remember(key, policy)
            return policy

    async def ensure_allowed(self, url: str) -> None:
        policy = await self.policy(url)
        if policy.error:
            error = policy.error
            # A cached temporary denial must defer until it expires, rather than
            # consuming another site attempt without making a new robots request.
            raise FetchError(
                str(error),
                category=error.category,
                retryable=error.retryable,
                retry_after=max(error.retry_after, policy.expires - time.monotonic())
                if error.retryable
                else 0,
                status=error.status,
            )
        if not policy.parser.can_fetch(url, self.client.settings.robot_name):
            raise FetchError("Disallowed by robots.txt", category="robots_denied")

    async def sitemaps(self, url: str) -> list[str]:
        policy = await self.policy(url)
        return [] if policy.error else policy.sitemaps
