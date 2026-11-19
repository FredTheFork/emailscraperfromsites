import pytest

from publicmail.http import public_address, retry_after_seconds
from publicmail.urls import crawl_link, hostname, normalize_url, same_site, site_key


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://acmeglazing.co.uk/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/",
        "http://internal.local/",
        "https://user:pass@acmeglazing.co.uk/",
        "https://acmeglazing.co.uk:22/",
        "https://acmeglazing.co.uk/\nfoo",
        "javascript:alert(1)",
        "https://2130706433/",
    ],
)
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(ValueError):
        normalize_url(url)


def test_normalization_idn_tracking_and_relative_urls():
    assert (
        normalize_url("HTTPS://BÜCHER.DE:443/contact?utm_campaign=x&page_id=12#form")
        == "https://xn--bcher-kva.de/contact?page_id=12"
    )
    assert (
        normalize_url("../contact/", "https://acmeglazing.co.uk/uk/about/")
        == "https://acmeglazing.co.uk/uk/contact/"
    )
    assert hostname("AcmeGlazing.co.uk.") == "acmeglazing.co.uk"


def test_scope_uses_public_suffix_not_string_suffix():
    assert same_site("https://www.acmeglazing.co.uk", "https://shop.acmeglazing.co.uk")
    assert not same_site("https://acmeglazing.co.uk", "https://other.co.uk")
    assert not same_site("https://acmeglazing.co.uk", "https://acmeglazing.co.uk.evil.com")
    assert site_key("https://a.blogspot.com") != site_key("https://b.blogspot.com")


@pytest.mark.parametrize(
    "link",
    [
        "/wp-admin/",
        "/cart",
        "/feed/",
        "/logo.svg",
        "/search?q=test",
        "/?session=x&s=test",
        "https://outside.org/contact",
        "mailto:info@acmeglazing.co.uk",
    ],
)
def test_unbounded_or_nonpage_links_are_not_crawled(link):
    assert crawl_link(link, "https://acmeglazing.co.uk/") is None


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "192.168.2.3",
        "100.100.100.200",
        "::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
        "168.63.129.16",
    ],
)
def test_nonpublic_dns_answers_are_rejected(ip):
    assert not public_address(ip)


def test_public_dns_and_retry_after():
    assert public_address("1.1.1.1")
    assert retry_after_seconds("120") == 120
    assert retry_after_seconds("-10") == 0
    assert retry_after_seconds("Wed, 18 Nov 2026 12:02:00 GMT", now=1795003200) >= 0
    assert retry_after_seconds("nonsense") == 0
    assert retry_after_seconds("NaN") == 0
