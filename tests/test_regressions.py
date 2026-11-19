from __future__ import annotations

import csv
import os

import pytest
from conftest import HOME, FakeFetcher, response
from filelock import FileLock

from publicmail.engine import Engine, SiteCrawler
from publicmail.extract import text_emails
from publicmail.importer import import_file, prepare_row
from publicmail.models import Analysis, EmailHit
from publicmail.pdf import PDFExtractor
from publicmail.robots import Robots


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("www.acmeglazing.co.uk", "acmeglazing.co.uk"),
        ("www.com", "www.com"),
        ("www.co.uk", "www.co.uk"),
        ("www.blogspot.com", "www.blogspot.com"),
    ],
)
def test_www_is_only_removed_when_it_is_a_real_same_site_alias(domain, expected):
    assert prepare_row({"domain": domain})["domain"] == expected


def test_hardlink_alias_cannot_be_used_for_input_or_backup(store, tmp_path):
    alias = tmp_path / "alias.db"
    os.link(store.path, alias)
    with pytest.raises(ValueError, match="different"):
        import_file(store, alias)
    with pytest.raises(ValueError, match="working database"):
        store.backup(alias)


def test_partial_csv_import_is_recoverable(store, tmp_path):
    path = tmp_path / "broken.csv"
    content = "domain,business_name\n" + "".join(f"glazing-{i}.co.uk,Company {i}\n" for i in range(550))
    path.write_text(content + 'otherglazing.co.uk,"unterminated\n', encoding="utf-8")
    with pytest.raises(csv.Error):
        import_file(store, path)
    assert store.stats()["total"] == 500  # First transaction, not an all-or-nothing large import.
    path.write_text(content, encoding="utf-8")
    report = import_file(store, path)
    assert report["duplicates"] == 500
    assert report["inserted"] == 50
    assert store.stats()["total"] == 550


async def test_rendered_evidence_uses_actual_page_url_after_same_site_navigation(
    store, seeded, fast_settings
):
    class Renderer:
        async def render(self, url):
            return Analysis(
                emails=[EmailHit("rendered@acmeglazing.co.uk", "rendered_mailto")],
                source_url=HOME + "en/contact",
            )

    cfg = fast_settings.model_copy(update={"browser": True, "browser_pages": 1})
    client = FakeFetcher(cfg, {HOME: response(HOME, "JavaScript-only public contact")})
    store.control("start")
    site = store.claim_site("test", 600, 3)
    await SiteCrawler(store, client, Robots(client), PDFExtractor(), Renderer()).crawl(site)
    assert store.get_site(1)["status"] == "completed"
    assert store.list_emails()["items"][0]["source_url"] == HOME + "en/contact"


def test_slash_and_question_mark_local_parts_are_not_truncated():
    hits = text_emails("Contact first/last?tag@acmeglazing.co.uk for support.")
    assert {hit.email for hit in hits} == {"first/last?tag@acmeglazing.co.uk"}


async def test_rejected_readonly_worker_cannot_pause_the_lock_owner(store):
    store.control("start")
    with FileLock(str(store.path) + ".worker.lock"):
        with pytest.raises(RuntimeError, match="Another worker"):
            await Engine(store).run(paused_on_start=True)
    assert store.get_meta("mode") == "running"
