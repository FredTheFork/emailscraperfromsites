"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  token: sessionStorage.getItem("publicmail-key") || "",
  view: "overview", stats: null, settings: null,
  domainPage: 1, emailPage: 1, domainRequest: 0, emailRequest: 0,
  domainTotal: 0, emailTotal: 0, pageSize: 25, refreshing: false, readonly: false,
};
const number = (value) => new Intl.NumberFormat("en-GB").format(value || 0);
const statusNames = {
  pending: "Queued", running: "In progress", retry: "Retry scheduled", completed: "Email found",
  no_email: "No email found", blocked: "Blocked", failed: "Failed",
};
const viewCopy = {
  overview: ["Overview", "Your collection, in focus.", "A persistent queue. Public sources. Nothing guessed."],
  domains: ["Domains", "Every website. Every outcome.", "Explore your queue, inspect page trails, and understand the gaps."],
  emails: ["Email addresses", "Published contacts, with proof.", "Every address is backed by the public page where it was found."],
  settings: ["Crawl settings", "Set the pace. Keep the guardrails.", "Configuration is saved with your collection and survives restarts."],
};
let toastTimer;
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = "toast" + (error ? " error" : "");
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 8500 : 5000);
}
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function clear(element) { element.replaceChildren(); }
function statusBadge(status) { return node("span", statusNames[status] || status, "status " + status); }
function sourceLink(url, label) {
  const anchor = node("a", label || url, "source-link");
  try {
    const parsed = new URL(url);
    if (["http:", "https:"].includes(parsed.protocol)) anchor.href = parsed.href;
  } catch { /* Untrusted values remain plain text. */ }
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  anchor.title = url;
  return anchor;
}
function emptyRow(body, columns, message) {
  const row = node("tr", null, "empty-row");
  const cell = node("td", message);
  cell.colSpan = columns;
  row.append(cell);
  body.append(row);
}
async function api(path, options = {}) {
  const headers = { "Authorization": "Bearer " + state.token, "X-Requested-With": "PublicMail", ...(options.headers || {}) };
  let body = options.body;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch("/api/" + path, { ...options, headers, body, credentials: "omit", cache: "no-store" });
  if (!response.ok) {
    let message = "Request failed (HTTP " + response.status + ")";
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : message;
    } catch { /* Keep the HTTP error. */ }
    if (response.status === 401 && path !== "login") lockWorkspace();
    throw new Error(message);
  }
  if (options.download) return response.blob();
  return response.json();
}
function unlockWorkspace() {
  $("access-view").hidden = true;
  $("shell").hidden = false;
}
function lockWorkspace() {
  state.token = "";
  sessionStorage.removeItem("publicmail-key");
  $("shell").hidden = true;
  $("access-view").hidden = false;
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  $("access-key").value = "";
  $("access-key").focus();
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-button").disabled = true;
  $("login-error").textContent = "";
  const token = $("access-key").value.trim();
  try {
    await api("login", { method: "POST", body: { token } });
    state.token = token;
    sessionStorage.setItem("publicmail-key", token);
    $("access-key").value = "";
    unlockWorkspace();
    await refresh();
  } catch (error) { $("login-error").textContent = error.message; }
  finally { $("login-button").disabled = false; }
});
$("logout-button").addEventListener("click", lockWorkspace);

