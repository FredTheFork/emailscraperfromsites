"""Full-queue coverage tests use synthetic sites, NEVER live-site yield."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
import zipfile

import pytest
from conftest import HOME, FakeFetcher, response

from publicmail.colab import Workspace, parser, run_session
from publicmail.coverage import (
    PLAN_KEY,
    automatic_profiles,
    collection_report,
    ensure_plan,
    finish_stage,
    prepare_stage,
)
from publicmail.engine import Engine
from publicmail.importer import prepare_row
from publicmail.models import Analysis, EmailHit, Link
from publicmail.store import Store


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace(tmp_path / "local", tmp_path / "drive")
    ws.prepare()
    return ws


def add_domains(ws, *domains):
    with ws.open_store() as store:
        store.add_sites(prepare_row({"domain": d}) for d in domains)


def factory_for(responses, calls=None, stages=None):
    def factory(store):
        if stages is not None:
            stages.append(store.get_meta(PLAN_KEY)["stage_index"])

        def client(cfg):
            result = FakeFetcher(cfg, responses)
            if calls is not None:
                result.calls = calls
            return result

        return Engine(store, client_factory=client)

    return factory


async def save(ws, seconds):
    return ws.checkpoint()


async def export(ws, seconds):
    return ws.export()


def deep_fixture():
    # The address is genuinely beyond the FIRST pass depth, not hidden in its
    # HTML. Increasing defaults must happen without a user changing options.
    pages = {HOME: response(HOME, '<a href="/level1">Public information</a>')}
    for i in range(1, 4):
        pages[HOME + f"level{i}"] = response(HOME + f"level{i}", f'<a href="/level{i + 1}">Continue</a>')
    pages[HOME + "level4"] = response(HOME + "level4", "deep-contact@yahoo.co.uk")
    return pages


def test_defaults_are_full_automatic_unlimited_session_and_browser_enabled():
    args = parser().parse_args(["--work-dir", "/tmp/test-work", "run"])
    assert args.max_sites == 0
    assert args.hours == 0
    assert args.browser and not args.single_pass
    assert args.max_pages is None and args.max_depth is None  # Not overriding deeper defaults.
    first, deeper, recovery = automatic_profiles()
    assert first["concurrency"] == 12 and first["delay"] >= 1.5
    assert first["browser"] and first["browser_contact_pages"]
    assert deeper["max_pages"] > first["max_pages"]
    assert deeper["max_depth"] > first["max_depth"]
    assert deeper["browser_pages"] > first["browser_pages"]
    assert recovery["max_attempts"] == 2


async def test_default_runner_attempts_more_than_100_and_rerun_is_not_a_recrawl(workspace, monkeypatch):
    class FixtureRenderer:
        def __init__(self, *args):
            pass

        async def check(self):
            pass

        async def close(self):
            pass

        async def render(self, url):
            return Analysis(source_url=url)

    # Exercise DEFAULT browser-on settings without any browser/network access.
    monkeypatch.setattr("publicmail.engine.BrowserRenderer", FixtureRenderer)
    domains = [f"fixture-{i}.co.uk" for i in range(257)]
    add_domains(workspace, *domains)
    pages = {
        f"https://{d}/": response(f"https://{d}/", f"fixture-{i}@gmail.com") for i, d in enumerate(domains)
    }
    calls = []
    factory = factory_for(pages, calls)
    result = await run_session(workspace, engine_factory=factory, checkpointer=save, exporter=export)
    assert result["reason"] == "automatic_complete"
    assert result["stats"]["sites_started"] == 257
    assert result["stats"]["sites_unstarted"] == 0
    assert result["stats"]["unique_emails"] == 257
    assert result["collection"]["completion"]["queue_remaining"] == 0
    stages = result["collection"]["automatic_plan"]["stages"]
    assert stages[0]["selected"] == 257
    assert all(s["state"] == "finished" for s in stages)
    with zipfile.ZipFile(result["export_zip"]) as zipped:
        coverage = list(csv.DictReader(io.StringIO(zipped.read("coverage.csv").decode("utf-8-sig"))))
        assert len(coverage) == 257 and all(r["started_at"] for r in coverage)
        assert b"Websites never started: 0" in zipped.read("READ_ME_FIRST.txt")
        assert json.loads(zipped.read("run_report.json"))["completion"]["all_sites_attempted"]
    before = len(calls)
    again = await run_session(workspace, engine_factory=factory, checkpointer=save, exporter=export)
    assert again["reason"] == "automatic_complete"
    assert len(calls) == before  # A finished Run all does not silently repeat all passes.


async def test_deeper_pass_increases_coverage_without_manual_requeue(workspace):
    add_domains(workspace, "acmeglazing.co.uk")
    result = await run_session(
        workspace,
        settings={"browser": False},
        engine_factory=factory_for(deep_fixture()),
        checkpointer=save,
        exporter=export,
    )
    plan = result["collection"]["automatic_plan"]
    assert plan["stages"][0]["unique_emails"] == 0
    assert plan["stages"][1]["unique_emails"] == 1
    assert result["stats"]["unique_emails"] == 1
    with workspace.open_store() as store:
        evidence = store.list_emails()["items"]
        assert evidence[0]["email"] == "deep-contact@yahoo.co.uk"
        assert evidence[0]["source_url"] == HOME + "level4"


async def test_pilot_cap_is_reported_as_partial_not_finished(workspace):
    add_domains(workspace, "acmeglazing.co.uk", "secondglazing.co.uk")
    result = await run_session(
        workspace,
        max_sites=1,
        settings={"browser": False},
        engine_factory=factory_for({HOME: response(HOME, "one@gmail.com")}),
        checkpointer=save,
        exporter=export,
    )
    assert result["reason"] == "claim_limit"
    assert result["collection"]["state"] == "PILOT_ONLY"
    assert result["stats"]["sites_started"] == 1 and result["stats"]["sites_unstarted"] == 1
    with zipfile.ZipFile(result["export_zip"]) as zipped:
        text = zipped.read("READ_ME_FIRST.txt").decode()
        assert "NOT A FINISHED FULL-LIST RESULT" in text
        assert "Websites never started: 1" in text


async def test_saved_old_pilot_queue_is_automatically_drained(workspace):
    add_domains(workspace, "acmeglazing.co.uk", "secondglazing.co.uk")
    with workspace.open_store() as store:
        store.control("start", 100)
        old = store.claim_site("old-pilot", 600, 3)
        store.enqueue(old["id"], [Link(HOME, 1000)], 0, "", 400, "seed")
        store.save_page(
            store.next_page(old["id"])["id"],
            status="done",
            home=True,
            hits=[EmailHit("saved@gmail.com", "public_text")],
        )
        store.finish_site(old["id"], "completed", "frontier_exhausted")
        store.control("pause")  # Simulates the PRE-v2 notebook, no plan/mode metadata.
    result = await run_session(
        workspace,
        settings={"browser": False},
        engine_factory=factory_for(
            {"https://secondglazing.co.uk/": response("https://secondglazing.co.uk/", "new@yahoo.com")}
        ),
        checkpointer=save,
        exporter=export,
    )
    assert result["stats"]["sites_unstarted"] == 0
    assert result["stats"]["unique_emails"] == 2
    assert result["collection"]["completion"]["automatic_passes_finished"]


async def test_transition_checkpoint_failure_resumes_the_saved_stage_on_new_runtime(workspace, tmp_path):
    add_domains(workspace, "acmeglazing.co.uk")
    stages = []
    fail_once = True

    async def crash_between_stages(ws, seconds):
        nonlocal fail_once
        with ws.open_store() as store:
            plan = store.get_meta(PLAN_KEY)
        if fail_once and plan["stage_index"] == 1:
            fail_once = False
            raise RuntimeError("Simulated reset at a stage boundary")
        return ws.checkpoint()

    with pytest.raises(RuntimeError, match="stage boundary"):
        await run_session(
            workspace,
            settings={"browser": False},
            engine_factory=factory_for(deep_fixture(), stages=stages),
            checkpointer=crash_between_stages,
            exporter=export,
        )
    assert stages == [0]
    new = Workspace(tmp_path / "new-runtime", workspace.drive_dir)
    restored = new.prepare()
    assert restored["restored"]
    with new.open_store() as store:
        plan = store.get_meta(PLAN_KEY)
        assert plan["stage_index"] == 1 and plan["prepared"]
        assert collection_report(store)["state"] == "INCOMPLETE"
    result = await run_session(
        new,
        settings={"browser": False},
        engine_factory=factory_for(deep_fixture(), stages=stages),
        checkpointer=save,
        exporter=export,
    )
    assert stages == [0, 1, 2]
    assert result["stats"]["unique_emails"] == 1
    assert result["collection"]["completion"]["automatic_passes_finished"]


@pytest.mark.parametrize("restriction", [401, 403, "challenge", "robots"])
async def test_automatic_passes_never_requeue_access_restrictions(workspace, restriction):
    add_domains(workspace, "acmeglazing.co.uk")
    pages = {
        HOME: response(
            HOME, "do-not-collect@gmail.com", status=restriction if isinstance(restriction, int) else 200
        )
    }
    if restriction == "challenge":
        pages[HOME] = response(HOME, "<title>Just a moment...</title>vendor-help@gmail.com")
    if restriction == "robots":
        pages[HOME + "robots.txt"] = response(
            HOME + "robots.txt", "User-agent: *\nDisallow: /", content_type="text/plain"
        )
    calls = []
    result = await run_session(
        workspace,
        settings={"browser": False},
        engine_factory=factory_for(pages, calls),
        checkpointer=save,
        exporter=export,
    )
    assert result["collection"]["state"] == "COMPLETE_WITH_GAPS"
    assert result["stats"]["sites_started"] == 1
    assert result["stats"]["statuses"]["blocked"] == 1
    assert result["stats"]["unique_emails"] == 0
    assert calls.count(HOME) == (0 if restriction == "robots" else 1)
    assert not any("www." in url or url.startswith("http:") for url in calls)
    assert len(calls) == (1 if restriction == "robots" else 2)


async def test_transient_recovery_checks_failed_sites_once_more_not_forever(workspace):
    add_domains(workspace, "acmeglazing.co.uk")
    result = await run_session(
        workspace,
        settings={"browser": False},
        engine_factory=factory_for(
            {HOME: [response(HOME, status=404), response(HOME, "recovered@outlook.com")]}
        ),
        checkpointer=save,
        exporter=export,
    )
    stages = result["collection"]["automatic_plan"]["stages"]
    assert stages[0]["unique_emails"] == 0
    assert stages[1]["selected"] == 0
    assert stages[2]["selected"] == 1
    assert result["stats"]["unique_emails"] == 1


async def test_no_public_email_finishes_with_truthful_gaps_and_no_fabrication(workspace):
    add_domains(workspace, "acmeglazing.co.uk")
    result = await run_session(
        workspace,
        settings={"browser": False},
        engine_factory=factory_for({HOME: response(HOME, "Contact form only.")}),
        checkpointer=save,
        exporter=export,
    )
    assert result["stats"]["unique_emails"] == 0
    assert result["stats"]["statuses"]["no_email"] == 1
    assert result["collection"]["state"] == "COMPLETE_WITH_GAPS"
    assert result["collection"]["completion"]["all_sites_attempted"]
    with zipfile.ZipFile(result["export_zip"]) as zipped:
        assert len(zipped.read("unique_emails.csv").decode("utf-8-sig").splitlines()) == 1


async def test_session_stop_with_unstarted_websites_is_unambiguously_incomplete(workspace):
    add_domains(workspace, *[f"slow-{i}.co.uk" for i in range(25)])

    async def slow(url):
        await asyncio.sleep(0.05)
        return response(url, "Contact form only.")

    pages = {f"https://slow-{i}.co.uk/": slow for i in range(25)}
    result = await run_session(
        workspace,
        settings={"browser": False, "concurrency": 1},
        duration=0.01,
        engine_factory=factory_for(pages),
        checkpointer=save,
        exporter=export,
    )
    assert result["reason"] == "session_budget"
    assert result["collection"]["state"] == "INCOMPLETE"
    assert result["stats"]["sites_unstarted"] == 24
    assert not result["collection"]["completion"]["automatic_passes_finished"]
    assert result["collection"]["completion"]["queue_remaining"] == 24


def test_pass_requeue_and_marker_are_one_transaction_and_idempotent(store, seeded, monkeypatch):
    ensure_plan(store)
    prepare_stage(store)
    store.control("start")
    site = store.claim_site("test", 600, 3)
    store.enqueue(site["id"], [Link(HOME, 1000), Link(HOME + "private", 950)], 0, "", 400)
    home = store.next_page(site["id"])
    store.save_page(home["id"], status="done", home=True)
    denied = store.next_page(site["id"])
    store.save_page(denied["id"], status="blocked", http_status=403, error="Denied")
    store.finish_site(site["id"], "no_email", "frontier_exhausted")
    store.control("pause")
    finish_stage(store)
    original = Store._set_meta

    def fail(db, key, value):
        if key == PLAN_KEY:
            raise RuntimeError("Marker write failed")
        original(db, key, value)

    monkeypatch.setattr(Store, "_set_meta", staticmethod(fail))
    with pytest.raises(RuntimeError, match="Marker write failed"):
        prepare_stage(store)
    assert store.get_site(1)["status"] == "no_email"  # The requeue was rolled back too.
    assert not store.get_meta(PLAN_KEY)["prepared"]
    monkeypatch.setattr(Store, "_set_meta", staticmethod(original))
    prepare_stage(store)
    assert store.get_site(1)["status"] == "pending"
    assert next(p for p in store.site_pages(1) if p["id"] == denied["id"])["status"] == "blocked"
    store.control("start")
    store.claim_site("test", 600, 3)
    before = store.get_site(1)
    prepare_stage(store)  # Already prepared: must not reset attempts/running work.
    assert store.get_site(1) == before


def test_all_complete_stages_are_required_even_when_first_queue_is_empty(store, seeded):
    ensure_plan(store)
    prepare_stage(store)
    store.control("start")
    site = store.claim_site("test", 600, 3)
    store.finish_site(site["id"], "no_email", "frontier_exhausted")
    assert collection_report(store)["completion"]["queue_drained"]
    assert collection_report(store)["state"] == "INCOMPLETE"
    with pytest.raises(ValueError, match="unfinished"):
        store.finish_site(site["id"], "retry", "network", retry_at=time.time() + 100)
        finish_stage(store)


def test_full_queue_claims_use_due_order_index_not_sort_every_remaining_domain(store):
    store.add_sites(prepare_row({"domain": f"index-fixture-{i}.co.uk"}) for i in range(1000))
    queries = []
    store.conn.set_trace_callback(queries.append)
    store.control("start")
    assert store.claim_site("fixture", 600, 3)
    store.conn.set_trace_callback(None)
    select = next(q for q in queries if q.startswith("SELECT * FROM sites INDEXED"))
    plan = " ".join(str(tuple(row)) for row in store.conn.execute("EXPLAIN QUERY PLAN " + select))
    assert "sites_due" in plan and "TEMP B-TREE" not in plan


async def test_original_error_is_not_masked_by_final_export_and_backup_errors(workspace):
    add_domains(workspace, "acmeglazing.co.uk")
    checkpoints = 0

    class BrokenEngine(Engine):
        async def run(self, **kwargs):
            self.started.set()
            raise RuntimeError("ORIGINAL worker failure")

    async def backup(ws, seconds):
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints > 1:
            raise RuntimeError("FINAL checkpoint failure")
        return ws.checkpoint()

    async def broken_export(ws, seconds):
        raise RuntimeError("FINAL export failure")

    with pytest.raises(RuntimeError, match="ORIGINAL worker failure"):
        await run_session(
            workspace,
            settings={"browser": False},
            engine_factory=BrokenEngine,
            checkpointer=backup,
            exporter=broken_export,
        )
    record = json.loads((workspace.work_dir / "last-run.json").read_text())
    assert record["collection"]["state"] == "INCOMPLETE"
    assert len(record["cleanup_errors"]) == 2
    assert checkpoints == 2


async def test_failed_initial_backup_never_starts_requests(workspace):
    add_domains(workspace, "acmeglazing.co.uk")
    called = False

    def engine(store):
        nonlocal called
        called = True
        return Engine(store)

    async def broken_backup(ws, seconds):
        raise RuntimeError("No backup storage")

    with pytest.raises(RuntimeError, match="No backup storage"):
        await run_session(
            workspace,
            settings={"browser": False},
            engine_factory=engine,
            checkpointer=broken_backup,
            exporter=export,
        )
    assert not called
    assert workspace.status()["stats"]["sites_started"] == 0
    record = json.loads((workspace.work_dir / "last-run.json").read_text())
    assert record["reason"] == "checkpoint_error"
    assert record["collection"]["state"] == "INCOMPLETE"


async def test_adding_domains_to_a_finished_plan_starts_a_new_generation_without_losing_evidence(workspace):
    add_domains(workspace, "acmeglazing.co.uk")
    pages = {
        HOME: response(HOME, "kept@acmeglazing.co.uk"),
        "https://secondglazing.co.uk/": response("https://secondglazing.co.uk/", "added@gmail.com"),
    }
    first = await run_session(
        workspace,
        settings={"browser": False},
        engine_factory=factory_for(pages),
        checkpointer=save,
        exporter=export,
    )
    add_domains(workspace, "secondglazing.co.uk")
    second = await run_session(
        workspace,
        settings={"browser": False},
        engine_factory=factory_for(pages),
        checkpointer=save,
        exporter=export,
    )
    assert (
        second["collection"]["automatic_plan"]["generation"]
        == first["collection"]["automatic_plan"]["generation"] + 1
    )
    assert second["stats"]["sites_started"] == 2
    assert second["stats"]["unique_emails"] == 2


def test_deeper_pass_reactivates_deferred_documents_without_losing_evidence(store, seeded):
    ensure_plan(store)
    prepare_stage(store)
    store.control("start")
    site = store.claim_site("test", 600, 3)
    store.enqueue(
        site["id"],
        [
            Link(HOME, 1000),
            Link(HOME + "extra.pdf", 900, "pdf"),
            Link(HOME + "extra-sitemap.xml", 800, "sitemap"),
            Link(HOME + "large.pdf", 700, "pdf"),
        ],
        0,
        "",
        400,
    )
    home = store.next_page(1)
    store.save_page(home["id"], status="done", home=True, hits=[EmailHit("kept@gmail.com", "public_text")])
    for path, error, state in [
        ("extra.pdf", "pdf_limit", "skipped"),
        ("extra-sitemap.xml", "sitemap_limit", "skipped"),
        ("large.pdf", "PDF text/page limit reached (no OCR)", "done"),
    ]:
        page = next(p for p in store.site_pages(1) if p["url"] == HOME + path)
        store.save_page(page["id"], status=state, error=error)
    store.finish_site(1, "completed", "frontier_exhausted")
    store.control("pause")
    finish_stage(store)
    prepare_stage(store)
    pages = {p["url"]: p for p in store.site_pages(1)}
    assert all(
        pages[HOME + p]["status"] == "pending" for p in ("extra.pdf", "extra-sitemap.xml", "large.pdf")
    )
    assert store.stats()["unique_emails"] == 1


async def test_automatic_recovery_cannot_clear_or_shorten_a_persistent_server_cooldown(store, seeded):
    from publicmail.config import CrawlSettings
    from publicmail.http import Fetcher
    from publicmail.models import FetchError

    ensure_plan(store)
    prepare_stage(store)
    store.control("start")
    store.claim_site("test", 600, 3)
    store.put_cooldown("acmeglazing.co.uk", time.time() + 3600, 429)
    store.finish_site(1, "blocked", "rate_limited")
    store.control("pause")
    finish_stage(store)
    prepare_stage(store)  # Deeper stage excludes rate-limited/denied sites.
    assert store.get_site(1)["status"] == "blocked"
    finish_stage(store)
    prepare_stage(store)
    assert store.get_site(1)["status"] == "pending"
    assert store.get_cooldown("acmeglazing.co.uk")[0] > 3590
    client = Fetcher(CrawlSettings())
    client.cooldown_store = store
    client.session = object()  # Any access to session.get would fail: NO network permitted here.
    with pytest.raises(FetchError) as error:
        await client.fetch(HOME)
    assert error.value.category == "rate_limited" and error.value.retry_after > 3590


async def test_bad_single_website_does_not_abort_the_other_domains(workspace):
    add_domains(workspace, "acmeglazing.co.uk", "broken-fixture.co.uk", "secondglazing.co.uk")

    def malformed(url):
        raise ValueError("Synthetic unexpected parser error")

    pages = {
        HOME: response(HOME, "one@gmail.com"),
        "https://broken-fixture.co.uk/": malformed,
        "https://secondglazing.co.uk/": response("https://secondglazing.co.uk/", "two@yahoo.com"),
    }
    result = await run_session(
        workspace,
        settings={"browser": False},
        engine_factory=factory_for(pages),
        checkpointer=save,
        exporter=export,
    )
    assert result["stats"]["sites_started"] == 3
    assert result["stats"]["unique_emails"] == 2
    assert result["stats"]["statuses"]["failed"] == 1
    assert result["collection"]["state"] == "COMPLETE_WITH_GAPS"
    assert result["collection"]["diagnostics"]["site_reasons"]["internal_error"] == 1


def test_finished_marker_alone_is_not_sufficient_to_certify_automatic_completion(store, seeded):
    ensure_plan(store)
    prepare_stage(store)
    store.control("start")
    store.claim_site("test", 600, 3)
    store.finish_site(1, "no_email", "frontier_exhausted")
    plan = store.get_meta(PLAN_KEY)
    plan["state"] = "finished"  # Missing finished stages must still show INCOMPLETE.
    store.set_meta(PLAN_KEY, plan)
    assert collection_report(store)["state"] == "INCOMPLETE"
