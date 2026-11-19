from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from contextlib import suppress

from filelock import FileLock, Timeout

from .browser import BrowserRenderer
from .config import CrawlSettings
from .extract import analyse_html, text_emails
from .http import Fetcher, retry_after_seconds
from .models import FetchError, Link
from .pdf import PDFExtractor
from .robots import Robots
from .sitemaps import parse_sitemap
from .store import Store
from .urls import (
    acceptable_home_redirect,
    homepage_candidates,
    normalize_url,
    origin,
    same_site,
)

log = logging.getLogger(__name__)


def retry_time(settings: CrawlSettings, attempt: int, retry_after: float = 0) -> float:
    delay = settings.retry_base * (2 ** max(0, attempt - 1))
    # Never shorten the server's Retry-After; jitter only adds time.
    return time.time() + max(delay, retry_after) + random.uniform(0, min(10, delay / 4))


class SiteCrawler:
    def __init__(
        self,
        store: Store,
        client: Fetcher,
        robots: Robots,
        pdf: PDFExtractor,
        renderer: BrowserRenderer | None = None,
    ):
        self.store, self.client, self.robots = store, client, robots
        self.settings, self.pdf, self.renderer = client.settings, pdf, renderer
        self.parse_slots = asyncio.Semaphore(min(4, self.settings.concurrency))

    async def _html(self, text: str, url: str):
        async with self.parse_slots:
            return await asyncio.to_thread(analyse_html, text, url, allow_private=self.client.allow_private)

    async def _home_links(self, url: str) -> list[Link]:
        links = [
            Link(origin(url) + path, score)
            for path, score in [
                ("/contact", 850),
                ("/contact-us", 850),
                ("/about-us", 650),
                ("/privacy-policy", 620),
            ]
        ]
        if self.settings.max_sitemaps:
            supplied = await self.robots.sitemaps(url)
            for value in supplied or [origin(url) + "/sitemap.xml", origin(url) + "/sitemap_index.xml"]:
                try:
                    target = normalize_url(value, url, allow_private=self.client.allow_private)
                except ValueError:
                    continue
                if same_site(target, url):
                    links.append(Link(target, 600, "sitemap"))
        return links

    def _defer(self, site: dict, error: FetchError, reason: str | None = None) -> bool:
        if error.retryable and site["attempts"] < self.settings.max_attempts:
            self.store.finish_site(
                site["id"],
                "retry",
                reason or error.category,
                str(error),
                retry_time(self.settings, site["attempts"], error.retry_after),
            )
            return True
        return False

    async def crawl(self, site: dict) -> None:
        sid, cfg = site["id"], self.settings
        if not site.get("effective_url"):
            roots = homepage_candidates(site["domain"], site["input_url"], cfg.http_fallback)
            self.store.enqueue(
                sid, [Link(url, 1000 - i) for i, url in enumerate(roots)], 0, "", cfg.max_candidates, "seed"
            )
        root_error: FetchError | None = None
        fallback_wanted = False
        rendered = sum(bool(page["browser_status"]) for page in self.store.site_pages(sid))
        reason = "frontier_exhausted"
        while True:
            current = self.store.get_site(sid)
            home = current["effective_url"]
            counts = self.store.page_counts(sid)
            page = self.store.next_page(sid, cfg.max_depth)
            if counts["pages"] >= cfg.max_pages:
                reason = "page_limit"
                page = None
            elif not page and counts["pending"]:
                reason = "depth_limit"
            if page:
                if page["kind"] == "sitemap" and counts["sitemap"] >= cfg.max_sitemaps:
                    self.store.save_page(page["id"], status="skipped", error="sitemap_limit")
                    continue
                if page["kind"] == "pdf" and counts["pdf"] >= cfg.max_pdf_documents:
                    self.store.save_page(page["id"], status="skipped", error="pdf_limit")
                    continue
                is_home = not home and page["role"] == "seed"
                self.store.begin_page(page["id"])
                try:
                    result = await self.client.fetch(
                        page["url"],
                        guard=self.robots.ensure_allowed,
                        redirect_check=acceptable_home_redirect if is_home else same_site,
                    )
                    if result.status in {401, 403}:
                        raise FetchError(
                            f"HTTP {result.status}: access denied; no bypass attempted",
                            category="access_denied",
                            status=result.status,
                        )
                    if result.status == 429 or result.status >= 500:
                        raise FetchError(
                            f"HTTP {result.status}",
                            category="rate_limited" if result.status == 429 else "http_server",
                            status=result.status,
                            retryable=True,
                            retry_after=retry_after_seconds(result.headers.get("retry-after")),
                        )
                    if result.status < 200 or result.status >= 300:
                        raise FetchError(
                            f"HTTP {result.status}", category="http_client", status=result.status
                        )
                except FetchError as exc:
                    # A forbidden path does not prohibit other allowed paths, but
                    # a forbidden homepage or an access challenge stops this site.
                    if exc.category == "robots_denied" and not is_home:
                        self.store.save_page(
                            page["id"], status="skipped", error=str(exc), http_status=exc.status
                        )
                        continue
                    if exc.category in {"access_denied", "robots_denied", "offsite_redirect"}:
                        self.store.save_page(
                            page["id"], status="blocked", error=str(exc), http_status=exc.status
                        )
                        self.store.finish_site(sid, "blocked", exc.category, str(exc))
                        return
                    if (
                        is_home
                        and exc.category in {"network", "tls", "http_client"}
                        or (is_home and exc.category == "robots_unavailable" and exc.status is None)
                    ):
                        self.store.save_page(
                            page["id"], status="error", error=str(exc), http_status=exc.status
                        )
                        if not root_error or exc.retryable:
                            root_error = exc
                        continue  # Only ordinary connectivity/404 failures try www/HTTP.
                    if exc.retryable and (page["kind"] != "sitemap" or exc.status == 429):
                        if site["attempts"] < cfg.max_attempts:
                            self.store.save_page(
                                page["id"], status="pending", error=str(exc), http_status=exc.status
                            )
                            self._defer(site, exc)
                            return
                        if exc.status == 429:
                            self.store.save_page(
                                page["id"], status="blocked", error=str(exc), http_status=429
                            )
                            self.store.finish_site(
                                sid,
                                "blocked",
                                "rate_limited",
                                "Retry budget exhausted; manual review required",
                            )
                            return
                    self.store.save_page(page["id"], status="error", error=str(exc), http_status=exc.status)
                    root_error = root_error or (exc if is_home else None)
                    continue

                base = dict(
                    final_url=result.url,
                    http_status=result.status,
                    content_type=result.content_type,
                    size=len(result.body),
                )
                if page["kind"] == "sitemap":
                    try:
                        links = await asyncio.to_thread(
                            parse_sitemap,
                            result.body,
                            result.url,
                            cfg.sitemap_urls,
                            allow_private=self.client.allow_private,
                        )
                        self.store.save_page(
                            page["id"],
                            status="done",
                            links=links,
                            link_depth=1,
                            cap=cfg.max_candidates,
                            **base,
                        )
                    except Exception as exc:
                        self.store.save_page(
                            page["id"],
                            status="error",
                            error=f"Invalid/unsafe sitemap: {str(exc)[:300]}",
                            **base,
                        )
                    continue

                if result.content_type == "application/pdf" or result.body.startswith(b"%PDF-"):
                    if counts["pdf"] >= cfg.max_pdf_documents:
                        self.store.save_page(
                            page["id"], status="skipped", kind="pdf", error="pdf_limit", **base
                        )
                        continue
                    extracted = await self.pdf.extract(result.body, cfg.pdf_pages)
                    hits = text_emails(extracted["text"], "pdf_text")
                    error = extracted.get("error", "")
                    if extracted.get("truncated"):
                        error = "PDF text/page limit reached (no OCR)"
                    links = await self._home_links(result.url) if is_home else []
                    self.store.save_page(
                        page["id"],
                        status="error" if extracted.get("error") else "done",
                        kind="pdf",
                        hits=hits,
                        error=error,
                        home=is_home,
                        links=links,
                        cap=cfg.max_candidates,
                        **base,
                    )
                    continue

                if result.content_type in {"text/html", "application/xhtml+xml", "text/plain"} or (
                    result.content_type == "application/octet-stream"
                    and b"<html" in result.body[:500].lower()
                ):
                    if result.content_type == "text/plain":
                        from .models import Analysis

                        analysis = Analysis(emails=text_emails(result.text(), "plain_text"))
                    else:
                        analysis = await self._html(result.text(), result.url)
                    if analysis.challenge:
                        self.store.save_page(
                            page["id"],
                            status="blocked",
                            error="Access challenge; no bypass attempted",
                            **base,
                        )
                        self.store.finish_site(
                            sid,
                            "blocked",
                            "access_challenge",
                            "CAPTCHA/browser challenge requires manual review",
                        )
                        return
                    links = analysis.links
                    if is_home:
                        links = sorted(
                            links + await self._home_links(result.url), key=lambda link: -link.priority
                        )
                    self.store.save_page(
                        page["id"],
                        status="done",
                        title=analysis.title,
                        hits=analysis.emails,
                        links=links,
                        home=is_home,
                        link_depth=page["depth"] + 1,
                        cap=cfg.max_candidates,
                        **base,
                    )
                else:
                    self.store.save_page(
                        page["id"], status="skipped", error="Unsupported content type", **base
                    )
                continue

            if not home:
                error = root_error or FetchError("No reachable public homepage", category="unreachable")
                if error.retryable and site["attempts"] < cfg.max_attempts:
                    self.store.reset_seed_errors(sid)
                    self._defer(site, error, "unreachable")
                else:
                    self.store.finish_site(sid, "failed", "unreachable", str(error))
                return
            # Browser is a deliberately small second pass ONLY for sites where
            # HTTP found no email. Once selected, finish up to browser_pages.
            fallback_wanted |= not current["email_count"]
            candidates = (
                self.store.browser_candidates(sid, 1)
                if self.renderer and fallback_wanted and rendered < cfg.browser_pages
                else []
            )
            if candidates:
                candidate = candidates[0]
                try:
                    async with asyncio.timeout(40):
                        analysis = await self.renderer.render(candidate["final_url"] or candidate["url"])
                    if analysis.challenge:
                        self.store.save_render(candidate["id"], (), "Access challenge; no bypass attempted")
                        self.store.finish_site(
                            sid, "blocked", "access_challenge", "Challenge encountered in JavaScript fallback"
                        )
                        return
                    self.store.save_render(
                        candidate["id"],
                        analysis.emails,
                        links=analysis.links,
                        cap=cfg.max_candidates,
                        source_url=analysis.source_url,
                    )
                except FetchError as exc:
                    if self._defer(site, exc):
                        return
                    self.store.save_render(candidate["id"], (), str(exc))
                    if exc.category in {"access_denied", "rate_limited", "robots_denied"}:
                        self.store.finish_site(sid, "blocked", exc.category, str(exc))
                        return
                except Exception as exc:
                    self.store.save_render(
                        candidate["id"],
                        (),
                        f"Browser fallback failed: {type(exc).__name__}: {str(exc)[:200]}",
                    )
                rendered += 1
                continue
            final = self.store.get_site(sid)
            self.store.finish_site(sid, "completed" if final["email_count"] else "no_email", reason)
            return


