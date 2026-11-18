from __future__ import annotations

import asyncio

from .extract import analyse_html
from .http import Fetcher, retry_after_seconds
from .models import Analysis, EmailHit, FetchError
from .robots import Robots
from .urls import normalize_url, same_site


class BrowserRenderer:
    """Optional JS fallback, not a stealth browser or an anti-bot bypass.

    Chromium has a dead outbound proxy and DNS disabled. All permitted GET
    requests are fulfilled by the same DNS-pinned, paced, robots-aware aiohttp
    client. Third-party resources, POSTs, service workers, and sockets are blocked.
    """

    def __init__(self, client: Fetcher, robots: Robots):
        self.client, self.robots = client, robots
        self.semaphore = asyncio.Semaphore(client.settings.browser_concurrency)
        self.start_lock = asyncio.Lock()
        self.playwright = self.browser = None

    async def _start(self) -> None:
        async with self.start_lock:
            if self.browser:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Install requirements-browser.lock and run 'playwright install chromium', "
                    "or build with compose.browser.yaml"
                ) from exc
            self.playwright = await async_playwright().start()
            try:
                self.browser = await self.playwright.chromium.launch(
                    headless=True,
                    proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
                    args=[
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-quic",
                        "--no-pings",
                        "--host-resolver-rules=MAP * ~NOTFOUND",
                    ],
                )
            except BaseException:
                await self.playwright.stop()
                self.playwright = None
                raise

    async def check(self) -> None:
        await self._start()

    async def close(self) -> None:
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.browser = self.playwright = None

    async def render(self, url: str) -> Analysis:
        async with self.semaphore:
            await self._start()
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            context = await self.browser.new_context(
                user_agent=self.client.settings.user_agent,
                service_workers="block",
                accept_downloads=False,
            )
            errors: list[FetchError] = []
            count = 0

            async def intercept(route) -> None:
                nonlocal count
                request = route.request
                try:
                    target = normalize_url(request.url, allow_private=self.client.allow_private)
                    if request.method != "GET" or not same_site(url, target):
                        await route.abort()
                        return
                    if (
                        request.resource_type not in {"document", "script", "xhr", "fetch", "stylesheet"}
                        or count >= 30
                    ):
                        await route.abort()
                        return
                    count += 1  # Blocked images/third parties do not consume the GET budget.
                    result = await self.client.fetch(
                        target,
                        guard=self.robots.ensure_allowed,
                        redirect_check=same_site,
                        max_bytes=2_000_000,
                    )
                    if result.status == 429 or result.status >= 500:
                        raise FetchError(
                            f"Rendered resource HTTP {result.status}",
                            category="rate_limited" if result.status == 429 else "http_server",
                            retryable=True,
                            retry_after=retry_after_seconds(result.headers.get("retry-after")),
                            status=result.status,
                        )
                    if result.status in {401, 403}:
                        raise FetchError(
                            "Rendered resource denied access", category="access_denied", status=result.status
                        )
                    headers = {
                        k: v
                        for k, v in result.headers.items()
                        if k
                        not in {
                            "content-encoding",
                            "content-length",
                            "transfer-encoding",
                            "connection",
                            "set-cookie",
                        }
                    }
                    await route.fulfill(status=result.status, headers=headers, body=result.body)
                except FetchError as exc:
                    errors.append(exc)
                    await route.abort()
                except Exception:
                    await route.abort()

            async def close_socket(socket) -> None:
                await socket.close()

            try:
                page = await context.new_page()
                await context.route("**/*", intercept)
                await page.route_web_socket("**/*", close_socket)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                    # Fixed short sleeps race with our own per-site pacing. Wait
                    # for permitted XHR/script requests, but never indefinitely.
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except PlaywrightTimeoutError:
                        pass
                    await page.wait_for_timeout(750)
                except Exception:
                    if errors:
                        raise errors[0] from None
                    raise
                if errors and any(e.category in {"rate_limited", "access_denied"} for e in errors):
                    raise next(e for e in errors if e.category in {"rate_limited", "access_denied"})
                source = await page.content()
                if len(source.encode("utf-8")) > self.client.settings.max_html_bytes:
                    raise FetchError("Rendered HTML exceeds size limit", category="too_large")
                final_url = normalize_url(page.url, allow_private=self.client.allow_private)
                if not same_site(url, final_url):
                    raise FetchError(
                        "Rendered navigation left the permitted website", category="offsite_redirect"
                    )
                analysis = await asyncio.to_thread(
                    analyse_html, source, final_url, allow_private=self.client.allow_private
                )
                analysis.source_url = final_url
                analysis.emails = [
                    EmailHit(h.email, "rendered_" + h.method, h.context, h.confidence)
                    for h in analysis.emails
                ]
                return analysis
            finally:
                await context.close()
