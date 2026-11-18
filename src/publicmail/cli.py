from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys

from . import __version__
from .engine import Engine
from .exporter import write_exports
from .importer import import_file
from .store import Store


async def _run(store: Store, args: argparse.Namespace) -> None:
    engine = Engine(store)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.stop)
        except NotImplementedError:
            pass
    overrides = {
        key: getattr(args, key)
        for key in (
            "concurrency",
            "delay",
            "max_pages",
            "max_depth",
            "site_timeout",
            "request_timeout",
            "browser",
            "user_agent",
        )
        if getattr(args, key) is not None
    }
    await engine.run(service=False, start=True, limit=args.max_sites, overrides=overrides)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="publicmail", description="Evidence-first, resumable public website email collection"
    )
    result.add_argument("--version", action="version", version=f"Public Mail {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=os.environ.get("SCRAPER_DB", "data/harvest.db"),
        help="State DB on local disk (default: data/harvest.db)",
    )
    subs = result.add_subparsers(dest="command", required=True)
    command = subs.add_parser(
        "import", parents=[common], help="Read a CSV or input SQLite DB; safe to repeat"
    )
    command.add_argument("input")
    command.add_argument("--table", help="SQLite input table (default: auto-detect, preferring domains)")
    command = subs.add_parser(
        "serve", parents=[common], help="Web dashboard and one persistent background runner"
    )
    command.add_argument("--host", default="0.0.0.0")
    command.add_argument("--port", type=int, default=8000)
    command = subs.add_parser(
        "run", parents=[common], help="Run in the terminal until drained, paused, or a pilot limit is reached"
    )
    command.add_argument(
        "--max-sites", type=int, help="Limit site claims (including retries); useful for a pilot"
    )
    command.add_argument("--concurrency", type=int)
    command.add_argument("--delay", type=float)
    command.add_argument("--max-pages", type=int)
    command.add_argument("--max-depth", type=int)
    command.add_argument("--site-timeout", type=float)
    command.add_argument("--request-timeout", type=float)
    command.add_argument("--browser", action=argparse.BooleanOptionalAction, default=None)
    command.add_argument("--user-agent")
    command = subs.add_parser("status", parents=[common], help="Print coverage and worker health")
    command.add_argument("--json", action="store_true")
    subs.add_parser("pause", parents=[common], help="Stop new claims; let active sites finish")
    command = subs.add_parser(
        "retry", parents=[common], help="Explicitly requeue selected outcomes; retain existing evidence"
    )
    command.add_argument(
        "--status", action="append", help="Repeat for multiple statuses; default: failed and no_email"
    )
    command.add_argument(
        "--fresh", action="store_true", help="Refetch pages instead of continuing the saved frontier"
    )
    command.add_argument("--limit", type=int)
    command = subs.add_parser(
        "export",
        parents=[common],
        help="Export emails, all evidence, unique addresses, coverage, and summary",
    )
    command.add_argument("--out", default="exports")
    command.add_argument(
        "--jsonl", action="store_true", help="Also write exact, non-spreadsheet-escaped observations.jsonl"
    )
    command = subs.add_parser(
        "backup", parents=[common], help="Create a consistent SQLite backup even during a live run"
    )
    command.add_argument("--output", required=True)
    subs.add_parser(
        "doctor", parents=[common], help="Check state database integrity, disk, and optional browser package"
    )
    return result


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "serve":
        import uvicorn

        from .web import create_app

        app = create_app(args.db)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                workers=1,
                proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
            )
        )
        app.state.on_worker_failure = lambda: setattr(server, "should_exit", True)
        server.run()
        if getattr(app.state, "worker_failed", False) or not server.started:
            raise SystemExit(1)
        return
    store = None
    try:
        store = Store(args.db)
        if args.command == "import":
            print(json.dumps(import_file(store, args.input, args.table), indent=2))
        elif args.command == "run":
            if args.max_sites is not None and not 1 <= args.max_sites <= 100_000:
                raise ValueError("--max-sites must be between 1 and 100,000")
            asyncio.run(_run(store, args))
            print(json.dumps(store.stats(), indent=2))
        elif args.command == "pause":
            store.control("pause")
            print("Pause requested. Active sites finish and checkpoint before stopping.")
        elif args.command == "status":
            stats = store.stats()
            if args.json:
                print(json.dumps(stats, indent=2))
            else:
                print(
                    f"Public Mail · {store.path}\n"
                    f"Mode: {stats['mode']} | worker: {stats['worker_state']}\n"
                    f"Domains: {stats['total']:,} | finished: {stats['terminal']:,} | with email: {stats['with_email']:,}\n"
                    f"Unique emails: {stats['unique_emails']:,} | domain-email pairs: {stats['site_email_pairs']:,}\n"
                    f"Pages scanned: {stats['pages_scanned']:,} | coverage: {stats['coverage_percent']}%\n"
                    f"Outcomes: {json.dumps(stats['statuses'], sort_keys=True)}"
                )
                if stats["worker_error"]:
                    print("Worker error:", stats["worker_error"])
        elif args.command == "retry":
            if args.limit is not None and args.limit < 1:
                raise ValueError("--limit must be positive")
            if store.get_meta("mode") == "running" or store.stats()["statuses"]["running"]:
                raise ValueError("Pause and let active sites finish before requeuing")
            print(
                f"Requeued {store.retry_sites(args.status or ['failed', 'no_email'], fresh=args.fresh, limit=args.limit):,} sites"
            )
        elif args.command == "export":
            for path in write_exports(store, args.out, jsonl=args.jsonl):
                print(path)
        elif args.command == "backup":
            print(store.backup(args.output))
        elif args.command == "doctor":
            with store.lock:
                integrity = store.conn.execute("PRAGMA quick_check").fetchone()[0]
            print(
                json.dumps(
                    {
                        "database": str(store.path),
                        "integrity": integrity,
                        "free_disk_gib": round(shutil.disk_usage(store.path.parent).free / (1024**3), 2),
                        "browser_package_installed": importlib.util.find_spec("playwright") is not None,
                        "python": sys.version.split()[0],
                        "settings": store.settings().model_dump(),
                        "note": "No network requests performed. Browser binaries and real-site yield require a deployment test.",
                    },
                    indent=2,
                )
            )
            if integrity != "ok":
                raise ValueError("Database integrity check failed; restore a known-good backup")
    except (ValueError, RuntimeError, OSError, sqlite3.Error, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
