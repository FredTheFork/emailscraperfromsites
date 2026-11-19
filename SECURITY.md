# Security and responsible operation

This is a **private, single-operator tool**, not a public arbitrary-URL proxy or multi-tenant scraping service.

- Keep the dashboard behind an SSH tunnel or HTTPS. Compose publishes the app on loopback only by default. Use a unique random access key and protect `.env`, `data/admin-token`, results, and backups. No credentials belong in Git.
- Use one service process/runner and local persistent storage. Do not share the SQLite WAL database over NFS/Drive or disable the worker lock.
- Input URLs and redirects are restricted to public web schemes/ports and validated hostnames. IP literals, credentials, private/reserved/mixed DNS answers, and several cloud-metadata addresses are refused. DNS filtering occurs in the actual HTTP connector, not merely before a second lookup. Environment HTTP proxies are not inherited.
- The optional browser's direct networking is disabled; permitted same-site GET resources are fulfilled by the same safe HTTP client. Non-GET traffic, third parties, WebSockets, downloads, and service workers are blocked. No browser login or challenge bypass is provided.
- Treat all remote pages, PDFs, and input databases as untrusted. XML entities are disabled; download, parser, process, and crawl budgets reduce resource abuse. They are not a formal sandbox proof. Keep dependencies/OS current, run the non-root image, restrict host egress to internal networks/cloud metadata, and monitor CPU/RAM/disk. Playwright's normal launch may not have a full OS browser sandbox inside every container; never use privileged mode as a workaround.
- Unreachable robots policies fail closed. Do not remove robots/access controls to chase a target number. Follow terms and applicable data-protection requirements.
- No outbound messages, mailbox guessing, SMTP checks, proxy rotation, CAPTCHA solving, or login scraping. An email found in a public page can still be personal data and may not be the business's own contact. Publication is not marketing permission.
- Evidence and original metadata are retained until the operator deletes them. There is no automatic retention/suppression workflow. Establish your own retention, deletion, lawful-basis, opt-out, and backup policies before use.
- The UI stores its bearer key in the current tab's session storage. Do not load untrusted scripts into the same origin. The API uses text-safe rendering/exports and no third-party frontend assets. Login rate limiting is in-memory and per process; use a reverse proxy/firewall for stronger protection if exposed.
- Access framing restrictions are intentionally omitted for Arena's embedded preview; data/control still require the access key. On a separately deployed production host you may add `frame-ancestors 'none'` in your reverse proxy if you do not need embedding.

If you find a vulnerability, report it privately to the repository owner using an available private channel or GitHub's private vulnerability reporting if enabled. Do not post real access keys, private datasets, or scraped personal data in a public issue. Rotate a leaked key and restrict access immediately.

## Google Colab

The notebook keeps application dependencies isolated and the live database on local runtime storage. Mount Drive only through Colab's normal authorization flow. Keep input, snapshots, exports and notebook outputs private; clear outputs before sharing. Snapshots are checksum-verified but not encrypted by this application, and Drive account permissions/retention are the operator's responsibility. Use only one runtime per project: Drive's advisory session marker is not a distributed lock. Runtime termination can lose work since the last fully saved snapshot. No keep-alive or platform-limit evasion is included. See `docs/COLAB.md`.
