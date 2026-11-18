from __future__ import annotations

import base64
import binascii
import codecs
import html
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup

from .models import Analysis, EmailHit, Link
from .urls import crawl_link, hostname, link_priority

# Bounded tokens + a left boundary matter: a megabyte-long script/base64
# literal must not trigger quadratic retries of the obfuscation expression.
_LOCAL = r"[A-Z0-9.!#$%&'*+/=?^_`{|}~\-]{1,64}"
_LABEL = r"[A-Z0-9\u0080-\uffff](?:[A-Z0-9\u0080-\uffff-]{0,61}[A-Z0-9\u0080-\uffff])?"
_TLD = r"(?:xn--[A-Z0-9-]{2,59}|[A-Z\u0080-\uffff]{2,63})"
_EMAIL = re.compile(rf"(?<![\w.+-])({_LOCAL}@(?:{_LABEL}\.)+{_TLD})(?![\w-])", re.I)
_LOCAL_VALID = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~\-]+$", re.I)
_AT = r"(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+|\s*@\s*)"
_DOT = r"(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+|\s*\.\s*)"
_OBFUSCATED = re.compile(
    rf"(?<![\w.+-])({_LOCAL})\s*{_AT}\s*([A-Z0-9-]{{1,63}}(?:\s*{_DOT}\s*[A-Z0-9-]{{1,63}}){{1,8}})", re.I
)
_PLACEHOLDER_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "yourdomain.com",
    "your-domain.com",
    "domain.tld",
}
_ASSET_TLDS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "woff", "woff2", "map", "ico"}
_CHALLENGE_TITLE = re.compile(
    r"just a moment|attention required.*cloudflare|checking your browser|access denied|verify (?:that )?you are human|security check",
    re.I,
)
_CONCAT = re.compile(r"(['\"])([^'\"\n]{0,250})\1\s*\+\s*(['\"])([^'\"\n]{0,250})\3")
_REVERSE = re.compile(r"(['\"])([^'\"\n]{3,300})\1\.split\(['\"]['\"]\)\.reverse\(\)\.join\(['\"]['\"]\)")


_ATOB = re.compile(r"\batob\(\s*['\"]([A-Za-z0-9+/=]{4,2048})['\"]\s*\)")
_CHARCODES = re.compile(r"String\.fromCharCode\(\s*((?:\d{1,6}\s*,\s*){1,300}\d{1,6})\s*\)")


def normalize_email(value: str) -> str | None:
    value = html.unescape(unquote(value)).strip(" \t\r\n<>\"'(),;:[]{}")
    if value.lower().startswith("mailto:"):
        value = value[7:].split("?", 1)[0]
    if value.count("@") != 1 or len(value) > 254:
        return None
    local, domain = value.rsplit("@", 1)
    domain = domain.rstrip(".")
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    if not _LOCAL_VALID.fullmatch(local):
        return None
    try:
        domain = hostname(domain)
    except ValueError:
        return None
    if domain in _PLACEHOLDER_DOMAINS or domain.rsplit(".", 1)[-1] in _ASSET_TLDS:
        return None
    if domain.endswith((".sentry.io", ".sentry.wixpress.com")) or domain == "sentry.io":
        return None
    return local.lower() + "@" + domain.lower()


def _context(text: str, start: int, end: int) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - 65) : min(len(text), end + 65)]).strip()[:200]


def text_emails(text: str, method: str = "public_text", confidence: str = "high") -> list[EmailHit]:
    text = html.unescape(text)
    hits: dict[tuple[str, str], EmailHit] = {}
    for match in _EMAIL.finditer(text):
        if email := normalize_email(match.group(1)):
            hits[email, method] = EmailHit(email, method, _context(text, *match.span()), confidence)
    for match in _OBFUSCATED.finditer(text):
        candidate = match.group(1) + "@" + re.sub(_DOT, ".", match.group(2), flags=re.I).replace(" ", "")
        if (email := normalize_email(candidate)) and (email, method) not in hits:
            key = (email, "deobfuscated" if method == "public_text" else f"{method}_deobfuscated")
            hits[key] = EmailHit(email, key[1], _context(text, *match.span()), "medium")
    return list(hits.values())


def decode_cf(value: str) -> str:
    try:
        data = bytes.fromhex(value)
        if len(data) < 2 or len(data) > 400:
            return ""
        return bytes(byte ^ data[0] for byte in data[1:]).decode("utf-8")
    except (ValueError, UnicodeError):
        return ""


