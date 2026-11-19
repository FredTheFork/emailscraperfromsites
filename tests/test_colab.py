from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from conftest import HOME, FakeFetcher, response
from filelock import FileLock

from publicmail.colab import Workspace, _atomic_json, checkpoint_job, run_session, workspace_job
from publicmail.engine import Engine
from publicmail.importer import prepare_row
from publicmail.models import EmailHit, Link


@pytest.fixture
def workspace(tmp_path):
    result = Workspace(tmp_path / "local", tmp_path / "drive", keep_snapshots=3)
    result.prepare()
    return result


def add_input(workspace, tmp_path):
    source = tmp_path / "domains.csv"
    source.write_text("domain,business_name\nacmeglazing.co.uk,Fixture Glazing\n", encoding="utf-8")
    return workspace.import_input(source)


def checkpoint(workspace):
    with workspace.lease():
        return workspace.checkpoint()


def test_paths_cannot_put_live_sqlite_on_drive_or_inside_backup_dir(tmp_path):
    with pytest.raises(ValueError, match="not Google Drive"):
        Workspace("/content/drive/MyDrive/unsafe")
    with pytest.raises(ValueError, match="separate"):
        Workspace(tmp_path / "shared", tmp_path / "shared" / "drive")
    with pytest.raises(ValueError, match="separate"):
        Workspace(tmp_path / "drive" / "local", tmp_path / "drive")
    with pytest.raises(RuntimeError, match="not mounted"):
        Workspace(tmp_path / "local", tmp_path / "drive" / "project", drive_mount=tmp_path / "drive")


def test_mount_is_checked_again_before_writing(workspace, monkeypatch):
    workspace.drive_mount = workspace.drive_dir
    monkeypatch.setattr(os.path, "ismount", lambda path: False)
    with pytest.raises(RuntimeError, match="not mounted"):
        workspace.checkpoint()
    assert len(workspace._manifests()) == 1


def test_prepare_is_paused_repeat_safe_and_initial_empty_state_is_backed_up(workspace):
    info = workspace.prepare()
    assert info["stats"]["total"] == 0
    assert info["stats"]["mode"] == "paused"
    assert info["persistent"]
    assert info["restored"] is None
    assert info["saved_input"] is None
    other = Workspace(workspace.work_dir.parent / "fresh-runtime", workspace.drive_dir)
    recovered = other.prepare()
    assert recovered["restored"]
    assert recovered["collection_id"] == info["collection_id"]


def test_import_saves_original_privately_and_is_idempotent(workspace, tmp_path):
    assert add_input(workspace, tmp_path)["queue_total"] == 1
    assert add_input(workspace, tmp_path)["duplicates"] == 1
    saved = Path(workspace.saved_input())
    assert saved.parent == workspace.drive_dir / "inputs"
    assert saved.read_text().startswith("domain,business_name")
    with pytest.raises(ValueError, match="state database"):
        workspace.import_input(workspace.db)


def test_wal_snapshot_restores_evidence_frontier_settings_and_cooldowns(workspace, tmp_path):
    add_input(workspace, tmp_path)
    with workspace.open_store() as store:
        store.control("start")
        site = store.claim_site("interrupted-worker", 300, 3)
        store.enqueue(site["id"], [Link(HOME, 1000)], 0, "", 250, "seed")
        page = store.next_page(site["id"])
        store.begin_page(page["id"])
        store.save_page(
            page["id"],
            status="done",
            final_url=HOME,
            home=True,
            hits=[EmailHit("office@acmeglazing.co.uk", "public_text")],
            links=[Link(HOME + "contact", 950)],
        )
        contact = store.next_page(site["id"])
        store.begin_page(contact["id"])
        store.put_cooldown("acmeglazing.co.uk", time.time() + 1200, 429)
        assert Path(str(workspace.db) + "-wal").exists()
        checkpoint(workspace)  # Real online backup while the source WAL is open.
    new = Workspace(tmp_path / "runtime-two", workspace.drive_dir)
    info = new.prepare()
    assert info["stats"]["total"] == 1
    assert info["stats"]["unique_emails"] == 1
    assert info["stats"]["mode"] == "paused"
    assert info["recovered"] == 1
    with new.open_store() as store:
        assert store.next_page(site["id"])["status"] == "pending"
        assert store.get_site(site["id"])["attempts"] == 0
        assert store.get_cooldown("acmeglazing.co.uk")[0] > 1100


def test_existing_local_progress_is_never_overwritten_by_drive(workspace, tmp_path):
    with workspace.open_store() as store:
        store.add_sites([prepare_row({"domain": "newglazing.co.uk"})])
    assert workspace.prepare()["stats"]["total"] == 1
    assert workspace.status()["stats"]["total"] == 1


def test_bad_latest_snapshot_falls_back_and_partials_are_ignored(workspace, tmp_path):
    add_input(workspace, tmp_path)
    latest = checkpoint(workspace)
    Path(latest["manifest"]).with_suffix(".db").write_bytes(b"broken")
    directory = workspace.drive_dir / "snapshots"
    (directory / "snapshot-99999999T999999999999Z-aaaaaaaaaaaa.db.partial-test").write_bytes(b"partial")
    other = Workspace(tmp_path / "runtime-two", workspace.drive_dir)
    assert other.prepare()["stats"]["total"] == 1


