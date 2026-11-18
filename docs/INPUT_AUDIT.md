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

The entire input was imported into the local, ignored `data/harvest.db` for the read-only development preview. No live domain crawl was started: this sandbox only permits outbound access to a small set of development/package hosts. There are **no real-site email yield claims** from this audit.

Neither `domains.csv`, its rows, nor the populated runtime database is committed/pushed. The deployment starts empty; upload your private input on your server. The repository contains only a tiny synthetic example and generated test fixtures.