class Engine:
    def __init__(self, store: Store, *, client_factory=Fetcher):
        self.store, self.client_factory = store, client_factory
        self.owner = uuid.uuid4().hex
        self.stop_event = asyncio.Event()
        self.started = asyncio.Event()
        self.last_heartbeat = 0.0
        self.last_progress = 0.0

    def stop(self) -> None:
        self.stop_event.set()

    def heartbeat(self, settings: CrawlSettings) -> None:
        if time.monotonic() - self.last_heartbeat > 3:
            self.store.heartbeat(self.owner, settings.site_timeout + 60)
            self.last_heartbeat = time.monotonic()
        if time.monotonic() - self.last_progress > 30:
            stats = self.store.stats()
            log.info(
                "Progress: %s/%s terminal | %s sites with email | %s unique addresses | %s active | mode=%s",
                stats["terminal"],
                stats["total"],
                stats["with_email"],
                stats["unique_emails"],
                stats["statuses"]["running"],
                stats["mode"],
            )
            self.last_progress = time.monotonic()

    async def _process(self, crawler: SiteCrawler, site: dict) -> None:
        try:
            async with asyncio.timeout(crawler.settings.site_timeout):
                await crawler.crawl(site)
        except TimeoutError:
            error = FetchError(
                "Per-site wall-clock budget reached; partial evidence saved",
                retryable=True,
                category="site_timeout",
            )
            if not crawler._defer(site, error):
                current = self.store.get_site(site["id"])
                self.store.finish_site(
                    site["id"],
                    "completed" if current["email_count"] else "failed",
                    "site_timeout",
                    str(error),
                )
        except asyncio.CancelledError:
            # Engine.release_owner resets in-flight frontiers after all tasks stop.
            raise
        except Exception as exc:
            log.exception("Unexpected error crawling %s", site["domain"])
            self.store.finish_site(
                site["id"], "failed", "internal_error", f"{type(exc).__name__}: {str(exc)[:1000]}"
            )

    async def _sleep(self, seconds: float = 0.5) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self.stop_event.wait(), seconds)

    async def run(
        self,
        *,
        service: bool = True,
        start: bool = False,
        limit: int | None = None,
        overrides: dict | None = None,
        paused_on_start: bool = False,
    ) -> None:
        lock = FileLock(str(self.store.path) + ".worker.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError(
                "Another worker already owns this database. Use one worker/one server process."
            ) from exc
        tasks: set[asyncio.Task] = set()
        try:
            recovered = self.store.recover_running()
            if paused_on_start:
                self.store.control("pause")
            if start:
                # Only the lock owner may configure/start a CLI run. A second
                # invocation must not alter the active server's mode or budget.
                if overrides:
                    self.store.set_meta("mode", "paused")
                    configured = self.store.settings().model_dump() | overrides
                    self.store.save_settings(CrawlSettings.model_validate(configured))
                self.store.control("start", limit)
            self.store.event(None, "info", f"Worker online; {recovered} interrupted sites recovered")
            self.heartbeat(self.store.settings())
            self.started.set()
            while not self.stop_event.is_set():
                settings = self.store.settings()
                self.heartbeat(settings)
                if self.store.get_meta("mode") != "running":
                    if not service:
                        break
                    await self._sleep()
                    continue
                async with self.client_factory(settings) as client:
                    client.cooldown_store = self.store
                    robots, pdf = Robots(client), PDFExtractor()
                    renderer = BrowserRenderer(client, robots) if settings.browser else None
                    try:
                        if renderer:
                            try:
                                await renderer.check()
                            except Exception as exc:
                                message = f"JavaScript browser is unavailable: {str(exc)[:500]}"
                                self.store.set_meta("worker_error", message)
                                self.store.control("pause")
                                self.store.event(None, "error", message)
                                if not service:
                                    raise RuntimeError(message) from exc
                                continue
                        crawler = SiteCrawler(self.store, client, robots, pdf, renderer)
                        shutdown_at = None
                        while True:
                            self.heartbeat(settings)
                            if self.stop_event.is_set() and shutdown_at is None:
                                shutdown_at = time.monotonic() + 30
                            mode = self.store.get_meta("mode")
                            if mode == "running" and not self.stop_event.is_set():
                                for _ in range(settings.concurrency - len(tasks)):
                                    site = self.store.claim_site(
                                        self.owner, settings.site_timeout + 60, settings.max_attempts
                                    )
                                    if not site:
                                        break
                                    tasks.add(asyncio.create_task(self._process(crawler, site)))
                            if tasks:
                                done, tasks = await asyncio.wait(
                                    tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED
                                )
                                for task in done:
                                    task.result()  # Unexpected storage errors must not disappear silently.
                            else:
                                pending, retry = self.store.queue_state()
                                if mode == "running" and (
                                    self.store.get_meta("run_remaining") == 0 or not pending + retry
                                ):
                                    self.store.control("pause")
                                    self.store.event(
                                        None,
                                        "info",
                                        "Run limit reached" if pending + retry else "Queue drained",
                                    )
                                    mode = "paused"
                                if self.stop_event.is_set() or not service and mode != "running":
                                    break
                                if self.store.settings() != settings:
                                    break
                                await self._sleep()
                            if shutdown_at and time.monotonic() >= shutdown_at:
                                break
                    finally:
                        for task in tasks:
                            task.cancel()
                        if tasks:
                            await asyncio.gather(*tasks, return_exceptions=True)
                        tasks.clear()
                        if renderer:
                            await renderer.close()
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                with suppress(Exception):
                    self.store.set_meta("worker_error", f"{type(exc).__name__}: {str(exc)[:1000]}")
            raise
        finally:
            try:
                self.store.release_owner(self.owner)
            finally:
                lock.release()
