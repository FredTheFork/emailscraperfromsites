from __future__ import annotations

import gzip
import io

import pytest
from defusedxml.common import EntitiesForbidden
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from publicmail.pdf import PDFExtractor
from publicmail.sitemaps import parse_sitemap


def make_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


async def test_pdf_extraction_runs_in_isolated_subprocess():
    result = await PDFExtractor().extract(make_pdf("trade@acmeglazing.co.uk"), 20)
    assert "trade@acmeglazing.co.uk" in result["text"]
    assert not result["error"]


async def test_malformed_pdf_does_not_crash_worker():
    result = await PDFExtractor().extract(b"%PDF-not-really-a-document", 20)
    assert result["error"]
    assert not result["text"]


def test_sitemaps_prioritize_contact_and_do_not_follow_other_sites():
    xml = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://acmeglazing.co.uk/blog/test</loc></url><url><loc>https://acmeglazing.co.uk/contact</loc></url><url><loc>https://outsider.com/contact</loc></url></urlset>'
    links = parse_sitemap(gzip.compress(xml), "https://acmeglazing.co.uk/sitemap.xml")
    assert [link.url for link in links] == [
        "https://acmeglazing.co.uk/contact",
        "https://acmeglazing.co.uk/blog/test",
    ]


def test_sitemap_index_and_entity_bombs():
    links = parse_sitemap(
        b"<sitemapindex><sitemap><loc>https://acmeglazing.co.uk/page-sitemap.xml</loc></sitemap></sitemapindex>",
        "https://acmeglazing.co.uk/sitemap.xml",
    )
    assert links[0].kind == "sitemap"
    with pytest.raises(EntitiesForbidden):
        parse_sitemap(
            b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><urlset><url><loc>&xxe;</loc></url></urlset>',
            "https://acmeglazing.co.uk/sitemap.xml",
        )
    with pytest.raises(ValueError, match="size limit"):
        parse_sitemap(gzip.compress(b"x" * 3_000_001), "https://acmeglazing.co.uk/sitemap.xml.gz")
