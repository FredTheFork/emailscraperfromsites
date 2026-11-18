from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote, urlsplit

from .store import Store
from .urls import hostname, normalize_url, same_site

_INPUT_KEYS = ("domain", "url", "website", "website_url", "homepage")


def prepare_row(row: dict) -> dict[str, str]:
    row = {str(k).strip().lower().lstrip("\ufeff"): v for k, v in row.items() if k is not None}
    raw = next((str(row[k]).strip() for k in _INPUT_KEYS if row.get(k)), "")
    if not raw:
        raise ValueError("Missing domain/URL")
    parsed = normalize_url(raw if "://" in raw else "https://" + raw)
    domain = hostname(urlsplit(parsed).hostname or "")
    if domain.startswith("www.") and same_site(domain, domain[4:]):
        domain = domain[4:]  # Not www.com / www.co.uk / a distinct private-suffix tenant.
    supplied_url = str(row.get("url") or row.get("website") or row.get("website_url") or parsed).strip()
    try:
        start_url = normalize_url(supplied_url if "://" in supplied_url else "https://" + supplied_url)
        if not same_site(domain, start_url):
            start_url = parsed
    except ValueError:
        start_url = parsed
    metadata = {
        key[:100]: (value if isinstance(value, (int, float, bool)) else str(value or "")[:4000])
        for key, value in list(row.items())[:100]
    }
    return {
        "domain": domain,
        "input_url": start_url,
        "business_name": str(row.get("business_name") or row.get("name") or "")[:1000],
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    }


def _csv_rows(path: Path) -> Iterator[dict]:
    csv.field_size_limit(1_000_000)
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        sample = source.read(8192)
        source.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(source, dialect=dialect, strict=True)
        fields = {str(k).strip().lower().lstrip("\ufeff") for k in reader.fieldnames or []}
        if not fields.intersection(_INPUT_KEYS):
            raise ValueError("CSV needs a header named domain, url, website, website_url, or homepage")
        yield from reader


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_rows(path: Path, table: str | None) -> Iterator[dict]:
    # Open ONLY as an input. Never apply our schema/PRAGMAs to the user's database.
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        tables = [
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if table:
            if table not in tables:
                raise ValueError(f"Input table {table!r} does not exist")
            tables = [table]
        else:
            tables.sort(key=lambda name: name != "domains")
        selected = None
        for candidate in tables:
            fields = {
                r[1].lower() for r in connection.execute(f"PRAGMA table_info({_quote_identifier(candidate)})")
            }
            if fields.intersection(_INPUT_KEYS):
                selected = candidate
                break
        if not selected:
            raise ValueError("No table containing a domain, url, or website column was found")
        for row in connection.execute(f"SELECT * FROM {_quote_identifier(selected)}"):
            yield dict(row)
    finally:
        connection.close()


def import_file(store: Store, filename: str | Path, table: str | None = None) -> dict:
    path = Path(filename).expanduser().resolve()
    if path == store.path or path.exists() and path.samefile(store.path):
        raise ValueError("Input and state database must be different files")
    if not path.is_file():
        raise ValueError(f"Input file not found: {path.name}")
    extension = path.suffix.lower()
    if extension == ".csv":
        rows = _csv_rows(path)
    elif extension in {".db", ".sqlite", ".sqlite3"}:
        rows = _sqlite_rows(path, table)
    else:
        raise ValueError("Upload a .csv, .db, .sqlite, or .sqlite3 file")
    report = {
        "filename": path.name,
        "total": 0,
        "inserted": 0,
        "duplicates": 0,
        "invalid": 0,
        "invalid_examples": [],
    }
    batch = []

    def flush() -> None:
        inserted, duplicates = store.add_sites(batch)
        report["inserted"] += inserted
        report["duplicates"] += duplicates
        batch.clear()

    for number, row in enumerate(rows, start=2):
        report["total"] += 1
        try:
            batch.append(prepare_row(row))
        except (ValueError, UnicodeError) as exc:
            report["invalid"] += 1
            if len(report["invalid_examples"]) < 10:
                report["invalid_examples"].append({"row": number, "reason": str(exc)[:200]})
        if len(batch) >= 500:
            flush()
    if batch:
        flush()
    store.record_import(report)
    return report
