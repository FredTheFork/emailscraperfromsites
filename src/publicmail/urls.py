from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import tldextract

# Bundled suffix list only: starting a crawl never requires a PSL download.
_SUFFIX = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TRACKING = {"gclid", "fbclid", "msclkid", "ref", "referrer", "session", "sid"}
_QUERY_KEYS = {"page_id", "p", "id", "lang", "language", "page", "route", "option", "view", "itemid", "catid"}
_ASSET = re.compile(
    r"\.(?:jpe?g|png|gif|webp|avif|svg|ico|css|js|map|woff2?|ttf|eot|mp[34]|mov|avi|webm|zip|rar|7z|exe|dmg|rss|xml)$",
    re.I,
)
_SKIP_PATH = re.compile(
    r"(?:^|/)(?:wp-admin|wp-json|wp-login\.php|xmlrpc\.php|cart|checkout|basket|logout|login|signin|sign-in|register|search|feed|calendar|tag|author)(?:/|\.|$)",
    re.I,
)
_DIRECTORY_HOSTS = {
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "youtube.com",
    "google.com",
    "google.co.uk",
    "yell.com",
    "checkatrade.com",
    "trustatrader.com",
}


def hostname(value: str, *, public_only: bool = True) -> str:
    host = value.strip().rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address:
        if public_only:
            raise ValueError("Use a public website domain, not an IP address")
        return str(address)
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid domain encoding") from exc
    labels = host.split(".")
    if len(host) > 253 or any(not _LABEL.fullmatch(label) for label in labels):
        raise ValueError("Invalid hostname")
    if public_only and (
        len(labels) < 2
        or len(labels[-1]) < 2
        or labels[-1].isdigit()
        or labels[-1] in {"local", "localhost", "internal", "test", "invalid", "example", "onion"}
    ):
        raise ValueError("Not a public website hostname")
    return host


def normalize_url(value: str, base: str | None = None, *, allow_private: bool = False) -> str:
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Empty, overlong, or malformed URL")
    value = urljoin(base, value) if base else value
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Only HTTP and HTTPS URLs are permitted")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs containing credentials are not permitted")
    host = hostname(parts.hostname, public_only=not allow_private)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid port") from exc
    if not allow_private and port not in {None, 80, 443}:
        raise ValueError("Only public HTTP/HTTPS ports are permitted")
    if ":" in host:
        host = f"[{host}]"
    # Do not change an explicit cross-scheme port, e.g. https://site:80/.
    netloc = host
    if port and (parts.scheme.lower(), port) not in {("https", 443), ("http", 80)}:
        netloc += f":{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = urlencode(
        [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING and not key.lower().startswith("utm_")
        ]
    )
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def site_key(url_or_host: str) -> str:
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").lower().strip(".")
    result = _SUFFIX(host)
    return result.top_domain_under_public_suffix or host.removeprefix("www.")


def same_site(a: str, b: str) -> bool:
    return site_key(a) == site_key(b)


def origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def crawl_link(href: str, base: str, *, allow_private: bool = False) -> str | None:
    try:
        url = normalize_url(href, base, allow_private=allow_private)
    except ValueError:
        return None
    if not same_site(url, base):
        return None
    parts = urlsplit(url)
    if _ASSET.search(parts.path) or _SKIP_PATH.search(parts.path):
        return None
    if parts.query:
        query = parse_qsl(parts.query)
        if any(k.lower() not in _QUERY_KEYS for k, _ in query) or len(query) > 5:
            return None
    return url


def link_priority(url: str, label: str = "") -> int:
    parts = urlsplit(url)
    text = (parts.path + " " + parts.query + " " + label).lower()
    if re.search(r"contact|enquir|e-mail|email|reach.us|get.in.touch", text):
        return 950
    if re.search(r"about|our.team|meet.the|impressum", text):
        return 800
    if re.search(r"privacy|legal|terms|cookie|accessibility", text):
        return 700
    if re.search(r"support|help|location|branch|showroom", text):
        return 750
    if re.search(r"\.pdf(?:$|\?)", url.lower()):
        return 450
    if re.search(r"blog|news|product|gallery|portfolio|project|archive", text):
        return 80
    return 200


def homepage_candidates(domain: str, input_url: str, http_fallback: bool = True) -> list[str]:
    candidates = [input_url]
    hosts = [domain]
    if not domain.startswith("www."):
        hosts.append("www." + domain)
    candidates.extend(f"https://{host}/" for host in hosts)
    if http_fallback:
        candidates.extend(f"http://{host}/" for host in hosts)
    return list(dict.fromkeys(candidates))


def acceptable_home_redirect(source: str, target: str) -> bool:
    # A rename is allowed, but a redirect to a directory/social network is not an
    # invitation to crawl that entire platform.
    return same_site(source, target) or site_key(target) not in _DIRECTORY_HOSTS