def test_all_bad_backups_fail_closed_without_creating_an_empty_queue(workspace, tmp_path):
    for manifest in workspace._manifests():
        manifest.with_suffix(".db").write_bytes(b"broken")
    other = Workspace(tmp_path / "runtime-two", workspace.drive_dir)
    with pytest.raises(ValueError, match="No verified snapshot"):
        other.prepare()
    assert not other.db.exists()
    assert workspace._manifests()


def test_manifest_path_traversal_is_rejected(workspace, tmp_path):
    manifest = workspace._manifests()[0]
    info = json.loads(manifest.read_text())
    info["database"] = "../../sensitive.db"
    _atomic_json(manifest, info)
    other = Workspace(tmp_path / "runtime-two", workspace.drive_dir)
    with pytest.raises(ValueError, match="No verified snapshot"):
        other.prepare()
    assert not other.db.exists()


def test_unrelated_drive_collection_does_not_replace_local_state(workspace, tmp_path):
    other = Workspace(tmp_path / "other-local", tmp_path / "other-drive")
    other.prepare()
    other.drive_dir = workspace.drive_dir
    other.backup_root = workspace.drive_dir
    with pytest.raises(ValueError, match="another collection"):
        other.prepare()


def test_verified_snapshot_retention_keeps_three_recovery_points(workspace):
    for _ in range(5):
        checkpoint(workspace)
    assert len(workspace._manifests()) == 3
    assert len(list((workspace.drive_dir / "snapshots").glob("*.db"))) == 3


def test_failed_copy_preserves_previous_committed_snapshot(workspace, monkeypatch):
    import publicmail.colab as module

    before = workspace._manifests()
    original = module._atomic_copy

    def fail(source, destination):
        if destination.parent.name == "snapshots":
            raise OSError("Drive quota exhausted")
        return original(source, destination)

    monkeypatch.setattr(module, "_atomic_copy", fail)
    with pytest.raises(OSError, match="quota"):
        checkpoint(workspace)
    assert workspace._manifests() == before
    assert before[0].with_suffix(".db").is_file()


def test_advisory_drive_marker_rejects_a_second_runtime_and_expires(workspace, tmp_path):
    other = Workspace(tmp_path / "runtime-two", workspace.drive_dir)
    with workspace.lease():
        with pytest.raises(RuntimeError, match="Another Colab runtime"):
            other.prepare()
    _atomic_json(
        workspace.drive_dir / "active-session.json",
        {"session_id": "previous-runtime", "expires_at": time.time() - 1},
    )
    assert other.prepare()["restored"]


def test_local_operation_and_worker_guards_prevent_duplicate_commands(workspace):
    with workspace.operation():
        with pytest.raises(RuntimeError, match="Another notebook"):
            workspace.prepare()
    with FileLock(str(workspace.db) + ".worker.lock"):
        with pytest.raises(RuntimeError, match="worker still owns"):
            workspace.prepare()


def test_exports_are_exactly_results_not_input_or_database(workspace, tmp_path):
    add_input(workspace, tmp_path)
    output = workspace.export()
    with zipfile.ZipFile(output["export_zip"]) as archive:
        assert set(archive.namelist()) == {
            "emails.csv",
            "unique_emails.csv",
            "observations.csv",
            "observations.jsonl",
            "coverage.csv",
            "summary.json",
        }
        assert b"acmeglazing.co.uk" in archive.read("coverage.csv")
    assert Path(output["drive_export_zip"]).read_bytes() == Path(output["export_zip"]).read_bytes()


async def test_real_checkpoint_child_process(workspace):
    output = await checkpoint_job(workspace, 20)
    assert await asyncio.to_thread(Path(output["manifest"]).is_file)
    workspace.release_lease()


