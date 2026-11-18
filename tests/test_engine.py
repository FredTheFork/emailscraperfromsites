from __future__ import annotations

import asyncio
import time

import pytest
from conftest import HOME, FakeFetcher, response

from publicmail.engine import Engine, SiteCrawler
from publicmail.importer import prepare_row
from publicmail.models import FetchError
from publicmail.pdf import PDFExtractor
from publicmail.robots import Robots


def claim(store):
    store.control("start")
    return store.claim_site("test", 600, 3)


async def crawl(store, settings, responses):
    client = FakeFetcher(settings, responses)
    site = claim(store)
    crawler = SiteCrawler(store, client, Robots(client), PDFExtractor())
    await crawler.crawl(site)
    return client


async def test_homepage_contact_sitemap_and_obfuscation(store, seeded, fast_settings):
    client = await crawl(
        store,
        fast_settings,
        {
            HOME: response(HOME, '<a href="/contact">Contact</a><a href="/private">Private</a>'),
            HOME + "robots.txt": response(
                HOME + "robots.txt",
                "User-agent: *\nDisallow: /private\nSitemap: " + HOME + "sitemap.xml",
                content_type="text/plain",
            ),
            HOME + "contact": response(
                HOME + "contact",
                '<p>owner@gmail.com</p><a href="mailto:quotes@acmeglazing.co.uk">Quotes</a><a href="https://outside.org/contact">External</a>',
            ),
            HOME + "sitemap.xml": response(
                HOME + "sitemap.xml",
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'
                + HOME
                + "legal-notice</loc></url></urlset>",
                content_type="application/xml",
            ),
            HOME + "legal-notice": response(
                HOME + "legal-notice", "privacy [at] acmeglazing [dot] co [dot] uk"
            ),
        },
    )
    assert store.get_site(1)["status"] == "completed"
    found = {row["email"] for row in store.list_emails()["items"]}
    assert {"owner@gmail.com", "quotes@acmeglazing.co.uk", "privacy@acmeglazing.co.uk"} <= found
    assert HOME + "private" not in client.calls
    assert not any("outside.org" in url for url in client.calls)
    assert store.stats()["with_email"] == 1
    assert any(p["last_error"] == "Disallowed by robots.txt" for p in store.site_pages(1))


async def test_robots_disallow_home_stops_without_trying_variants(store, seeded, fast_settings):
    client = await crawl(
        store,
        fast_settings,
        {
            HOME + "robots.txt": response(
                HOME + "robots.txt", "User-agent: *\nDisallow: /", content_type="text/plain"
            ),
            HOME: response(HOME, "info@acmeglazing.co.uk"),
        },
    )
    assert store.get_site(1)["status"] == "blocked"
    assert store.get_site(1)["completion_reason"] == "robots_denied"
    assert client.calls == [HOME + "robots.txt"]
    assert not store.stats()["unique_emails"]


@pytest.mark.parametrize("status", [401, 403])
async def test_access_denial_is_not_bypassed(store, seeded, fast_settings, status):
    client = await crawl(store, fast_settings, {HOME: response(HOME, status=status)})
    assert store.get_site(1)["status"] == "blocked"
    assert client.calls == [HOME + "robots.txt", HOME]


async def test_retry_after_is_persisted_and_resume_does_not_lose_emails(store, seeded, fast_settings):
    before = time.time()
    client = await crawl(
        store,
        fast_settings,
        {
            HOME: response(HOME, 'info@acmeglazing.co.uk <a href="/contact">Contact</a>'),
            HOME + "contact": response(HOME + "contact", status=429, headers={"retry-after": "300"}),
        },
    )
    site = store.get_site(1)
    assert site["status"] == "retry"
    assert site["email_count"] == 1
    assert site["next_attempt_at"] >= before + 300
    assert store.claim_site("test", 600, 3) is None
    with store.transaction() as db:
        db.execute("UPDATE sites SET next_attempt_at=0 WHERE id=1")
    site = store.claim_site("test", 600, 3)
    client.responses[HOME + "contact"] = response(HOME + "contact", "quotes@yahoo.com")
    home_calls = client.calls.count(HOME)
    await SiteCrawler(store, client, Robots(client), PDFExtractor()).crawl(site)
    assert client.calls.count(HOME) == home_calls  # Homepage was checkpointed, not refetched.
    assert store.get_site(1)["email_count"] == 2
    assert store.get_site(1)["status"] == "completed"


async def test_retry_budget_is_finite(store, seeded, fast_settings):
    settings = fast_settings.model_copy(update={"max_attempts": 1})
    await crawl(store, settings, {HOME: response(HOME, status=429)})
    assert store.get_site(1)["status"] == "blocked"
    assert store.get_site(1)["completion_reason"] == "rate_limited"