function updateStats(stats) {
  state.stats = stats;
  state.readonly = !!stats.read_only;
  const outcomes = stats.statuses;
  const review = outcomes.no_email + outcomes.blocked + outcomes.failed;
  const workerOnline = stats.worker_state === "online" && Date.now() / 1000 - stats.worker_heartbeat < 60;
  const running = stats.mode === "running";
  const draining = !running && outcomes.running > 0;
  const mappings = {
    "metric-total": stats.total, "metric-with-email": stats.with_email, "metric-unique": stats.unique_emails,
    "metric-pages": stats.pages_scanned, "processed-count": stats.terminal, "active-count": outcomes.running,
    "queued-count": outcomes.pending, "retry-count": outcomes.retry, "outcome-finished": stats.terminal,
    "nav-domain-count": stats.total, "nav-email-count": stats.unique_emails, "nav-review-count": review,
  };
  for (const [id, value] of Object.entries(mappings)) $(id).textContent = number(value);
  $("metric-coverage").textContent = stats.coverage_percent + "% of your list";
  $("processed-total").textContent = "/ " + number(stats.total) + " domains processed";
  $("processed-percent").textContent = stats.processed_percent + "%";
  $("progress-fill").style.width = stats.processed_percent + "%";
  $("queue-total-label").textContent = number(stats.total) + " websites";
  $("sidebar-worker").textContent = workerOnline ? "Worker online" : "Worker offline";
  $("sidebar-worker-dot").className = "dot " + (workerOnline ? "green" : "red");
  $("run-badge").textContent = !workerOnline ? "Offline" : running ? "Running" : draining ? "Pausing" : "Paused";
  $("run-badge").className = "badge" + (running && workerOnline ? " running" : "");
  $("run-symbol").textContent = running ? "▷" : "Ⅱ";
  let title, description;
  if (!workerOnline) {
    title = "Worker is not connected.";
    description = "Start or restart the server. Your saved evidence and queue are intact.";
  } else if (running) {
    title = outcomes.running ? "Collection is underway." : "Waiting for the next retry.";
    description = "New pages and source evidence are being saved as each request finishes.";
    if (stats.run_remaining !== null) description += " " + number(stats.run_remaining) + " site claims remain in this run.";
    if (!outcomes.running && stats.next_retry_at) description = "Next retry eligible " + dateTime(stats.next_retry_at) + ". Retry-After is respected.";
  } else if (draining) {
    title = "Finishing active websites.";
    description = "No new sites will start. Active sites finish their current crawl and checkpoint.";
  } else if (!stats.total) {
    title = "Your collection starts here.";
    description = "Import your domain CSV or database, then start with a 100-site pilot.";
  } else if (!outcomes.pending && !outcomes.retry) {
    title = "This pass is complete.";
    description = "Export the coverage report and review gaps before deciding on a deeper pass.";
  } else {
    title = "Ready when you are.";
    description = "Your queue is saved. Start a measured pilot or continue the full collection.";
  }
  $("run-title").textContent = title;
  $("run-description").textContent = description;
  $("pilot-button").hidden = running || draining;
  $("run-button").hidden = running || draining;
  $("pause-button").hidden = !running && !draining;
  const cannotStart = state.readonly || !workerOnline || !stats.total || !outcomes.pending && !outcomes.retry;
  $("pilot-button").disabled = cannotStart;
  $("run-button").disabled = cannotStart;
  $("pause-button").disabled = state.readonly || draining;
  $("import-button").disabled = state.readonly;
  $("retry-button").disabled = state.readonly || running || draining;
  $("preview-banner").hidden = !state.readonly;
  if (state.readonly) $("preview-banner").textContent = "READ-ONLY PREVIEW · " + number(stats.total) + " websites loaded. Queue controls are disabled. Deploy on your internet-connected server to run the real collection.";
  $("worker-error").textContent = stats.worker_error ? "Worker needs attention: " + stats.worker_error : "";
  $("worker-error").hidden = !stats.worker_error;
  $("heartbeat-label").textContent = stats.worker_heartbeat ? "Last heartbeat " + new Date(stats.worker_heartbeat * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "Waiting for heartbeat";
  const bars = $("outcome-bars"), list = $("outcome-list");
  clear(bars); clear(list);
  const rows = [
    ["completed", "Email found", "#779e6b"], ["pending", "Queued", "#d8dfcd"],
    ["running", "In progress", "#b5ce8e"], ["no_email", "No email", "#d3be87"],
    ["blocked", "Blocked", "#c8937c"], ["failed", "Failed", "#aa7868"], ["retry", "Retry scheduled", "#b2a16b"],
  ];
  for (const [key, label, color] of rows) {
    if (outcomes[key] > 0) {
      const bar = node("div"); bar.style.width = (outcomes[key] / stats.total * 100) + "%";
      bar.style.background = color; bar.title = label + ": " + number(outcomes[key]); bars.append(bar);
    }
    if (key === "running" && !outcomes[key]) continue;
    const row = node("div", null, "outcome-row");
    const dot = node("i", null, "dot"); dot.style.background = color;
    row.append(dot, node("span", label), node("strong", number(outcomes[key])));
    list.append(row);
  }
  updateSettingsLock();
}
function dateTime(value) {
  return new Date(value * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function domainCell(site, withSubtitle = false) {
  const cell = node("td"), wrap = node("div", null, "domain-cell");
  wrap.append(node("span", "◎", "domain-symbol"));
  const copy = node("div");
  copy.append(sourceLink(site.effective_url || site.input_url, site.domain));
  if (withSubtitle && site.business_name) copy.append(node("small", site.business_name, "domain-subtitle"));
  wrap.append(copy); cell.append(wrap); return cell;
}
function trailCell(site) {
  const cell = node("td", null, "trail-column");
  const button = node("button", "↗", "trail-button");
  button.title = "Inspect persisted pages for " + site.domain;
  button.setAttribute("aria-label", button.title);
  button.addEventListener("click", () => showTrail(site));
  cell.append(button); return cell;
}
function fillSiteTable(body, items, overview) {
  clear(body);
  if (!items.length) { emptyRow(body, 6, state.stats?.total ? "No domains match this filter." : "No domains yet. Import your CSV or SQLite database to build the queue."); return; }
  for (const site of items) {
    const row = node("tr");
    row.append(domainCell(site, !overview));
    if (overview) row.append(node("td", site.business_name || "—"));
    const status = node("td"); status.append(statusBadge(site.status)); row.append(status);
    row.append(node("td", number(site.email_count), "numeric"), node("td", number(site.pages_scanned), "numeric"));
    if (!overview) {
      const note = site.last_error || site.completion_reason.replaceAll("_", " ") || (site.status === "retry" ? "Eligible " + dateTime(site.next_attempt_at) : "—");
      const cell = node("td", note); cell.title = note; row.append(cell);
    }
    row.append(trailCell(site)); body.append(row);
  }
}
async function loadOverview() {
  const [domains, events] = await Promise.all([api("sites?size=7"), api("events")]);
  if (state.view !== "overview") return;
  fillSiteTable($("queue-body"), domains.items, true);
  const list = $("activity-list"); clear(list);
  if (!events.length) list.append(node("p", "Your worker activity will appear here. Import a list to get started.", "empty-activity"));
  for (const event of events.slice(0, 12)) {
    const item = node("div", null, "activity-item");
    item.append(node("i", null, "dot " + (event.level === "error" ? "red" : event.level === "warning" ? "amber" : "green")));
    const text = node("p");
    if (event.domain) text.append(node("strong", event.domain + " · "));
    text.append(document.createTextNode(event.message));
    item.append(text, node("time", dateTime(event.created_at)));
    list.append(item);
  }
}
function pageRange(total, page) {
  return total ? number((page - 1) * state.pageSize + 1) + "–" + number(Math.min(page * state.pageSize, total)) + " of " + number(total) : "0 results";
}
async function loadDomains() {
  const requestId = ++state.domainRequest;
  const query = new URLSearchParams({q: $("domain-search").value, status: $("domain-status").value, page: state.domainPage, size: state.pageSize});
  const data = await api("sites?" + query);
  if (requestId !== state.domainRequest) return;
  state.domainTotal = data.total;
  fillSiteTable($("domains-body"), data.items, false);
  $("domain-range").textContent = pageRange(data.total, state.domainPage);
  $("domain-prev").disabled = state.domainPage <= 1;
  $("domain-next").disabled = state.domainPage * state.pageSize >= data.total;
}
async function loadEmails() {
  const requestId = ++state.emailRequest;
  const query = new URLSearchParams({q: $("email-search").value, page: state.emailPage, size: state.pageSize});
  const data = await api("emails?" + query);
  if (requestId !== state.emailRequest) return;
  state.emailTotal = data.total;
  const body = $("emails-body"); clear(body);
  if (!data.items.length) emptyRow(body, 5, "No collected addresses yet. Publicly published emails will appear here with their source evidence.");
  for (const email of data.items) {
    const row = node("tr");
    row.append(node("td", email.email), node("td", email.domain));
    const relationship = node("td"); relationship.append(node("span", {
      on_domain: "Site domain", free_provider: "Free provider", external: "External / review",
    }[email.relationship] || email.relationship, "relationship " + email.relationship));
    row.append(relationship);
    const source = node("td"); source.append(sourceLink(email.source_url, "View public page ↗"));
    source.title = email.sources + " public source(s)";
    row.append(source, node("td", email.methods.replaceAll("_", " "), "method-cell"));
    body.append(row);
  }
  $("email-range").textContent = pageRange(data.total, state.emailPage);
  $("email-prev").disabled = state.emailPage <= 1;
  $("email-next").disabled = state.emailPage * state.pageSize >= data.total;
}
async function showTrail(site) {
  $("trail-title").textContent = site.domain;
  clear($("trail-body")); emptyRow($("trail-body"), 4, "Loading persisted frontier…");
  $("trail-dialog").showModal();
  try {
    const pages = await api("sites/" + site.id + "/pages");
    const body = $("trail-body"); clear(body);
    if (!pages.length) emptyRow(body, 4, "This domain is queued. No pages have been requested yet.");
    for (const page of pages) {
      const row = node("tr"), url = node("td");
      url.append(sourceLink(page.final_url || page.url));
      row.append(url, node("td", page.status), node("td", page.http_status || "—"), node("td", page.last_error || page.browser_status || page.kind));
      body.append(row);
    }
  } catch (error) { toast(error.message, true); }
}
async function refresh() {
  if (!state.token || state.refreshing) return;
  state.refreshing = true;
  try {
    updateStats(await api("status"));
    if (state.view === "overview") await loadOverview();
    if (state.view === "domains") await loadDomains();
    if (state.view === "emails") await loadEmails();
  } catch (error) { if (state.token) toast("Connection issue: " + error.message, true); }
  finally { state.refreshing = false; }
}
async function setView(view) {
  if (!viewCopy[view]) return;
  state.view = view;
  document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== "view-" + view; });
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  const [label, title, subtitle] = viewCopy[view];
  $("breadcrumb-current").textContent = label; $("page-title").textContent = title; $("page-subtitle").textContent = subtitle;
  try {
    if (view === "settings") await loadSettings();
    else if (view === "domains") await loadDomains();
    else if (view === "emails") await loadEmails();
    else await loadOverview();
  } catch (error) { toast(error.message, true); }
}
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
$("view-domains-button").addEventListener("click", () => setView("domains"));
$("review-nav").addEventListener("click", () => {
  $("domain-status").value = "no_email"; state.domainPage = 1; setView("domains");
  $("review-nav").classList.add("active");
});
function debounce(fn) { let timer; return () => { clearTimeout(timer); timer = setTimeout(() => fn().catch((error) => toast(error.message, true)), 300); }; }
$("domain-search").addEventListener("input", debounce(async () => { state.domainPage = 1; await loadDomains(); }));
$("domain-status").addEventListener("change", () => { state.domainPage = 1; loadDomains().catch((error) => toast(error.message, true)); });
$("email-search").addEventListener("input", debounce(async () => { state.emailPage = 1; await loadEmails(); }));
for (const [id, type, delta] of [["domain-prev", "domain", -1], ["domain-next", "domain", 1], ["email-prev", "email", -1], ["email-next", "email", 1]]) {
  $(id).addEventListener("click", () => {
    const key = type + "Page"; state[key] = Math.max(1, state[key] + delta);
    (type === "domain" ? loadDomains() : loadEmails()).catch((error) => toast(error.message, true));
  });
}
function confirmAction(title, description, label, action) {
  $("confirm-title").textContent = title;
  $("confirm-description").textContent = description;
  $("confirm-action").textContent = label;
  $("confirm-action").onclick = async () => {
    $("confirm-action").disabled = true;
    try { await action(); $("confirm-dialog").close(); await refresh(); }
    catch (error) { toast(error.message, true); }
    finally { $("confirm-action").disabled = false; }
  };
  $("confirm-dialog").showModal();
}
$("pilot-button").addEventListener("click", () => confirmAction("Start a measured pilot", "Process up to 100 site claims with the current settings. Retries count toward the limit. Inspect coverage and ownership of collected emails before running the whole list.", "Start 100-site pilot", () => api("control", {method: "POST", body: {action: "start", limit: 100}})));
$("run-button").addEventListener("click", () => confirmAction("Run the full saved queue?", "The worker will process all queued sites and scheduled retries. It saves progress continuously and stops at each website’s configured limits. There is no guaranteed address count. You can pause new sites at any time.", "Start collection", () => api("control", {method: "POST", body: {action: "start"}})));
$("pause-button").addEventListener("click", async () => {
  try { await api("control", {method: "POST", body: {action: "pause"}}); toast("Pause requested. Active sites will finish and checkpoint."); await refresh(); }
  catch (error) { toast(error.message, true); }
});

$("import-button").addEventListener("click", () => $("import-dialog").showModal());
$("export-button").addEventListener("click", () => $("export-dialog").showModal());
$("retry-button").addEventListener("click", () => { if ($("domain-status").value && !["pending", "running"].includes($("domain-status").value)) $("retry-status").value = $("domain-status").value; $("retry-dialog").showModal(); });
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
$("import-file").addEventListener("change", () => { $("upload-name").textContent = $("import-file").files[0]?.name || "Choose a file to import"; $("import-result").textContent = ""; });
$("import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("import-file").files[0]; if (!file) return;
  if (file.size > 256 * 1024 * 1024) { toast("File exceeds 256 MiB; use CLI import for larger databases.", true); return; }
  $("import-submit").disabled = true; $("import-result").textContent = "Importing and deduplicating… Keep this window open.";
  try {
    const body = new FormData(); body.append("file", file);
    const report = await api("import", {method: "POST", body});
    $("import-result").textContent = number(report.inserted) + " added · " + number(report.duplicates) + " already present · " + number(report.invalid) + " invalid. " + (report.invalid_examples.length ? "First issue: row " + report.invalid_examples[0].row + " — " + report.invalid_examples[0].reason : "Your collection is ready.");
    toast("Imported " + number(report.inserted) + " domains. No crawling has been started.");
    state.domainPage = 1; await refresh();
  } catch (error) { $("import-result").textContent = error.message; toast(error.message, true); }
  finally { $("import-submit").disabled = false; }
});
document.querySelectorAll("[data-export]").forEach((button) => button.addEventListener("click", async () => {
  button.disabled = true;
  try {
    const kind = button.dataset.export;
    const blob = await api("export/" + kind, {download: true});
    const url = URL.createObjectURL(blob), anchor = node("a");
    anchor.href = url; anchor.download = kind + ".csv"; document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000); toast("Your " + kind.replaceAll("_", " ") + " export is ready.");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}));
