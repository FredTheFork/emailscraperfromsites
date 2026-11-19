# Architecture and failure semantics

## Data flow

```text
CSV / source SQLite (read-only)
          │ normalize + deduplicate, retain metadata
          ▼
state SQLite: sites ──claim──► async site workers (default 24)
                  ▲                 │
                  │          robots + public DNS guard
                  │                 │
                  │       paced, bounded HTTP requests
                  │                 │
                  │      HTML / text / sitemap / PDF parser
                  │                 │
                  └──── ATOMIC COMMIT ── page + evidence + discovered frontier
                                    │
                        optional bounded JS fallback
                                    │
                     completed / no_email / retry / blocked / failed
                                    │
                       authenticated dashboard + CSV/JSONL
```

**One runner per database.** The FastAPI service embeds the same runner as the CLI. A file lock lives beside the state DB; a second runner refuses to start. API/CLI import, status, backup, and export operations may use separate connections/processes. Do not use a network filesystem or a distributed/shared-volume deployment.

## Persistent schema

- `sites`: original domain, preferred starting URL, business name, metadata JSON, status, attempt count, lease owner/time, effective homepage, next-attempt time, per-site totals, completion/error/limit indicators.
- `pages`: unique `(site_id, url)` frontier, depth, priority, kind, seed/page role, pending/fetching/done/error/blocked/skipped state, attempt count, final URL, HTTP/content metadata, error, browser state.
- `email_addresses`: globally normalized, unique addresses and first/last seen.
- `observations`: unique `(site_id, email, source_url, method)` evidence, confidence, relationship, short context, timestamps.
- `meta`: schema settings, desired run/pause mode, remaining pilot claim budget, worker heartbeat/error.
- `request_cooldowns`: registrable-domain cooldowns shared by all workers and persisted across restarts; a different input alias must not evade a server's Retry-After.
- `imports`: batch import summary. `events`: latest ~10,000 operational events (bounded).

Schema version 1 uses SQLite WAL, `synchronous=FULL`, foreign keys, prepared parameters, short immediate write transactions, and a busy timeout. Downloaded bodies are not persisted. Corrupted/unsupported state or an input database used accidentally as state is rejected rather than deliberately migrated into the input.

## Atomicity and resumption

A page's observations, discovered links, terminal page state, and homepage adoption (if applicable) commit in **one transaction**. If a crash occurs before commit, the transaction rolls back and the fetching page is eligible for refetch. If it occurs after commit, the evidence and frontier both survive. A page can be requested more than once after failure (at-least-once processing); unique keys make evidence writes idempotent. This is not an exactly-once network guarantee.

Workers mark site ownership/leases. Heartbeats extend leases. On startup, after obtaining the exclusive file lock, the runner knows the former local runner no longer owns it and recovers **all interrupted running sites**, even if their old lease timestamps are in the future. In-flight pages become pending. A killed site claim is not allowed to strand its frontier by consuming the last whole-site attempt. The lock, not lease expiry alone, is the single-machine mutual-exclusion authority.

Desired mode survives restart. Pause means no new claims, **not** immediate cancellation of active sites. A normal shutdown drains for up to 30 seconds and then cancels active network/PDF/browser operations; remaining running sites are released for resume. A full site timeout saves prior completed pages and schedules a retry up to the configured attempt limit. A site with saved emails can ultimately finish with `site_timeout`; that is visible partial coverage, not exhaustive success.

Each import commits batches of 500 valid normalized rows. Reimport is idempotent and does not erase results or update an already-imported identity's original metadata. If parsing fails midway, earlier batches remain and can safely be encountered again.

## Scheduling, pacing, and failures

- Default: at most 24 concurrent site tasks and 24 HTTP requests globally. Site tasks crawl sequentially; the optional browser also shares the global cap.
- Minimum delay is keyed by registrable domain using an offline bundled public suffix list, including private suffixes. Apex/www/subdomains of one site share pacing. This intentionally groups more than just origins.
- Each origin's robots is fetched and parsed before content; every redirect hop and every permitted rendered resource is checked. Caching avoids repeated robots fetches. Crawl-delay/request-rate can increase the minimum delay.
- Unreachable robots and robots 5xx/429 fail closed. 404/410 robots means no rules. Robots 401/403 or disallowed homepage means blocked. A disallowed individual content path is skipped while allowed paths remain eligible.
- Ordinary network/TLS/404 homepage failures can try supplied URL, HTTPS apex/www, then optional public HTTP apex/www. This is **never** used as a workaround for 401/403 or a challenge. TLS certificate verification is never disabled; HTTP fallback is explicitly plaintext and optional.
- 429 and transient failures schedule a durable future site attempt, with exponential backoff, positive jitter, and no earlier than `Retry-After`. Work on unrelated sites can continue. No spin/sleep loop monopolizes a worker for a long Retry-After.
- 401/403 content and recognized access challenges stop the site and preserve any prior evidence. No proxy pool, CAPTCHA solver, browser-fingerprint spoofing, or authentication is used.
- Retry budgets are finite. Exhausted rate-limited sites become blocked. Persistent content-page failures remain in the evidence trail and coverage report even when another page produced emails.

