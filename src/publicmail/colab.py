"""Colab session runner: local SQLite, verified snapshots, and private exports.

No google.colab import is required here. The notebook calls this module in an
isolated Python environment, not inside IPython's already-running event loop.
Drive is backup storage only, never the live SQLite filesystem.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import sqlite3
import sys
import time
import uuid
import zipfile
from collections.abc import Callable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from filelock import FileLock, Timeout

from . import __version__
from .config import CrawlSettings
from .coverage import (
    WORKFLOW_VERSION,
    automatic_profiles,
    collection_report,
    ensure_plan,
    finish_stage,
    prepare_stage,
    report_text,
)
from .engine import Engine
from .exporter import write_exports
from .importer import import_file
from .store import Store

log = logging.getLogger(__name__)
_SNAPSHOT = re.compile(r"snapshot-\d{8}T\d{12}Z-[0-9a-f]{12}$")
_APPLICATION_ID = 0x504D4149
LEASE_SECONDS = 900


def _json(path: Path) -> dict:
    if path.stat().st_size > 64_000:
        raise ValueError(f"Metadata file is too large: {path.name}")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"Invalid metadata: {path.name}")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_copy(source: Path, destination: Path) -> None:
    """Publish only complete files; a snapshot's JSON commit marker comes last."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
    try:
        with source.open("rb") as src, partial.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            # Drive's FUSE layer acknowledges writes, not a cloud durability SLA.
            # Hash verification below is still required after copying.
        os.replace(partial, destination)
    finally:
        with suppress(OSError):
            partial.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial-" + uuid.uuid4().hex)
    try:
        with partial.open("x", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, allow_nan=False)
            target.write("\n")
            target.flush()
        os.replace(partial, path)
    finally:
        with suppress(OSError):
            partial.unlink(missing_ok=True)


def _inspect_database(path: Path) -> dict:
    # Called only for closed, LOCAL snapshot copies, never an active WAL database.
    connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
            raise ValueError("Not a Public Mail state snapshot")
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Unsupported snapshot schema; update the application")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Snapshot integrity check failed")
        row = connection.execute("SELECT value FROM meta WHERE key='colab_collection_id'").fetchone()
        identifier = json.loads(row[0]) if row else None
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Snapshot has no valid Colab collection identity")
        return {
            "collection_id": identifier,
            "totals": {
                "total": connection.execute("SELECT COUNT(*) FROM sites").fetchone()[0],
                "terminal": connection.execute(
                    "SELECT COUNT(*) FROM sites WHERE status IN ('completed','no_email','blocked','failed')"
                ).fetchone()[0],
                "unique_emails": connection.execute("SELECT COUNT(*) FROM email_addresses").fetchone()[0],
            },
        }
    finally:
        connection.close()


