"""Automatic coverage passes and honest, address-count-independent completion.

The plan and each requeue transition commit in the SAME SQLite transaction.
Resuming a snapshot cannot silently restart a finished pass or skip its frontier.
Access denials are never turned into unrestricted retries.
"""

from __future__ import annotations

import json
import time

from .config import CrawlSettings
from .store import Store

PLAN_KEY = "automatic_coverage_plan"
WORKFLOW_VERSION = 2
STAGES = ("whole_list", "deeper_contacts", "transient_recovery")


def automatic_profiles(browser: bool = True) -> tuple[dict, ...]:
    common = dict(
        concurrency=12,
        delay=1.5,
        browser=browser,
        browser_contact_pages=True,
        browser_concurrency=2,
        request_timeout=20,
        max_attempts=3,
    )
    first = dict(
        common,
        max_pages=40,
        max_depth=3,
        max_candidates=400,
        site_timeout=360,
        browser_pages=2,
        max_pdf_documents=4,
        pdf_pages=30,
        max_sitemaps=6,
        sitemap_urls=4000,
    )
    deeper = dict(
        common,
        max_pages=80,
        max_depth=5,
        max_candidates=800,
        site_timeout=600,
        browser_pages=4,
        max_pdf_documents=8,
        pdf_pages=60,
        max_sitemaps=10,
        sitemap_urls=8000,
    )
    recovery = dict(deeper, max_attempts=2)
    return tuple(CrawlSettings.model_validate(p).model_dump() for p in (first, deeper, recovery))


def ensure_plan(store: Store) -> dict:
    stats = store.stats()
    if not stats["total"]:
        raise ValueError("Import a website list before starting automatic collection")
    with store.transaction() as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", (PLAN_KEY,)).fetchone()
        plan = json.loads(row[0]) if row else None
        if plan and plan.get("version") != WORKFLOW_VERSION:
            raise ValueError("Unsupported automatic plan version; update the notebook, do not reset the data")
        remaining = sum(stats["statuses"][s] for s in ("pending", "retry", "running"))
        if not plan or plan["input_total"] != stats["total"] or plan["state"] == "finished" and remaining:
            plan = {
                "version": WORKFLOW_VERSION,
                "generation": (plan or {}).get("generation", 0) + 1,
                "input_total": stats["total"],
                "stage_index": 0,
                "prepared": False,
                "state": "running",
                "started_at": time.time(),
                "stages": [{"name": name, "state": "pending"} for name in STAGES],
            }
            Store._set_meta(db, PLAN_KEY, plan)
        Store._set_meta(db, "collection_run_kind", "automatic")
    return plan


