from __future__ import annotations

import zlib

from defusedxml import ElementTree

from .models import Link
from .urls import crawl_link, link_priority, normalize_url, same_site


def parse_sitemap(body: bytes, url: str, limit: int = 2000, *, allow_private: bool = False) -> list[Link]:
    if body.startswith(b"\x1f\x8b"):
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        body = decoder.decompress(body, 3_000_001)
        if len(body) > 3_000_000 or decoder.unconsumed_tail or not decoder.eof:
            raise ValueError("Compressed sitemap exceeds the size limit")
    root = ElementTree.fromstring(body)
    index = root.tag.rsplit("}", 1)[-1] == "sitemapindex"
    if root.tag.rsplit("}", 1)[-1] not in {"sitemapindex", "urlset"}:
        raise ValueError("Response is not a sitemap")
    links: dict[str, Link] = {}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "loc" or not node.text:
            continue
        try:
            target = normalize_url(node.text.strip(), url, allow_private=allow_private)
        except ValueError:
            continue
        if not same_site(target, url):
            continue
        if index:
            # URL indexes only, never arbitrary resources/archives.
            links[target] = Link(target, 580 + ("page" in target.lower()) * 5, "sitemap")
        elif target := crawl_link(target, url, allow_private=allow_private):
            links[target] = Link(
                target,
                link_priority(target),
                "pdf" if target.split("?", 1)[0].lower().endswith(".pdf") else "html",
            )
        if len(links) >= limit:
            break
    return sorted(links.values(), key=lambda link: -link.priority)
