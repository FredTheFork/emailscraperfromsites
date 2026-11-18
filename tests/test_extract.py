from __future__ import annotations

import codecs

import pytest

from publicmail.extract import analyse_html, normalize_email, text_emails

BASE = "https://acmeglazing.co.uk/"


def emails(hits):
    return {hit.email for hit in hits}


@pytest.mark.parametrize(
    "candidate, expected",
    [
        (" Sales+Trade@ACMEGLAZING.CO.UK ", "sales+trade@acmeglazing.co.uk"),
        ("mailto:hello%2Bquotes@acmeglazing.co.uk", "hello+quotes@acmeglazing.co.uk"),
        ("o'brien@acmeglazing.co.uk", "o'brien@acmeglazing.co.uk"),
        ("'info@acmeglazing.co.uk'", "info@acmeglazing.co.uk"),
        ("name@bücher.de", "name@xn--bcher-kva.de"),
        ("info@sub.acmeglazing.co.uk", "info@sub.acmeglazing.co.uk"),
        ("contact@gmail.com", "contact@gmail.com"),
        ("trade@yahoo.co.uk", "trade@yahoo.co.uk"),
        ("test@example.com", None),
        ("image@2x.png", None),
        ("bad..dots@acmeglazing.co.uk", None),
        (".bad@acmeglazing.co.uk", None),
        ("ab@localhost", None),
        ("info@127.0.0.1", None),
        ("a" * 65 + "@acmeglazing.co.uk", None),
        ("a" * 32 + "@o123.ingest.sentry.io", None),
    ],
)
def test_email_validation(candidate, expected):
    assert normalize_email(candidate) == expected


def test_visible_mailto_structured_and_external_emails():
    page = """<html><title>Contact us</title><body>
      <a href="mailto:Sales%2BTrade@AcmeGlazing.co.uk?cc=owner%40gmail.com&amp;subject=not-an-email%40subject.org">Email sales</a>
      <p>Office: info&#64;acmeglazing.co.uk and owner@yahoo.com</p>
      <footer>Website by design@agency.co.uk</footer>
      <script type="application/ld+json">{"email":"quotes@acmeglazing.co.uk"}</script>
      <a href="/contact#form">Contact</a><a href="/privacy-policy?utm_source=x">Privacy</a>
      <a href="https://facebook.com/acme">Social</a><a href="/images/logo.png">Logo</a>
    </body></html>"""
    result = analyse_html(page, BASE)
    assert {
        "sales+trade@acmeglazing.co.uk",
        "owner@gmail.com",
        "info@acmeglazing.co.uk",
        "owner@yahoo.com",
        "design@agency.co.uk",
        "quotes@acmeglazing.co.uk",
    } <= emails(result.emails)
    assert "not-an-email@subject.org" not in emails(result.emails)
    assert [link.url for link in result.links] == [BASE + "contact", BASE + "privacy-policy"]
    assert result.title == "Contact us"


@pytest.mark.parametrize(
    "text",
    [
        "info [at] acmeglazing [dot] co [dot] uk",
        "info (at) acmeglazing (dot) co (dot) uk",
        "info at acmeglazing dot co dot uk",
        "info @ acmeglazing . co . uk",
        "info{at}acmeglazing{dot}co{dot}uk",
    ],
)
def test_obfuscated_text(text):
    assert "info@acmeglazing.co.uk" in emails(text_emails(text))


def test_public_email_obfuscation_and_js_literals():
    email = "cf@acmeglazing.co.uk"
    key = 0x42
    encoded = bytes([key, *(ord(c) ^ key for c in email)]).hex()
    rot13 = codecs.encode("rot[at]acmeglazing.co.uk", "rot13")
    source = f'''<span data-cfemail="{encoded}">protected</span>
      <a data-enc-email="{rot13}">Email</a>
      <script>var x = 'literal@acmeglazing.co.uk';
      var y = 'joined' + '@' + 'acmeglazing.co.uk';
      var z = "escaped\\u0040acmeglazing.co.uk";
      var rev = "ku.oc.gnizalgemca@desrever".split("").reverse().join("");</script>'''
    found = emails(analyse_html(source, BASE).emails)
    assert {
        email,
        "rot@acmeglazing.co.uk",
        "literal@acmeglazing.co.uk",
        "joined@acmeglazing.co.uk",
        "escaped@acmeglazing.co.uk",
        "reversed@acmeglazing.co.uk",
    } <= found
    assert not any(address.startswith("'") for address in found)


def test_challenge_is_not_an_invitation_to_use_browser():
    result = analyse_html(
        "<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/foo'></script><p>help@vendor.com</p>",
        BASE,
    )
    assert result.challenge
    assert not result.emails


def test_normal_contact_form_recaptcha_is_not_a_challenge():
    assert not analyse_html(
        "<title>Contact</title><div class='g-recaptcha'></div><p>info@acmeglazing.co.uk</p>", BASE
    ).challenge


def test_repeated_addresses_are_deduplicated_per_method():
    assert (
        len(
            [
                h
                for h in text_emails("info@acmeglazing.co.uk info@acmeglazing.co.uk")
                if h.email == "info@acmeglazing.co.uk"
            ]
        )
        == 1
    )