def prepare_stage(store: Store) -> dict:
    """Only call while idle and owning the notebook operation/worker guards."""
    with store.transaction() as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", (PLAN_KEY,)).fetchone()
        plan = json.loads(row[0])
        index = plan["stage_index"]
        if index >= len(STAGES) or plan["prepared"]:
            return plan
        if db.execute("SELECT COUNT(*) FROM sites WHERE status='running'").fetchone()[0]:
            raise ValueError("Stop active workers before preparing a coverage pass")
        selected = []
        if index == 1:
            # No-email sites get ONE fresh, deeper check. Sites with evidence
            # continue only if their trail shows limits/errors/unrendered contacts.
            selected = db.execute("""
                SELECT s.id,s.status,s.candidate_truncated FROM sites s
                WHERE s.status='no_email' OR s.status='completed' AND (
                    s.candidate_truncated=1 OR s.completion_reason IN ('page_limit','depth_limit','site_timeout')
                    OR EXISTS (SELECT 1 FROM pages p WHERE p.site_id=s.id AND (
                        p.status IN ('pending','fetching') OR p.status='error' AND
                            (p.http_status IS NULL OR p.http_status>=500 OR p.http_status IN (408,429))
                        OR p.last_error IN ('sitemap_limit','pdf_limit')
                        OR p.last_error LIKE 'PDF text/page limit%'))
                    OR EXISTS (SELECT 1 FROM pages p WHERE p.site_id=s.id AND p.status='done'
                        AND p.kind='html' AND (p.role='seed' OR p.priority>=650)
                        AND (p.browser_status='' OR p.browser_status LIKE 'error:%'))
                ) ORDER BY s.id
            """).fetchall()
        elif index == 2:
            # A finite follow-up, not endless count chasing. Persistent website
            # cooldowns still apply, including to an exhausted 429 queue.
            selected = db.execute("""
                SELECT id,status,candidate_truncated FROM sites
                WHERE status='failed' AND completion_reason IN
                    ('unreachable','network','http_server','robots_unavailable','site_timeout','attempt_limit')
                   OR status='blocked' AND completion_reason='rate_limited'
                ORDER BY id
            """).fetchall()
        fresh_count = 0
        for site in selected:
            refresh = index == 1 and (site["status"] == "no_email" or site["candidate_truncated"])
            fresh_count += bool(refresh)
            # Keep denied paths and their evidence, even when refreshing public
            # pages. Never retry a 401/403, robots denial or access challenge.
            db.execute(
                "UPDATE sites SET status='pending',attempts=0,finished_at=NULL,"
                "completion_reason='',last_error='',updated_at=? WHERE id=?",
                (time.time(), site["id"]),
            )
            if refresh:
                db.execute(
                    "UPDATE pages SET status='pending',attempts=0,browser_status='' "
                    "WHERE site_id=? AND status='done'",
                    (site["id"],),
                )
                db.execute("UPDATE sites SET candidate_truncated=0 WHERE id=?", (site["id"],))
            db.execute(
                "UPDATE pages SET status='pending',attempts=0,browser_status='' "
                "WHERE site_id=? AND (status='fetching' OR status='error' AND "
                "(http_status IS NULL OR http_status>=500 OR http_status IN (408,429)) "
                "OR status='blocked' AND http_status=429 "
                "OR status='skipped' AND last_error IN ('sitemap_limit','pdf_limit') "
                "OR status='done' AND last_error LIKE 'PDF text/page limit%')",
                (site["id"],),
            )
            if index == 2:
                # An unreachable homepage may simply have been an ordinary 404.
                # Give those seed URLs the single recovery round too; never a
                # denied/challenged seed (blocked sites are not selected above).
                db.execute(
                    "UPDATE pages SET status='pending',attempts=0 WHERE site_id=? "
                    "AND role='seed' AND status='error'",
                    (site["id"],),
                )
            db.execute(
                "UPDATE pages SET browser_status='' WHERE site_id=? AND browser_status LIKE 'error:%'",
                (site["id"],),
            )
        plan["prepared"] = True
        plan["state"] = "running"
        queued = db.execute("SELECT COUNT(*) FROM sites WHERE status IN ('pending','retry')").fetchone()[0]
        plan["stages"][index].update(
            state="running", selected=queued, refreshed=fresh_count, started_at=time.time()
        )
        Store._set_meta(db, PLAN_KEY, plan)
    return plan


def finish_stage(store: Store) -> dict:
    stats = store.stats()
    if any(stats["statuses"][s] for s in ("pending", "retry", "running")):
        raise ValueError("Cannot complete a coverage stage while its queue is unfinished")
    with store.transaction() as db:
        plan = json.loads(db.execute("SELECT value FROM meta WHERE key=?", (PLAN_KEY,)).fetchone()[0])
        index = plan["stage_index"]
        plan["stages"][index].update(
            state="finished",
            finished_at=time.time(),
            terminal=stats["terminal"],
            with_email=stats["with_email"],
            unique_emails=stats["unique_emails"],
        )
        plan["stage_index"] += 1
        plan["prepared"] = False
        if plan["stage_index"] == len(STAGES):
            plan.update(state="finished", finished_at=time.time())
        Store._set_meta(db, PLAN_KEY, plan)
    return plan


