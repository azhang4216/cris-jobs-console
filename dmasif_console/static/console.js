"use strict";

(() => {
  const labels = {
    CHECKING_REQUEST: "Checking request", REJECTED: "Rejected",
    PREPARATION_FAILED: "Preparation failed", WAITING_FOR_CAPACITY: "Waiting for app slot",
    PREPARING: "Preparing", SUBMITTING: "Submitting", SUBMISSION_UNKNOWN: "Submission uncertain",
    QUEUED: "Queued on cluster", RUNNING: "Running", VALIDATING_RESULTS: "Checking results",
    SUCCEEDED: "Succeeded", PARTIAL: "Partial results", FAILED: "Failed", CANCELLED: "Cancelled",
    TIMED_OUT: "Timed out", NEEDS_REVIEW: "Needs review", COMPLETED: "Completed",
  };
  const attentionStates = new Set(["SUBMISSION_UNKNOWN", "NEEDS_REVIEW", "PREPARATION_FAILED", "REJECTED", "PARTIAL", "FAILED", "TIMED_OUT"]);
  const monitoredStates = new Set(["QUEUED", "RUNNING", "SUBMITTING", "SUBMISSION_UNKNOWN", "VALIDATING_RESULTS"]);
  const page = document.body.dataset.page;
  const apiBase = (document.body.dataset.apiBaseUrl || "").replace(/\/$/, "");
  const pollMilliseconds = Math.max(3000, Number(document.body.dataset.pollSeconds || 15) * 1000);
  const clusterPollMilliseconds = Math.max(1000, Number(document.body.dataset.clusterPollSeconds || 30) * 1000);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const dateFormatter = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  const fullDateFormatter = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short" });
  let latestRuns = null;
  let latestDetailRun = null;
  let previousRuns = "";
  let previousActivity = "";
  let previousArtifacts = "";
  let previousEvents = "";
  let previousJobs = "";
  let previousActors = "";
  let polling = false;
  let pollRequested = false;
  let filterPollTimer;
  let trainingEventsController = null;
  let trainingEventsRequest = 0;
  let trainingBefore = null;
  let trainingPageStack = [];
  let trainingNextBefore = null;
  let trainingKind = "";
  let trainingStep = "";
  let previousTrainingRows = "";
  let previousTrainingSummary = "";
  let trainingEventsLoaded = false;

  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined && value !== null) element.textContent = String(value);
    return element;
  }

  function readable(value) {
    if (value === null || value === undefined) return "";
    return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  }

  function isTraining(run) {
    const training = run?.training || {};
    return run?.config?.job_type === "dmasif_train" || Boolean(training.phase || training.event_count);
  }

  function phaseLabel(phase) {
    const phases = { training: "Training", evaluation: "Evaluating held-out data", checkpoint: "Saving checkpoint", completed: "Completed", starting: "Starting" };
    return phases[phase] || String(phase || "Waiting to start").replaceAll("_", " ");
  }

  function artifactLabel(name) {
    const extension = String(name || "").split(".").at(-1);
    return /^[A-Za-z0-9]{1,6}$/.test(extension) ? extension.toUpperCase() : "FILE";
  }

  function metricNodes(metrics) {
    if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) return [];
    return Object.entries(metrics).map(([key, value]) => {
      const field = node("div");
      const number = node("dd", "mono", value === null ? "—" : readable(value));
      if (value !== null) number.title = readable(value);
      field.append(node("dt", "", key), number);
      return field;
    });
  }

  function renderTraining(run) {
    const enabled = isTraining(run);
    for (const id of ["training-overview", "evaluation-panel", "checkpoints-panel", "training-events-panel"]) {
      document.getElementById(id).hidden = !enabled;
    }
    document.getElementById("legacy-training-note").hidden = enabled;
    const batch = run.config?.batch_size;
    document.getElementById("detail-batch-row").hidden = !Number.isInteger(batch);
    document.getElementById("detail-batch").textContent = Number.isInteger(batch) ? String(batch) : "—";
    if (!enabled) return;
    const training = run.training || {};
    const fingerprint = JSON.stringify([training, batch]);
    if (fingerprint === previousTrainingSummary) return;
    previousTrainingSummary = fingerprint;
    const step = Number.isInteger(training.step) ? training.step : null;
    const total = Number.isInteger(training.total_steps) && training.total_steps > 0 ? training.total_steps : null;
    document.getElementById("training-phase").textContent = phaseLabel(training.phase);
    document.getElementById("training-step").textContent = step === null ? "—" : `${step.toLocaleString()}${total ? ` / ${total.toLocaleString()}` : ""}`;
    document.getElementById("training-batch").textContent = Number.isInteger(batch) ? String(batch) : "—";
    document.getElementById("training-updated").replaceChildren(timeNode(training.last_event_at));
    document.getElementById("training-progress-track").hidden = step === null || total === null;
    const progress = document.getElementById("training-progress");
    progress.max = total || 1;
    progress.value = Math.max(0, Math.min(step || 0, total || 1));
    const reason = document.getElementById("training-complete-reason");
    reason.textContent = training.complete_reason || "";
    reason.hidden = !training.complete_reason;
    const metrics = metricNodes(training.latest_metrics);
    document.getElementById("latest-metrics").replaceChildren(...metrics);
    document.getElementById("latest-metrics-empty").hidden = metrics.length > 0;
    const evaluation = training.latest_evaluation || {};
    document.getElementById("evaluation-metrics").replaceChildren(...metricNodes(evaluation.metrics));
    const context = document.getElementById("evaluation-context");
    if (Object.keys(evaluation).length) {
      context.replaceChildren(document.createTextNode(`Step ${evaluation.step ?? "—"} · `), timeNode(evaluation.timestamp));
    } else context.textContent = "Evaluation runs automatically on held-out data, separately from training.";
    const checkpoints = (Array.isArray(training.checkpoints) ? training.checkpoints : []).map((checkpoint) => {
      const item = node("li");
      item.append(node("strong", "mono", checkpoint.name || "Checkpoint"), node("span", "", `Step ${checkpoint.step ?? "—"}`), timeNode(checkpoint.timestamp));
      return item;
    });
    document.getElementById("training-checkpoints").replaceChildren(...(checkpoints.length ? checkpoints : [node("li", "muted", "No checkpoint reported yet.")]));
  }

  function trainingEventRow(event) {
    const row = node("tr");
    row.dataset.sequence = String(event.seq ?? "");
    const labels = { step: "Training", evaluation: "Evaluation", checkpoint: "Checkpoint", start: "Started", complete: "Completed" };
    const kind = Object.hasOwn(labels, event.kind) ? labels[event.kind] : "Update";
    const step = node("td", "mono", event.step ?? "—");
    const type = node("td");
    type.append(node("span", `event-kind${event.kind === "evaluation" ? " event-evaluation" : event.kind === "checkpoint" ? " event-checkpoint" : ""}`, kind));
    const time = node("td", "time-cell"); time.append(timeNode(event.timestamp));
    const details = node("td", "event-details");
    if (event.name) details.append(node("strong", "mono checkpoint-name", event.name));
    if (event.phase) details.append(node("span", "cell-secondary", phaseLabel(event.phase)));
    const metrics = metricNodes(event.metrics);
    if (metrics.length) {
      const fields = node("dl", "event-metrics");
      fields.append(...metrics); details.append(fields);
    }
    if (event.reason) details.append(node("p", "small-note", event.reason));
    if (!details.childNodes.length) details.append(node("span", "muted", "No metrics in this event."));
    row.append(step, type, time, details);
    return row;
  }

  async function loadTrainingEvents({ reset = false, refresh = false, pageChange = false } = {}) {
    if (!isTraining(latestDetailRun)) return;
    // Polling never moves a researcher away from an older page they are reading.
    if (refresh && (trainingBefore !== null || trainingEventsController)) return;
    if (reset) {
      trainingBefore = null;
      trainingPageStack = [];
      trainingNextBefore = null;
    }
    if (reset || pageChange) {
      trainingEventsLoaded = false;
      previousTrainingRows = "";
      document.getElementById("training-events-body").replaceChildren();
      document.getElementById("training-events-table").hidden = true;
    }
    trainingEventsController?.abort();
    const controller = new AbortController();
    trainingEventsController = controller;
    const request = ++trainingEventsRequest;
    const panel = document.getElementById("training-events-panel");
    const message = document.getElementById("training-events-message");
    const retry = document.getElementById("training-retry");
    panel.setAttribute("aria-busy", "true");
    retry.hidden = true;
    document.getElementById("training-newer").disabled = true;
    document.getElementById("training-older").disabled = true;
    if (!trainingEventsLoaded) {
      message.textContent = "Loading training events…";
      message.hidden = false;
    }
    message.classList.remove("connection-stale");
    const query = new URLSearchParams({ limit: "50" });
    if (trainingKind) query.set("kind", trainingKind);
    if (trainingStep !== "") query.set("step", trainingStep);
    if (trainingBefore !== null) query.set("before_seq", String(trainingBefore));
    const id = encodeURIComponent(String(latestDetailRun.id));
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(`${apiBase}/api/runs/${id}/training-events?${query}`, {
        headers: { Accept: "application/json" }, credentials: "omit", cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Training events returned ${response.status}`);
      const data = await response.json();
      if (request !== trainingEventsRequest) return;
      if (!Array.isArray(data.events) || !data.events.every((event) => event && typeof event === "object")) throw new Error("Invalid training events");
      const fingerprint = JSON.stringify(data.events);
      if (fingerprint !== previousTrainingRows) {
        previousTrainingRows = fingerprint;
        const table = document.getElementById("training-events-table");
        const scrollTop = table.scrollTop;
        document.getElementById("training-events-body").replaceChildren(...data.events.map(trainingEventRow));
        table.scrollTop = scrollTop;
      }
      trainingEventsLoaded = true;
      trainingNextBefore = Number.isInteger(data.next_before_seq) ? data.next_before_seq : null;
      document.getElementById("training-events-table").hidden = data.events.length === 0;
      message.hidden = data.events.length > 0;
      message.textContent = trainingKind || trainingStep !== ""
        ? "No events match these filters yet. Try another step or event type."
        : "No training events yet. Steps, evaluations, and checkpoints appear here as they are reported.";
      const total = Number.isInteger(data.total) ? ` of ${data.total.toLocaleString()} matching events` : " events";
      document.getElementById("training-events-count").textContent = `${data.events.length}${total} · ${trainingBefore === null ? "newest first" : "older events; page held while live updates continue"}`;
    } catch (error) {
      if (request !== trainingEventsRequest) return;
      message.hidden = false;
      message.classList.add("connection-stale");
      message.textContent = trainingEventsLoaded ? "Training updates delayed. Showing saved events; retry to refresh." : "Could not load training events. Retry to load them.";
      retry.hidden = false;
    } finally {
      clearTimeout(timeout);
      if (request === trainingEventsRequest) {
        trainingEventsController = null;
        panel.setAttribute("aria-busy", "false");
        document.getElementById("training-newer").disabled = trainingPageStack.length === 0;
        document.getElementById("training-older").disabled = trainingNextBefore === null;
      }
    }
  }

  function chooseTrainingKind(kind) {
    trainingKind = kind;
    trainingStep = "";
    document.getElementById("training-kind").value = kind;
    document.getElementById("training-step-filter").value = "";
    loadTrainingEvents({ reset: true });
    document.getElementById("training-events-title").focus({ preventScroll: true });
    document.getElementById("training-events-panel").scrollIntoView({ block: "start", behavior: "instant" });
  }

  function publicProvenance(value) {
    const source = value || {};
    const keys = ["source_commit", "source_sha256", "archive_sha256", "runtime_sha256", "checkpoint_sha256", "adapter_version", "adapter_sha256", "input_manifest_sha256", "dataset_id", "seed", "gpu_type", "gpu_name", "requested_gpu_count", "allocated_gpu_count", "used_gpu_count", "python", "torch", "cuda_runtime", "feature_dimensions", "commit_author", "commit_committer"];
    return Object.fromEntries(keys.filter((key) => Object.hasOwn(source, key)).map((key) => [key, source[key]]));
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function observationDate(value) {
    // A timezone-free value cannot establish when the cluster was checked.
    return typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? parseDate(value) : null;
  }

  function timeNode(value, fallback = "—") {
    const date = parseDate(value);
    if (!date) return node("span", "muted", fallback);
    const element = node("time", "", dateFormatter.format(date));
    element.dateTime = date.toISOString();
    element.title = `${fullDateFormatter.format(date)} · ${date.toISOString()} UTC`;
    element.dataset.time = "";
    return element;
  }

  function formatTimes(root = document) {
    root.querySelectorAll("time[data-time]").forEach((element) => {
      const date = parseDate(element.dateTime);
      if (!date) return;
      element.textContent = dateFormatter.format(date);
      element.title = `${fullDateFormatter.format(date)} · ${date.toISOString()} UTC`;
    });
  }

  function duration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    const total = Math.floor(seconds);
    if (total < 60) return `${total}s`;
    if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
    if (total < 86400) return `${Math.floor(total / 3600)}h ${Math.floor(total % 3600 / 60)}m`;
    return `${Math.floor(total / 86400)}d ${Math.floor(total % 86400 / 3600)}h`;
  }

  function monitoringData(element, run) {
    if (!run) return;
    element.dataset.state = run.state || "";
    element.dataset.observed = run.last_observed_at || "";
    element.dataset.monitorError = String(Boolean(run.monitor_error));
    element.dataset.monitoringStale = String(Boolean(run.monitoring_stale));
  }

  function monitoringStale(element) {
    if (!monitoredStates.has(element.dataset.state || element.dataset.status)) return false;
    const observed = observationDate(element.dataset.observed);
    return element.dataset.monitorError === "true" || element.dataset.monitoringStale === "true"
      || !observed || Date.now() - observed.getTime() > Math.max(120000, clusterPollMilliseconds * 3);
  }

  function updateRuntime(element, run) {
    monitoringData(element, run);
    if (run) {
      element.dataset.start = run.started_at || "";
      element.dataset.end = run.ended_at || "";
      element.dataset.seconds = run.runtime_seconds ?? "";
      element.dataset.state = run.state || "";
    }
    const start = parseDate(element.dataset.start);
    const end = parseDate(element.dataset.end);
    const observed = observationDate(element.dataset.observed);
    const stale = monitoringStale(element);
    let seconds = element.dataset.seconds === "" ? null : Number(element.dataset.seconds);
    if (start && end) seconds = (end - start) / 1000;
    else if (start && element.dataset.state === "RUNNING") {
      seconds = stale ? (observed && observed >= start ? (observed - start) / 1000 : null) : (Date.now() - start) / 1000;
    }
    const capped = stale && !end && seconds !== null;
    element.textContent = seconds === null ? "—" : `${duration(seconds)}${capped ? " · last check" : ""}`;
    element.title = capped && observed ? `Runtime at the last successful cluster check: ${fullDateFormatter.format(observed)}` : "";
  }

  function bytes(value) {
    const amount = Number(value || 0);
    if (!Number.isFinite(amount) || amount < 0) return "Unknown size";
    if (amount < 1024) return `${amount} B`;
    if (amount < 1024 * 1024) return `${(amount / 1024).toFixed(1)} KiB`;
    return `${(amount / (1024 * 1024)).toFixed(1)} MiB`;
  }

  function updateStatus(element) {
    const state = element.dataset.status;
    const stale = monitoringStale(element);
    const text = `${stale ? "Last known: " : ""}${labels[state] || state}`;
    if (element.textContent.trim() !== text) {
      const dot = node("span", "status-dot");
      dot.setAttribute("aria-hidden", "true");
      element.replaceChildren(dot, document.createTextNode(text));
    }
    element.title = stale ? "Cluster updates are delayed; this is the last known status." : "";
  }

  function statusNode(state, run) {
    const safeState = Object.hasOwn(labels, state) ? state : "NEEDS_REVIEW";
    const element = node("span", `status-badge status-${safeState.toLowerCase()}`);
    element.dataset.status = safeState;
    monitoringData(element, run);
    updateStatus(element);
    return element;
  }

  function runLink(id, className, label) {
    const element = node("a", className, label);
    element.href = `/runs/${encodeURIComponent(String(id || ""))}`;
    return element;
  }

  function commitNode(run) {
    if (!run.commit_sha) return node("span", "muted", "—");
    let url;
    try { url = new URL(run.commit_url); } catch { url = null; }
    if (!url || url.protocol !== "https:") return node("span", "mono", String(run.commit_sha).slice(0, 8));
    const link = node("a", "commit-link mono", String(run.commit_sha).slice(0, 8));
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = "View full commit on GitHub";
    const arrow = node("span", "external-arrow", "↗");
    arrow.setAttribute("aria-hidden", "true");
    link.append(arrow);
    return link;
  }

  function replacePreservingFocus(container, children) {
    const focused = container.contains(document.activeElement) ? document.activeElement : null;
    const href = focused?.getAttribute("href");
    container.replaceChildren(...children);
    if (href) {
      const replacement = Array.from(container.querySelectorAll("a")).find((item) => item.getAttribute("href") === href);
      replacement?.focus({ preventScroll: true });
    }
  }

  function rowNode(run) {
    const row = node("tr");
    row.dataset.runId = run.id || "";
    row.dataset.actor = run.actor_login || "";
    row.dataset.runStatus = run.state || "";
    row.dataset.experiment = run.experiment || "";
    const identity = node("td");
    const link = runLink(run.id, "experiment-link", run.experiment || "Untitled experiment");
    link.title = `Run ${run.id || ""}`;
    identity.append(link);
    if (run.source_kind === "slurm" && run.scheduler_job_id) identity.append(node("span", "cell-secondary", `Job ${run.scheduler_job_id}`));
    const researcher = node("td", "", run.actor_login || (run.source_kind === "slurm" ? "—" : "Unknown researcher"));
    const commit = node("td"); commit.append(commitNode(run));
    const progress = node("td", "run-progress");
    const training = run.training || {};
    if (Number.isInteger(training.step)) {
      progress.append(node("span", "mono", `Step ${training.step.toLocaleString()}${Number.isInteger(training.total_steps) ? ` / ${training.total_steps.toLocaleString()}` : ""}`));
      if (training.phase) progress.append(node("span", "cell-secondary", phaseLabel(training.phase)));
    } else progress.append(node("span", "muted", isTraining(run) ? "Waiting for training" : "—"));
    const state = node("td"); state.append(statusNode(run.state, run));
    const received = node("td", "nowrap time-cell"); received.append(timeNode(run.source_kind === "slurm" ? run.submitted_at : run.created_at));
    const runtime = node("td", "mono runtime-cell"); runtime.dataset.runtime = ""; updateRuntime(runtime, run);
    row.append(identity, researcher, commit, progress, state, received, runtime);
    return row;
  }

  function filters() {
    const form = document.getElementById("run-filters");
    if (!form) return {};
    return Object.fromEntries(new FormData(form));
  }

  function applyFilters() {
    const current = filters();
    let count = 0;
    const rows = document.querySelectorAll("#runs-body tr");
    rows.forEach((row) => {
      const visible = (!current.actor || row.dataset.actor === current.actor)
        && (!current.status || row.dataset.runStatus === current.status)
        && (!current.experiment || row.dataset.experiment.toLowerCase().includes(current.experiment.toLowerCase()));
      row.hidden = !visible;
      if (visible) count += 1;
    });
    document.getElementById("visible-count").textContent = String(count);
    const empty = document.getElementById("empty-runs");
    empty.hidden = count !== 0;
    const hasFilters = Object.values(current).some(Boolean);
    document.getElementById("empty-title").textContent = hasFilters ? "No matching runs" : "No runs yet";
    document.getElementById("empty-description").textContent = hasFilters ? "Try different filters." : "Your experiments will appear here.";
    const nextURL = new URL(location.href);
    for (const key of ["actor", "status", "experiment"]) {
      if (current[key]) nextURL.searchParams.set(key, current[key]);
      else nextURL.searchParams.delete(key);
    }
    history.replaceState(null, "", nextURL);
  }

  function updateActorOptions(runs, availableActors) {
    const select = document.getElementById("actor-filter");
    const selected = select.value;
    const actors = [...new Set((Array.isArray(availableActors) ? availableActors : runs.map((run) => run.actor_login)).filter((actor) => typeof actor === "string" && actor))];
    if (selected && !actors.includes(selected)) actors.push(selected);
    actors.sort();
    const fingerprint = JSON.stringify(actors);
    if (fingerprint === previousActors) return;
    previousActors = fingerprint;
    const options = [new Option("All researchers", ""), ...actors.sort().map((actor) => new Option(actor, actor))];
    select.replaceChildren(...options);
    select.value = selected;
  }

  function renderActivity(activity) {
    const list = document.getElementById("activity-list");
    if (!list) return;
    const fingerprint = JSON.stringify(activity);
    if (fingerprint === previousActivity) return;
    previousActivity = fingerprint;
    const items = activity.slice(0, 10).map((event) => {
      const item = node("li");
      const top = node("div", "activity-top");
      top.append(node("strong", "", event.actor_login || event.actor || "Repository event"), node("span", "activity-decision", String(event.decision || event.status || "received").replaceAll("_", " ").toLowerCase()));
      const bottom = node("div", "activity-bottom");
      bottom.append(timeNode(event.received_at || event.created_at));
      if (event.run_id) bottom.append(runLink(event.run_id, "", "View run →"));
      item.append(top, node("p", "", event.reason || event.ref || "Push received"), bottom);
      return item;
    });
    if (!items.length) items.push(node("li", "activity-empty", "No pushes yet."));
    replacePreservingFocus(list, items);
  }

  function renderObservation(observation) {
    const checked = document.getElementById("observation-time");
    if (!checked) return;
    const state = observation || {};
    const observed = parseDate(state.observed_at);
    const stale = observed && Date.now() - observed.getTime() > Math.max(120000, clusterPollMilliseconds * 3);
    checked.replaceChildren(...(observed ? [document.createTextNode("Last check "), timeNode(state.observed_at)] : [document.createTextNode("Waiting for the first cluster check")]));
    checked.classList.toggle("connection-stale", Boolean(stale));
    const error = document.getElementById("observation-error");
    error.textContent = state.error || (stale ? "Cluster updates delayed; showing saved data." : "");
    error.hidden = !error.textContent;
  }

  function renderHistory(data) {
    if (!Array.isArray(data.runs)) throw new Error("Invalid runs response");
    if (data.status_labels) Object.assign(labels, data.status_labels);
    latestRuns = data.runs;
    updateActorOptions(latestRuns, data.actors);
    const fingerprint = JSON.stringify(latestRuns);
    if (fingerprint !== previousRuns) {
      previousRuns = fingerprint;
      replacePreservingFocus(document.getElementById("runs-body"), latestRuns.map(rowNode));
      applyFilters();
    }
    renderActivity(Array.isArray(data.activity) ? data.activity : []);
  }

  function renderArtifacts(artifacts, source, noJobSubmitted) {
    const fingerprint = JSON.stringify([artifacts, source, noJobSubmitted]);
    if (fingerprint === previousArtifacts) return;
    previousArtifacts = fingerprint;
    document.getElementById("artifact-count").textContent = String(artifacts.length);
    const elements = artifacts.map((artifact) => {
      const row = node("div", "artifact-row");
      const icon = node("div", "file-icon", artifactLabel(artifact.name)); icon.setAttribute("aria-hidden", "true");
      const info = node("div", "artifact-info");
      const meta = node("span", "cell-secondary", bytes(artifact.size));
      if (artifact.sha256) {
        const checksum = node("span", "mono", String(artifact.sha256).slice(0, 12));
        checksum.title = `SHA-256: ${artifact.sha256}`;
        meta.append(document.createTextNode(" · "), checksum);
      }
      info.append(node("strong", "", artifact.name || "Result file"), meta);
      if (artifact.points !== null && artifact.points !== undefined && artifact.atoms !== null && artifact.atoms !== undefined) {
        info.append(node("span", "cell-secondary", `${artifact.points} surface points · ${artifact.atoms} atoms`));
      }
      if (artifact.reason) info.append(node("span", "small-note", artifact.reason));
      row.append(icon, info);
      if (artifact.cached && artifact.id) {
        const download = node("a", "download-link", "Download ↓");
        download.href = `${apiBase}/artifacts/${encodeURIComponent(String(artifact.id))}`;
        row.append(download);
      } else row.append(node("span", "download-unavailable", "Download unavailable"));
      return row;
    });
    if (!elements.length) {
      const empty = node("div", "results-empty");
      empty.append(node("p", "", noJobSubmitted ? "No results: no cluster job was submitted." : (source === "slurm" ? "Results have not been imported." : "No results yet.")));
      elements.push(empty);
    }
    replacePreservingFocus(document.getElementById("artifacts-list"), elements);
  }

  function renderJobs(jobs, noJobSubmitted) {
    const fingerprint = JSON.stringify([jobs, noJobSubmitted]);
    if (fingerprint === previousJobs) return;
    previousJobs = fingerprint;
    const elements = jobs.map((job) => {
      const item = node("div", "scheduler-job");
      item.append(node("strong", "mono", job.job_id || job.slurm_job_id || "Pending"), node("span", "", job.state || job.raw_state || "Unknown"), node("p", "small-note", `Exit code: ${job.exit_code || "Not available"}`));
      return item;
    });
    if (!elements.length) elements.push(node("p", "small-note jobs-empty", noJobSubmitted ? "No cluster job was submitted." : "No cluster job yet."));
    document.getElementById("scheduler-jobs").replaceChildren(...elements);
  }

  function renderEvents(events) {
    const fingerprint = JSON.stringify(events);
    if (fingerprint === previousEvents) return;
    previousEvents = fingerprint;
    const elements = events.map((event) => {
      const item = node("li");
      const marker = node("span", "event-marker"); marker.setAttribute("aria-hidden", "true");
      const content = node("div");
      const kind = String(event.kind || event.type || event.event_type || "Update").replaceAll("_", " ").toLowerCase();
      const label = Object.hasOwn(labels, event.to)
        ? `${Object.hasOwn(labels, event.from) ? `${labels[event.from]} → ` : ""}${labels[event.to]}`
        : (Object.hasOwn(labels, event.state) ? labels[event.state] : kind.charAt(0).toUpperCase() + kind.slice(1));
      content.append(node("strong", "", label), node("p", "", readable(event.reason || event.message || event.details)), timeNode(event.created_at || event.timestamp));
      item.append(marker, content);
      return item;
    });
    if (!elements.length) elements.push(node("li", "activity-empty", "No updates yet."));
    document.getElementById("events-list").replaceChildren(...elements);
  }

  function renderLogs(text, observedAt, source, noJobSubmitted) {
    const output = document.getElementById("log-output");
    const following = document.getElementById("follow-logs").checked;
    const scrollTop = output.scrollTop;
    const scrollLeft = output.scrollLeft;
    const next = text || (noJobSubmitted ? "No logs: no cluster job was submitted." : (source === "slurm" ? "Logs unavailable for this imported job." : "Waiting for job output…"));
    if (output.textContent !== next) output.textContent = next;
    output.scrollTop = following ? output.scrollHeight : scrollTop;
    output.scrollLeft = scrollLeft;
    document.getElementById("log-observed").replaceChildren(document.createTextNode("Updated "), timeNode(observedAt));
  }

  function renderDetailMonitoring(run) {
    const stale = monitoringStale(document.querySelector("#detail-status [data-status]"));
    const needsHelp = attentionStates.has(run.state);
    const explanation = document.getElementById("status-explanation");
    explanation.hidden = needsHelp || stale || !run.reason;
    explanation.textContent = run.reason || "";
    const notice = document.getElementById("run-notice");
    notice.hidden = !needsHelp && !run.monitor_error && !stale;
    document.getElementById("notice-label").textContent = needsHelp ? "Needs attention" : "Cluster check delayed";
    document.getElementById("run-reason").textContent = (needsHelp ? run.reason : run.monitor_error)
      || (stale ? "Cluster updates are delayed; showing the last known status." : "Contact the operator for help.");
    document.getElementById("operator-help").hidden = !needsHelp;
    document.getElementById("capacity-note").hidden = !run.capacity_reserved;
    const observed = document.getElementById("last-observed");
    observed.replaceChildren(timeNode(run.last_observed_at));
    observed.classList.toggle("connection-stale", stale);
    if (stale) observed.append(node("span", "cell-secondary", "Update delayed"));
  }

  function renderDetail(data) {
    const run = data.run;
    if (!run || !run.id) throw new Error("Invalid run response");
    latestDetailRun = run;
    if (data.status_labels) Object.assign(labels, data.status_labels);
    document.getElementById("detail-status").replaceChildren(statusNode(run.state, run));
    renderDetailMonitoring(run);
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    const noJobSubmitted = ["REJECTED", "PREPARATION_FAILED"].includes(run.state) && !run.submitted_at && jobs.length === 0;
    document.querySelectorAll("[data-timing]").forEach((element) => {
      element.replaceChildren(timeNode(run[element.dataset.timing]));
    });
    document.querySelectorAll("[data-runtime]").forEach((element) => updateRuntime(element, run));
    document.getElementById("detail-dataset").textContent = run.dataset_id || "—";
    renderTraining(run);
    for (const key of ["validation", "config", "provenance"]) {
      document.getElementById(`${key}-json`).textContent = JSON.stringify(key === "provenance" ? publicProvenance(run[key]) : (run[key] || {}), null, 2);
    }
    renderArtifacts(Array.isArray(run.artifacts) ? run.artifacts : [], run.source_kind, noJobSubmitted);
    renderJobs(jobs, noJobSubmitted);
    renderEvents(Array.isArray(data.events) ? data.events : []);
    if (typeof run.log_tail === "string") renderLogs(run.log_tail, run.last_observed_at, run.source_kind, noJobSubmitted);
  }

  async function getJSON(path) {
    const response = await fetch(`${apiBase}${path}`, { headers: { Accept: "application/json" }, credentials: "omit", cache: "no-store", signal: AbortSignal.timeout(12000) });
    if (!response.ok) throw new Error(`Status service returned ${response.status}`);
    return response.json();
  }

  async function poll() {
    if (document.hidden) return;
    if (polling) { pollRequested = true; return; }
    polling = true;
    const connection = document.getElementById("connection-state");
    try {
      if (page === "history") {
        const query = new URLSearchParams();
        const current = filters();
        for (const key of ["actor", "status", "experiment"]) if (current[key]) query.set(key, current[key]);
        const data = await getJSON(`/api/runs${query.size ? `?${query}` : ""}`);
        renderHistory(data);
        renderObservation(data.observation);
      }
      else if (page === "detail") {
        const id = document.getElementById("run-detail").dataset.runId;
        const data = await getJSON(`/api/runs/${encodeURIComponent(id)}`);
        renderDetail(data);
        renderObservation(data.observation);
        await loadTrainingEvents({ refresh: true });
        if (typeof data.run?.log_tail !== "string") {
          const logs = await getJSON(`/api/runs/${encodeURIComponent(id)}/logs`);
          const noJobSubmitted = ["REJECTED", "PREPARATION_FAILED"].includes(data.run.state) && !data.run.submitted_at && !data.jobs?.length;
          renderLogs(logs.text, logs.last_observed_at, data.run.source_kind, noJobSubmitted);
        }
      }
      connection.textContent = "Updates automatically";
      connection.classList.remove("connection-stale");
      connection.title = `Last dashboard refresh: ${fullDateFormatter.format(new Date())}`;
    } catch (error) {
      connection.textContent = "Updates delayed · retrying";
      connection.classList.add("connection-stale");
      connection.title = error.message;
    } finally {
      polling = false;
      if (pollRequested) { pollRequested = false; setTimeout(poll, 0); }
    }
  }

  function onFilterChange() {
    applyFilters();
    clearTimeout(filterPollTimer);
    filterPollTimer = setTimeout(poll, 250);
  }

  document.querySelectorAll("[data-timezone]").forEach((element) => { element.textContent = zone; });
  document.querySelectorAll("[data-bytes]").forEach((element) => { element.textContent = bytes(element.dataset.bytes); });
  formatTimes();
  function refreshMonitoring() {
    document.querySelectorAll("[data-runtime]").forEach((element) => updateRuntime(element));
    document.querySelectorAll(".status-badge[data-status]").forEach((element) => updateStatus(element));
    if (latestDetailRun) renderDetailMonitoring(latestDetailRun);
  }
  refreshMonitoring();
  if (page === "history") {
    const form = document.getElementById("run-filters");
    form.addEventListener("submit", (event) => { event.preventDefault(); onFilterChange(); });
    form.addEventListener("input", onFilterChange);
    form.addEventListener("change", onFilterChange);
    form.addEventListener("reset", (event) => {
      event.preventDefault();
      for (const element of form.elements) if (element.name) element.value = "";
      onFilterChange();
    });
    applyFilters();
  }
  if (page === "detail") {
    const trainingForm = document.getElementById("training-filters");
    function applyTrainingFilters() {
      if (!trainingForm.reportValidity()) return;
      const step = document.getElementById("training-step-filter").value;
      if (step !== "" && (!Number.isSafeInteger(Number(step)) || Number(step) < 0)) return;
      trainingKind = document.getElementById("training-kind").value;
      trainingStep = step === "" ? "" : String(Number(step));
      loadTrainingEvents({ reset: true });
    }
    trainingForm.addEventListener("submit", (event) => { event.preventDefault(); applyTrainingFilters(); });
    document.getElementById("training-kind").addEventListener("change", applyTrainingFilters);
    trainingForm.addEventListener("reset", (event) => {
      event.preventDefault();
      document.getElementById("training-kind").value = "";
      document.getElementById("training-step-filter").value = "";
      applyTrainingFilters();
    });
    document.getElementById("training-newest").addEventListener("click", () => loadTrainingEvents({ reset: true }));
    document.getElementById("training-retry").addEventListener("click", () => loadTrainingEvents());
    document.getElementById("training-older").addEventListener("click", () => {
      if (trainingNextBefore === null || trainingEventsController) return;
      trainingPageStack.push(trainingBefore);
      trainingBefore = trainingNextBefore;
      trainingNextBefore = null;
      loadTrainingEvents({ pageChange: true });
    });
    document.getElementById("training-newer").addEventListener("click", () => {
      if (!trainingPageStack.length || trainingEventsController) return;
      trainingBefore = trainingPageStack.pop();
      trainingNextBefore = null;
      loadTrainingEvents({ pageChange: true });
    });
    document.getElementById("view-evaluations").addEventListener("click", () => chooseTrainingKind("evaluation"));
    document.getElementById("view-checkpoints").addEventListener("click", () => chooseTrainingKind("checkpoint"));
    const output = document.getElementById("log-output");
    output.scrollTop = output.scrollHeight;
    output.addEventListener("scroll", () => {
      const atBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 30;
      if (!atBottom) document.getElementById("follow-logs").checked = false;
    });
    document.getElementById("follow-logs").addEventListener("change", (event) => {
      if (event.target.checked) output.scrollTop = output.scrollHeight;
    });
  }
  // Poll serially; slow requests cannot create a growing queue of overlapping requests.
  async function schedule() { await poll(); setTimeout(schedule, pollMilliseconds); }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  setInterval(() => { if (!document.hidden) refreshMonitoring(); }, 1000);
  schedule();
})();