async def test_challenge_stops_even_with_browser_enabled(store, seeded, fast_settings):
    await crawl(
        store, fast_settings, {HOME: response(HOME, "<title>Just a moment...</title><p>help@vendor.com</p>")}
    )
    assert store.get_site(1)["completion_reason"] == "access_challenge"
    assert store.stats()["unique_emails"] == 0


async def test_ordinary_connection_failure_can_use_www(store, seeded, fast_settings):
    client = await crawl(
        store,
        fast_settings,
        {
            HOME: FetchError("Connection failed", retryable=True),
            "https://www.acmeglazing.co.uk/": response(
                "https://www.acmeglazing.co.uk/", "info@acmeglazing.co.uk"
            ),
        },
    )
    assert "https://www.acmeglazing.co.uk/" in client.calls
    assert store.get_site(1)["effective_url"] == "https://www.acmeglazing.co.uk/"
    assert not any(url.startswith("http://") for url in client.calls)


async def test_page_budget_and_deeper_pass_continue_frontier(store, seeded, fast_settings):
    responses = {
        HOME: response(HOME, '<a href="/contact">Contact</a>'),
        HOME + "contact": response(HOME + "contact", "info@acmeglazing.co.uk"),
    }
    await crawl(store, fast_settings.model_copy(update={"max_pages": 1}), responses)
    assert store.get_site(1)["completion_reason"] == "page_limit"
    assert store.get_site(1)["status"] == "no_email"
    store.control("pause")
    assert store.retry_sites(["no_email"]) == 1
    await crawl(store, fast_settings, responses)
    assert store.get_site(1)["status"] == "completed"
    assert store.get_site(1)["email_count"] == 1


async def test_no_email_is_an_honest_terminal_outcome(store, seeded, fast_settings):
    await crawl(store, fast_settings, {HOME: response(HOME, "We use a contact form only.")})
    assert store.get_site(1)["status"] == "no_email"
    assert store.stats()["unique_emails"] == 0


async def test_worker_exclusive_lock_and_start_budget(store, seeded):
    engine = Engine(store, client_factory=FakeFetcher)
    task = asyncio.create_task(engine.run())
    await asyncio.wait_for(engine.started.wait(), 2)
    try:
        with pytest.raises(RuntimeError, match="Another worker"):
            await Engine(store, client_factory=FakeFetcher).run(start=True, limit=100)
        assert store.get_meta("mode") == "paused"  # Rejected process did not change anything.
    finally:
        engine.stop()
        await asyncio.wait_for(task, 2)


async def test_service_pilot_does_not_overclaim(store):
    for index in range(5):
        store.add_sites([prepare_row({"domain": f"glazing-{index}.co.uk"})])

    def factory(settings):
        return FakeFetcher(
            settings,
            {
                f"https://glazing-{i}.co.uk/": response(f"https://glazing-{i}.co.uk/", "No public mailbox")
                for i in range(5)
            },
        )

    await asyncio.wait_for(Engine(store, client_factory=factory).run(service=False, start=True, limit=2), 5)
    assert store.stats()["terminal"] == 2
    assert store.stats()["statuses"]["pending"] == 3
    assert store.get_meta("mode") == "paused"


async def test_cancelled_worker_releases_inflight_page_for_resume(store, seeded):
    fetching = asyncio.Event()
    never = asyncio.Event()

    async def slow(url):
        fetching.set()
        await never.wait()
        return response(url, "")

    engine = Engine(store, client_factory=lambda cfg: FakeFetcher(cfg, {HOME: slow}))
    task = asyncio.create_task(engine.run(start=True))
    await asyncio.wait_for(fetching.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_site(1)["status"] == "retry"
    assert store.next_page(1)["status"] == "pending"
    assert store.get_site(1)["lease_owner"] is None


async def test_site_timeout_retains_partial_evidence(store, seeded, fast_settings):
    never = asyncio.Event()

    async def slow(url):
        await never.wait()

    client = FakeFetcher(
        fast_settings.model_copy(update={"site_timeout": 0.1, "max_attempts": 1}),
        {
            HOME: response(HOME, 'info@acmeglazing.co.uk<a href="/contact">Contact</a>'),
            HOME + "contact": slow,
        },
    )
    engine = Engine(store)
    await engine._process(SiteCrawler(store, client, Robots(client), PDFExtractor()), claim(store))
    assert store.get_site(1)["email_count"] == 1
    assert store.get_site(1)["completion_reason"] == "site_timeout"
    assert store.get_site(1)["status"] == "completed"
