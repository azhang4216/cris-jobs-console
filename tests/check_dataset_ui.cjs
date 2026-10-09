/* Optional browser-DOM regression checks, with no SSH and no new project dependency.
 * JSDOM_MODULE=/path/to/node_modules/jsdom node tests/check_dataset_ui.cjs
 * Uses .venv/bin/python to render the template; override with PYTHON_BIN if needed.
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { JSDOM } = require(process.env.JSDOM_MODULE || "jsdom");
const root = path.resolve(__dirname, "..");
const html = execFileSync(process.env.PYTHON_BIN || path.join(root, ".venv/bin/python"), ["-c", `
from jinja2 import Environment, FileSystemLoader, select_autoescape
env = Environment(loader=FileSystemLoader("dmasif_console/templates"), autoescape=select_autoescape(["html"]))
print(env.get_template("history.html").render(settings={"mode":"fake"}, status_labels={}, runs=[], activity=[], filters={}, stats={}))
`], { cwd: root, encoding: "utf8" });
const dom = new JSDOM(html, { url: "https://console.example/", runScripts: "outside-only", pretendToBeVisual: true });
const window = dom.window;
const requests = [];
const timers = [];
const pendingCatalog = [];
const pendingFiles = [];
const el = (id) => window.document.getElementById(id);
const catalogCalls = () => requests.filter(({ url }) => url.endsWith("/api/datasets"));
const fileCalls = () => requests.filter(({ url }) => url.includes("/api/datasets/files?"));
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(setImmediate); };
const catalogData = {
  datasets: [
    { name: '<script>alert("unsafe")</script>', scope: "registered", dataset_id: "registered-1", description: "Atomic <coordinates>", file_count: 123, available: true, enabled: true },
    { name: "structures & models", scope: "project", dataset_id: null, description: "CIF models", file_count: 223972, available: true, enabled: false },
    { name: "Missing", scope: "registered", dataset_id: "missing", description: "", file_count: null, available: false, enabled: true },
  ], source: "ssh", checked_at: "2026-10-09T16:00:00Z", truncated: true,
};
const respond = (queue, data) => queue.shift().resolve({ ok: true, json: async () => data });
const filesData = (page, files, hasMore = false) => ({ files, page, page_size: 50, has_more: hasMore, source: "ssh", checked_at: "2026-10-09T16:01:00Z", truncated: false });
const coordinate = (id) => ({ name: `${id}.cif`, model_id: id, relative_path: `${id}/system.cif`, format: "cif" });
window.AbortSignal.timeout = (milliseconds) => { assert([12000, 45000].includes(milliseconds)); return undefined; };
window.HTMLElement.prototype.scrollIntoView = () => {};
window.setInterval = () => 0;
window.setTimeout = (callback, delay) => { timers.push({ callback, delay }); return timers.length; };
window.clearTimeout = () => {};
window.fetch = (url, options) => {
  requests.push({ url, options });
  if (url.includes("/api/datasets/files?")) return new Promise((resolve, reject) => pendingFiles.push({ resolve, reject, options }));
  if (url.endsWith("/api/datasets")) return new Promise((resolve, reject) => pendingCatalog.push({ resolve, reject, options }));
  return Promise.resolve({ ok: true, json: async () => ({ runs: [], activity: [], actors: [] }) });
};

(async () => {
  window.eval(fs.readFileSync(path.join(root, "dmasif_console/static/console.js"), "utf8"));
  await flush();
  assert.equal(catalogCalls().length, 1);
  assert.equal(el("refresh-datasets").disabled, true);
  assert.match(el("datasets-message").textContent, /Loading/);
  assert.equal(catalogCalls()[0].options.cache, "no-store");
  el("refresh-datasets").dispatchEvent(new window.Event("click"));
  assert.equal(catalogCalls().length, 1, "No overlapping catalog requests");
  respond(pendingCatalog, catalogData);
  await flush();
  assert.equal(el("datasets-body").children.length, 3);
  assert.equal(el("datasets-body").querySelector("script"), null);
  assert.match(el("datasets-body").textContent, /<script>alert\("unsafe"\)<\/script>/);
  for (const text of ["Enabled for runs", "Not registered", "Not enabled for runs", "Unavailable"]) {
    assert(el("datasets-body").textContent.includes(text));
  }
  assert.equal(el("datasets-truncated").hidden, false);
  assert.match(el("datasets-checked").textContent, /Live cluster via SSH/);
  const browse = () => [...window.document.querySelectorAll(".dataset-browse")];
  assert.equal(browse()[2].disabled, true);

  await timers.find(({ delay }) => delay === 15000).callback();
  await flush();
  const search = window.document.querySelector('input[name="experiment"]');
  search.value = "changed";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  await timers.findLast(({ delay }) => delay === 250).callback();
  await flush();
  assert.equal(catalogCalls().length, 1, "Run polling and filters never refetch datasets");
  assert.equal(fileCalls().length, 0, "Files are fetched only on demand");

  browse()[0].click();
  assert.equal(el("dataset-files").hidden, false);
  assert.equal(el("next-dataset-files").disabled, true);
  assert.equal(el("dataset-files-page").disabled, true);
  let query = new URL(fileCalls().at(-1).url, window.location.href).searchParams;
  assert.equal(query.get("scope"), "registered");
  assert.equal(query.get("name"), catalogData.datasets[0].name);
  assert.equal(query.get("page_size"), "50");
  assert.equal(fileCalls().at(-1).options.cache, "no-store");
  const files = Array.from({ length: 50 }, (_, index) => coordinate(`model-${index}`));
  files[0] = coordinate("<script>model</script>");
  respond(pendingFiles, filesData(1, files, true));
  await flush();
  assert.equal(el("dataset-files-body").children.length, 50);
  assert.equal(el("dataset-files-body").querySelector("script"), null);
  assert.match(el("dataset-files-body").textContent, /<script>model<\/script>/);
  assert.equal(el("previous-dataset-files").disabled, true);
  assert.equal(el("next-dataset-files").disabled, false);

  el("next-dataset-files").click();
  assert.equal(el("dataset-files-body").children.length, 0, "Page changes immediately clear previous rows");
  respond(pendingFiles, filesData(2, [], true));
  await flush();
  assert.equal(el("dataset-files-message").textContent, "No readable coordinate files on this page.");
  assert.equal(el("next-dataset-files").disabled, false, "An empty sparse page can still have more files");
  assert.equal(el("previous-dataset-files").disabled, false);

  el("dataset-files-page").value = "6000";
  el("dataset-page-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  query = new URL(fileCalls().at(-1).url, window.location.href).searchParams;
  assert.equal(query.get("page"), "6000");
  respond(pendingFiles, { ...filesData(6000, [coordinate("deep-model")]), truncated: true });
  await flush();
  assert.equal(el("dataset-files-page").value, "6000");
  assert.equal(el("dataset-files-truncated").hidden, false);
  assert.equal(el("next-dataset-files").disabled, true);
  el("previous-dataset-files").click();
  pendingFiles.shift().resolve({ ok: false, status: 503 });
  await flush();
  assert.equal(el("dataset-files-body").children.length, 0);
  assert.equal(el("dataset-files-retry").hidden, false);
  assert.equal(el("dataset-files-checked").textContent, "");
  el("retry-dataset-files").click();
  query = new URL(fileCalls().at(-1).url, window.location.href).searchParams;
  assert.equal(query.get("page"), "5999", "Retry repeats the failed page");
  respond(pendingFiles, filesData(5999, [coordinate("recovered")], true));
  await flush();

  el("next-dataset-files").click();
  const superseded = pendingFiles.shift();
  browse()[1].click();
  assert.equal(superseded.options.signal.aborted, true, "Switching collections cancels the old request");
  respond(pendingFiles, filesData(1, [coordinate("current-collection")]));
  await flush();
  superseded.resolve({ ok: true, json: async () => filesData(6000, [coordinate("stale-collection")]) });
  await flush();
  assert.match(el("dataset-files-body").textContent, /current-collection/);
  assert(!el("dataset-files-body").textContent.includes("stale-collection"));
  assert.match(el("dataset-files-title").textContent, /structures & models/);

  const beforeInvalid = fileCalls().length;
  el("dataset-files-page").value = "100001";
  el("dataset-page-form").dispatchEvent(new window.Event("submit", { cancelable: true }));
  assert.equal(fileCalls().length, beforeInvalid, "Out-of-range pages are rejected");
  el("close-dataset-files").click();
  assert.equal(el("dataset-files").hidden, true);
  assert.equal(window.document.activeElement, browse()[1], "Close returns focus to the collection");

  browse()[0].click();
  const oldInventory = pendingFiles.shift();
  el("refresh-datasets").click();
  assert.equal(catalogCalls().length, 2);
  assert.equal(el("datasets-body").children.length, 0);
  assert.equal(el("dataset-files").hidden, true);
  assert.equal(oldInventory.options.signal.aborted, true);
  oldInventory.resolve({ ok: true, json: async () => filesData(1, [coordinate("old-inventory")]) });
  pendingCatalog.shift().resolve({ ok: false, status: 503 });
  await flush();
  assert.equal(el("dataset-files-body").children.length, 0, "Catalog refresh invalidates pending file results");
  assert.equal(el("datasets-table").hidden, true);
  assert.equal(el("datasets-footer").hidden, true);
  assert.equal(el("refresh-datasets").textContent, "Retry");
  assert.equal(el("refresh-datasets").disabled, false);
  assert.equal(el("datasets-message").dataset.state, "error");
  el("refresh-datasets").click();
  respond(pendingCatalog, { datasets: [], source: "demo", checked_at: "2026-10-09T16:02:00Z", truncated: false });
  await flush();
  assert.match(el("datasets-message").textContent, /No datasets/);
  assert.match(el("datasets-checked").textContent, /Demo data/);
  assert.equal(el("dataset-count").textContent, "0");
  assert.equal(el("datasets-truncated").hidden, true);
  assert.equal(el("refresh-datasets").textContent, "Refresh");
  console.log("Dataset UI checks passed: live catalog, safe text, polling isolation, pagination/jumps, sparse pages, retry, cancellation, stale response protection, empty results.");
  window.close();
})().catch((error) => {
  console.error(error);
  window.close();
  process.exitCode = 1;
});