class Workspace:
    def __init__(
        self,
        work_dir: str | Path,
        drive_dir: str | Path | None = None,
        *,
        drive_mount: str | Path | None = None,
        keep_snapshots: int = 3,
    ):
        self.work_dir = Path(work_dir).expanduser().resolve()
        self.drive_dir = Path(drive_dir).expanduser().resolve() if drive_dir else None
        self.drive_mount = Path(drive_mount).expanduser().resolve() if drive_mount else None
        if not 2 <= keep_snapshots <= 20:
            raise ValueError("Keep between 2 and 20 snapshot generations")
        self.keep_snapshots = keep_snapshots
        if (
            self.work_dir == Path("/content/drive")
            or Path("/content/drive") in self.work_dir.parents
            or self.drive_mount
            and (self.drive_mount == self.work_dir or self.drive_mount in self.work_dir.parents)
        ):
            raise ValueError("The working database must be on local disk, not Google Drive")
        if self.drive_dir and (
            self.drive_dir == self.work_dir
            or self.drive_dir in self.work_dir.parents
            or self.work_dir in self.drive_dir.parents
        ):
            raise ValueError("Local work and Drive backup directories must be separate, not nested")
        if self.drive_mount and (
            not self.drive_dir
            or self.drive_mount != self.drive_dir
            and self.drive_mount not in self.drive_dir.parents
        ):
            raise ValueError("Drive backup directory must be inside the specified mount")
        self.check_mount()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db = self.work_dir / "state.db"
        self.backup_root = self.drive_dir or self.work_dir / "local-backups"
        self.staging = self.work_dir / ".staging"
        self.staging.mkdir(exist_ok=True)
        session_file = self.work_dir / "session-id"
        # One session identity survives cell reruns, not a runtime reset.
        with FileLock(str(session_file) + ".lock", timeout=5):
            if not session_file.exists():
                session_file.write_text(uuid.uuid4().hex, encoding="ascii")
            self.session_id = session_file.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", self.session_id):
            raise ValueError("Invalid local session identity; inspect the work directory")

    def check_mount(self) -> None:
        if self.drive_mount and not os.path.ismount(self.drive_mount):
            raise RuntimeError("Google Drive is not mounted. Re-run the notebook's Drive/setup cell.")

    @contextmanager
    def operation(self):
        lock = FileLock(str(self.work_dir / "colab-session.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError(
                "Another notebook command is active. Stop/wait for it before rerunning."
            ) from exc
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def idle_worker(self):
        lock = FileLock(str(self.db) + ".worker.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError(
                "A worker still owns this database. Stop it before changing/restoring state."
            ) from exc
        try:
            yield
        finally:
            lock.release()

    def renew_lease(self) -> None:
        """Advisory cross-runtime guard; Drive is NOT a distributed lock service."""
        if not self.drive_dir:
            return
        self.check_mount()
        path = self.backup_root / "active-session.json"
        if path.exists():
            lease = _json(path)
            expiry = lease.get("expires_at")
            if not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                raise ValueError("Invalid Drive session marker; inspect active-session.json")
            if lease.get("session_id") != self.session_id and expiry > time.time():
                raise RuntimeError(
                    "Another Colab runtime is using this Drive project. Stop it first. "
                    "After a runtime dies, its advisory marker expires within 15 minutes. "
                    "Never run two runtimes against the same project."
                )
        _atomic_json(path, {"session_id": self.session_id, "expires_at": time.time() + LEASE_SECONDS})
        if _json(path).get("session_id") != self.session_id:
            raise RuntimeError("Drive session marker changed; another runtime may be active")

    def release_lease(self) -> None:
        if self.drive_dir:
            self.check_mount()
            path = self.backup_root / "active-session.json"
            if path.exists() and _json(path).get("session_id") == self.session_id:
                path.unlink()

    @contextmanager
    def lease(self):
        self.renew_lease()
        try:
            yield
        finally:
            try:
                self.release_lease()
            except Exception:
                log.warning("Could not release Drive session marker; it will expire", exc_info=True)

    def identity(self, store: Store) -> str:
        self.check_mount()
        path = self.backup_root / "collection.json"
        remote = _json(path).get("collection_id") if path.exists() else None
        current = store.get_meta("colab_collection_id")
        if remote is not None and not isinstance(remote, str):
            raise ValueError("Invalid collection identity in the backup folder")
        if current and remote and current != remote:
            raise ValueError(
                "This Drive folder belongs to another collection. Choose the correct project name."
            )
        if not current:
            # An existing collection record with no restoreable snapshot must not
            # turn into a silent empty/new run with the same identity.
            if remote:
                raise ValueError(
                    "Existing Drive collection has no usable restored state; refusing an empty reset"
                )
            current = uuid.uuid4().hex
            store.set_meta("colab_collection_id", current)
        if not isinstance(current, str) or not re.fullmatch(r"[0-9a-f]{32}", current):
            raise ValueError("Invalid local collection identity")
        if not remote:
            _atomic_json(path, {"format": 1, "collection_id": current, "created_at": time.time()})
        return current

    def _manifests(self) -> list[Path]:
        directory = self.backup_root / "snapshots"
        return sorted(
            (p for p in directory.glob("snapshot-*.json") if _SNAPSHOT.fullmatch(p.stem)), reverse=True
        )

    def _validate_copy(self, manifest_path: Path, destination: Path) -> dict:
        manifest = _json(manifest_path)
        if manifest.get("format") != 1 or manifest.get("database") != manifest_path.stem + ".db":
            raise ValueError("Invalid snapshot manifest/filename")
        if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256", ""))):
            raise ValueError("Invalid snapshot checksum")
        source = manifest_path.with_suffix(".db")
        if source.is_symlink() or not source.is_file() or source.stat().st_size != manifest.get("bytes"):
            raise ValueError("Snapshot database is absent or incomplete")
        _atomic_copy(source, destination)
        if _sha256(destination) != manifest["sha256"]:
            raise ValueError("Snapshot checksum mismatch")
        if _inspect_database(destination)["collection_id"] != manifest.get("collection_id"):
            raise ValueError("Snapshot collection identity mismatch")
        return manifest

    def restore(self) -> str | None:
        self.check_mount()
        if self.db.exists():
            return None  # Local in-session progress always wins; never overwrite it.
        if any(Path(str(self.db) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise ValueError("Orphan local SQLite sidecars found; inspect them before restoring")
        identity_path = self.backup_root / "collection.json"
        expected = _json(identity_path).get("collection_id") if identity_path.exists() else None
        manifests = self._manifests()
        errors = []
        for path in manifests:
            temporary = self.staging / ("restore-" + uuid.uuid4().hex + ".db")
            try:
                metadata = self._validate_copy(path, temporary)
                if expected and expected != metadata["collection_id"]:
                    raise ValueError("Snapshot does not match this Drive collection")
                os.replace(temporary, self.db)
                log.info("Restored verified snapshot %s", path.name)
                return path.name
            except (ValueError, OSError, sqlite3.Error) as exc:
                errors.append(f"{path.name}: {exc}")
                log.warning("Skipping unusable snapshot %s: %s", path.name, exc)
            finally:
                temporary.unlink(missing_ok=True)
        # Even a first, incomplete publication signals existing work. Do not
        # silently create a fresh empty queue if all backups are damaged.
        if manifests or expected or any((self.backup_root / "snapshots").glob("snapshot-*.db")):
            raise ValueError(
                "No verified snapshot can be restored. Existing backups were kept. " + "; ".join(errors[:3])
            )
        return None

    def prepare(self) -> dict:
        with self.operation(), self.lease(), self.idle_worker():
            restored = self.restore()
            with self.open_store() as store:
                identifier = self.identity(store)
                recovered = store.recover_running()
                store.control("pause")
                stats = store.stats()
            self.checkpoint()
            return {
                "restored": restored,
                "recovered": recovered,
                "collection_id": identifier,
                "stats": stats,
                "saved_input": self.saved_input(),
                "work_dir": str(self.work_dir),
                "backup_dir": str(self.backup_root),
                "persistent": bool(self.drive_dir),
            }

    @contextmanager
    def open_store(self):
        store = Store(self.db)
        try:
            yield store
        finally:
            store.close()

    def saved_input(self) -> str | None:
        self.check_mount()
        marker = self.backup_root / "saved-input.json"
        if not marker.exists():
            return None
        info = _json(marker)
        name = info.get("filename", "")
        if not isinstance(name, str) or not re.fullmatch(
            r"input-[0-9a-f]{64}\.(csv|db|sqlite|sqlite3)", name
        ):
            raise ValueError("Invalid saved input filename")
        path = self.backup_root / "inputs" / name
        if path.is_file() and _sha256(path) == info.get("sha256"):
            return str(path)
        raise ValueError("Saved input is missing or damaged. Upload your original CSV again.")

    def import_input(self, source: str | Path, *, table: str | None = None) -> dict:
        source = Path(source).expanduser().resolve()
        if source.suffix.lower() not in {".csv", ".db", ".sqlite", ".sqlite3"} or not source.is_file():
            raise ValueError("Choose an existing CSV or input SQLite file")
        if source == self.db or self.db.exists() and source.samefile(self.db):
            raise ValueError("Do not import the working state database as an input")
        with self.operation(), self.lease(), self.idle_worker():
            if not self.db.exists():
                raise ValueError("Run prepare before importing input")
            local = self.work_dir / "inputs" / ("input" + source.suffix.lower())
            if source != local:
                _atomic_copy(source, local)  # Never query a source SQLite file on Drive.
            with self.open_store() as store:
                self.identity(store)
                report = import_file(store, local, table)
                report["queue_total"] = store.stats()["total"]
            digest = _sha256(local)
            name = "input-" + digest + local.suffix
            saved = self.backup_root / "inputs" / name
            _atomic_copy(local, saved)
            if _sha256(saved) != digest:
                raise OSError("Input copy checksum failed; local input and state are intact")
            _atomic_json(self.backup_root / "saved-input.json", {"filename": name, "sha256": digest})
            self.checkpoint()
            return report

    def checkpoint(self) -> dict:
        self.check_mount()
        self.renew_lease()
        if not self.db.exists():
            raise ValueError("There is no local database to checkpoint; run prepare first")
        with FileLock(str(self.work_dir / "checkpoint.lock"), timeout=5):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            name = f"snapshot-{stamp}-{uuid.uuid4().hex[:12]}"
            local = self.staging / (name + ".db")
            try:
                with self.open_store() as store:
                    identifier = self.identity(store)
                    store.backup(local)
                inspected = _inspect_database(local)
                if inspected["collection_id"] != identifier:
                    raise ValueError("Local snapshot identity check failed")
                digest = _sha256(local)
                directory = self.backup_root / "snapshots"
                database = directory / (name + ".db")
                _atomic_copy(local, database)
                if database.stat().st_size != local.stat().st_size or _sha256(database) != digest:
                    raise OSError("Published snapshot failed checksum verification")
                info = {
                    "format": 1,
                    "database": database.name,
                    "collection_id": identifier,
                    "session_id": self.session_id,
                    "created_at": time.time(),
                    "application_version": __version__,
                    "sha256": digest,
                    "bytes": local.stat().st_size,
                    "totals_in_snapshot": inspected["totals"],
                }
                manifest = database.with_suffix(".json")
                _atomic_json(manifest, info)  # Commit marker: publish LAST.
                if _json(manifest) != info:
                    raise OSError("Snapshot manifest could not be verified")
                _atomic_json(self.work_dir / "last-checkpoint.json", {**info, "manifest": str(manifest)})
                # Retention starts only after a new complete, verified publication.
                # Invalid generations are not counted as good recovery points.
                good = 0
                for candidate in self._manifests():
                    try:
                        previous = _json(candidate)
                        db = candidate.with_suffix(".db")
                        if previous.get("collection_id") != identifier or not db.is_file():
                            continue
                        if db.stat().st_size != previous.get("bytes") or _sha256(db) != previous.get(
                            "sha256"
                        ):
                            continue
                        good += 1
                        if good > self.keep_snapshots:
                            candidate.unlink()  # Remove marker before its now-unreferenced DB.
                            db.unlink()
                    except (ValueError, OSError):
                        log.warning(
                            "Could not inspect/prune snapshot %s; kept where possible", candidate.name
                        )
                log.info("Verified checkpoint: %s", manifest)
                return {**info, "manifest": str(manifest)}
            finally:
                local.unlink(missing_ok=True)

    def export(self) -> dict:
        if not self.db.exists():
            raise ValueError("Run prepare/import before exporting")
        name = datetime.now(UTC).strftime("results-%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:8]
        directory = self.work_dir / "exports" / name
        with self.open_store() as store:
            paths = write_exports(store, directory, jsonl=True)
        archive = directory.with_suffix(".zip")
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as zipped:
            for path in paths:
                zipped.write(path, arcname=path.name)
        result = {"export_dir": str(directory), "export_zip": str(archive), "drive_export_zip": None}
        _atomic_json(self.work_dir / "last-export.json", result)
        if self.drive_dir:
            self.check_mount()
            target = self.backup_root / "exports" / archive.name
            _atomic_copy(archive, target)
            if _sha256(archive) != _sha256(target):
                raise OSError(f"Drive export verification failed. Local ZIP is safe at {archive}")
            result["drive_export_zip"] = str(target)
            _atomic_json(self.work_dir / "last-export.json", result)
        return result

    def status(self) -> dict:
        if not self.db.exists():
            raise ValueError("Run prepare first so Drive recovery is considered before opening a database")
        with self.open_store() as store:
            stats = store.stats()
            collection = collection_report(store)
        marker = self.work_dir / "last-checkpoint.json"
        return {
            "stats": stats,
            "collection": collection,
            "last_checkpoint": _json(marker) if marker.exists() else None,
            "work_dir": str(self.work_dir),
            "backup_dir": str(self.backup_root),
            "persistent": bool(self.drive_dir),
        }

    def command(self, action: str) -> list[str]:
        args = [
            sys.executable,
            "-m",
            "publicmail.colab",
            "--work-dir",
            str(self.work_dir),
            "--keep-snapshots",
            str(self.keep_snapshots),
        ]
        if self.drive_dir:
            args += ["--drive-dir", str(self.drive_dir)]
        if self.drive_mount:
            args += ["--drive-mount", str(self.drive_mount)]
        return [*args, action]


async def workspace_job(workspace: Workspace, action: str, deadline_seconds: float) -> dict:
    """Bound slow Drive FUSE work in a child process, not an executor thread."""
    process = await asyncio.create_subprocess_exec(
        *workspace.command(action), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=deadline_seconds)
        if process.returncode:
            raise RuntimeError(f"{action} failed: " + stderr.decode(errors="replace")[-1500:])
        return json.loads(stdout)
    except TimeoutError as exc:
        raise RuntimeError(
            f"{action} timed out; stopping rather than continuing without verified backups"
        ) from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def checkpoint_job(workspace: Workspace, deadline_seconds: float) -> dict:
    return await workspace_job(workspace, "_checkpoint", deadline_seconds)


async def export_job(workspace: Workspace, deadline_seconds: float) -> dict:
    return await workspace_job(workspace, "_export", deadline_seconds)


async def run_session(
    workspace: Workspace,
    *,
    max_sites: int | None = None,
    settings: dict | None = None,
    duration: float = 0,
    automatic: bool = True,
    checkpoint_interval: float = 120,
    checkpoint_timeout: float = 180,
    engine_factory: Callable = Engine,
    checkpointer: Callable = checkpoint_job,
    exporter: Callable = export_job,
) -> dict:
    """Drain the WHOLE list, then bounded persisted coverage/recovery passes.

    No default pilot or planned session stop. A user-requested claim/time cap is
    still supported, but must never be reported as a completed full collection.
    Google can terminate a runtime; the next Run all resumes the saved stage.
    """
    if max_sites is not None and not 1 <= max_sites <= 100_000:
        raise ValueError("Pilot limit must be between 1 and 100,000 claims, or None for the full queue")
    if not all(math.isfinite(x) and x > 0 for x in (checkpoint_interval, checkpoint_timeout)):
        raise ValueError("Checkpoint intervals/timeouts must be positive finite values")
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("Session duration must be nonnegative and finite (0 means no planned stop)")
    if not workspace.db.exists():
        raise ValueError("Run prepare first; do not bypass Drive recovery")
    automatic = automatic and max_sites is None
    overrides = settings or {}
    profiles = automatic_profiles(browser=overrides.get("browser", True))
    with workspace.operation(), workspace.lease():
        with workspace.idle_worker(), workspace.open_store() as initial:
            workspace.identity(initial)
            initial.recover_running()
            initial.control("pause")
            if not initial.stats()["total"]:
                raise ValueError("The queue is empty. Import your CSV before starting a crawl.")
            if automatic:
                plan = ensure_plan(initial)
                plan = prepare_stage(initial)
                index = min(plan["stage_index"], len(profiles) - 1)
                configured = CrawlSettings.model_validate(profiles[index] | overrides)
            else:
                configured = CrawlSettings.model_validate(initial.settings().model_dump() | overrides)
                initial.set_meta("collection_run_kind", "pilot" if max_sites else "standard")
            initial.save_settings(configured)
            initial.set_meta("collection_stop_reason", "running")
        store = Store(workspace.db)
        engine = task = None
        loop = asyncio.get_running_loop()
        previous_handlers = {}
        interrupted = False
        reason = "error"
        primary_error = None
        cleanup_errors = []
        exports = final = None
        # Configuration was guarded, but a rejected worker startup must not
        # pause/export another process that won the worker lock in the gap.
        safe_finalize = True
        deadline = time.monotonic() + duration if duration else math.inf
        next_backup = time.monotonic() + checkpoint_interval

        def stop() -> None:
            nonlocal interrupted
            interrupted = True
            if engine is not None:
                engine.stop()

        async def save_checkpoint() -> dict:
            nonlocal next_backup, reason
            try:
                result = await checkpointer(workspace, checkpoint_timeout)
            except Exception:
                reason = "checkpoint_error"
                raise
            next_backup = time.monotonic() + checkpoint_interval
            return result

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[sig] = signal.getsignal(sig)
                with suppress(NotImplementedError, ValueError):
                    loop.add_signal_handler(sig, stop)
            # If the first backup fails, no target-site requests may start.
            await save_checkpoint()
            while True:
                if interrupted:
                    reason = "interrupted"
                    break
                if time.monotonic() >= deadline:
                    reason = "session_budget"
                    break
                if automatic:
                    plan = store.get_meta("automatic_coverage_plan")
                    if plan["state"] == "finished":
                        reason = "automatic_complete"
                        break
                    log.info(
                        "AUTOMATIC COVERAGE %s/3: %s; %s input websites. No pilot limit.",
                        plan["stage_index"] + 1,
                        plan["stages"][plan["stage_index"]]["name"],
                        plan["input_total"],
                    )
                engine = engine_factory(store)
                safe_finalize = False
                task = asyncio.create_task(engine.run(service=False, start=True, limit=max_sites))
                while not task.done():
                    now = time.monotonic()
                    if now >= deadline:
                        reason = "session_budget"
                        engine.stop()
                        break
                    await asyncio.wait({task}, timeout=max(0, min(next_backup, deadline) - now))
                    if task.done():
                        break
                    if time.monotonic() >= next_backup:
                        await save_checkpoint()
                        log.info("Verified checkpoint saved; full-list collection continues")
                await asyncio.wait_for(asyncio.shield(task), timeout=40)
                safe_finalize = engine.started.is_set()
                if interrupted:
                    reason = "interrupted"
                    break
                if reason == "session_budget":
                    break
                stats = store.stats()
                remaining = sum(stats["statuses"][s] for s in ("pending", "retry", "running"))
                if remaining:
                    reason = "claim_limit" if store.get_meta("run_remaining") == 0 else "paused"
                    break
                if not automatic:
                    reason = "queue_drained"
                    break
                with workspace.idle_worker():
                    plan = finish_stage(store)
                    if plan["state"] == "finished":
                        reason = "automatic_complete"
                        break
                    plan = prepare_stage(store)
                    store.save_settings(
                        CrawlSettings.model_validate(profiles[plan["stage_index"]] | overrides)
                    )
                # Persist both the completed stage AND the next selection before
                # issuing requests in that pass. Interrupted transitions resume.
                await save_checkpoint()
        except BaseException as exc:
            primary_error = exc
            if reason != "checkpoint_error":
                reason = (
                    "interrupted" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "error"
                )
        finally:
            if engine is not None:
                engine.stop()
                if task and not task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=40)
                    except (TimeoutError, asyncio.CancelledError):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    except Exception:
                        log.exception("Worker stopped after a session/checkpoint error")
                safe_finalize = engine.started.is_set()
            try:
                if safe_finalize:
                    # Retain the idle-worker lock THROUGH export/checkpoint so
                    # an external CLI cannot change rows in an aligned bundle.
                    with workspace.idle_worker():
                        store.control("pause")
                        store.set_meta("collection_stop_reason", reason)
                        # Preserve the FIRST error. Failed final I/O must not
                        # mask it; attempt BOTH export and checkpoint anyway.
                        for action, job in (("export", exporter), ("checkpoint", checkpointer)):
                            try:
                                output = await job(workspace, checkpoint_timeout)
                                if action == "export":
                                    exports = output
                                else:
                                    final = output
                            except Exception as exc:
                                cleanup_errors.append(exc)
                                log.exception(
                                    "Final %s failed; local evidence and previous snapshots are retained",
                                    action,
                                )
            except Exception as exc:
                safe_finalize = False
                cleanup_errors.append(exc)
            finally:
                try:
                    store.close()
                except Exception as exc:
                    cleanup_errors.append(exc)
                finally:
                    for sig, previous in previous_handlers.items():
                        with suppress(NotImplementedError, ValueError):
                            loop.remove_signal_handler(sig)
                            signal.signal(sig, previous)
        if safe_finalize:
            try:
                result = {
                    "reason": reason,
                    **workspace.status(),
                    **(exports or {}),
                    "checkpoint": final,
                    "cleanup_errors": [f"{type(e).__name__}: {e}" for e in cleanup_errors],
                }
                _atomic_json(workspace.work_dir / "last-run.json", result)
                log.info("%s", report_text(result["collection"]))
            except Exception as exc:
                cleanup_errors.append(exc)
        if primary_error is not None:
            raise primary_error
        if cleanup_errors:
            raise cleanup_errors[0]
        if not safe_finalize:
            raise RuntimeError("The worker never acquired ownership; no completed run is being reported")
        return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--work-dir", required=True)
    result.add_argument("--drive-dir")
    result.add_argument("--drive-mount", help="Require this actual mount at every Drive operation")
    result.add_argument("--keep-snapshots", type=int, default=3)
    subs = result.add_subparsers(dest="action", required=True)
    for name in ("prepare", "status", "pause", "checkpoint", "_checkpoint", "export", "_export"):
        subs.add_parser(name)
    command = subs.add_parser("import")
    command.add_argument("input")
    command.add_argument("--table")
    command = subs.add_parser("run")
    command.add_argument(
        "--max-sites",
        type=int,
        default=0,
        help="0 (default) = full saved queue; positive = explicit pilot claim limit",
    )
    command.add_argument(
        "--hours",
        type=float,
        default=0,
        help="0 (default) = no planned stop; Colab runtime limits still apply",
    )
    command.add_argument("--checkpoint-seconds", type=float, default=120)
    command.add_argument("--checkpoint-timeout", type=float, default=180)
    command.add_argument("--concurrency", type=int)
    command.add_argument("--delay", type=float)
    command.add_argument("--max-pages", type=int)
    command.add_argument("--max-depth", type=int)
    command.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True)
    command.add_argument(
        "--single-pass", action="store_true", help="Advanced: disable automatic follow-up passes"
    )
    command = subs.add_parser("retry")
    command.add_argument(
        "--status",
        choices=["no_email", "failed", "blocked", "completed", "retry"],
        action="append",
        required=True,
    )
    command.add_argument("--limit", type=int, default=100)
    command.add_argument("--fresh", action="store_true")
    command = subs.add_parser("doctor")
    command.add_argument("--check-browser", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        workspace = Workspace(
            args.work_dir, args.drive_dir, drive_mount=args.drive_mount, keep_snapshots=args.keep_snapshots
        )
        if args.action == "prepare":
            output = workspace.prepare()
        elif args.action == "status":
            output = workspace.status()
        elif args.action == "import":
            output = workspace.import_input(args.input, table=args.table)
        elif args.action == "_checkpoint":
            # Internal bounded child; the parent owns the long-lived session lock.
            output = workspace.checkpoint()
        elif args.action == "_export":
            output = workspace.export()
        elif args.action == "run":
            if not 30 <= args.checkpoint_seconds <= 1800 or not 30 <= args.checkpoint_timeout <= 600:
                raise ValueError("Use checkpoint intervals 30–1800s and timeouts 30–600s")
            if args.checkpoint_seconds + args.checkpoint_timeout >= LEASE_SECONDS - 60:
                raise ValueError("Checkpoint interval + timeout must fit inside the advisory session lease")
            settings = {
                k: getattr(args, k)
                for k in ("concurrency", "delay", "max_pages", "max_depth", "browser")
                if getattr(args, k) is not None
            }
            output = asyncio.run(
                run_session(
                    workspace,
                    max_sites=None if args.max_sites == 0 else args.max_sites,
                    settings=settings,
                    duration=args.hours * 3600,
                    automatic=not args.single_pass,
                    checkpoint_interval=args.checkpoint_seconds,
                    checkpoint_timeout=args.checkpoint_timeout,
                )
            )
        elif args.action == "pause":
            if not workspace.db.exists():
                raise ValueError("Run prepare first")
            with workspace.open_store() as store:
                store.control("pause")
            output = {
                "paused": True,
                "note": "Active sites finish; stopping the run cell also requests a final backup/export.",
            }
        elif args.action == "doctor":
            if args.check_browser:
                from .browser import BrowserRenderer
                from .http import Fetcher
                from .robots import Robots

                async def check_browser():
                    async with Fetcher(CrawlSettings()) as client:
                        renderer = BrowserRenderer(client, Robots(client))
                        try:
                            await renderer.check()
                        finally:
                            await renderer.close()

                asyncio.run(check_browser())
            output = {
                "python": sys.version.split()[0],
                "version": __version__,
                "workflow_version": WORKFLOW_VERSION,
                "free_disk_gib": round(shutil.disk_usage(workspace.work_dir).free / 1024**3, 2),
                "browser_launch_checked": args.check_browser,
                "persistent": bool(workspace.drive_dir),
                "note": "No target-site requests performed.",
            }
        else:
            with workspace.operation(), workspace.lease(), workspace.idle_worker():
                if args.action == "checkpoint":
                    output = workspace.checkpoint()
                elif args.action == "export":
                    output = workspace.export()
                    _atomic_json(workspace.work_dir / "last-export.json", output)
                elif args.action == "retry":
                    if not 1 <= args.limit <= 100_000:
                        raise ValueError("Retry limit must be 1–100,000")
                    if not workspace.db.exists():
                        raise ValueError("Run prepare first")
                    with workspace.open_store() as store:
                        store.control("pause")
                        store.recover_running()
                        count = store.retry_sites(args.status, fresh=args.fresh, limit=args.limit)
                    output = {"requeued": count, "checkpoint": workspace.checkpoint()}
        print(json.dumps(output, ensure_ascii=False, allow_nan=False), flush=True)
    except (ValueError, RuntimeError, OSError, sqlite3.Error, csv.Error, Timeout) as exc:
        print(
            f"Colab command failed: {exc}\nLocal progress is kept. Do not delete the work/Drive project folders.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
