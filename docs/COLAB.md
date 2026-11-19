# Automatic full-list collection in Google Colab

[Open the notebook in Colab](https://colab.research.google.com/github/FredTheFork/emailscraperfromsites/blob/main/notebooks/PublicMail_Colab.ipynb)

Before this PR is merged, use [the pinned preview notebook](https://colab.research.google.com/github/FredTheFork/emailscraperfromsites/blob/b6602493439a02f982b4b9119ab3e4076690569c/notebooks/PublicMail_Colab.ipynb). The link is updated with each published workflow revision. Setup automatically fetches the full-list workflow, falling back to its feature branch until `main` includes workflow version 2. It prints the source commit and rejects a stale installed application. Private input/results never enter Git.

## Run it — no crawl options to change

1. Open the notebook in a **CPU Python runtime** (Python 3.11–3.13 tested).
2. Choose **Runtime → Run all**. Approve the normal Google Drive mount prompt.
3. Upload your original **`domains.csv`** when asked. The supplied list has **33,144 valid unique domains** and is intentionally absent from the public repository.
4. Leave the long-running collection cell working. It processes the **whole saved queue**, automatically performs deeper collection and recovery, and saves the result ZIP privately to **MyDrive → PublicMail → publicmail-33144 → exports**. A download is offered when the run stops.

**There is no default 100-claim pilot, five-hour planned stop, or manual deeper-pass switch.** The old pilot could produce a small result while almost the entire list remained pending; address count alone never proves full-list coverage. The user's previous result ZIP/state was not available for diagnosis, so the exact cause of their 120-email output is not established.

No local Python, Docker, VPS, public dashboard, tunnel or GPU is required. Dependencies and Chromium are installed in `/content/publicmail-venv`, not the Colab kernel. Only the ordinary Google authorization/upload steps require your interaction. You must have permission to process the sites/data.

## Continue the existing collection, including an old pilot

Use the **same Google account and project name** (`publicmail-33144` unless you previously chose another), then **Run all again**. Do not delete the old project, create a new empty queue, or upload `emails.csv` in place of the original website list.

- Within the same runtime, the live local database wins; it is never replaced with an older snapshot.
- After a runtime reset, the newest checksum/integrity-verified snapshot is copied **from Drive onto local disk**. If damaged, older valid generations are tried. If none is valid, recovery fails closed, preserving the files rather than silently starting over.
- Saved website metadata, addresses, public sources, frontier, cooldowns and automatic stage survive the checkpoint. No second input upload is normally needed.
- The pre-v2 pilot database is supported: its unstarted queue is drained and its existing evidence is retained. Deeper checks are selected automatically from saved coverage.
- Running an already-finished automatic plan again does **not** restart every pass. Adding new domains starts a new plan generation without erasing prior observations.

Setup refreshes a **clean** source checkout every time. It does not use `git reset`/`git clean`, delete private state or overwrite local source edits. If source edits are detected, preserve them and use a fresh runtime with the **same Drive project**. The installed application's workflow-version preflight prevents silent execution of old pilot defaults.

## What runs automatically

Every supplied domain is independently queued, including sites that ultimately fail or deny access. A website is "started" when its worker begins processing it; a robots denial can prevent its homepage from being fetched. This is not a claim that blocked pages were visited.

| Stage | Automatic behavior |
| --- | --- |
| **1. Whole list** | Drain all pending websites and scheduled retries; 12 concurrent sites, ≥1.5s same-site delay (robots may increase it), up to 40 content URLs, depth 3, 400 frontier entries, 360s per-site attempt, 3 attempts, 6 sitemaps / 4,000 accepted URLs per sitemap, 4 PDFs / 30 pages each, 2 rendered pages. |
| **2. Deeper contacts** | Refresh permitted successful pages on no-email/truncated-frontier sites; continue saved work for sites with limits, transient errors, deferred documents or unrendered important pages. Increase to 80 content URLs, depth 5, 800 frontier entries, 600s per-site attempt, 10 sitemaps / 8,000 accepted URLs, 8 PDFs / 60 pages each, 4 rendered pages. Historical evidence and denied paths are kept. Ordinary content 404s are not blindly retried. |
| **3. Transient recovery** | One extra bounded round for unreachable/transient failures and exhausted 429s, using the deeper profile and at most 2 attempts. Persistent cooldowns/Retry-After still apply. Robots/authentication/CAPTCHA blocks are not selected for evasion. |

The stage marker and selected requeue commit in the **same SQLite transaction**. Stage transitions are checkpointed before requests for that pass. A reset cannot repeatedly restart a finished pass just because the email count is low. Page/document/depth/time/frontier limits are finite; exhausting the plan is not proof of finding every possible address.

Discovery prioritizes actual contact/about/team/legal links, common contact paths (including `.html`/`.php`), bounded CMS query links, sitemaps and text PDFs. A JavaScript homepage shell is rendered **before** the HTTP budget is spent guessing paths. Important contact pages remain eligible for rendering **even when a designer/footer address was found**. Text, mailto, HTML attributes/entities, public noscript/templates, common obfuscations, supported script literals and rendered DOM evidence are included. All providers are eligible; there is no business-domain-only filter or address guessing.

Homepage access denials and recognized access challenges stop that site. An individually denied leaf path is retained and not retried by the automatic deeper pass; independently permitted public paths remain eligible. No proxy rotation, authentication/CAPTCHA bypass, or keep-alive trick is included.

### Progress and stopping

Progress appears about every 30 seconds, including **websites started**, **NEVER STARTED**, websites with email, unique addresses and active workers. A long Retry-After can legitimately leave the queue waiting, with a next eligible retry timestamp.

To pause deliberately, click the run cell's **Interrupt once** and wait for bounded graceful cleanup. New claims stop; active work drains/cancels safely, then exports and a final checkpoint are attempted. Repeated interruption or resetting the runtime can prevent cleanup. Another Run all with the same project continues automatically.

**Colab can still terminate a runtime.** No free or paid runtime is guaranteed to finish this list in one session. Plan for workload-dependent hours/days and multiple resumable sessions. Your PC can be off because it is not the worker, but neither browser disconnection nor remote execution guarantees that Google keeps the backend running. No platform-limit evasion is attempted.

## Read the completion report, not just the email count

Every ZIP includes:

| File | Meaning |
| --- | --- |
| **`READ_ME_FIRST.txt`** | Prominent completion label, input/start/unstarted/pending counts, address counts, stop reason and next action. |
| **`run_report.json`** | Machine-readable completion checks, per-stage history, outcomes, HTTP/error/limit diagnostics. |
| `summary.json` | Existing totals/worker fields plus the same `collection` report. |
| `coverage.csv` | **Every input domain**, including pending, blocked, failed and no-email sites; reasons, limits, source frontier counts, errors, timestamps. |
| `emails.csv` | Website/address associations, relationship flags, representative public source and evidence methods. |
| `unique_emails.csv` | Globally unique observed addresses (not number of websites). |
| `observations.csv`, `observations.jsonl` | Per-website/address/source/method evidence, context and timestamps. |

Completion labels:

- **`INCOMPLETE`**: some websites have not started, remain pending/retry/active, **or automatic follow-up stages remain unfinished**, even if the first queue was drained. Resume the same project.
- **`PILOT_ONLY`**: an explicit advanced claim cap was requested; not a full-list result.
- **`COMPLETE_WITH_GAPS`**: all websites were started, the queue is drained, and all automatic stages finished, but blocked/failed/no-email/limited/error outcomes remain. Inspect them; they are not successes to conceal.
- **`COMPLETE_PASS`**: the requested finite pass finished without those recorded gaps. This still does not certify an exhaustive crawl of the whole web or mailbox validity.
- **`EMPTY`**: nothing was imported; crawling refuses to start.

`completed` at the **individual website** level means at least one address was observed, not that every possible page was exhausted. A free-provider/personal address or a designer's address can be public without being the business's own contact. Publication is not ownership, deliverability, verification or marketing consent. No promise of 30,000/33,144 addresses is made.

Exports are refreshed from the current local state by default, rather than silently downloading an old pilot ZIP. If Drive copying fails, the download cell can recover the last completed local ZIP and prints **that archive's own** completion report plus a warning that it may predate current progress. CSVs are BOM UTF-8 and formula escaped; JSONL preserves exact strings. Bundles contain no state DB, original input or access key. Notebook output does not display collected addresses by default.

## Storage and failure semantics

| Location | Contents / durability |
| --- | --- |
| `/content/publicmail-code` | Disposable, automatically refreshed clean source checkout. No private input/state belongs here. |
| `/content/publicmail-venv` | Isolated application Python; kernel dependencies are untouched. |
| `/content/publicmail-workspaces/<project>/state.db` | **Live local SQLite WAL database**. Lost if the runtime is destroyed. |
| `MyDrive/PublicMail/<project>/inputs` | Content-hashed private original input. |
| `MyDrive/PublicMail/<project>/snapshots` | Three retained valid generations, each a `.db` + `.json` pair. |
| `MyDrive/PublicMail/<project>/exports` | Timestamped, readback-verified result ZIPs; not automatically deleted. |

Do **not** put the live DB on Drive or copy only an active `.db` without its WAL. The helper uses SQLite's online backup API onto local disk, validates identity/integrity, computes SHA-256, copies to Drive, verifies the copied bytes, and publishes the JSON marker **last**. Retention runs only after a new verified publication. Incomplete copies are ineligible for restore; old good snapshots are not removed on a failed copy.

Checkpoints happen before target requests, between automatic stages, periodically (default two minutes **after the previous backup completes**), and at graceful exit. Periodic snapshots may contain running work; restore recovers it and pauses before starting again. A failed/timed-out backup stops new claims. Export and final backup are both attempted, and cleanup errors do not mask the original failure. Final export/checkpoint retain the idle-worker lock for an aligned bundle.

Backup/export I/O runs in bounded child processes (default deadline 180s). Allow several times the working DB size for snapshots/staging, plus original input and exports. A FUSE write/readback verifies mounted bytes, **not a formal cloud replication guarantee**. Abrupt runtime termination can lose work since the last completed remote snapshot or unsynced backing-service writes. Keep protected off-account backups if needed.

### One runtime per project

Local file locks guard commands and workers. Drive's advisory session marker catches many accidental overlaps, expiring within 15 minutes after a dead runtime. **Drive FUSE is not a distributed atomic lock service.** Never run two runtimes against the same project, bypass/delete a live marker, or merge divergent state histories. Preserve both histories and investigate if that happened.

## Troubleshooting and advanced input

| Symptom | Action |
| --- | --- |
| Only ~120 emails / old pilot ZIP | Use this updated notebook, same account/project, Run all. Check NEVER STARTED, queue counts, pass history and `READ_ME_FIRST.txt`. Do not infer 33k attempts from an address count. |
| Colab reset / browser disconnected | Same project → Run all. Only saved checkpoint progress can be recovered. |
| Drive mount/quota/I/O error | Fix Drive, recover/download local results while possible, rerun. Preserve old snapshot pairs. |
| Another session | Stop the other runtime; after a crash, wait up to 15 minutes. Never run two copies. |
| Browser install/launch error | Rerun setup and preflight. Missing Chromium pauses before mass failures; do not assume a completed collection. |
| Many 403/429/robots blocks | Shared Colab/data-centre IPs may be restricted. Inspect coverage; accept/manual-review denied sites. Do not evade access restrictions. |
| Unexpected input count | Check selected project/file, import report, invalid rows and previous additions. Reimport is idempotent; a new project is not a recovery step. |
| All snapshots invalid | Keep all files and investigate. No empty-reset fallback is performed. |
| Low disk space | Safely archive/remove old export bundles you no longer need, not the live DB/WAL or only good snapshots. |

The default CSV upload needs no edits. For an advanced private Drive source, set `INPUT_MODE = "Use a Drive file path"` and `DRIVE_INPUT_PATH` in the settings **code** before Run all; for a genuinely different source table, also set `SQLITE_INPUT_TABLE`. Input SQLite must be a consistent closed export/backup, not just the main file of an active WAL database. It is copied locally before querying and never used as state.

The advanced helper still supports an **explicit** pilot (`run --max-sites 100`), a planned time cap (`--hours`), `--single-pass`, `--no-browser`, reviewed requeues and configuration overrides for developers. None is required by the default notebook. The separate Docker/VPS dashboard and regular `publicmail` CLI continue to work unchanged; see [deployment](DEPLOYMENT.md).

## Validation scope

Tests exercise the actual engine across **257 synthetic websites with default automatic settings**, automatic deeper discovery, old pilot migration, interruption/stage recovery, denied-site exclusion, no-email outcomes, honest reports, query planning, input safety and failure isolation. The entire notebook is executed twice against controlled fixtures; installation tests verify kernel packages are unchanged. Real Chromium tests cover intercepted JavaScript/XHR/network restrictions and contact/navigation regressions.

The supplied **33,144-domain** CSV was also imported and restored with zero invalid rows and **zero target requests**. A separate, explicitly synthetic dispatch benchmark claimed all 33,144 entries exactly once without a 100-site cap; this is **not crawling or live-site yield**. No actual Google account/cloud Drive session was exercised here. See [input/validation audit](INPUT_AUDIT.md).

```bash
pip install -e '.[dev,notebook]'
PUBLICMAIL_TEST_NOTEBOOK=1 PUBLICMAIL_TEST_COLAB_INSTALL=1 pytest
# With installed Playwright Chromium:
PUBLICMAIL_TEST_BROWSER=1 pytest tests/test_browser.py -v
```

Keep outputs private and follow applicable data-protection and marketing rules.
