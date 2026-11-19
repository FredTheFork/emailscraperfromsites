from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import CrawlSettings
from .models import EmailHit, Link
from .urls import site_key

STATUSES = {"pending", "running", "retry", "completed", "no_email", "blocked", "failed"}
TERMINAL = {"completed", "no_email", "blocked", "failed"}
FREE_PROVIDERS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "ymail.com",
    "hotmail.com",
    "hotmail.co.uk",
    "outlook.com",
    "live.com",
    "live.co.uk",
    "msn.com",
    "aol.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "proton.me",
    "protonmail.com",
    "gmx.com",
    "gmx.co.uk",
    "mail.com",
    "btinternet.com",
    "btconnect.com",
    "sky.com",
    "talktalk.net",
    "virginmedia.com",
    "ntlworld.com",
    "blueyonder.co.uk",
    "tiscali.co.uk",
    "plus.net",
    "btopenworld.com",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    input_url TEXT NOT NULL,
    business_name TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    effective_url TEXT,
    http_status INTEGER,
    email_count INTEGER NOT NULL DEFAULT 0,
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    candidate_truncated INTEGER NOT NULL DEFAULT 0,
    completion_reason TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    imported_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS sites_queue ON sites(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS sites_email_count ON sites(email_count);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'html',
    role TEXT NOT NULL DEFAULT 'page',
    depth INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 200,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    discovered_from TEXT NOT NULL DEFAULT '',
    final_url TEXT,
    http_status INTEGER,
    content_type TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    checked_at REAL,
    browser_status TEXT NOT NULL DEFAULT '',
    UNIQUE(site_id, url)
);
CREATE INDEX IF NOT EXISTS pages_frontier ON pages(site_id, status, priority DESC, id);
CREATE TABLE IF NOT EXISTS email_addresses (
    email TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    email TEXT NOT NULL REFERENCES email_addresses(email),
    source_url TEXT NOT NULL,
    method TEXT NOT NULL,
    confidence TEXT NOT NULL,
    relationship TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY(site_id, email, source_url, method)
);
CREATE INDEX IF NOT EXISTS observations_email ON observations(email);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    site_id INTEGER REFERENCES sites(id) ON DELETE SET NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_cooldowns (
    site_key TEXT PRIMARY KEY,
    until_at REAL NOT NULL,
    http_status INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    created_at REAL NOT NULL,
    total INTEGER NOT NULL,
    inserted INTEGER NOT NULL,
    duplicates INTEGER NOT NULL,
    invalid INTEGER NOT NULL
);
"""


def relationship(email: str, domain: str) -> str:
    host = email.rsplit("@", 1)[-1]
    if same := site_key(host) == site_key(domain):
        return "on_domain"
    if not same and host in FREE_PROVIDERS:
        return "free_provider"
    return "external"  # Kept, but may belong to a designer/supplier, not the business.


class Store:
    """Small synchronous transactions; one runner, multiple API/CLI readers.

    Each completed page and all its email evidence commit atomically. No network
    operation occurs inside a transaction. SQLite lives on local disk, not NFS/Drive.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        # Serialize first-time schema creation across a simultaneous web/CLI
        # startup. Runtime runner ownership uses a separate long-lived lock.
        from filelock import FileLock

        with FileLock(str(self.path) + ".init.lock", timeout=30):
            try:
                self._initialize()
            except BaseException:
                if hasattr(self, "conn"):
                    self.conn.close()
                raise

    def _initialize(self) -> None:
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA trusted_schema=OFF")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        application = self.conn.execute("PRAGMA application_id").fetchone()[0]
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"meta", "sites", "pages", "email_addresses", "observations", "events", "imports"}
        if application not in {0, 0x504D4149} or tables and not expected.issubset(tables):
            self.conn.close()
            raise ValueError("This is an input database, not a Public Mail state DB. Use the import command.")
        if version not in {0, 1}:
            self.conn.close()
            raise ValueError(f"Unsupported database schema {version}; upgrade Public Mail first")
        # Identity is checked BEFORE persistent PRAGMAs: accidentally using an
        # input file as state must not even change its journal mode/header.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA application_id=0x504D4149")
        self.conn.execute("PRAGMA user_version=1")
        with self.transaction() as db:
            for key, value in {
                "settings": CrawlSettings().model_dump(),
                "mode": "paused",
                "run_remaining": None,
                "created_at": time.time(),
                "worker_state": "offline",
                "worker_heartbeat": 0,
            }.items():
                db.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set_meta(db: sqlite3.Connection, key: str, value: Any) -> None:
        db.execute(
            "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def set_meta(self, key: str, value: Any) -> None:
        with self.transaction() as db:
            self._set_meta(db, key, value)

    def get_cooldown(self, key: str) -> tuple[float, int]:
        with self.lock:
            row = self.conn.execute(
                "SELECT until_at,http_status FROM request_cooldowns WHERE site_key=?", (key,)
            ).fetchone()
        return (max(0, row[0] - time.time()), row[1]) if row else (0, 0)

    def put_cooldown(self, key: str, until_at: float, status: int) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO request_cooldowns VALUES (?,?,?) ON CONFLICT(site_key) DO UPDATE SET "
                "http_status=CASE WHEN excluded.until_at>=until_at THEN excluded.http_status ELSE http_status END,"
                "until_at=MAX(until_at,excluded.until_at)",
                (key, until_at, status),
            )

    def settings(self) -> CrawlSettings:
        return CrawlSettings.model_validate(self.get_meta("settings"))

    def save_settings(self, settings: CrawlSettings) -> None:
        with self.transaction() as db:
            mode = json.loads(db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0])
            active = db.execute("SELECT COUNT(*) FROM sites WHERE status='running'").fetchone()[0]
            if mode == "running" or active:
                raise ValueError("Pause and let active sites finish before changing settings")
            self._set_meta(db, "settings", settings.model_dump())

    def control(self, action: str, limit: int | None = None) -> None:
        if action not in {"start", "pause"}:
            raise ValueError("Action must be start or pause")
        if limit is not None and not 1 <= limit <= 100_000:
            raise ValueError("Run limit must be between 1 and 100,000 sites")
        with self.transaction() as db:
            self._set_meta(db, "mode", "running" if action == "start" else "paused")
            if action == "start":
                self._set_meta(db, "run_remaining", limit)
                self._set_meta(db, "run_started_at", time.time())
                self._set_meta(db, "worker_error", "")
        self.event(
            None,
            "info",
            f"{'Crawl requested' if action == 'start' else 'Pause requested'}"
            + (f" (up to {limit:,} site claims)" if limit else ""),
        )

    def add_sites(self, rows: Iterable[dict[str, str]]) -> tuple[int, int]:
        inserted = duplicate = 0
        now = time.time()
        with self.transaction() as db:
            for row in rows:
                cur = db.execute(
                    "INSERT OR IGNORE INTO sites(domain,input_url,business_name,metadata_json,imported_at,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        row["domain"],
                        row["input_url"],
                        row.get("business_name", "")[:1000],
                        row.get("metadata_json", "{}"),
                        now,
                        now,
                    ),
                )
                inserted += cur.rowcount
                duplicate += 1 - cur.rowcount
        return inserted, duplicate

    def record_import(self, report: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO imports(filename,created_at,total,inserted,duplicates,invalid) VALUES (?,?,?,?,?,?)",
                (
                    report["filename"],
                    time.time(),
                    report["total"],
                    report["inserted"],
                    report["duplicates"],
                    report["invalid"],
                ),
            )
        self.event(
            None,
            "info",
            f"Imported {report['inserted']:,} domains; "
            f"{report['duplicates']:,} duplicates, {report['invalid']:,} invalid",
        )

    def recover_running(self) -> int:
        """Called only AFTER obtaining the exclusive worker file lock."""
        now = time.time()
        with self.transaction() as db:
            db.execute("DELETE FROM request_cooldowns WHERE until_at<=?", (now,))
            result = db.execute(
                "UPDATE sites SET status='retry',next_attempt_at=?,lease_owner=NULL,lease_until=NULL,"
                "last_error='Previous worker stopped; resuming saved frontier',updated_at=? WHERE status='running'",
                (now, now),
            )
            db.execute("UPDATE pages SET status='pending' WHERE status='fetching'")
            # A killed attempt still counts as a network attempt, but must not
            # prevent its saved frontier from being recovered on the last attempt.
            db.execute(
                "UPDATE sites SET attempts=MAX(0,attempts-1) WHERE status='retry' AND next_attempt_at=?",
                (now,),
            )
        return result.rowcount

    def claim_site(self, owner: str, lease_seconds: float, max_attempts: int) -> dict | None:
        now = time.time()
        with self.transaction() as db:
            mode = json.loads(db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0])
            remaining = json.loads(
                db.execute("SELECT value FROM meta WHERE key='run_remaining'").fetchone()[0]
            )
            if mode != "running" or remaining == 0:
                return None
            # A lowered attempt setting must not leave an unclaimable retry
            # queue permanently reporting 'running'. Existing evidence stays.
            db.execute(
                "UPDATE sites SET status='failed',completion_reason='attempt_limit',finished_at=?,updated_at=? "
                "WHERE status='retry' AND attempts>=?",
                (now, now, max_attempts),
            )
            row = db.execute(
                "SELECT * FROM sites WHERE status IN ('pending','retry') AND next_attempt_at<=? "
                "AND attempts<? ORDER BY next_attempt_at,id LIMIT 1",
                (now, max_attempts),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE sites SET status='running',attempts=attempts+1,lease_owner=?,lease_until=?,"
                "started_at=COALESCE(started_at,?),finished_at=NULL,updated_at=? WHERE id=?",
                (owner, now + lease_seconds, now, now, row["id"]),
            )
            if remaining is not None:
                self._set_meta(db, "run_remaining", remaining - 1)
            data = dict(row)
            data.update(status="running", attempts=data["attempts"] + 1, lease_owner=owner)
            return data

    def heartbeat(self, owner: str, lease_seconds: float) -> None:
        now = time.time()
        with self.transaction() as db:
            self._set_meta(db, "worker_heartbeat", now)
            self._set_meta(db, "worker_state", "online")
            db.execute(
                "UPDATE sites SET lease_until=? WHERE lease_owner=? AND status='running'",
                (now + lease_seconds, owner),
            )

    def release_owner(self, owner: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE sites SET status='retry',next_attempt_at=0,lease_owner=NULL,lease_until=NULL,"
                "attempts=MAX(0,attempts-1),last_error='Worker stopped; safe to resume',updated_at=? "
                "WHERE lease_owner=? AND status='running'",
                (time.time(), owner),
            )
            db.execute("UPDATE pages SET status='pending' WHERE status='fetching'")
            self._set_meta(db, "worker_state", "offline")

    def finish_site(
        self, site_id: int, status: str, reason: str, error: str = "", retry_at: float = 0
    ) -> None:
        if status not in STATUSES - {"running", "pending"}:
            raise ValueError("Invalid completion status")
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "UPDATE sites SET status=?,completion_reason=?,last_error=?,next_attempt_at=?,updated_at=?,"
                "finished_at=?,lease_owner=NULL,lease_until=NULL WHERE id=?",
                (status, reason, error[:2000], retry_at, now, None if status == "retry" else now, site_id),
            )
            db.execute("UPDATE pages SET status='pending' WHERE site_id=? AND status='fetching'", (site_id,))
        if status in {"failed", "blocked", "retry"}:
            self.event(site_id, "warning", f"{status}: {reason}" + (f" — {error[:250]}" if error else ""))

    def set_home(self, site_id: int, page_id: int, url: str, status: int) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE sites SET effective_url=?,http_status=?,updated_at=? WHERE id=?",
                (url, status, time.time(), site_id),
            )
            db.execute(
                "UPDATE pages SET status='skipped',last_error='Alternate homepage not needed' "
                "WHERE site_id=? AND role='seed' AND id!=? AND status='pending'",
                (site_id, page_id),
            )

    def get_site(self, site_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _enqueue(
        db: sqlite3.Connection,
        site_id: int,
        links: Iterable[Link],
        depth: int,
        source: str,
        cap: int,
        role: str = "page",
    ) -> None:
        count = db.execute("SELECT COUNT(*) FROM pages WHERE site_id=?", (site_id,)).fetchone()[0]
        for link in links:
            exists = db.execute(
                "SELECT id FROM pages WHERE site_id=? AND url=?", (site_id, link.url)
            ).fetchone()
            if exists:
                db.execute(
                    "UPDATE pages SET priority=MAX(priority,?),depth=MIN(depth,?) WHERE id=? AND status='pending'",
                    (link.priority, depth, exists[0]),
                )
            elif count < cap:
                db.execute(
                    "INSERT INTO pages(site_id,url,kind,role,depth,priority,discovered_from) VALUES(?,?,?,?,?,?,?)",
                    (site_id, link.url, link.kind, role, depth, link.priority, source),
                )
                count += 1
            else:
                db.execute("UPDATE sites SET candidate_truncated=1 WHERE id=?", (site_id,))
                break

    def enqueue(
        self, site_id: int, links: Iterable[Link], depth: int, source: str, cap: int, role: str = "page"
    ) -> None:
        with self.transaction() as db:
            self._enqueue(db, site_id, links, depth, source, cap, role)

    def next_page(self, site_id: int, max_depth: int = 10) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM pages WHERE site_id=? AND status='pending' AND (depth<=? OR kind='sitemap') "
                "ORDER BY priority DESC,id LIMIT 1",
                (site_id, max_depth),
            ).fetchone()
            return dict(row) if row else None

    def reset_seed_errors(self, site_id: int) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE pages SET status='pending' WHERE site_id=? AND role='seed' AND status='error'",
                (site_id,),
            )

    def queue_state(self) -> tuple[int, int]:
        with self.lock:
            row = self.conn.execute(
                "SELECT SUM(status='pending'),SUM(status='retry') FROM sites WHERE status IN ('pending','retry')"
            ).fetchone()
            return row[0] or 0, row[1] or 0

    def begin_page(self, page_id: int) -> int:
        with self.transaction() as db:
            db.execute("UPDATE pages SET status='fetching',attempts=attempts+1 WHERE id=?", (page_id,))
            return db.execute("SELECT attempts FROM pages WHERE id=?", (page_id,)).fetchone()[0]

    def page_counts(self, site_id: int) -> dict:
        with self.lock:
            rows = self.conn.execute(
                "SELECT kind,status,COUNT(*) AS n FROM pages WHERE site_id=? GROUP BY kind,status",
                (site_id,),
            ).fetchall()
        counts = {"pages": 0, "pdf": 0, "sitemap": 0, "pending": 0, "skipped": 0}
        for row in rows:
            if row["status"] == "pending":
                counts["pending"] += row["n"]
            elif row["status"] == "skipped":
                counts["skipped"] += row["n"]
            elif row["status"] in {"done", "error", "blocked", "fetching"}:
                key = "sitemap" if row["kind"] == "sitemap" else "pages"
                counts[key] += row["n"]
                if row["kind"] == "pdf":
                    counts["pdf"] += row["n"]
        return counts

    @staticmethod
    def _save_hits(
        db: sqlite3.Connection, site_id: int, source_url: str, hits: Iterable[EmailHit], now: float
    ) -> None:
        domain = db.execute("SELECT domain FROM sites WHERE id=?", (site_id,)).fetchone()[0]
        for hit in hits:
            db.execute(
                "INSERT INTO email_addresses VALUES(?,?,?) ON CONFLICT(email) DO UPDATE SET last_seen=excluded.last_seen",
                (hit.email, now, now),
            )
            db.execute(
                "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(site_id,email,source_url,method) DO UPDATE SET "
                "last_seen=excluded.last_seen,context=excluded.context,confidence=excluded.confidence",
                (
                    site_id,
                    hit.email,
                    source_url,
                    hit.method,
                    hit.confidence,
                    relationship(hit.email, domain),
                    hit.context[:240],
                    now,
                    now,
                ),
            )
        db.execute(
            "UPDATE sites SET email_count=(SELECT COUNT(DISTINCT email) FROM observations WHERE site_id=?),"
            "updated_at=? WHERE id=?",
            (site_id, now, site_id),
        )

    def save_page(
        self,
        page_id: int,
        *,
        status: str,
        final_url: str = "",
        http_status: int | None = None,
        content_type: str = "",
        size: int = 0,
        title: str = "",
        error: str = "",
        hits: Iterable[EmailHit] = (),
        kind: str | None = None,
        home: bool = False,
        links: Iterable[Link] = (),
        link_depth: int = 1,
        cap: int = 250,
    ) -> None:
        now = time.time()
        with self.transaction() as db:
            page = db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
            if page["role"] == "seed" and http_status is not None:
                db.execute("UPDATE sites SET http_status=? WHERE id=?", (http_status, page["site_id"]))
            self._save_hits(db, page["site_id"], final_url or page["url"], hits, now)
            db.execute(
                "UPDATE pages SET status=?,final_url=?,http_status=?,content_type=?,bytes=?,title=?,last_error=?,"
                "checked_at=?,kind=COALESCE(?,kind) WHERE id=?",
                (
                    status,
                    final_url or page["url"],
                    http_status,
                    content_type,
                    size,
                    title[:500],
                    error[:2000],
                    now,
                    kind,
                    page_id,
                ),
            )
            # Commit discovery and evidence WITH the completed page. A crash can
            # never mark a page done while losing its newly discovered frontier.
            self._enqueue(db, page["site_id"], links, link_depth, final_url or page["url"], cap)
            if home:
                db.execute(
                    "UPDATE sites SET effective_url=?,http_status=? WHERE id=?",
                    (final_url or page["url"], http_status, page["site_id"]),
                )
                db.execute(
                    "UPDATE pages SET status='skipped',last_error='Alternate homepage not needed' "
                    "WHERE site_id=? AND role='seed' AND id!=? AND status='pending'",
                    (page["site_id"], page_id),
                )
            db.execute(
                "UPDATE sites SET pages_scanned=(SELECT COUNT(*) FROM pages WHERE site_id=? "
                "AND status='done' AND kind!='sitemap') WHERE id=?",
                (page["site_id"], page["site_id"]),
            )

    def browser_candidates(self, site_id: int, limit: int) -> list[dict]:
        with self.lock:
            return [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM pages WHERE site_id=? AND status='done' AND kind='html' AND browser_status='' "
                    "ORDER BY (role='seed'),priority DESC,id LIMIT ?",
                    (site_id, limit),
                )
            ]

    def save_render(
        self,
        page_id: int,
        hits: Iterable[EmailHit],
        error: str = "",
        *,
        links: Iterable[Link] = (),
        cap: int = 250,
        source_url: str = "",
    ) -> None:
        now = time.time()
        with self.transaction() as db:
            page = db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
            source_url = source_url or page["final_url"] or page["url"]
            self._save_hits(db, page["site_id"], source_url, hits, now)
            self._enqueue(db, page["site_id"], links, page["depth"] + 1, source_url, cap)
            db.execute(
                "UPDATE pages SET browser_status=? WHERE id=?",
                (("error: " + error[:250]) if error else "rendered", page_id),
            )

    def retry_sites(
        self,
        statuses: list[str],
        *,
        fresh: bool = False,
        limit: int | None = None,
        ids: list[int] | None = None,
    ) -> int:
        if not statuses or set(statuses) - (TERMINAL | {"retry"}):
            raise ValueError("Only completed/no_email/failed/blocked/retry sites can be requeued")
        placeholders = ",".join("?" for _ in statuses)
        args: list[Any] = list(statuses)
        condition = f"status IN ({placeholders})"
        if ids:
            condition += " AND id IN (" + ",".join("?" for _ in ids) + ")"
            args.extend(ids)
        with self.transaction() as db:
            selected = [
                r[0]
                for r in db.execute(
                    f"SELECT id FROM sites WHERE {condition} ORDER BY id LIMIT ?",
                    (*args, limit or -1),
                )
            ]
            for site_id in selected:
                db.execute(
                    "UPDATE sites SET status='pending',attempts=0,next_attempt_at=0,finished_at=NULL,"
                    "completion_reason='',last_error='',updated_at=? WHERE id=?",
                    (time.time(), site_id),
                )
                if fresh:
                    db.execute("DELETE FROM pages WHERE site_id=?", (site_id,))
                    db.execute(
                        "UPDATE sites SET effective_url=NULL,pages_scanned=0,candidate_truncated=0 WHERE id=?",
                        (site_id,),
                    )
                else:
                    db.execute(
                        "UPDATE pages SET status='pending',attempts=0,browser_status='' "
                        "WHERE site_id=? AND status IN ('error','blocked','fetching')",
                        (site_id,),
                    )
        self.event(
            None,
            "info",
            f"Requeued {len(selected):,} sites ({'fresh pages' if fresh else 'continue frontier'}); evidence retained",
        )
        return len(selected)

    def event(self, site_id: int | None, level: str, message: str) -> None:
        with self.transaction() as db:
            cur = db.execute(
                "INSERT INTO events(created_at,site_id,level,message) VALUES(?,?,?,?)",
                (time.time(), site_id, level, message[:2000]),
            )
            if cur.lastrowid % 100 == 0:
                db.execute("DELETE FROM events WHERE id<?", (cur.lastrowid - 10_000,))

    def stats(self) -> dict:
        with self.lock:
            statuses = {s: 0 for s in STATUSES}
            statuses.update(
                {r[0]: r[1] for r in self.conn.execute("SELECT status,COUNT(*) FROM sites GROUP BY status")}
            )
            total = sum(statuses.values())
            with_email = self.conn.execute("SELECT COUNT(*) FROM sites WHERE email_count>0").fetchone()[0]
            unique = self.conn.execute("SELECT COUNT(*) FROM email_addresses").fetchone()[0]
            pairs = self.conn.execute("SELECT COALESCE(SUM(email_count),0) FROM sites").fetchone()[0]
            pages = self.conn.execute("SELECT COALESCE(SUM(pages_scanned),0) FROM sites").fetchone()[0]
            next_retry = self.conn.execute(
                "SELECT MIN(next_attempt_at) FROM sites WHERE status='retry'"
            ).fetchone()[0]
            recent = self.conn.execute(
                "SELECT COUNT(*) FROM sites WHERE finished_at>?",
                (time.time() - 3600,),
            ).fetchone()[0]
            last_import = self.conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 1").fetchone()
            meta = {r[0]: json.loads(r[1]) for r in self.conn.execute("SELECT key,value FROM meta")}
        terminal = sum(statuses[s] for s in TERMINAL)
        return {
            "total": total,
            "with_email": with_email,
            "unique_emails": unique,
            "site_email_pairs": pairs,
            "pages_scanned": pages,
            "terminal": terminal,
            "coverage_percent": round(100 * with_email / total, 2) if total else 0,
            "processed_percent": round(100 * terminal / total, 2) if total else 0,
            "statuses": statuses,
            "next_retry_at": next_retry,
            "finished_last_hour": recent,
            "mode": meta.get("mode", "paused"),
            "run_remaining": meta.get("run_remaining"),
            "worker_state": meta.get("worker_state"),
            "worker_heartbeat": meta.get("worker_heartbeat", 0),
            "worker_error": meta.get("worker_error", ""),
            "run_started_at": meta.get("run_started_at"),
            "last_import": dict(last_import) if last_import else None,
        }

    def list_sites(self, q: str = "", status: str = "", page: int = 1, size: int = 50) -> dict:
        if status and status not in STATUSES:
            raise ValueError("Invalid site status")
        where, args = ["1=1"], []
        if status:
            where.append("status=?")
            args.append(status)
        if q:
            where.append("(domain LIKE ? ESCAPE '\\' OR business_name LIKE ? ESCAPE '\\')")
            query = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend([query, query])
        clause = " AND ".join(where)
        with self.lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM sites WHERE {clause}", args).fetchone()[0]
            rows = self.conn.execute(
                "SELECT id,domain,input_url,business_name,status,attempts,email_count,pages_scanned,"
                "completion_reason,last_error,effective_url,updated_at,next_attempt_at,finished_at,candidate_truncated "
                f"FROM sites WHERE {clause} ORDER BY id LIMIT ? OFFSET ?",
                (*args, size, (page - 1) * size),
            ).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "page": page, "size": size}

    def list_emails(self, q: str = "", page: int = 1, size: int = 50) -> dict:
        query = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clause = "o.email LIKE ? ESCAPE '\\' OR s.domain LIKE ? ESCAPE '\\'"
        with self.lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM observations o JOIN sites s ON s.id=o.site_id WHERE {clause} GROUP BY o.site_id,o.email)",
                (query, query),
            ).fetchone()[0]
            rows = self.conn.execute(
                "SELECT o.email,s.domain,s.business_name,o.relationship,MIN(o.source_url) AS source_url,"
                "COUNT(DISTINCT o.source_url) AS sources,GROUP_CONCAT(DISTINCT o.method) AS methods,MAX(o.last_seen) AS last_seen "
                f"FROM observations o JOIN sites s ON s.id=o.site_id WHERE {clause} "
                "GROUP BY o.site_id,o.email ORDER BY MAX(o.last_seen) DESC,o.email LIMIT ? OFFSET ?",
                (query, query, size, (page - 1) * size),
            ).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "page": page, "size": size}

    def events(self, limit: int = 30) -> list[dict]:
        with self.lock:
            return [
                dict(r)
                for r in self.conn.execute(
                    "SELECT e.*,s.domain FROM events e LEFT JOIN sites s ON s.id=e.site_id ORDER BY e.id DESC LIMIT ?",
                    (min(limit, 200),),
                )
            ]

    def site_pages(self, site_id: int) -> list[dict]:
        with self.lock:
            return [
                dict(r)
                for r in self.conn.execute("SELECT * FROM pages WHERE site_id=? ORDER BY id", (site_id,))
            ]

    def backup(self, output: str | Path) -> Path:
        path = Path(output).expanduser().resolve()
        if path == self.path or path.exists() and path.samefile(self.path):
            raise ValueError("Backup destination must not be the working database (including aliases)")
        path.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(path)
        try:
            with self.lock:
                self.conn.backup(destination, pages=256)
        finally:
            destination.close()
        return path
