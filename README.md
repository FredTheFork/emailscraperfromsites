# Public Mail

**A complete, resumable website-email collection worker and private web dashboard.**

Import your existing website list, crawl public pages with per-site pacing, and export addresses **with the exact public sources where they were found**. Gmail, Yahoo, Outlook, ISP mailboxes, and business-domain addresses are all included. There are no guessed mailboxes, SMTP probes, proxy rotations, or CAPTCHA bypasses.

## First: what can actually be guaranteed?

Your supplied CSV contains **33,144 unique domains**, not 33,144 confirmed email addresses. All 33,144 import successfully. Some websites may be dead, unrelated to the intended industry, robots-disallowed, access-restricted, or contact-form-only. A scraper cannot honestly guarantee 30,000 emails, complete coverage of every site, zero blocks, or perfect accuracy.

This project makes the **process recoverable and the gaps measurable**. Limits, failures, blocked sites, and no-email outcomes are exported alongside successes. An observed email is not proof of ownership, deliverability, or marketing consent.

**Run remotely in Google Colab:** use the notebook below for installation, private input upload, pilot/full runs, verified Drive checkpoints, resume, and downloads—no local Python or VPS required. Colab sessions can expire, so plan for resumable sessions rather than a guarantee of one uninterrupted run. An always-on VPS with Docker remains an alternative for continuously supervised operation.

**About the old log:** the pasted excerpt ends during Companies House resolution. It contains no traceback or final exit status, so it does not establish whether the process crashed, was interrupted, or was still running. The preceding Common Crawl stages report successful completion.

## What is included

- **Import:** CSV and read-only SQLite input, schema detection, IDN normalization, repeat-safe deduplication, original business metadata preserved.
- **Discovery:** homepage, HTTPS/www alternatives for ordinary connectivity failures, contact/about/legal/privacy links, a few common contact paths, robots-declared sitemaps and sitemap indexes, other internal links up to configurable budgets.
- **Extraction:** visible text, `mailto:` (including free providers), HTML entities, structured data, data attributes, common `[at]`/`[dot]` forms, static Cloudflare email obfuscation, ROT13 email attributes, simple JS literals/concatenations/reversals, and text PDFs.
- **Optional JS fallback:** Chromium rendering of a small number of pages on no-email sites; uses the same safe HTTP pipeline. Not stealth and not an access-control bypass.
- **Reliability:** persistent site queue and page frontier, per-page atomic evidence/discovery commits, an exclusive runner lock, recovery after interruption, bounded requests/downloads/PDF parsing, durable backoff and `Retry-After`.
- **Observability:** authenticated dashboard, live totals, pause/resume, 100-claim pilot, full queue, page-level trails, searchable domains/emails, requeue controls, worker heartbeat, coverage CSV, online SQLite backups.
- **Colab:** complete notebook with isolated installation, private CSV/SQLite upload, optional Chromium, measured pilot/full runs, progress, verified multi-generation Drive snapshots, recovery, gap requeue, and CSV/JSONL ZIP downloads.
- **Deployment alternative:** non-root Docker image, Compose, persistent volume, optional HTTPS reverse proxy, optional browser build, pinned runtime dependencies, automated tests and CI.

No raw HTML, PDF bodies, or screenshots are retained by default. Only source URLs, short evidence context, methods, and timestamps are saved.

## Quick start: Google Colab — no local installation

### [Open Public Mail in Google Colab](https://colab.research.google.com/github/FredTheFork/emailscraperfromsites/blob/main/notebooks/PublicMail_Colab.ipynb)

