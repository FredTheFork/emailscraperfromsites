# Run Public Mail entirely in Google Colab

[Open the notebook in Colab](https://colab.research.google.com/github/FredTheFork/emailscraperfromsites/blob/main/notebooks/PublicMail_Colab.ipynb)

While the Colab feature PR is still awaiting merge, use [its branch notebook](https://colab.research.google.com/github/FredTheFork/emailscraperfromsites/blob/arena/ff9cb618-emailscraperfromsites/notebooks/PublicMail_Colab.ipynb). The bootstrap uses `main` once it includes Colab support; until then it explicitly reports its feature-branch fallback. Your input/results do not go to either Git branch.

## What you need

- A Google account with access to a CPU Python Colab runtime and sufficient private Drive space. A GPU/TPU is unnecessary.
- Your original `domains.csv`, or a consistent source SQLite export. The original CSV contains **33,144 domains, not confirmed email addresses**. It is private and deliberately absent from the public repository.
- Permission to process these sites/data, and realistic coverage expectations. Some domains will not publish an address or will deny access. Free email providers and external addresses are eligible; publication is not proof of ownership, deliverability, or marketing consent.

**No local Python, Docker, VPS, public dashboard, tunneling service, or paid provider account is needed for this notebook.** Google may limit runtime availability/quotas. Installing dependencies and handling Drive authorization happen in the remote Colab runtime/browser, not on your computer.

## First run: six steps

1. Open the notebook. Use a **CPU Python runtime** (Python 3.11–3.13 tested). Keep **Use Google Drive** enabled and retain the default project name `publicmail-33144`, or choose a different name for a genuinely different collection.
2. Select **Runtime → Run all**. Approve the normal Drive mount prompt. The notebook clones the repository, installs pinned application dependencies in `/content/publicmail-venv`, and optionally installs Chromium and its system dependencies. It does **not** replace Colab kernel packages.
3. At the upload prompt, choose your actual `domains.csv`. Alternatively set Input mode to **Use a Drive file path**, enter the full private path, rerun settings, then setup/restore/import. Large SQLite input is best read this way. The input is copied locally before import and saved privately in your Drive project.
4. The default Start/resume mode runs a **100-claim pilot**. Retries count toward that limit; it is not necessarily 100 distinct domains or a representative sample. Inspect coverage, source URLs and relationship flags in the exported ZIP.
5. To process the remaining queue, select **full in the Start/resume cell itself**, then run **that cell again**. No reinstall or upload is needed. The default session budget is five hours, after which it stops gracefully and exports. Run full again to continue. Zero hours means no planned stop, not unlimited Colab runtime.
6. Download the ZIP in the export cell, or obtain the verified ZIP from **MyDrive → PublicMail → project-name → exports**. Results remain remote even if your browser was closed when the run completed.

Use the notebook, not a simultaneous `publicmail serve`/CLI worker, against this DB. The original web dashboard remains available for VPS deployments, but no public server or exposed bearer key is needed in Colab. Notebook forms, progress logs, status, CSV evidence and downloads provide the Colab workflow.

## Resume after a runtime reset

1. Reopen the notebook and use the **same project name and Google account**.
2. Run settings, installation, Drive/restore, and input cells again (Run all is fine; its default is a pilot).
3. The newest checksum/integrity-verified snapshot is copied **from Drive to local disk**. If that generation is damaged, older valid generations are tried. Interrupted pages/sites become retryable; saved evidence and discovery remain.
4. The queue already contains imported domains/metadata. Normally no second CSV upload is requested. Select full in Start/resume to continue.

An existing local database always wins on a cell rerun: restore never overwrites it with an older Drive snapshot. If every backup is invalid, recovery **fails closed**, keeping the files instead of quietly starting an empty collection. If the live local DB is damaged, keep it and its sidecars for investigation; do not overwrite or delete it to clear an error.

The source checkout is reused within one Colab runtime without destructive Git resets or automatic updates. Start a fresh Colab runtime to pick up a newer application revision, after verifying the last Drive snapshot. The notebook prints the source commit used.

## Storage and failure semantics

| Location | Contents / durability |
| --- | --- |
| `/content/publicmail-code` | Disposable source checkout. No private input/state belongs in Git. |
| `/content/publicmail-venv` | Isolated application Python; kernel dependencies are untouched. |
| `/content/publicmail-workspaces/<project>/state.db` | **Live local SQLite WAL database**. Lost if the runtime is destroyed. |
| `MyDrive/PublicMail/<project>/inputs` | Content-hashed private original input. Never committed to GitHub. |
| `MyDrive/PublicMail/<project>/snapshots` | Three retained valid snapshot generations, each a `.db` + `.json` pair. |
| `MyDrive/PublicMail/<project>/exports` | Timestamped verified result ZIPs. Exports are not automatically deleted. |

Do **not** put the live SQLite DB on the Drive FUSE mount. Do **not** copy only an active `.db` and omit its WAL. The worker uses SQLite's online backup API to create a consistent closed snapshot on local disk, validates its identity/integrity, computes SHA-256, copies it to Drive, verifies the copied bytes, and publishes a JSON commit marker **last**. A copy without a valid marker is not eligible for automatic restore. Retention runs only after a new complete publication; checksummed older generations are retained as recovery points. Previous committed snapshots are not removed on copy/quota failure.

Backups are taken before the run, periodically during it (default two minutes **after the preceding backup finishes**), and at the end of a graceful run. Periodic snapshots can contain a running desired mode or in-flight pages; restore explicitly recovers and pauses before any new crawl. The three-generation limit is configurable through the helper CLI. Allow several times the working DB size for snapshots and local staging; exports and the original input need additional space.

Drive publication/export work during a crawl runs in bounded child processes. A failed/timed-out periodic snapshot stops new site claims; the worker drains/cancels its current work, retains local evidence, and attempts a final export/checkpoint. Local ZIP metadata is written **before** attempting its Drive copy so the download cell can recover a completed ZIP after a Drive failure. No process can guarantee cleanup if Google forcibly kills the runtime.

A successful FUSE write/readback verifies the mounted copy, **not a formal guarantee that Drive has durably replicated it**. Abrupt runtime loss can discard work since the last completed remote snapshot (plus any work not yet synced by the backing service). Check the last verified timestamp. Copies in one Drive account are not protection against account loss/deletion; keep protected off-account backups if needed.

### One runtime per project

Local file locks prevent duplicate notebook commands and duplicate workers on one runtime. An advisory `active-session.json` in Drive detects many accidental concurrent Colab sessions, with a 15-minute expiry after a killed runtime. A restarted runtime may therefore have to wait for the stale marker to expire.

**Drive FUSE is not a distributed atomic lock/consensus service.** Caching/races mean the marker cannot prove mutual exclusion across machines. Do not run two notebooks/runtimes against the same project, bypass the guard, delete a live marker, or merge divergent snapshot histories. If concurrent runs happened, preserve both histories and investigate before choosing a recovery point.

## Stop, requeue and exports

- Click the run cell's **Interrupt/stop once**, then wait for graceful completion and final backup/export. It stops new claims and allows bounded cleanup. Repeated interruption or a runtime reset can interrupt that cleanup.
- Failed/no-email/blocked/completed are honest outcomes, not a count to hide. Review `coverage.csv` before the optional, explicitly disabled-by-default deeper-pass cell.
- Continuation visits saved pending candidates under the new budgets. A fresh refetch rebuilds the frontier while retaining historical observations/timestamps. Requeueing never removes robots, access checks or server cooldowns.
- `emails.csv` groups website/email associations; `unique_emails.csv` contains global addresses; `observations.csv`/`observations.jsonl` preserve source/method evidence; `coverage.csv` includes **every input domain**; `summary.json` records totals.
- CSVs are BOM UTF-8 and spreadsheet-formula escaped; JSONL preserves exact machine-readable strings. The result ZIP does not contain the working DB, original input, or access key.
- When a run errors, read its output, then try the download cell before resetting the runtime. Set Refresh exports to regenerate from current saved local evidence; after a runtime reset, regenerate from the restored DB.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Drive mount/quota/I/O error | Fix Drive, recover/download local results if possible, rerun. Do not remove old snapshot pairs. |
| Another active session | Stop the other runtime. After a crash, wait up to 15 minutes. Never run two copies. |
| Browser install fails | Rerun setup; or disable optional browser fallback and rerun settings/setup/environment check. The safe HTTP/PDF path still works. |
| Retry queue looks idle | Check `next_retry_at`; Retry-After is intentional. Do not shorten a site's cooldown to chase yield. |
| Many 403/429/robots blocks | Colab's data-centre/shared IP can be restricted. Inspect coverage, reduce load, or accept/manual-review those gaps. No proxy/CAPTCHA evasion is included. |
| Unexpected input count | Check import report, selected project/file, invalid rows and previous additions. Reimport is idempotent; a new project is for a new collection, not recovery. |
| All snapshots invalid | Keep every file, stop, investigate. No empty/reset fallback is performed. |
| Disk space low | Safely copy/remove old export bundles you no longer need. Do not delete the live DB/WAL or only surviving snapshot pair. |

## Colab is remote, not an always-on guarantee

You can turn your PC off without making it the worker machine, but Colab can terminate/disconnect the backend because of runtime/idle/resource limits or availability. Neither free nor paid access creates a guarantee that a single session will finish this list. There are no notebook keep-alive tricks. Plan for **multiple resumable sessions** and workload-dependent hours/days. A continuously supervised VPS is an alternative if you later need stronger unattended operation, not a prerequisite for this workflow.

**No promise of 30,000 or 33,144 emails is made.** Tests prove controlled behavior, not live-site yield. Keep outputs private and follow applicable data-protection/marketing rules.

## Testing and implementation

The notebook calls `python -m publicmail.colab` inside the isolated environment. The helper is also testable without a Google account:

```bash
pip install -e '.[dev,notebook]'
pytest tests/test_colab.py tests/test_colab_notebook.py
PUBLICMAIL_TEST_NOTEBOOK=1 PUBLICMAIL_TEST_COLAB_INSTALL=1 \
  pytest tests/test_colab_notebook.py -v
```

Controlled tests cover online WAL snapshots, checksum/identity validation, fallback recovery, retention/copy failure, advisory/local guards, actual subprocess checkpointing, the normal engine's pilot/resume/export path, and executing the notebook against synthetic fixtures. They cannot exercise your Google login/Drive cloud account or establish live-site yield from this development sandbox. GitHub CI runs the private-environment installer and notebook execution, with no private CSV or scraped addresses committed.