$("retry-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.submitter; if (submit) submit.disabled = true;
  try {
    const result = await api("retry", {method: "POST", body: {statuses: [$("retry-status").value], limit: Number($("retry-limit").value), fresh: $("retry-fresh").checked}});
    $("retry-dialog").close(); toast(number(result.requeued) + " domains requeued. Start a pilot when ready."); await refresh();
  } catch (error) { toast(error.message, true); }
  finally { if (submit) submit.disabled = false; }
});
async function loadSettings() {
  state.settings = await api("settings");
  for (const [key, value] of Object.entries(state.settings)) {
    const input = $("settings-form").elements.namedItem(key);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = value;
    else input.value = value;
  }
  updateSettingsLock();
}
function updateSettingsLock() {
  const locked = state.readonly || !state.stats || state.stats.mode === "running" || state.stats.statuses.running > 0;
  for (const input of $("settings-form").querySelectorAll("input, button[type=submit]")) input.disabled = locked;
  $("settings-lock-note").textContent = state.readonly ? "Read-only development preview." : locked ? "Pause and let active sites finish to edit settings." : "Changes apply on the next site claim. Robots and network safeguards always stay enabled.";
}
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.settings) return;
  const settings = {...state.settings};
  for (const input of $("settings-form").querySelectorAll("input[name]")) settings[input.name] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
  $("save-settings-button").disabled = true;
  try { state.settings = await api("settings", {method: "PUT", body: settings}); toast("Crawl settings saved."); }
  catch (error) { toast(error.message, true); }
  finally { updateSettingsLock(); }
});

if (state.token) {
  unlockWorkspace();
  refresh().catch((error) => toast(error.message, true));
}
setInterval(() => { if (!document.hidden && state.token) refresh(); }, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.token) refresh(); });