Before the Colab PR is merged, [open the pinned preview notebook here](https://colab.research.google.com/github/FredTheFork/emailscraperfromsites/blob/ffd518c60945df351f1c03b374bb75a01dda4a61/notebooks/PublicMail_Colab.ipynb). The installer uses `main` when ready, otherwise explicitly reports the feature-branch fallback.

1. Choose a **CPU Python runtime**; leave **Use Google Drive** enabled.
2. Run all cells, approve the normal Drive mount, and upload your **actual `domains.csv`** when prompted (or select a private Drive file path). Your 33,144-domain CSV is **not** published in this repository.
3. Review the default **100-claim pilot** and its evidence/coverage ZIP.
4. Select **full in the Start/resume cell**, then run that cell again. It processes the saved queue remotely; no reinstall/re-upload is needed.
5. Download the result ZIP, also copied to your private Drive project. After a runtime reset, run setup with the **same project name**, restore the verified snapshot, and continue.

The notebook keeps the active SQLite DB on Colab's **local `/content` disk**, not the Drive mount; only verified closed snapshots/input/result ZIPs go to Drive. It retains three good snapshot generations, handles graceful interruption, and stops new claims if periodic backup fails. An abrupt runtime loss can still discard work since the last completed remote snapshot. Use only **one Colab runtime per project**; Drive's advisory marker is not a distributed lock guarantee.

The application is installed in a private virtual environment, avoiding Colab kernel dependency conflicts and IPython `asyncio.run` errors. Browser fallback is enabled by default in the notebook and includes installation/preflight. No public web server, tunnel, or dashboard key is required.

See **[the complete Colab guide](docs/COLAB.md)** for first-run/resume instructions, runtime limits, storage layout, exports, deeper passes and troubleshooting. No scraper can promise 30,000/33,144 emails from a domain count.

## Always-on Docker alternative

On your **cloud server**, with Git and Docker Engine + Compose installed:

```bash
git clone https://github.com/FredTheFork/emailscraperfromsites.git publicmail
cd publicmail
cp .env.example .env
chmod 600 .env
docker compose up -d --build

# If SCRAPER_TOKEN is empty, retrieve the generated dashboard access key:
docker compose exec -T app cat /data/admin-token
```

The dashboard is bound to the **server's loopback interface by default**, not exposed to the internet. On your laptop, open a tunnel (replace the SSH destination):

```bash
ssh -N -L 8000:127.0.0.1:8000 your-user@your-server
```

Open **http://localhost:8000**, enter the access key, and:

1. **Import domains** → select your `domains.csv` or original `.db` file.
2. Review **Crawl settings**. Defaults: 24 concurrent sites, 1.5 seconds minimum between requests to the same registrable domain, 24 completed/failed content URLs per site (plus retries/redirects and separate robots/sitemap requests), depth 3, and a 240-second site budget per attempt.
3. **Start 100-site pilot**. Inspect results, external-address flags, blocked sites, and evidence. A pilot limit counts claims, including retries, not guaranteed distinct websites.
4. **Run full queue** when satisfied. Closing the browser, disconnecting SSH, or switching off your PC does not stop Docker on the VPS.
5. **Export results** at any time. Investigate the coverage report before a deeper second pass.

**Do not run a second `publicmail run` while the dashboard service owns the database.** The dashboard already includes its worker. A lock enforces one runner.

See **[full cloud deployment, backups, HTTPS, and recovery](docs/DEPLOYMENT.md)** for the complete runbook. No cloud account has been provisioned or paid for by this repository.

### Optional JavaScript fallback

The small default image does not contain Chromium. To install it:

```bash
docker compose -f compose.yaml -f compose.browser.yaml up -d --build
```

Then pause, let active sites finish, enable **Optional JavaScript fallback** in settings, and run/requeue a small pilot. Use the same `-f` options for subsequent Compose build/up commands. Budget more RAM and time. Cross-site resources, cookies, service workers, WebSockets, non-GET actions, and access challenges are not bypassed. Some JS sites therefore still cannot be extracted.

### Optional HTTPS dashboard

Set `SCRAPER_HOSTNAME` in `.env` to a real DNS name pointed at your VPS, permit inbound ports 80/443, then:

```bash
docker compose --profile https up -d --build
```

Caddy obtains TLS certificates. Keep port 8000 private and protect the access key. Do not publish the plain HTTP dashboard on an untrusted network.

## Python / CLI alternative

Python **3.11–3.13**, preferably Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .

# Source and result DB MUST be different files.
publicmail import /path/to/domains.csv --db data/harvest.db
# Or: publicmail import /path/to/window_websites.db --table domains
publicmail run --max-sites 100
publicmail status
publicmail export --out exports/pilot --jsonl
publicmail run                  # resumes saved progress, including due retries
```

Run this on the VPS, not a laptop that will sleep. Prefer the Docker service for automatic process restarts. A terminal run waits for scheduled retries; a long `Retry-After` can legitimately look idle.

Use the dashboard instead with `publicmail serve --host 0.0.0.0 --port 8000`. The generated key is `data/admin-token` unless you set a random `SCRAPER_TOKEN` of at least 20 characters. Use one Uvicorn process; never add `--workers` or `--reload` around the embedded worker.

More commands:

```bash
publicmail pause
publicmail run --concurrency 24 --delay 1.5 --max-pages 24 --max-depth 3
publicmail retry --status failed --limit 100
# Deeper continuation: settings page budget must exceed the previous pass.
publicmail retry --status no_email --limit 100
publicmail run --max-pages 60 --max-depth 4 --max-sites 100
# Fresh refetch instead; existing evidence remains historical until seen again:
publicmail retry --status no_email --fresh --limit 100
publicmail run --browser --max-sites 20  # needs optional install below
publicmail backup --output backups/harvest-2026-11-18.db
publicmail doctor
publicmail --help
```

For a non-Docker browser install: `pip install -r requirements-browser.lock` then `playwright install --with-deps chromium` (Linux system-package installation may need root). The browser setting is off by default; a missing browser pauses the run rather than marking every website failed.

## Inputs and outputs

**CSV:** UTF-8, header `domain`, `url`, `website`, `website_url`, or `homepage`. A `domain` + `url` + `business_name` layout works directly. Example: [examples/domains.csv](examples/domains.csv). A URL from a different website cannot silently replace the domain identity.

**SQLite input:** auto-select a real table with a domain/URL column, preferring `domains`; use CLI `--table` to override. Export/close the source database first, or create a SQLite backup; uploading only the main file of an actively written WAL database can omit uncheckpointed input rows. Never point `--db` at the input database.

**Your actual dataset:** [input audit](docs/INPUT_AUDIT.md). The uploaded CSV and the local preview's populated state database are **not committed or pushed to GitHub**. Upload your CSV once on your deployment.

| Export | Meaning |
| --- | --- |
| `emails.csv` | One row per website + normalized email; business name, relationship, evidence confidence, representative source, total source count, methods, first/last seen, site status. |
| `unique_emails.csv` | One row per globally unique address, website count, representative public source, first/last seen. |
| `observations.csv` | **Every** website/address/source/method observation, short context, confidence, and timestamps. |
| `coverage.csv` | **Every** imported website, including those without emails: status, reason, attempts, limits, remaining frontier, page errors/skips, browser errors, and timestamps. |
| `summary.json` | Current totals and worker state (CLI export). |
| `observations.jsonl` | Optional exact machine-readable observations (`--jsonl`), without spreadsheet escaping. |

CSV is UTF-8 with a BOM for Excel. Cells beginning with spreadsheet formulas are prefixed with `'` for safety; use JSONL for exact programmatic strings. Live exports are consistent per individual file; pause and let active sites finish for aligned multi-file totals.

Email deduplication lowercases the address, including the local part (a pragmatic convention, not a claim that all mail systems are case-insensitive). `on_domain`, `free_provider`, and `external` describe the relationship to the **original input domain**, not verified ownership. A rebranded redirect may therefore produce legitimate `external` addresses. Free-provider classification is a helpful list, not an exhaustive provider registry. All valid providers remain eligible.

## How to assess coverage honestly

Use **websites with at least one email / imported websites**, not merely the total email count. One business can publish many addresses, and one web designer's email can occur on hundreds of sites. The dashboard separates unique addresses, domain-email pairs, website coverage, and terminal outcomes.

- `completed`: at least one address is recorded; it does **not** mean the whole website was exhausted.
- `no_email`: no address was found within this pass's permitted pages and limits.
- `blocked`: robots/access policy, rate-limit exhaustion, access challenge, or restricted offsite redirect. No bypass attempted.
- `failed`: unreachable after attempts, a time budget exhausted without evidence, or an internal error needing inspection.
- `retry`: a future attempt is scheduled; look at `next_attempt_at` and the reason.

Check `completion_reason` (`page_limit`, `depth_limit`, `frontier_exhausted`, `site_timeout`, etc.), `candidate_truncated`, remaining pages, and page errors. Increase budgets only where justified, then pilot a continuation or an explicit fresh pass. Historical observations are retained on retries; timestamps reveal when they were last seen. There is no mailbox verification or automatic deletion/retention policy.

Throughput is workload-dependent. At this scale, plan for **hours to days**, not a fixed completion promise. Measure the pilot's observed sites/hour, inspect 429/403 rates, and adjust conservatively. A bigger concurrency number cannot make one site safely accept unlimited requests.

## Security, acceptable use, and limitations

- Respects robots on every origin and redirect; inaccessible robots policies fail closed. 404/410 robots files allow crawling. 401/403 homepages and access challenges stop that site. An individual robots-disallowed path is skipped.
- Identifiable user agent, paced same-site requests, timeout limits, TLS verification, backoff/jitter and `Retry-After`. Public HTTP fallback is optional and never used to work around a 401/403/CAPTCHA.
- Rejects credentials in URLs, non-web schemes/ports, IP-literal targets, and private/reserved DNS answers, including redirect/resource targets. The optional browser cannot directly access the network; permitted requests go through the safe HTTP client.
- Authentication is a private access key; the UI keeps it in session storage for the current tab and sends an Authorization header. No public results API, token-in-URL links, or unauthenticated control endpoints.
- This is for lists you are authorized to process. Check site terms and applicable data-protection rules. In the UK, public availability is **not** blanket permission for marketing; UK GDPR/PECR, sole-trader/personal-address distinctions, lawful basis, transparency, retention, and suppression/opt-outs may matter. Obtain appropriate advice.
- Cannot extract non-published addresses, login-only content, image-only text (no OCR), encrypted PDFs, every possible obfuscation, or every dynamically loaded/cross-site resource. Finite page/depth/frontier/time/document limits are intentional. Rare quoted or internationalized local-part email syntax is not fully supported.
- A single persistent worker and local SQLite are appropriate for this list. This is **not** a multi-node distributed scheduler. Do not put the working DB on NFS or Google Drive, run multiple replicas, or expose the dashboard as a public multi-tenant scraping API.

See [architecture and recovery semantics](docs/ARCHITECTURE.md) and [security notes](SECURITY.md).

## Development and validation

```bash
pip install -e '.[dev]'
ruff check src tests notebooks/colab_client.py
ruff format --check src tests notebooks/colab_client.py
pytest --cov=publicmail --cov-report=term-missing
node --check src/publicmail/static/app.js
```

The automated suite uses controlled local/fake websites, not unsolicited live crawling. It covers extraction, input schemas, deduplication, safe exports, transaction rollback, robots and redirects, response bounds, retries, pause/resume primitives, worker locks, cancellation recovery, PDFs, authentication, and the API/CLI. CI also builds and health-checks the standard container. Real-site yield is **not** established by unit tests. Run the deployment pilot before the full crawl.

Colab-specific tests also verify snapshot recovery, failed-copy retention, subprocess supervision and private-environment installation. See `docs/COLAB.md` for the opt-in Jupyter notebook execution tests; these use synthetic fixtures, not a connected Google account or real-site yield.
