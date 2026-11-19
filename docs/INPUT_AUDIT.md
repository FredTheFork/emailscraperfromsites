# Supplied input audit

Inspected on 18 November 2026. This describes the **user-supplied CSV**, not the contents of the public repository and not verified real-site crawl yield.

| Check | Result |
| --- | ---: |
| CSV rows | 33,144 |
| Unique nonempty domain values | 33,144 |
| Duplicate domain values | 0 |
| Valid normalized domains imported | 33,144 |
| Rejected import rows | 0 |
| Rows with a URL | 33,144 |
| Rows with business name | 3,868 |
| Rows with country | 20,254 |
| Rows with city | 3,820 |
| Rows with postcode | 3,858 |
| Rows with phone | 3,673 |
| Rows with a page title / meta description | 204 |

Columns:

```text
domain, url, business_name, categories, country, region, city, postcode,
address, phone, lat, lon, sources, source_count, confidence, sample_query,
page_title, meta_description, http_status, verified, final_url,
discovered_at, updated_at, verified_at
```

The CSV has **120 more domains than the 33,024 total shown at the earlier Common Crawl stage in the pasted log**. This is a difference between the supplied artifacts, not proof of what happened to the old notebook.

The old harvester is a domain-discovery pipeline, not evidence that every site has a published email or is still active. Keyword/index discovery can include unrelated, parked, outdated, or duplicate-business domains. This scraper preserves supplied metadata and collects public evidence; it does not independently verify industry membership or ownership.

An earlier read-only development preview imported the entire input into an ignored local state DB; runtime DBs are not part of the repository. No live domain crawl was started: this sandbox only permits outbound access to a small set of development/package hosts. There are **no real-site email yield claims** from this audit.

Neither `domains.csv`, its rows, nor the populated runtime database is committed/pushed. The deployment starts empty; upload your private input on your server. The repository contains only a tiny synthetic example and generated test fixtures.

## Colab workflow validation

The supplied CSV was also imported through the new Colab session helper and restored into a separate fresh-runtime directory from a verified online SQLite snapshot: **33,144 inserted, zero invalid, 33,144 pending after restore**. No target-site crawl ran; there were zero scanned pages and zero collected emails in that audit. Local directories simulated the backup filesystem for this check; a Google account/Drive cloud mount was not accessed. This establishes input/recovery behavior, not real-site yield or a guarantee of uninterrupted Colab hosting.

The original input is available privately in the workspace as `data/inbox/domains.csv` and remains ignored by Git. The notebook asks for a private upload or Drive file path on its first run, then retains the queue and a private saved-input copy for later sessions. It cannot transfer an Arena upload into someone else's Google Drive without that user's normal Colab upload/authorization step.

## Full-list default / dispatch regression validation

The original CSV was revalidated after removing the notebook's default 100-claim pilot and five-hour stop. Through `Workspace.import_input` and a **separate fresh-runtime restore**, it produced **33,144 imported, zero invalid, 33,144 restored, zero sites started / target requests** in **4.00 seconds** on this sandbox. Local directories simulated Drive; this is not a Google cloud/mount test.

A separate, explicitly marked **SYNTHETIC-DISPATCH-ONLY** copy then exercised the real queue claim/finish transactions: **all 33,144 IDs claimed exactly once, zero remaining entries, 13.98 seconds**, using the new due-order index. This benchmark performed **zero target requests and collected zero emails**. Synthetic completion markers in that ignored benchmark DB are **not real crawl progress** and must never be restored/exported as a customer's collection. The original/restored audit queues remain unstarted. Private source/state files stay ignored.

Automated full-run tests separately use **257 synthetic websites** with the actual HTTP engine and default automatic profiles (mocked network/browser), checking that no 100-claim cap remains, every website starts, deeper/recovery stages finish, coverage has one row per input and an already-finished rerun issues no new target requests. Additional fixtures verify deeper-only emails, early rendered navigation, contact rendering despite designer emails, stage-boundary recovery, denied-site exclusion and no fabricated count on contact-form-only sites. These tests validate behavior, **not live-site address yield**. No claim of collecting thousands of real addresses from the supplied list has been made.
