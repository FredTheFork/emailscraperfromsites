"""Opt-in real Chromium smoke tests against intercepted fixtures, never live sites.

Default suite skips this module. CI installs the official Playwright browser and
sets PUBLICMAIL_TEST_BROWSER=1. A sandbox can optionally supply its own test-only
executable via PUBLICMAIL_TEST_CHROMIUM and additional launch arguments.
"""

from __future__ import annotations

import json
import os
import shlex

import pytest
from conftest import HOME, FakeFetcher, response

from publicmail.browser import BrowserRenderer
from publicmail.models import FetchError
from publicmail.robots import Robots

pytestmark = pytest.mark.skipif(
    os.environ.get("PUBLICMAIL_TEST_BROWSER") != "1", reason="Optional real Chromium test"
)


async def renderer_for_test(client):
    renderer = BrowserRenderer(client, Robots(client))
    if executable := os.environ.get("PUBLICMAIL_TEST_CHROMIUM"):
        from playwright.async_api import async_playwright

        renderer.playwright = await async_playwright().start()
        renderer.browser = await renderer.playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
            args=[
                "--disable-background-networking",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                *shlex.split(os.environ.get("PUBLICMAIL_TEST_CHROMIUM_ARGS", "")),
            ],
        )
    else:
        await renderer.check()  # Exercises the real production launch path in CI.
    return renderer


async def test_real_rendered_xhr_and_network_restrictions(fast_settings):
    images = "".join(f'<img src="/image-{i}.png">' for i in range(50))
    source = (
        """<html><title>Public contact</title><body>"""
        + images
        + """
    <script>
    fetch('/public-contact').then(r=>r.json()).then(d=>{
      const a=document.createElement('a'); a.href='mailto:'+d.local+'@'+d.host;
      a.textContent='Email our office'; document.body.appendChild(a);
    });
    fetch('http://169.254.169.254/latest/meta-data').catch(()=>{});
    fetch('https://outside.org/track').catch(()=>{});
    fetch('/must-not-post',{method:'POST',body:'data'}).catch(()=>{});
    </script></body></html>"""
    )
    client = FakeFetcher(
        fast_settings,
        {
            HOME: response(HOME, source),
            HOME + "public-contact": response(
                HOME + "public-contact",
                json.dumps({"local": "rendered", "host": "acmeglazing.co.uk"}),
                content_type="application/json",
            ),
        },
    )
    renderer = await renderer_for_test(client)
    try:
        result = await renderer.render(HOME)
        assert "rendered@acmeglazing.co.uk" in {hit.email for hit in result.emails}
        assert all(hit.method.startswith("rendered_") for hit in result.emails)
        assert HOME + "public-contact" in client.calls
        assert not any(
            "image-" in url or "169.254" in url or "outside.org" in url or "must-not-post" in url
            for url in client.calls
        )
    finally:
        await renderer.close()


async def test_real_rendered_denial_is_not_bypassed(fast_settings):
    client = FakeFetcher(fast_settings, {HOME: response(HOME, status=403)})
    renderer = await renderer_for_test(client)
    try:
        with pytest.raises(FetchError) as caught:
            await renderer.render(HOME)
        assert caught.value.category == "access_denied"
        assert client.calls == [HOME + "robots.txt", HOME]
    finally:
        await renderer.close()


async def test_real_contact_rendering_is_not_suppressed_by_footer_email(store, seeded, fast_settings):
    from publicmail.engine import SiteCrawler
    from publicmail.pdf import PDFExtractor

    contact = (
        """<html><title>Contact us</title><body>"""
        + ("Public office information. " * 30)
        + """
    <script>fetch('/contact-data').then(r=>r.json()).then(d=>{
      const a=document.createElement('a'); a.href='mailto:'+d.local+'@'+d.host;
      a.textContent='Email our office'; document.body.appendChild(a);
    });</script></body></html>"""
    )
    cfg = fast_settings.model_copy(update={"browser": True, "browser_contact_pages": True})
    client = FakeFetcher(
        cfg,
        {
            HOME: response(HOME, '<a href="/contact">Contact</a><footer>design@agency.co.uk</footer>'),
            HOME + "contact": response(HOME + "contact", contact),
            HOME + "contact-data": response(
                HOME + "contact-data",
                json.dumps({"local": "real-rendered-office", "host": "acmeglazing.co.uk"}),
                content_type="application/json",
            ),
        },
    )
    renderer = await renderer_for_test(client)
    try:
        store.control("start")
        site = store.claim_site("test", 600, 3)
        await SiteCrawler(store, client, Robots(client), PDFExtractor(), renderer).crawl(site)
        evidence = {e["email"]: e for e in store.list_emails()["items"]}
        assert {"real-rendered-office@acmeglazing.co.uk", "design@agency.co.uk"} <= evidence.keys()
        assert evidence["real-rendered-office@acmeglazing.co.uk"]["source_url"] == HOME + "contact"
    finally:
        await renderer.close()


async def test_real_js_navigation_is_discovered_before_page_budget_is_spent(store, seeded, fast_settings):
    from publicmail.engine import SiteCrawler
    from publicmail.pdf import PDFExtractor

    shell = (
        """<html><title>Our office</title><body><div id="app"></div><script>
    const a=document.createElement('a'); a.href='/real/office-contact';
    a.textContent='Contact our office'; document.body.appendChild(a);
    </script>"""
        + (" " * 250)
        + "</body></html>"
    )
    # Script text, not total HTML size, drives the shell hint.
    shell = shell.replace("</script>", "/*" + ("public client navigation " * 20) + "*/</script>")
    cfg = fast_settings.model_copy(update={"browser": True, "browser_pages": 1, "max_pages": 2})
    client = FakeFetcher(
        cfg,
        {
            HOME: response(HOME, shell),
            HOME + "real/office-contact": response(HOME + "real/office-contact", "real-navigation@gmail.com"),
        },
    )
    renderer = await renderer_for_test(client)
    try:
        store.control("start")
        site = store.claim_site("test", 600, 3)
        await SiteCrawler(store, client, Robots(client), PDFExtractor(), renderer).crawl(site)
        assert store.get_site(1)["email_count"] == 1
        assert HOME + "real/office-contact" in client.calls
        assert HOME + "contact" not in client.calls
    finally:
        await renderer.close()
