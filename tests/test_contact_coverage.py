from __future__ import annotations

import pytest
from conftest import HOME, FakeFetcher, response

from publicmail.engine import SiteCrawler
from publicmail.extract import analyse_html, text_emails
from publicmail.models import Analysis, EmailHit, Link
from publicmail.pdf import PDFExtractor
from publicmail.robots import Robots
from publicmail.urls import crawl_link, link_priority


async def test_js_home_navigation_is_rendered_before_guessing_away_page_budget(store, seeded, fast_settings):
    rendered = []

    class Renderer:
        async def render(self, url):
            rendered.append(url)
            return Analysis(links=[Link(HOME + "real/contact-office", 950)], source_url=url)

    shell = '<div id="app"></div><script>' + "var app='client-side-navigation';" * 20 + "</script>"
    assert analyse_html(shell, HOME).javascript_hint
    cfg = fast_settings.model_copy(update={"max_pages": 2, "browser": True, "browser_pages": 1})
    client = FakeFetcher(
        cfg,
        {
            HOME: response(HOME, shell),
            HOME + "real/contact-office": response(HOME + "real/contact-office", "office@acmeglazing.co.uk"),
        },
    )
    store.control("start")
    site = store.claim_site("test", 600, 3)
    await SiteCrawler(store, client, Robots(client), PDFExtractor(), Renderer()).crawl(site)
    assert rendered == [HOME]
    assert HOME + "real/contact-office" in client.calls
    assert HOME + "contact" not in client.calls
    assert store.get_site(1)["email_count"] == 1
    assert store.list_emails()["items"][0]["source_url"] == HOME + "real/contact-office"


async def test_designer_email_does_not_suppress_important_contact_rendering(store, seeded, fast_settings):
    calls = []

    class Renderer:
        async def render(self, url):
            calls.append(url)
            return Analysis(
                emails=[EmailHit("actual-business@gmail.com", "rendered_mailto")]
                if url.endswith("/contact")
                else [],
                source_url=url,
            )

    cfg = fast_settings.model_copy(
        update={"browser": True, "browser_contact_pages": True, "browser_pages": 2}
    )
    client = FakeFetcher(
        cfg,
        {
            HOME: response(HOME, '<a href="/contact">Contact</a> Website by designer@agency.co.uk'),
            HOME + "contact": response(HOME + "contact", "Contact our office. " * 100),
        },
    )
    store.control("start")
    site = store.claim_site("test", 600, 3)
    await SiteCrawler(store, client, Robots(client), PDFExtractor(), Renderer()).crawl(site)
    assert HOME + "contact" in calls
    evidence = store.list_emails()["items"]
    assert {e["email"] for e in evidence} == {"designer@agency.co.uk", "actual-business@gmail.com"}
    assert (
        next(e for e in evidence if e["email"] == "actual-business@gmail.com")["source_url"]
        == HOME + "contact"
    )


async def test_leaf_denial_is_retained_while_independent_public_contact_is_visited(
    store, seeded, fast_settings
):
    client = FakeFetcher(
        fast_settings,
        {
            HOME: response(
                HOME,
                '<a href="/private-contact">Email archive</a><a href="/public-contact">Contact office</a>',
            ),
            HOME + "private-contact": response(HOME + "private-contact", status=403),
            HOME + "public-contact": response(HOME + "public-contact", "public@acmeglazing.co.uk"),
        },
    )
    store.control("start")
    site = store.claim_site("test", 600, 3)
    await SiteCrawler(store, client, Robots(client), PDFExtractor()).crawl(site)
    assert client.calls.index(HOME + "private-contact") < client.calls.index(HOME + "public-contact")
    assert store.get_site(1)["email_count"] == 1
    denied = next(p for p in store.site_pages(1) if p["url"] == HOME + "private-contact")
    assert denied["status"] == "blocked" and denied["http_status"] == 403
    assert client.calls.count(HOME + "private-contact") == 1


async def test_unlinked_common_contact_html_path_is_discovered(store, seeded, fast_settings):
    client = FakeFetcher(
        fast_settings,
        {
            HOME: response(HOME, "Welcome to our business"),
            HOME + "contact.html": response(HOME + "contact.html", "contact@acmeglazing.co.uk"),
        },
    )
    store.control("start")
    site = store.claim_site("test", 600, 3)
    await SiteCrawler(store, client, Robots(client), PDFExtractor()).crawl(site)
    assert store.get_site(1)["email_count"] == 1
    assert HOME + "contact.html" in client.calls


def test_published_noscript_template_and_encoded_attributes_are_not_discarded():
    source = """<noscript>nojs@acmeglazing.co.uk</noscript>
    <template><span>template@acmeglazing.co.uk</span></template>
    <a data-email="encoded%40acmeglazing.co.uk">Email</a>
    <img alt="Email alt@acmeglazing.co.uk" src="/logo.png">
    <p>zero&#8203;@acmeglazing.co.uk</p>"""
    hits = analyse_html(source, HOME).emails
    addresses = {h.email for h in hits}
    assert {
        "nojs@acmeglazing.co.uk",
        "template@acmeglazing.co.uk",
        "encoded@acmeglazing.co.uk",
        "alt@acmeglazing.co.uk",
        "zero@acmeglazing.co.uk",
    } <= addresses
    assert next(h for h in hits if h.email == "nojs@acmeglazing.co.uk").method == "noscript_text"
    assert next(h for h in hits if h.email == "template@acmeglazing.co.uk").method == "template_text"


@pytest.mark.parametrize("joiner", ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"])
def test_nonprinting_joiners_are_removed_not_inferred(joiner):
    assert {h.email for h in text_emails(f"office{joiner}@acmeglazing.co.uk")} == {"office@acmeglazing.co.uk"}
    assert not text_emails("Email us using the form")


def test_joomla_contact_query_is_eligible_and_prioritized_without_allowing_actions():
    href = "/index.php?option=com_contact&view=contact&id=4&Itemid=12"
    url = crawl_link(href, HOME)
    assert url and "com_contact" in url
    assert link_priority(url) >= 900
    assert crawl_link("/index.php?action=delete&id=4", HOME) is None
    assert crawl_link("https://outside.org" + href, HOME) is None


def test_challenge_still_discards_even_noscript_and_encoded_emails():
    page = '<title>Verify you are human</title><noscript>vendor@somewhere.com</noscript><a data-email="sales%40somewhere.com">Email</a>'
    analysis = analyse_html(page, HOME)
    assert analysis.challenge and not analysis.emails