def _decode_js(text: str) -> str:
    # Decode literals, never execute arbitrary JavaScript or eval website code.
    text = re.sub(r"\\u([0-9a-f]{4})", lambda m: chr(int(m[1], 16)), text, flags=re.I)
    text = re.sub(r"\\x([0-9a-f]{2})", lambda m: chr(int(m[1], 16)), text, flags=re.I)
    text = re.sub(r"\\[nrt]", " ", text).replace("\\/", "/")
    for _ in range(4):
        combined = _CONCAT.sub(lambda m: json.dumps(m[2] + m[4]), text)
        if combined == text:
            break
        text = combined
    return text


def analyse_html(source: str, base_url: str, *, allow_private: bool = False) -> Analysis:
    soup = BeautifulSoup(source, "html.parser")
    title = soup.title.get_text(" ", strip=True)[:500] if soup.title else ""
    challenge = bool(_CHALLENGE_TITLE.search(title)) or (
        "/cdn-cgi/challenge-platform/" in source and len(soup.get_text(strip=True)) < 3000
    )
    if challenge:
        return Analysis(title=title, challenge=True)
    hits: list[EmailHit] = []
    links: dict[str, Link] = {}
    for node in soup.find_all(True):
        href = str(node.get("href") or "")
        if href.lower().startswith("mailto:"):
            address, _, query = unquote(href[7:]).partition("?")
            values = [address]
            params = parse_qs(query)
            values.extend(params.get("cc", []) + params.get("bcc", []))
            for value in values:
                # Include unusual but valid local parts in explicit mailto links.
                for item in re.split(r"[,;]", value):
                    if email := normalize_email(item):
                        hits.append(EmailHit(email, "mailto", node.get_text(" ", strip=True)[:200]))
        elif node.name == "a" and href:
            if url := crawl_link(href, base_url, allow_private=allow_private):
                label = node.get_text(" ", strip=True)[:200]
                kind = "pdf" if urlsplit(url).path.lower().endswith(".pdf") else "html"
                links[url] = Link(
                    url, max(link_priority(url, label), links.get(url, Link(url, 0)).priority), kind
                )
        encoded = node.get("data-cfemail")
        if not encoded and "/cdn-cgi/l/email-protection#" in href:
            encoded = href.rsplit("#", 1)[-1]
        if encoded and (email := normalize_email(decode_cf(str(encoded)))):
            hits.append(EmailHit(email, "cloudflare_email", "Public email obfuscation decoded"))
        user = node.get("data-user") or node.get("data-username")
        domain = node.get("data-domain") or node.get("data-host")
        if user and domain and (email := normalize_email(str(user) + "@" + str(domain))):
            hits.append(EmailHit(email, "split_attribute", node.get_text(" ", strip=True)[:200], "medium"))
        for attr, value in node.attrs.items():
            if (
                attr in {"data-email", "data-mail", "data-contact", "content", "aria-label", "title"}
                or "email" in attr
            ):
                value = " ".join(value) if isinstance(value, list) else str(value)
                if attr == "data-enc-email":
                    value = codecs.decode(value, "rot_13")
                hits.extend(text_emails(value, "html_attribute", "medium"))
    for script in soup.find_all("script"):
        script_text = _decode_js(script.get_text()[:1_000_000])
        structured = "ld+json" in str(script.get("type", ""))
        hits.extend(
            text_emails(
                script_text,
                "structured_data" if structured else "script_literal",
                "high" if structured else "medium",
            )
        )
        for match in _REVERSE.finditer(script_text):
            hits.extend(text_emails(match[2][::-1], "reversed_literal", "medium"))
        for match in _ATOB.finditer(script_text):
            try:
                decoded = base64.b64decode(match[1], validate=True).decode("utf-8")
                hits.extend(text_emails(decoded, "base64_literal", "medium"))
            except (ValueError, UnicodeError, binascii.Error):
                pass
        for match in _CHARCODES.finditer(script_text):
            try:
                decoded = "".join(chr(int(part.strip())) for part in match[1].split(","))
                hits.extend(text_emails(decoded, "character_codes", "medium"))
            except (ValueError, OverflowError):
                pass
    script_size = sum(len(node.get_text()) for node in soup.find_all("script"))
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    visible = soup.get_text(" ", strip=True)
    hits.extend(text_emails(visible))
    unique = {(hit.email, hit.method): hit for hit in hits}
    return Analysis(
        emails=list(unique.values()),
        links=sorted(links.values(), key=lambda link: -link.priority),
        title=title,
        javascript_hint=len(visible) < 500 and script_size > 200,
        challenge=False,
    )
