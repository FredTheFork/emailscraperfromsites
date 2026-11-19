# Always-on cloud deployment and operations

## 1. Choose the right runtime

Use an always-on Linux VPS/VM, not a free web host that sleeps and not an interactive Colab runtime. A sensible starting point is **2–4 vCPU, 4 GB RAM, and at least 20–40 GB persistent local SSD**. Optional Chromium benefits from more RAM; monitor actual usage before increasing browser concurrency. Provider prices/quotas vary; no provider account or paid resources are created by this project.

Install Git and Docker Engine with the Compose plugin using your provider/OS's official instructions. Enable Docker at boot. Restrict SSH access, keep the host updated, and verify that your provider permits the workload. An IP with poor reputation can be blocked regardless of scraper quality.

## 2. Install the application

```bash
git clone --branch arena/ca32fcad-emailscraperfromsites \
  https://github.com/FredTheFork/emailscraperfromsites.git publicmail
cd publicmail
cp .env.example .env
chmod 600 .env
docker compose up -d --build
docker compose ps
docker compose logs --tail=50 app
docker compose exec -T app cat /data/admin-token
```

After this feature is merged, a normal default-branch clone is also fine. Keep your access key private. If you supply `SCRAPER_TOKEN` instead, the key file is not generated and the `cat` command is unnecessary. Generate a random unique value; do not reuse another service's password.

The initial queue is paused, so installing or importing does not start traffic. After an already-running service restarts, its **persisted desired mode** is restored, and interrupted sites resume from the saved frontier. A deliberately paused service stays paused.

### Secure dashboard access: choose one

**SSH tunnel (no public web port needed):**

```bash
# Run on your computer, replacing the destination:
ssh -N -L 8000:127.0.0.1:8000 your-user@your-server
```

Visit http://localhost:8000. Closing this tunnel only closes the dashboard connection; Docker remains running on your server. Reconnect the tunnel to check progress later.

**HTTPS with a real hostname:**

1. Point a DNS A/AAAA record you control at the VPS. Ensure both records are correct if IPv6 is published.
2. Set `SCRAPER_HOSTNAME` in `.env` to that hostname only (no `https://`).
3. Permit inbound TCP 80/443 and optionally UDP 443 for HTTP/3.
4. Keep `SCRAPER_BIND=127.0.0.1` and run:

```bash
docker compose --profile https up -d --build
docker compose --profile https logs --tail=50 caddy
```

Visit `https://your-hostname`. Caddy's own volumes persist certificates. Do not set `SCRAPER_BIND=0.0.0.0` on a public host just to avoid setting up TLS. The bearer key and results would otherwise travel over plaintext HTTP.

The CLI trusts forwarded headers in Compose because only the private app port/proxy should be reachable. If you independently expose Uvicorn, limit `FORWARDED_ALLOW_IPS` to your actual trusted reverse proxy addresses.

## 3. Import your real input

The private uploaded CSV is **not part of the Git repository**. Use the dashboard's **Import domains** button after login. CSV and SQLite uploads up to 256 MiB are accepted. Inputs are copied temporarily, imported in idempotent batches, then the uploaded temporary file is deleted; original business metadata is retained in the state DB.

For a large file or a chosen SQLite table, use CLI import into the already-running container:

```bash
# From the server checkout; /tmp is a writable temporary filesystem:
docker compose cp /path/to/domains.csv app:/tmp/domains.csv
docker compose exec -T app publicmail import /tmp/domains.csv
docker compose exec -T app rm /tmp/domains.csv

# SQLite source example:
docker compose cp /path/to/window_websites.db app:/tmp/input.db
docker compose exec -T app publicmail import /tmp/input.db --table domains
docker compose exec -T app rm /tmp/input.db
```

The working state DB is `/data/harvest.db`, on the `scraper_data` Docker volume. Input files are never used as working state. Before uploading a source SQLite database, close its writer or create a consistent `.backup` so a missing WAL file does not hide input rows.

Import can be repeated without losing crawl state or creating duplicate website rows. A corrupt file can stop after some committed batches; correct it and reimport safely.

## 4. Pilot before running all 33,144

1. Review defaults in Settings and add a real contact URL to the transparent user agent if you have one.
2. Start the **100-site pilot**. The limit is 100 total site claims including retries.
3. Export **coverage**, **website emails**, and **observations**.
4. Inspect both successes and failures. Does a source belong to the business or its designer? Are domains offline? Are 403/429 rates high? Are site budgets too small? Are sites outside the intended industry?
5. Run the rest only when the pilot is acceptable. Increase concurrency gradually, not as a way to evade blocking. No setting guarantees a count.

The first 100 input rows are a convenience pilot, **not a statistically representative random sample**. Use a separately sampled CSV/state DB if you need an unbiased coverage estimate.

## 5. Pause, monitor, and recover

```bash
docker compose exec -T app publicmail status
docker compose exec -T app publicmail status --json
docker compose logs -f --tail=100 app
docker stats
# Stop NEW claims; active sites finish up to their site time budget:
docker compose exec -T app publicmail pause
```

The dashboard also shows a heartbeat and the last worker error. `retry` sites may be idle until a future `Retry-After`; inspect the timestamp before assuming a hang. A slow phase is not itself evidence of a crash.

**Do not run `publicmail run` alongside `publicmail serve`**. Both contain a runner, and the second is rejected by the exclusive file lock. Use the dashboard to start/continue the service queue.

If the server or process stops, saved page evidence and discovery survive. The next lock-owning worker recovers interrupted sites and pages. A graceful shutdown allows up to 30 seconds for active work, then cancels in-flight requests and preserves them for retry; already completed pages are not discarded.

