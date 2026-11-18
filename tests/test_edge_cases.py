from __future__ import annotations

import asyncio
import base64
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import HOME, FakeFetcher, response

from publicmail.engine import Engine, SiteCrawler
from publicmail.extract import analyse_html, normalize_email
from publicmail.http import RateLimiter
from publicmail.models import FetchError
from publicmail.pdf import PDFExtractor
from publicmail.robots import Robots
from publicmail.store import Store
from publicmail.urls import site_key


def test_state_database_identity_check_does_not_modify_input(tmp_path):
    path = tmp_path / "source.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE domains(domain TEXT)")
    connection.execute("INSERT INTO domains VALUES ('acmeglazing.co.uk')")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="input database"):
        Store(path)
    assert path.read_bytes() == before
    assert not (tmp_path / "source.db-wal").exists()


def test_simultaneous_first_time_database_creation_is_serialized(tmp_path):
    path = tmp_path / "shared.db"

    def initialize(_):
        database = Store(path)
        count = database.stats()["total"]
        database.close()
        return count

    with ThreadPoolExecutor(max_workers=5) as executor:
        assert list(executor.map(initialize, range(5))) == [0] * 5
    assert Store(path).path == path


def test_very_long_literals_do_not_trigger_regex_backtracking():
    program = "from publicmail.extract import text_emails; x=text_emails('A'*3_000_000+' info@acmeglazing.co.uk'); assert len(x)==1"
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_additional_public_js_encodings_and_split_attributes():
    encoded = base64.b64encode(b"base@acmeglazing.co.uk").decode()
    codes = ",".join(str(ord(c)) for c in "codes@acmeglazing.co.uk")
    source = f'''<a data-user="split" data-domain="acmeglazing.co.uk">Email</a>
      <script>atob("{encoded}"); String.fromCharCode({codes}); atob("notvalid=");</script>'''
    hits = analyse_html(source, HOME).emails
    assert {"base@acmeglazing.co.uk", "codes@acmeglazing.co.uk", "split@acmeglazing.co.uk"} <= {
        hit.email for hit in hits
    }
    # A long hexadecimal local part can be real; don't reject it just for shape.
    assert normalize_email("abcdef01234567890123456789@gmail.com")


async def test_robots_wildcards_end_anchors_and_fractional_rate(fast_settings):
    text = "User-agent: *\nDisallow: /private/*\nAllow: /private/contact$\nCrawl-delay: 2.5\nRequest-rate: 2/10\n"
    client = FakeFetcher(
        fast_settings, {HOME + "robots.txt": response(HOME + "robots.txt", text, content_type="text/plain")}
    )
    robots = Robots(client)
    await robots.ensure_allowed(HOME + "private/contact")
    with pytest.raises(FetchError, match="Disallowed"):
        await robots.ensure_allowed(HOME + "private/other")
    with pytest.raises(FetchError):
        await robots.ensure_allowed(HOME + "private/contact?other=1")
    assert client.limiter.delays[site_key(HOME)] == 5
    assert client.calls == [HOME + "robots.txt"]


async def test_unavailable_cached_robots_preserves_cooldown(fast_settings):
    client = FakeFetcher(
        fast_settings,
        {HOME + "robots.txt": response(HOME + "robots.txt", status=503, headers={"retry-after": "5"})},
    )
    robots = Robots(client)
    for _ in range(2):
        with pytest.raises(FetchError) as caught:
            await robots.ensure_allowed(HOME)
        assert caught.value.retryable
        assert caught.value.retry_after > 59
    assert client.calls == [HOME + "robots.txt"]


async def test_robots_cache_is_bounded(fast_settings):
    client = FakeFetcher(fast_settings)
    robots = Robots(client)
    for index in range(1100):
        await robots.ensure_allowed(f"https://glazing-{index}.co.uk/")
    assert len(robots.policies) == 1024
    assert robots.cache_bytes <= 8_000_000


async def test_new_robots_delay_extends_already_planned_request():
    limiter = RateLimiter(0.001)
    await limiter.wait(HOME)
    started = time.monotonic()
    limiter.set_delay(HOME, 0.04)
    await limiter.wait(HOME)
    assert time.monotonic() - started >= 0.03


async def test_lowering_max_attempts_cannot_strand_a_retry_queue(store, seeded):
    with store.transaction() as db:
        db.execute("UPDATE sites SET status='retry',attempts=3,next_attempt_at=0")
    await asyncio.wait_for(
        Engine(store, client_factory=FakeFetcher).run(
            service=False, start=True, overrides={"max_attempts": 1}
        ),
        3,
    )
    assert store.get_site(1)["status"] == "failed"
    assert store.get_site(1)["completion_reason"] == "attempt_limit"
    assert store.get_meta("mode") == "paused"


async def test_pdf_and_sitemap_limits_are_explicit_in_trail(store, seeded, fast_settings):
    config = fast_settings.model_copy(update={"max_pdf_documents": 0, "max_sitemaps": 0})
    client = FakeFetcher(config, {HOME: response(HOME, '<a href="/brochure.pdf">Brochure</a>')})
    store.control("start")
    site = store.claim_site("test", 600, 3)
    await SiteCrawler(store, client, Robots(client), PDFExtractor()).crawl(site)
    assert HOME + "brochure.pdf" not in client.calls
    assert any(page["last_error"] == "pdf_limit" for page in store.site_pages(1))
    assert not any("sitemap" in url for url in client.calls)


async def test_transient_root_network_failure_resets_all_homepage_candidates(store, seeded, fast_settings):
    root_urls = [
        HOME,
        "https://www.acmeglazing.co.uk/",
        "http://acmeglazing.co.uk/",
        "http://www.acmeglazing.co.uk/",
    ]
    client = FakeFetcher(fast_settings, {url: FetchError("offline", retryable=True) for url in root_urls})
    store.control("start")
    site = store.claim_site("test", 600, 3)
    await SiteCrawler(store, client, Robots(client), PDFExtractor()).crawl(site)
    assert store.get_site(1)["status"] == "retry"
    assert sum(page["status"] == "pending" for page in store.site_pages(1)) == 4


async def test_browser_failure_pauses_instead_of_failing_the_whole_dataset(store, seeded, monkeypatch):
    async def unavailable(self):
        raise RuntimeError("missing test browser")

    monkeypatch.setattr("publicmail.engine.BrowserRenderer.check", unavailable)
    with pytest.raises(RuntimeError, match="browser is unavailable"):
        await Engine(store, client_factory=FakeFetcher).run(
            service=False, start=True, overrides={"browser": True}
        )
    assert store.get_site(1)["status"] == "pending"
    assert store.get_meta("mode") == "paused"
    assert "missing test browser" in store.get_meta("worker_error")
