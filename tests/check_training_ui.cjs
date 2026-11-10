/* Optional DOM regression checks; no SSH or live service.
 * JSDOM_MODULE=/path/to/node_modules/jsdom node tests/check_training_ui.cjs
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { JSDOM } = require(process.env.JSDOM_MODULE || "jsdom");
const root = path.resolve(__dirname, "..");
const code = fs.readFileSync(path.join(root, "dmasif_console/static/console.js"), "utf8");
const attack = '<img src=x onerror="alert(1)">';
const run = {
  id: "training-1", experiment: "surface-encoder", state: "RUNNING", dataset_id: "dataset_v2",
  actor_login: "researcher", created_at: "2026-11-10T12:00:00Z", log_tail: "first output",
  last_observed_at: new Date().toISOString(), config: { job_type: "dmasif_train", batch_size: 8, steps: 1000 },
  training: {
    phase: "training", step: 200, total_steps: 1000, event_count: 60,
    latest_metrics: { loss: 0.125, additional_loss: null, [attack]: 0.75 },
    latest_evaluation: { step: 150, timestamp: "2026-11-10T12:03:00Z", metrics: { recall_at_1: 0.43, custom_eval: 0.22 } },
    checkpoints: [{ name: attack, step: 150, timestamp: "2026-11-10T12:03:01Z" }],
  }, artifacts: [{ name: "latest.pt", size: 128, cached: false }],
};

function setup(page = "detail", sourceRun = run) {
  const html = execFileSync(process.env.PYTHON_BIN || path.join(root, ".venv/bin/python"), ["-c", `
import json,sys
from jinja2 import Environment, FileSystemLoader, select_autoescape
env=Environment(loader=FileSystemLoader("dmasif_console/templates"),autoescape=select_autoescape(["html"]))
run=json.loads(sys.stdin.read())
print(env.get_template("${page}.html").render(settings={"mode":"fake"},status_labels={},run=run,runs=[run],jobs=[],events=[],activity=[],filters={},stats={}))
`], { cwd: root, encoding: "utf8", input: JSON.stringify(sourceRun) });
  const dom = new JSDOM(html, { url: `https://console.example/${page === "detail" ? "runs/training-1" : ""}`, runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  const requests = [], timers = [], pending = [];
  let currentRun = structuredClone(sourceRun);
  w.AbortSignal.timeout = () => undefined;
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.setInterval = () => 0;
  w.setTimeout = (callback, delay) => { timers.push({ callback, delay }); return timers.length; };
  w.clearTimeout = () => {};
  w.fetch = (url, options) => {
    requests.push({ url, options });
    if (url.includes("/training-events?")) return new Promise((resolve, reject) => pending.push({ resolve, reject, options, url }));
    assert(!url.includes("/api/datasets"), "Removed explorer must never query the VM");
    return Promise.resolve({ ok: true, json: async () => page === "detail" ? { run: currentRun, events: [], jobs: [] } : { runs: [currentRun], activity: [], actors: ["researcher"] } });
  };
  const el = (id) => w.document.getElementById(id);
  const respond = (events, next = null, total = events.length) => pending.shift().resolve({ ok: true, json: async () => ({ events, next_before_seq: next, total }) });
  const query = () => new URL(requests.filter(({ url }) => url.includes("/training-events?")).at(-1).url, w.location.href).searchParams;
  w.eval(code);
  return { w, el, requests, pending, timers, respond, query, setRun: (value) => { currentRun = value; } };
}
const flush = async () => { for (let n = 0; n < 5; n++) await new Promise(setImmediate); };
const event = (seq, kind = "step", step = seq) => ({ schema_version: 1, seq, kind, step, timestamp: "2026-11-10T12:00:00Z", metrics: { loss: 0.12, [attack]: 0.33, custom: null }, name: kind === "checkpoint" ? attack : undefined });

(async () => {
  const ui = setup(); await flush();
  assert.equal(ui.pending.length, 1);
  assert.equal(ui.query().get("limit"), "50");
  ui.respond([event(60), event(59, "evaluation"), event(58, "checkpoint")], 58, 60); await flush();
  assert.equal(ui.el("training-events-body").children.length, 3);
  assert.match(ui.el("training-step").textContent, /200.*1,000/);
  assert.equal(ui.el("training-batch").textContent, "8");
  assert.match(ui.el("evaluation-metrics").textContent, /custom_eval/);
  assert.match(ui.el("training-events-body").textContent, /custom/);
  assert.match(ui.el("training-events-body").textContent, /<img/);
  assert.equal(ui.w.document.querySelectorAll("img").length, 0, "Metric names, checkpoint names, and phases render as text");
  assert.equal(ui.el("training-older").disabled, false);
  assert.equal(ui.el("training-newer").disabled, true);
  assert.match(ui.el("artifacts-list").textContent, /PT/);

  ui.el("training-older").click(); await flush();
  assert.equal(ui.query().get("before_seq"), "58");
  assert.equal(ui.el("training-events-body").children.length, 0, "Paging clears the previous page");
  ui.respond([event(57), event(56)], 56, 60); await flush();
  const calls = ui.requests.length;
  await ui.timers.findLast(({ delay }) => delay === 15000).callback(); await flush();
  assert.equal(ui.requests.length, calls + 1, "Status polling does not replace an older metrics page");
  assert.equal(ui.el("training-events-body").firstChild.dataset.sequence, "57");
  ui.el("training-newer").click(); await flush();
  assert.equal(ui.query().get("before_seq"), null);
  ui.respond([event(62)], 62, 62); await flush();

  ui.el("view-evaluations").click(); await flush();
  assert.equal(ui.query().get("kind"), "evaluation");
  assert.equal(ui.w.document.activeElement.id, "training-events-title");
  ui.respond([event(61, "evaluation", 200)], null, 1); await flush();
  ui.el("training-step-filter").value = "200";
  ui.el("training-filters").dispatchEvent(new ui.w.Event("submit", { bubbles: true, cancelable: true })); await flush();
  assert.equal(ui.query().get("kind"), "evaluation");
  assert.equal(ui.query().get("step"), "200");
  ui.respond([event(61, "evaluation", 200)]); await flush();
  const polling = ui.timers.findLast(({ delay }) => delay === 15000).callback(); await flush();
  assert.equal(ui.query().get("kind"), "evaluation");
  assert.equal(ui.query().get("step"), "200", "Exact-step filter survives polling");
  ui.respond([event(61, "evaluation", 200)]); await polling; await flush();

  ui.el("training-step-filter").value = "999";
  ui.el("training-filters").dispatchEvent(new ui.w.Event("submit", { bubbles: true, cancelable: true })); await flush();
  ui.respond([]); await flush();
  assert.match(ui.el("training-events-message").textContent, /No events match/);
  assert.equal(ui.el("training-events-table").hidden, true);
  ui.el("training-retry").click(); await flush();
  ui.pending.shift().reject(new Error("Network unavailable")); await flush();
  assert.equal(ui.el("training-retry").hidden, false);
  assert.match(ui.el("training-events-message").textContent, /delayed/);

  // Rapid filter changes must ignore an older response even if fetch ignores abort.
  ui.el("view-checkpoints").click(); await flush();
  const old = ui.pending.shift();
  ui.el("view-evaluations").click(); await flush();
  ui.respond([event(70, "evaluation", 300)]); await flush();
  old.resolve({ ok: true, json: async () => ({ events: [event(69, "checkpoint")], total: 1 }) }); await flush();
  assert.equal(ui.el("training-events-body").firstChild.dataset.sequence, "70");
  assert.equal(ui.el("training-kind").value, "evaluation");

  const output = ui.el("log-output");
  Object.defineProperty(output, "scrollHeight", { value: 1000 });
  Object.defineProperty(output, "clientHeight", { value: 200 });
  output.scrollTop = 50; output.dispatchEvent(new ui.w.Event("scroll"));
  assert.equal(ui.el("follow-logs").checked, false);
  const updated = structuredClone(run); updated.log_tail = "second output"; ui.setRun(updated);
  const nextPoll = ui.timers.findLast(({ delay }) => delay === 15000).callback(); await flush();
  ui.respond([]); await nextPoll; await flush();
  assert.equal(output.scrollTop, 50, "Paused log position survives live updates");

  // A final scheduler state must update placeholders even when telemetry is unchanged.
  const failedRun = structuredClone(run);
  failedRun.state = "FAILED";
  failedRun.training.latest_evaluation = {};
  failedRun.training.checkpoints = [];
  ui.setRun(failedRun);
  const failedPoll = ui.timers.findLast(({ delay }) => delay === 15000).callback(); await flush();
  ui.respond([event(80)]); await failedPoll; await flush();
  assert.equal(ui.el("training-overview").hidden, false);
  assert.equal(ui.el("training-events-panel").hidden, false, "Failure preserves recorded telemetry");
  assert.equal(ui.el("training-stopped").hidden, true);
  assert.match(ui.el("training-phase").textContent, /Last reported/);
  assert.equal(ui.el("evaluation-context").textContent, "No held-out evaluation was recorded.");
  assert.equal(ui.el("training-checkpoints").textContent, "No checkpoint was recorded.");

  const queuedRun = { id: "blocked", state: "QUEUED", config: { job_type: "dmasif_train", batch_size: 8 }, training: {} };
  const blocked = setup("detail", queuedRun); await flush();
  blocked.respond([]); await flush();
  assert.equal(blocked.el("training-overview").hidden, false);
  assert.equal(blocked.el("training-stopped").hidden, true);
  assert.match(blocked.el("latest-metrics-empty").textContent, /will appear/);
  const rejected = { ...queuedRun, state: "REJECTED", reason: `dataset_v2 is not registered. ${attack}` };
  blocked.setRun(rejected);
  const eventRequests = blocked.requests.filter(({ url }) => url.includes("/training-events?")).length;
  await blocked.timers.findLast(({ delay }) => delay === 15000).callback(); await flush();
  assert.equal(blocked.el("training-stopped").hidden, false);
  assert.equal(blocked.el("training-stopped-title").textContent, "Training did not start");
  assert.equal(blocked.el("training-stopped-context").textContent, "No cluster job was submitted.");
  assert.equal(blocked.el("training-stopped-reason").textContent, rejected.reason);
  assert.equal(blocked.w.document.querySelectorAll("img").length, 0);
  for (const id of ["training-overview", "evaluation-panel", "checkpoints-panel", "training-events-panel"]) {
    assert.equal(blocked.el(id).hidden, true, "Unsubmitted rejection hides empty training panels");
  }
  assert.equal(blocked.requests.filter(({ url }) => url.includes("/training-events?")).length, eventRequests);

  const blockedHistory = setup("history", queuedRun); await flush();
  assert.match(blockedHistory.el("runs-body").textContent, /Waiting for training/);
  blockedHistory.setRun(rejected);
  await blockedHistory.timers.findLast(({ delay }) => delay === 15000).callback(); await flush();
  assert.match(blockedHistory.el("runs-body").textContent, /Not started · rejected/);
  assert.match(blockedHistory.el("runs-body").textContent, /dataset_v2 is not registered/);
  assert.doesNotMatch(blockedHistory.el("runs-body").textContent, /Waiting for training/);
  blockedHistory.setRun({ ...rejected, state: "PREPARATION_FAILED" });
  await blockedHistory.timers.findLast(({ delay }) => delay === 15000).callback(); await flush();
  assert.match(blockedHistory.el("runs-body").textContent, /Not started · preparation failed/);

  const older = setup("detail", { id: "old", state: "SUCCEEDED", config: { job_type: "dmasif_extract" }, training: {}, artifacts: [] });
  await flush();
  assert.equal(older.pending.length, 0);
  assert.equal(older.el("training-overview").hidden, true);
  assert.equal(older.el("legacy-training-note").hidden, false);
  const history = setup("history"); await flush();
  assert.equal(history.el("datasets-panel"), null);
  assert.match(history.el("runs-body").textContent, /Step 200/);
  assert.equal(history.requests.length, 1);
  for (const context of [ui, older, history, blocked, blockedHistory]) context.w.close();
  console.log("Training UI DOM checks passed: metric safety, paging, filters, retries, race handling, log follow, and legacy runs.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