`restart: unless-stopped` restarts a stopped/crashed application container and brings it up after a Docker/host reboot unless you deliberately stopped it. The standard `publicmail serve` command shuts down on a fatal worker failure so Docker can restart it. A browser-install/configuration problem pauses instead of entering a restart loop. Docker's *unhealthy* status alone does **not** automatically restart a container; inspect logs and use `docker compose restart app` if necessary. Independently hosting the ASGI app also requires your own process supervisor.

Disk full, corrupted disks, deleted volumes, provider outages, and machine loss require backups; SQLite transactions do not protect against those events. Monitor disk capacity and `/healthz` externally if this is business-critical.

### Explicit second pass

Pause and let active sites finish. Increase budgets in Settings, then use **Domains → Requeue outcomes**. Options:

- **Continue frontier:** retains successful pages and visits new/pending pages under the larger budgets; failed/blocked pages are reset only because you explicitly selected that outcome. A no-email retry with unchanged budgets may not do additional work.
- **Fresh refetch:** rebuilds the frontier and refetches from the homepage. Previously collected observations are retained as historical evidence, with old `last_seen` until rediscovered. It does not mean those addresses still exist.

Use fresh refetch after raising a document/sitemap cap that previously marked pages skipped, or after raising frontier capacity when candidates had been discarded at the old cap. Requeueing blocked sites never disables robots, challenges, or rate limits; it simply rechecks under the same rules.

## 6. Exports and backups

The dashboard exports individual CSVs. For a complete bundle saved server-side:

```bash
docker compose exec -T app publicmail export --out /data/exports/latest --jsonl
docker compose cp app:/data/exports/latest ./exports-latest
```

Create **consistent online backups**, not a naive copy of an active WAL file:

```bash
docker compose exec -T app publicmail backup --output /data/backups/harvest-latest.db
docker compose cp app:/data/backups/harvest-latest.db ./harvest-latest.db
```

Schedule the first command periodically with the server's timer/cron, and copy snapshots **off the VPS** into protected backup storage. Keep multiple generations rather than constantly overwriting your only backup. Results and evidence may contain personal data; use appropriate access restrictions, retention, and encryption for storage/backups. A Docker volume is persistence, not a backup. The access key is separate from the DB backup; retain it securely or generate a new one after restore.

### Restore procedure

1. Keep a separate snapshot of the current database before overwriting it. Stop the app and ensure no other worker uses the volume.
2. Stream a **trusted, consistent SQLite backup** into a one-off container as the application user. This avoids `docker cp` ownership/permissions problems with a private backup file. The replacement and sidecar cleanup happen only after the input stream is fully written.

```bash
docker compose stop app
docker compose run --rm -T --no-deps app sh -c \
  'umask 077; cat > /data/restore.tmp && rm -f /data/harvest.db-wal /data/harvest.db-shm && mv /data/restore.tmp /data/harvest.db' \
  < ./harvest-latest.db
# Inspect safely: explicitly pause the restored desired mode BEFORE the app starts.
docker compose run --rm --no-deps app publicmail pause
docker compose run --rm --no-deps app publicmail doctor
# Proceed only if the integrity check is OK:
docker compose up -d app
```

Do not delete the volume. Without the explicit pause step, restoring a snapshot whose desired mode was running would automatically resume it. Keep the pre-restore snapshot until the restored copy is verified, then use the dashboard to start deliberately.

## 7. Optional browser build

```bash
docker compose -f compose.yaml -f compose.browser.yaml up -d --build
```

This installs Chromium and its system dependencies at build time; the VM needs normal outbound internet access. Browser fallback must also be enabled in Settings. Start with one or two browser workers, not 24 browser instances. The browser uses intercepted same-site GET requests through the robots-aware HTTP client, a dead direct-network proxy, no service workers, and no sockets. It is intentionally less permissive than a normal user browser, so some dynamic websites will not work.

For HTTPS + browser together:

```bash
docker compose -f compose.yaml -f compose.browser.yaml --profile https up -d --build
```

The browser image is heavier and has a larger attack surface. Run as the supplied non-root user, keep it updated, keep cloud metadata/internal networks firewalled, and do not enable privileged mode. Treat remote pages and PDFs as untrusted content.

## 8. Updates and environment

Back up first, pause, wait for active sites, then update **your current checkout/branch** and rebuild/restart the same service. The named volume survives a normal rebuild/restart. **Never run `docker compose down -v` unless you intentionally want to destroy all collection data and keys.**

| Variable | Default / purpose |
| --- | --- |
| `SCRAPER_DB` | `data/harvest.db` in Python; `/data/harvest.db` in Docker. Local SSD only. |
| `SCRAPER_TOKEN` | Empty → generated file beside DB; otherwise random access key, ≥20 characters. |
| `SCRAPER_BIND` | Compose host binding, `127.0.0.1`. |
| `SCRAPER_PORT` | Compose host port, `8000`. |
| `SCRAPER_HOSTNAME` | Real DNS hostname for optional Caddy HTTPS. |
| `LOG_LEVEL` | `INFO`. |
| `FORWARDED_ALLOW_IPS` | Uvicorn trusted proxies; Compose trusts its private proxy network. |
| `SCRAPER_READ_ONLY` | `1` disables web mutations for a restricted-internet development preview; do not use for a real run. |

Do not run multiple replicas or Uvicorn workers against one SQLite database. A cloud provider's ephemeral container filesystem is insufficient unless `/data` has persistent storage. Never place the active WAL database on object-storage mounts, NFS, or Drive.