Robots parsing uses Protego, including wildcard/end-anchor rules, longest matching rules with allow precedence, agent groups, fractional crawl-delay, and request-rate directives. Unusual nonstandard dialects can still need review. The bounded LRU policy cache does not retain every robots file from a 33k-site run. The bundled suffix snapshot avoids startup network dependencies but may lag newly introduced suffixes. Update and retest dependencies periodically.

## Discovery and finite limits

The starting page is depth 0. Contact/enquiry links score highest, then about/team/location/help, legal/privacy, PDFs, other internal pages, and low-priority blog/product pages. URL fragments/tracking parameters are removed, only a small set of content query keys is followed, and obvious search/login/cart/calendar/asset paths are excluded. It is a bounded public-page crawl, not a form submitter or an entire web mirror.

Homepage HTML links, a few common contact paths, and robots/default sitemap URLs seed the frontier. Sitemaps may reveal orphan contact/legal pages and PDF documents. XML external entities are forbidden; compressed/HTTP-decoded data and candidate counts are bounded. Large sitemaps are scanned only up to the configured number of accepted URLs, so useful entries after that point can be missed.

Limits include content-page attempts, link depth, candidate frontier, total site wall-clock per attempt, request timeout, sitemap documents/URLs, PDF documents/bytes/pages/text, and browser pages/resources/time. The defaults favor a practical first pass, not an impossible promise of all addresses. Reasons and skipped/error/pending candidates reveal where the limits applied. Increasing the saved frontier cap cannot recreate already-discarded links; use a fresh pass if needed.

## Extraction and evidence quality

Each normalized address records how it was found and the exact final public source URL. There is no filtering to the website's own domain; personal/free-provider addresses and third-party addresses are retained. Relationship flags are relative to the **original domain**, not an ownership verification system.

Extraction covers mailto links, normal text, HTML entities, JSON-LD/literal scripts, named email attributes, common at/dot spacing/obfuscation, XOR email obfuscation, ROT13 attributes, simple literal concatenation/reversal, and PDF text. JavaScript is never `eval`uated by the static extractor. Example domains, obvious image suffixes, and known telemetry-looking addresses are filtered to reduce noise; false positives and false negatives remain possible. Rare quoted or non-ASCII local parts are not fully supported. `high`/`medium` refer to evidence syntax, **not** mailbox validity.

Email normalization lowercases the local part as well as IDNA domain, a documented practical deduplication choice. One email may appear on many input websites and in several methods/pages. The main email export aggregates per original website/address; the observations ledger retains all individual sources/methods. No SMTP/DNS deliverability check or message is sent.

PDF parsing runs in a separate subprocess with CPU/address-space limits where available, a wall-clock timeout, and bounded input/output. It extracts text only; OCR and encrypted documents are not solved. PDF limits/errors are explicit in page trails. HTML parsing uses background threads to avoid tying up the async request loop, with bounded input size.

## Optional browser boundary

When enabled, only sites with no HTTP-found email enter a small rendered fallback (default two selected HTML pages). Chromium has a dead direct-network proxy and DNS disabled. Routes are fulfilled through the existing safe aiohttp client, which pins public DNS answers and enforces robots, pacing, scope, body bounds, and redirects. Non-GET traffic, cross-site resources, service workers, downloads, and WebSockets are blocked. A real access challenge remains blocked.

This is intentionally not a complete normal browser session. Sites depending on third-party bundles/APIs/cookies may stay empty. Chromium/PDF parsers still process untrusted data; non-root containers, host egress restrictions, updates, and resource monitoring remain important. The optional browser path needs an actual Chromium installation; default CI does not prove live-site rendering coverage.

## Dashboard and operations

All data/control APIs require a constant-time-compared bearer access key. The static UI/login shell and a minimal health/version endpoint are public; no domain/email data is included in those files. The browser keeps the key in per-tab session storage, avoiding cross-site-preview cookie issues and token-in-URL logs. No inline scripts or remote frontend dependencies are needed. User-provided values are rendered as text, not HTML. Mutating requests reject mismatched Origin headers. Login attempts and body/upload sizes are bounded.

CSV formula injection is escaped for spreadsheet consumers; JSONL preserves exact data. Exports stream through independent read-only SQLite connections. Each export query sees a read snapshot; files generated at different times during a live crawl can differ in totals. Backups use SQLite's online backup API and require separate protected/offsite storage for machine-loss recovery.

The CLI web launcher supervises the embedded worker: an unexpected runtime failure exits the service for a process supervisor to restart. Health also reports stale/stopped workers. No guarantee is made against disk loss, credential leakage, OS compromise, or a provider suspending the server. The deployment guide describes backups, least exposure, and explicit recovery.
