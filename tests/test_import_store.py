from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from publicmail.exporter import csv_chunks, write_exports
from publicmail.importer import import_file, prepare_row
from publicmail.models import EmailHit, Link
from publicmail.store import relationship


def test_csv_import_deduplicates_and_rejects_private_inputs(store, tmp_path):
    source = tmp_path / "domains.csv"
    source.write_text(
        "\ufeffdomain,url,business_name\nACMEGLAZING.co.uk,https://acmeglazing.co.uk,Acme\nwww.acmeglazing.co.uk,https://www.acmeglazing.co.uk,Duplicate\notherglazing.com,https://otherglazing.com,Other\nlocalhost,http://localhost,Private\n,https://thirdglazing.com,Third\n",
        encoding="utf-8",
    )
    report = import_file(store, source)
    assert (report["total"], report["inserted"], report["duplicates"], report["invalid"]) == (5, 3, 1, 1)
    assert store.stats()["total"] == 3
    assert import_file(store, source)["inserted"] == 0
    assert store.get_meta("mode") == "paused"
    assert json.loads(store.get_site(1)["metadata_json"])["business_name"] == "Acme"


def test_sqlite_input_is_read_only_and_supports_quoted_table_names(store, tmp_path):
    source = tmp_path / "input.db"
    conn = sqlite3.connect(source)
    conn.execute('CREATE TABLE "web""sites" (domain TEXT, url TEXT, business_name TEXT)')
    conn.execute(
        'INSERT INTO "web""sites" VALUES (?,?,?)', ("acmeglazing.co.uk", "https://acmeglazing.co.uk", "Acme")
    )
    conn.commit()
    conn.close()
    before = source.read_bytes()
    assert import_file(store, source, 'web"sites')["inserted"] == 1
    assert source.read_bytes() == before
    with pytest.raises(ValueError, match="does not exist"):
        import_file(store, source, "domains;DROP TABLE sites")
    with pytest.raises(ValueError, match="different"):
        import_file(store, store.path)


def test_mismatched_input_url_cannot_change_identity():
    row = prepare_row({"domain": "acmeglazing.co.uk", "url": "https://unrelated.com/"})
    assert row["input_url"] == "https://acmeglazing.co.uk/"


def test_claim_budget_and_crash_recovery(store, seeded):
    store.control("start", limit=1)
    claimed = store.claim_site("dead-worker", 600, 3)
    assert claimed["attempts"] == 1
    assert store.claim_site("second", 600, 3) is None
    store.enqueue(claimed["id"], [Link(claimed["input_url"], 1000)], 0, "", 20)
    page = store.next_page(claimed["id"])
    store.begin_page(page["id"])
    assert store.recover_running() == 1  # Lock owner can recover even before lease expiry.
    assert store.get_site(claimed["id"])["status"] == "retry"
    assert store.get_site(claimed["id"])["attempts"] == 0
    assert store.next_page(claimed["id"])["status"] == "pending"


def test_page_email_and_frontier_are_one_transaction(store, seeded, monkeypatch):
    store.enqueue(1, [Link("https://acmeglazing.co.uk/", 1000)], 0, "", 250, "seed")
    page = store.next_page(1)
    store.begin_page(page["id"])
    original = store._enqueue

    def fail(*args, **kwargs):
        raise RuntimeError("Simulated crash before commit")

    monkeypatch.setattr(store, "_enqueue", fail)
    with pytest.raises(RuntimeError):
        store.save_page(
            page["id"],
            status="done",
            home=True,
            hits=[EmailHit("info@acmeglazing.co.uk", "mailto")],
            links=[Link("https://acmeglazing.co.uk/contact", 950)],
        )
    assert store.stats()["unique_emails"] == 0
    assert store.site_pages(1)[0]["status"] == "fetching"
    monkeypatch.setattr(store, "_enqueue", original)
    store.save_page(
        page["id"],
        status="done",
        home=True,
        hits=[EmailHit("info@acmeglazing.co.uk", "mailto")],
        links=[Link("https://acmeglazing.co.uk/contact", 950)],
    )
    assert store.stats()["unique_emails"] == 1
    assert store.next_page(1)["url"].endswith("/contact")


def test_deduplication_evidence_relationships_and_exports(store, seeded, tmp_path):
    store.enqueue(1, [Link("https://acmeglazing.co.uk/", 1000)], 0, "", 250)
    page = store.next_page(1)
    hits = [
        EmailHit("info@acmeglazing.co.uk", "mailto"),
        EmailHit("owner@gmail.com", "public_text"),
        EmailHit("web@agency.com", "public_text"),
    ]
    for _ in range(2):
        store.save_page(page["id"], status="done", hits=hits)
    assert store.stats()["unique_emails"] == 3
    assert store.get_site(1)["email_count"] == 3
    assert relationship("x@www.acmeglazing.co.uk", "acmeglazing.co.uk") == "on_domain"
    assert relationship("x@gmail.com", "acmeglazing.co.uk") == "free_provider"
    assert relationship("x@agency.co.uk", "acmeglazing.co.uk") == "external"
    paths = write_exports(store, tmp_path / "exports", jsonl=True)
    assert len(paths) == 8
    with (tmp_path / "exports/emails.csv").open(encoding="utf-8-sig", newline="") as target:
        rows = list(csv.DictReader(target))
    assert len(rows) == 3
    assert {r["email"] for r in rows} == {h.email for h in hits}
    assert all(r["source_url"] == "https://acmeglazing.co.uk/" for r in rows)
    assert (
        len(list(csv.DictReader("".join(csv_chunks(store, "coverage")).lstrip("\ufeff").splitlines()))) == 1
    )


def test_csv_formula_injection_is_escaped_but_jsonl_is_exact(store, tmp_path):
    store.add_sites(
        [prepare_row({"domain": "acmeglazing.co.uk", "business_name": '=HYPERLINK("https://evil.org")'})]
    )
    data = "".join(csv_chunks(store, "coverage"))
    assert "'=HYPERLINK" in data


def test_backup_is_consistent_and_does_not_overwrite_state(store, seeded, tmp_path):
    backup = store.backup(tmp_path / "backup.db")
    conn = sqlite3.connect(backup)
    assert conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 1
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    with pytest.raises(ValueError):
        store.backup(store.path)


def test_settings_and_invalid_filters_are_guarded(store, seeded):
    store.control("start")
    with pytest.raises(ValueError, match="Pause"):
        store.save_settings(store.settings())
    with pytest.raises(ValueError):
        store.list_sites(status="' OR 1=1")
    assert store.list_sites(q="%_")["total"] == 0
