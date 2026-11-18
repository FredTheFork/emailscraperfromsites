from __future__ import annotations

import asyncio
import gzip
import time
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from publicmail.http import Fetcher, PublicResolver, RateLimiter
from publicmail.models import FetchError
from publicmail.robots import Robots
from publicmail.urls import same_site


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


async def test_real_http_redirect_guard_for_every_hop_and_gzip(fast_settings):
    seen = []

    async def handler(request):
        seen.append(request.path)
        assert request.headers["User-Agent"] == "PublicMailBot/1.0"
        if request.path == "/start":
            raise web.HTTPFound("/contact")
        return web.Response(
            body=gzip.compress(b"info@acmeglazing.co.uk"),
            headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
        )

    async with server(handler) as base, Fetcher(fast_settings, allow_private=True) as client:
        checked = []

        async def guard(url):
            checked.append(url)

        result = await client.fetch(base + "/start", guard=guard, redirect_check=same_site)
        assert result.body == b"info@acmeglazing.co.uk"
        assert checked == [base + "/start", base + "/contact"]
        assert result.url == base + "/contact"
        assert seen == ["/start", "/contact"]


async def test_robots_is_checked_before_redirect_target(fast_settings):
    seen = []

    async def handler(request):
        seen.append(request.path)
        if request.path == "/robots.txt":
            return web.Response(text="User-agent: *\nDisallow: /private")
        if request.path == "/":
            raise web.HTTPFound("/private")
        return web.Response(text="must not be requested")

    async with server(handler) as base, Fetcher(fast_settings, allow_private=True) as client:
        robots = Robots(client)
        with pytest.raises(FetchError, match="Disallowed"):
            await client.fetch(base + "/", guard=robots.ensure_allowed)
        assert seen == ["/robots.txt", "/"]


async def test_decoded_download_limit_and_timeout(fast_settings):
    async def handler(request):
        if request.path == "/slow":
            await asyncio.sleep(0.15)
            return web.Response(text="late")
        return web.Response(body=gzip.compress(b"x" * 2_000_000), headers={"Content-Encoding": "gzip"})

    config = fast_settings.model_copy(update={"request_timeout": 0.05})
    async with server(handler) as base, Fetcher(config, allow_private=True) as client:
        with pytest.raises(FetchError, match="size limit"):
            await client.fetch(base + "/large", max_bytes=10_000)
        with pytest.raises(FetchError) as caught:
            await client.fetch(base + "/slow")
        assert caught.value.retryable


async def test_production_fetcher_cannot_connect_to_localhost(fast_settings):
    async with Fetcher(fast_settings) as client:
        with pytest.raises(FetchError) as caught:
            await client.fetch("http://127.0.0.1/")
        assert caught.value.category == "unsafe_url"


async def test_dns_filter_is_in_actual_connector_resolver(monkeypatch):
    resolver = PublicResolver()

    async def dangerous(host, port, family):
        return [{"host": "1.1.1.1"}, {"host": "10.0.0.1"}]

    monkeypatch.setattr(resolver.resolver, "resolve", dangerous)
    try:
        with pytest.raises(OSError, match="non-public"):
            await resolver.resolve("glazing.co.uk", 443)
    finally:
        await resolver.close()


async def test_rate_limiter_serializes_apex_and_www_but_not_other_sites():
    limiter = RateLimiter(0.025)
    started = time.monotonic()
    await limiter.wait("https://acmeglazing.co.uk")
    await limiter.wait("https://www.acmeglazing.co.uk")
    assert time.monotonic() - started >= 0.02
    started = time.monotonic()
    await limiter.wait("https://otherglazing.co.uk")
    assert time.monotonic() - started < 0.02