async def test_slow_backup_child_is_killed_not_left_running(workspace, monkeypatch):
    monkeypatch.setattr(
        workspace, "command", lambda action: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    with pytest.raises(RuntimeError, match="timed out"):
        await workspace_job(workspace, "_checkpoint", 0.05)


async def test_full_pilot_run_uses_real_engine_and_exports_then_resumes(workspace, tmp_path):
    add_input(workspace, tmp_path)
    with workspace.open_store() as store:
        store.add_sites([prepare_row({"domain": "secondglazing.co.uk"})])

    def factory(store):
        def client(settings):
            return FakeFetcher(
                settings,
                {
                    HOME: response(HOME, "hello@acmeglazing.co.uk"),
                    "https://secondglazing.co.uk/": response(
                        "https://secondglazing.co.uk/", "second@gmail.com"
                    ),
                },
            )

        return Engine(store, client_factory=client)

    first = await run_session(workspace, max_sites=1, settings={"max_pages": 1}, engine_factory=factory)
    assert first["stats"]["terminal"] == 1
    assert first["stats"]["statuses"]["pending"] == 1
    assert first["stats"]["unique_emails"] == 1
    assert await asyncio.to_thread(Path(first["drive_export_zip"]).is_file)
    second = await run_session(workspace, max_sites=None, engine_factory=factory)
    assert second["stats"]["terminal"] == 2
    assert second["stats"]["unique_emails"] == 2
    assert second["stats"]["mode"] == "paused"
    assert json.loads((workspace.work_dir / "last-run.json").read_text())["stats"]["terminal"] == 2


async def test_periodic_backup_failure_stops_claims_and_keeps_local_progress(workspace, tmp_path):
    add_input(workspace, tmp_path)
    calls = 0

    async def save(ws, seconds):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Drive disconnected")
        return checkpoint(ws)

    async def slow(url):
        await asyncio.sleep(0.1)
        return response(url, "partial@acmeglazing.co.uk")

    def factory(store):
        return Engine(store, client_factory=lambda cfg: FakeFetcher(cfg, {HOME: slow}))

    with pytest.raises(RuntimeError, match="Drive disconnected"):
        await run_session(
            workspace,
            settings={"max_pages": 1},
            engine_factory=factory,
            checkpointer=save,
            checkpoint_interval=0.02,
        )
    status = workspace.status()["stats"]
    assert status["mode"] == "paused"
    assert status["worker_state"] == "offline"
    assert status["unique_emails"] == 1
    assert (workspace.work_dir / "last-export.json").is_file()


async def test_session_budget_stops_safely_without_chasing_an_email_count(workspace, tmp_path):
    add_input(workspace, tmp_path)

    async def slow(url):
        await asyncio.sleep(0.1)
        return response(url, "A contact form, no public email")

    result = await run_session(
        workspace,
        max_sites=None,
        settings={"max_pages": 1},
        duration=0.01,
        engine_factory=lambda store: Engine(store, client_factory=lambda cfg: FakeFetcher(cfg, {HOME: slow})),
    )
    assert result["reason"] == "session_budget"
    assert result["stats"]["mode"] == "paused"
    assert result["stats"]["unique_emails"] == 0
    assert await asyncio.to_thread(Path(result["export_zip"]).is_file)


def test_cli_prepare_import_export_and_recovery(tmp_path):
    work, drive = tmp_path / "local", tmp_path / "drive"
    prefix = [sys.executable, "-m", "publicmail.colab", "--work-dir", str(work), "--drive-dir", str(drive)]

    def run(*args):
        result = subprocess.run([*prefix, *map(str, args)], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert run("prepare")["persistent"]
    source = tmp_path / "input.csv"
    source.write_text("domain\nacmeglazing.co.uk\n", encoding="utf-8")
    assert run("import", source)["inserted"] == 1
    assert run("status")["stats"]["total"] == 1
    assert Path(run("export")["export_zip"]).exists()
    assert run("doctor")["browser_launch_checked"] is False
    assert run("pause")["paused"]
    result = subprocess.run([*prefix, "run", "--max-sites", "-1"], capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "Pilot limit" in result.stderr


def test_source_sqlite_is_copied_before_querying_drive_input(workspace, tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE domains(domain TEXT)")
        connection.execute("INSERT INTO domains VALUES ('acmeglazing.co.uk')")
    before = source.read_bytes()
    assert workspace.import_input(source)["inserted"] == 1
    assert source.read_bytes() == before
    assert (workspace.work_dir / "inputs" / "input.db").exists()


def test_drive_mount_rejects_live_database_even_with_a_custom_mount(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "ismount", lambda path: True)
    mount = tmp_path / "mounted-drive"
    with pytest.raises(ValueError, match="not Google Drive"):
        Workspace(mount / "live-db", mount / "project" / "backups", drive_mount=mount)


def test_failed_drive_export_keeps_a_downloadable_local_zip(workspace, tmp_path, monkeypatch):
    import publicmail.colab as module

    add_input(workspace, tmp_path)
    original = module._atomic_copy

    def fail(source, destination):
        if destination.parent == workspace.drive_dir / "exports":
            raise OSError("Drive export quota failure")
        return original(source, destination)

    monkeypatch.setattr(module, "_atomic_copy", fail)
    with pytest.raises(OSError, match="quota"):
        workspace.export()
    marker = json.loads((workspace.work_dir / "last-export.json").read_text())
    assert Path(marker["export_zip"]).is_file()
    assert marker["drive_export_zip"] is None
    assert workspace._manifests()


async def test_runner_rejected_before_ownership_does_not_pause_someone_elses_run(workspace, tmp_path):
    add_input(workspace, tmp_path)

    class RejectedEngine(Engine):
        async def run(self, **kwargs):
            self.store.control("start")  # Simulates a different runner winning the startup race.
            raise RuntimeError("Another worker won the lock")

    with pytest.raises(RuntimeError, match="Another worker"):
        await run_session(workspace, engine_factory=RejectedEngine)
    assert workspace.status()["stats"]["mode"] == "running"
    with workspace.open_store() as store:
        store.control("pause")
