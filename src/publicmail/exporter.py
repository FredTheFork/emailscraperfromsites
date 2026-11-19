from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .store import Store

QUERIES = {
    "emails": """
        SELECT s.domain,s.business_name,o.email,o.relationship,
               CASE WHEN MAX(o.confidence='high') THEN 'high' ELSE 'medium' END AS confidence,
               MIN(o.source_url) AS source_url,COUNT(DISTINCT o.source_url) AS source_count,
               GROUP_CONCAT(DISTINCT o.method) AS methods,
               MIN(o.first_seen) AS first_seen,MAX(o.last_seen) AS last_seen,s.status AS site_status
        FROM observations o JOIN sites s ON s.id=o.site_id
        GROUP BY o.site_id,o.email ORDER BY s.id,o.email
    """,
    "observations": """
        SELECT s.domain,s.business_name,o.email,o.relationship,o.source_url,o.method,o.confidence,
               o.context,o.first_seen,o.last_seen
        FROM observations o JOIN sites s ON s.id=o.site_id ORDER BY s.id,o.email,o.source_url,o.method
    """,
    "unique_emails": """
        SELECT e.email,COUNT(DISTINCT o.site_id) AS website_count,MIN(o.source_url) AS source_url,
               e.first_seen,e.last_seen
        FROM email_addresses e JOIN observations o ON o.email=e.email GROUP BY e.email ORDER BY e.email
    """,
    "coverage": """
        SELECT s.domain,s.business_name,s.input_url,s.effective_url,s.status,s.completion_reason,
               s.email_count,s.pages_scanned,s.attempts,s.http_status,s.candidate_truncated,
               (SELECT COUNT(*) FROM pages p WHERE p.site_id=s.id AND p.status='pending') AS remaining_pages,
               (SELECT COUNT(*) FROM pages p WHERE p.site_id=s.id AND p.status IN ('error','blocked')) AS pages_with_errors,
               (SELECT COUNT(*) FROM pages p WHERE p.site_id=s.id AND p.status='skipped'
                 AND p.last_error!='Alternate homepage not needed') AS skipped_pages,
               (SELECT COUNT(*) FROM pages p WHERE p.site_id=s.id AND p.last_error='Disallowed by robots.txt') AS robots_disallowed_pages,
               (SELECT COUNT(*) FROM pages p WHERE p.site_id=s.id AND p.browser_status LIKE 'error:%') AS browser_errors,
               s.last_error,s.next_attempt_at,s.imported_at,s.started_at,s.finished_at,s.updated_at
        FROM sites s ORDER BY s.id
    """,
}
_TIME_FIELDS = {
    "first_seen",
    "last_seen",
    "next_attempt_at",
    "imported_at",
    "started_at",
    "finished_at",
    "updated_at",
}


def _iso(value) -> str:
    try:
        return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds") if value else ""
    except (ValueError, OverflowError, OSError):
        # Absurd server Retry-After values must not make a coverage export fail.
        return "outside_supported_date_range"


def spreadsheet_safe(value):
    # Quoting does not prevent CSV formula injection in spreadsheet software.
    if isinstance(value, str) and value.lstrip(" \r\n").startswith(("=", "+", "-", "@", "\t")):
        return "'" + value
    return value if value is not None else ""


def export_rows(store: Store, kind: str) -> Iterator[dict]:
    if kind not in QUERIES:
        raise ValueError("Unknown export type")
    conn = sqlite3.connect(
        f"file:{quote(str(store.path))}?mode=ro", uri=True, check_same_thread=False, timeout=30
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        cursor = conn.execute(QUERIES[kind])
        columns = [column[0] for column in cursor.description]
        # A sentinel header also makes an empty crawl export valid, with headers.
        yield {"_fields": columns}
        while rows := cursor.fetchmany(500):
            for row in rows:
                yield {key: _iso(row[key]) if key in _TIME_FIELDS else row[key] for key in columns}
    finally:
        conn.close()


def csv_chunks(store: Store, kind: str) -> Iterator[str]:
    rows = export_rows(store, kind)
    try:
        fields = next(rows)["_fields"]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        yield "\ufeff" + buffer.getvalue()  # Excel-compatible UTF-8, not a locale-dependent encoding.
        buffer.seek(0)
        buffer.truncate(0)
        for index, row in enumerate(rows, 1):
            writer.writerow({key: spreadsheet_safe(value) for key, value in row.items()})
            if index % 250 == 0:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        if buffer.tell():
            yield buffer.getvalue()
    finally:
        rows.close()


def write_exports(store: Store, output: str | Path, *, jsonl: bool = False) -> list[Path]:
    directory = Path(output).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for kind in QUERIES:
        path = directory / f"{kind}.csv"
        with path.open("w", encoding="utf-8", newline="") as target:
            target.writelines(csv_chunks(store, kind))
        written.append(path)
    if jsonl:
        path = directory / "observations.jsonl"
        rows = export_rows(store, "observations")
        next(rows)
        with path.open("w", encoding="utf-8") as target:
            for row in rows:
                target.write(json.dumps(row, ensure_ascii=False) + "\n")
        written.append(path)
    summary = directory / "summary.json"
    summary.write_text(json.dumps(store.stats(), indent=2) + "\n", encoding="utf-8")
    written.append(summary)
    return written