def collection_report(store: Store) -> dict:
    stats = store.stats()
    remaining = sum(stats["statuses"][s] for s in ("pending", "retry", "running"))
    kind = store.get_meta("collection_run_kind", "standard")
    plan = store.get_meta(PLAN_KEY) if kind == "automatic" else None
    all_attempted = bool(stats["total"]) and stats["sites_unstarted"] == 0
    queue_done = bool(stats["total"]) and remaining == 0
    automatic_done = bool(
        plan
        and plan.get("version") == WORKFLOW_VERSION
        and plan.get("state") == "finished"
        and plan.get("input_total") == stats["total"]
        and len(plan.get("stages", [])) == len(STAGES)
        and all(stage.get("state") == "finished" for stage in plan["stages"])
    )
    gaps = stats["statuses"]["no_email"] + stats["statuses"]["failed"] + stats["statuses"]["blocked"]
    if not stats["total"]:
        state = "EMPTY"
    elif kind == "pilot":
        state = "PILOT_ONLY"
    elif not queue_done or not all_attempted or kind == "automatic" and not automatic_done:
        state = "INCOMPLETE"
    else:
        state = "COMPLETE_WITH_GAPS" if gaps else "COMPLETE_PASS"
    with store.lock:
        reasons = {
            r[0] or "not_finished": r[1]
            for r in store.conn.execute(
                "SELECT completion_reason,COUNT(*) FROM sites GROUP BY completion_reason"
            )
        }
        http = {
            str(r[0]) if r[0] is not None else "no_status": r[1]
            for r in store.conn.execute("SELECT http_status,COUNT(*) FROM sites GROUP BY http_status")
        }
        errors = {
            r[0]: r[1]
            for r in store.conn.execute(
                "SELECT status,COUNT(*) FROM pages WHERE status IN ('error','blocked','skipped') GROUP BY status"
            )
        }
        browser_errors = store.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE browser_status LIKE 'error:%'"
        ).fetchone()[0]
        limited = store.conn.execute(
            "SELECT COUNT(*) FROM sites WHERE candidate_truncated=1 OR "
            "completion_reason IN ('page_limit','depth_limit','site_timeout')"
        ).fetchone()[0]
        remaining_pages = store.conn.execute("SELECT COUNT(*) FROM pages WHERE status='pending'").fetchone()[
            0
        ]
        finite_skips = store.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE last_error IN ('sitemap_limit','pdf_limit')"
        ).fetchone()[0]
        meaningful_skips = store.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status='skipped' AND last_error!='Alternate homepage not needed'"
        ).fetchone()[0]
    if state == "COMPLETE_PASS" and (
        limited
        or browser_errors
        or remaining_pages
        or finite_skips
        or meaningful_skips
        or errors.get("error")
        or errors.get("blocked")
    ):
        state = "COMPLETE_WITH_GAPS"
    return {
        "state": state,
        "requested_mode": kind,
        "stop_reason": store.get_meta("collection_stop_reason", "not_recorded"),
        "completion": {
            "input_websites": stats["total"],
            "sites_started": stats["sites_started"],
            "sites_unstarted": stats["sites_unstarted"],
            "queue_remaining": remaining,
            "all_sites_attempted": all_attempted,
            "queue_drained": queue_done,
            "automatic_passes_finished": automatic_done,
            "exhaustive_web_coverage_guaranteed": False,
        },
        "results": {
            k: stats[k] for k in ("with_email", "unique_emails", "site_email_pairs", "pages_scanned")
        },
        "outcomes": stats["statuses"],
        "automatic_plan": plan,
        "diagnostics": {
            "site_reasons": reasons,
            "site_http_statuses": http,
            "page_outcomes": errors,
            "browser_errors": browser_errors,
            "limited_sites": limited,
            "remaining_pages": remaining_pages,
            "document_sitemap_limit_skips": finite_skips,
            "skipped_content_pages": meaningful_skips,
        },
        "next_step": "Resume automatic collection using the SAME project; do not reset or re-upload the list."
        if state in {"PILOT_ONLY", "INCOMPLETE"}
        else "Review coverage.csv for blocked, failed, no-email and finite-limit outcomes.",
        "note": "A website count is not a guaranteed address count. Completed means evidence was found, not every possible page exhausted.",
    }


def report_text(report: dict) -> str:
    c, r = report["completion"], report["results"]
    return "\n".join(
        [
            "PUBLIC MAIL COLLECTION STATUS: " + report["state"],
            "NOT A FINISHED FULL-LIST RESULT"
            if report["state"] in {"PILOT_ONLY", "INCOMPLETE"}
            else "Queue status is not a guarantee that every possible public email was found.",
            f"Input websites: {c['input_websites']:,}",
            f"Websites started: {c['sites_started']:,}",
            f"Websites never started: {c['sites_unstarted']:,}",
            f"Pending / retry / active websites: {c['queue_remaining']:,}",
            f"Websites with email evidence: {r['with_email']:,}",
            f"Unique email addresses: {r['unique_emails']:,}",
            "Requested mode: " + report["requested_mode"],
            "Stop reason: " + report["stop_reason"],
            report["next_step"],
            "See run_report.json and coverage.csv for diagnostics; addresses always retain public sources.",
            "",
        ]
    )
