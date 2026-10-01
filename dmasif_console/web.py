"""Public read-only dashboard and a signed, durable GitHub webhook receiver.

The web routes never use cluster credentials. In a single-service deployment,
web and worker share the same backend filesystem and secret-access boundary.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings, load_settings
from .db import DeliveryConflict, Store
from .supervisor import maintenance_enabled, supervisor_healthy


STATUS_LABELS = {
    "RECEIVED": "Checking request", "CHECKING_REQUEST": "Checking request", "REJECTED": "Rejected",
    "PREPARATION_FAILED": "Preparation failed", "WAITING_FOR_CAPACITY": "Waiting for app slot",
    "PREPARING": "Preparing", "SUBMITTING": "Submitting", "SUBMISSION_UNKNOWN": "Submission uncertain",
    "QUEUED": "Queued on cluster", "RUNNING": "Running", "VALIDATING_RESULTS": "Checking results",
    "SUCCEEDED": "Succeeded", "PARTIAL": "Partial results", "FAILED": "Failed", "CANCELLED": "Cancelled",
    "TIMED_OUT": "Timed out", "NEEDS_REVIEW": "Needs review",
    "COMPLETED": "Completed",
}
PUBLIC_RUN_FIELDS = {
    "id", "display_id", "actor_login", "actor_id", "experiment", "branch", "commit_sha", "commit_url",
    "dataset_id", "state", "reason", "created_at", "updated_at", "submitted_at", "started_at", "ended_at",
    "capacity_reserved", "log_tail", "monitor_error",
    "scheduler_job_id",
}
PROVENANCE_FIELDS = {
    "source_commit", "source_sha256", "archive_sha256", "runtime_sha256", "checkpoint_sha256", "adapter_version",
    "adapter_sha256", "input_manifest_sha256", "dataset_id", "seed", "gpu_type", "gpu_name", "feature_dimensions",
    "python", "torch", "cuda_runtime",
}


def _redact(value, settings: Settings, run: dict | None = None):
    """Private operator configuration never appears in API projections or logs."""
    if isinstance(value, dict):
        return {k: _redact(v, settings, run) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, settings, run) for v in value]
    if not isinstance(value, str):
        return value
    replacements = {
        settings.cluster.host, settings.cluster.user, settings.cluster.account,
        settings.cluster.root, settings.cluster.helper_path, settings.cluster.runtime_image,
        settings.cluster.checkpoint_path, str(settings.state_dir),
        str(settings.cluster.ssh_key_path or ""), str(settings.cluster.known_hosts_path or ""),
        settings.webhook_secret, settings.viewer_password,
    }
    # Old jobs may use a prior SSH identity after operator configuration changes.
    # Their accepted policy remains the source of truth for both execution and privacy.
    for scope in ((run or {}).get("policy", {}), (run or {}).get("resolved", {})):
        cluster = scope.get("cluster", {})
        for key in ("host", "user", "account", "root", "helper_path", "runtime_image", "checkpoint_path"):
            if isinstance(cluster.get(key), str):
                replacements.add(cluster[key])
        datasets = scope.get("datasets", {})
        if scope.get("dataset"):
            datasets = {"selected": scope["dataset"]}
        for dataset in datasets.values():
            for entry in dataset.get("entries", []):
                if isinstance(entry.get("path"), str):
                    replacements.add(entry["path"])
    for sensitive in sorted((s for s in replacements if len(s) >= 4), key=len, reverse=True):
        value = value.replace(sensitive, "[private]")
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
                   "[private key redacted]", value, flags=re.S)
    return value


def public_run(run: dict, settings: Settings) -> dict:
    result = {k: run.get(k) for k in PUBLIC_RUN_FIELDS}
    result["branch"] = run.get("branch") or (run.get("ref") or "").removeprefix("refs/heads/")
    config = run.get("config") or {}
    result["config"] = {k: config[k] for k in ("schema_version", "job_type", "dataset_id", "preset_id", "seed", "repeat_id") if k in config}
    result["dataset_id"] = run.get("dataset_id") or config.get("dataset_id")
    sha = run.get("commit_sha")
    result["commit_url"] = f"{settings.repository.url}/commit/{sha}" if sha else None
    result["source_kind"] = "slurm" if run.get("source_kind") == "slurm" else "github"
    result["last_observed_at"] = run.get("last_observed_at") or run.get("last_checked_at")
    result["runtime_seconds"] = None
    if run.get("started_at") and (run.get("ended_at") or run.get("state") == "RUNNING"):
        try:
            start = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(run["ended_at"].replace("Z", "+00:00")) if run.get("ended_at") else datetime.now(timezone.utc)
            result["runtime_seconds"] = max(0, (end - start).total_seconds())
        except (ValueError, TypeError):
            pass
    raw_validation = run.get("validation") or run.get("result") or {}
    result["validation"] = {k: raw_validation[k] for k in ("outcome", "expected", "valid", "expected_count", "valid_count") if k in raw_validation}
    result["validation"]["errors"] = [
        {"sample": str(e.get("name", e.get("sample", "input"))), "reason": str(e.get("reason", e.get("error", "Validation failed")))}
        if isinstance(e, dict) else str(e) for e in raw_validation.get("errors", [])[:100]
    ]
    provenance = {**(run.get("provenance") or {}), **(raw_validation.get("provenance") or {})}
    result["provenance"] = {k: provenance[k] for k in PROVENANCE_FIELDS if k in provenance}
    source = run.get("source") if isinstance(run.get("source"), dict) else {}
    result["provenance"].update(source_commit=run.get("commit_sha"),
                                 source_sha256=source.get("sha256") or provenance.get("source_sha256"),
                                 commit_author=run.get("commit_author", {}), commit_committer=run.get("commit_committer", {}))
    if provenance.get("gpu"):
        result["provenance"]["gpu_name"] = provenance["gpu"]
    # Never expose remote paths, source archive locations, module paths, or raw policies.
    result["artifacts"] = [
        {"id": a.get("id"), "name": a.get("name"), "size": a.get("size"), "sha256": a.get("sha256"),
         "points": a.get("points"), "atoms": a.get("atoms"),
         "cached": a.get("cache_state") == "cached", "reason": a.get("cache_reason", "")}
        for a in run.get("artifacts", [])
    ]
    return _redact(result, settings, run)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    maintenance = maintenance_enabled()
    if settings.mode != "observe" and len(settings.webhook_secret) < 16:
        raise ValueError("Configure a webhook secret (16+ characters).")
    store = Store(settings.database_path)
    if not maintenance:
        store.initialize()
    app = FastAPI(title="dMaSIF research console", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.store = settings, store
    directory = Path(__file__).parent
    templates = Jinja2Templates(directory=directory / "templates")
    app.mount("/static", StaticFiles(directory=directory / "static"), name="static")
    public_settings = {"mode": settings.mode, "operator_contact": settings.operator_contact,
                       "poll_seconds": settings.dashboard_poll_seconds, "repository_url": settings.repository.url,
                       "cluster_poll_seconds": settings.poll_seconds,
                       "instructions_url": "https://github.com/azhang4216/dmasif-console#researcher-workflow"}

    @app.middleware("http")
    async def maintenance_and_headers(request: Request, call_next):
        # Viewing is public. Only the webhook accepts writes, with its own HMAC
        # and repository/sender checks; operator actions stay in the local CLI.
        if maintenance and request.url.path != "/healthz":
            response = JSONResponse({"detail": "Console maintenance is in progress. Please try again later."},
                                    status_code=503, headers={"Retry-After": "60"})
        else:
            response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.get("/healthz")
    def health():
        if not supervisor_healthy(os.environ.get("DMASIF_SUPERVISOR_STATUS")):
            raise HTTPException(503, "Service is restarting")
        if maintenance:
            # Restores can replace the DB while maintenance HTTP is running.
            return {"status": "maintenance"}
        try:
            with store.connection() as con:
                con.execute("SELECT id FROM runs LIMIT 1").fetchone()
        except (sqlite3.Error, OSError):
            raise HTTPException(503, "State storage unavailable") from None
        return {"status": "ok"}

    @app.post("/webhooks/github")
    async def webhook(request: Request):
        if settings.mode == "observe":
            raise HTTPException(403, "This dashboard only observes existing cluster jobs; submissions are disabled.")
        if (settings.state_dir / "restore-pending.json").exists():
            raise HTTPException(503, "Restore reconciliation is pending; redeliver after the operator resumes service.")
        signature = request.headers.get("x-hub-signature-256", "")
        if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
            raise HTTPException(401, "Invalid webhook signature")
        chunks, length = [], 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > settings.max_webhook_bytes:
                raise HTTPException(413, "Webhook exceeds configured size limit")
            chunks.append(chunk)
        body = b"".join(chunks)
        expected = "sha256=" + hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(401, "Invalid webhook signature")
        delivery_id = request.headers.get("x-github-delivery", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", delivery_id):
            raise HTTPException(400, "Missing or invalid delivery identity")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "Payload must be an object")
        event = request.headers.get("x-github-event", "")
        repo = payload.get("repository") or {}
        if not isinstance(repo, dict) or type(repo.get("id")) is not int or repo["id"] != settings.repository.id:
            raise HTTPException(403, "Repository is not approved")
        sender = payload.get("sender") or {}
        if not isinstance(sender, dict):
            raise HTTPException(400, "Invalid sender")
        ref = payload.get("ref", "")
        if not isinstance(ref, str) or len(ref) > 1024:
            raise HTTPException(400, "Invalid ref")
        actor_id, login = sender.get("id"), sender.get("login", "")
        decision, reason, run_data = "IGNORED", "Only pushes to run branches create jobs", None
        if event == "push" and ref.startswith("refs/heads/runs/") and not payload.get("deleted", False):
            if (type(actor_id) is not int or actor_id not in settings.allowed_actor_ids
                    or sender.get("type") != "User" or not isinstance(login, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login)):
                decision, reason = "REJECTED", "Pushing account is not an allowed personal GitHub account"
            else:
                branch = ref.removeprefix("refs/heads/")
                experiment = branch.removeprefix("runs/")
                sha = payload.get("after", "")
                if not experiment.strip():
                    decision, reason = "REJECTED", "Run branch must be runs/experiment-name"
                elif not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or sha == "0" * 40:
                    raise HTTPException(400, "Push must identify an exact commit SHA")
                else:
                    head = payload.get("head_commit") or {}
                    if not isinstance(head, dict):
                        head = {}
                    def author(key):
                        value = head.get(key, {})
                        return {k: str(value[k])[:200] for k in ("name", "email", "username") if k in value} if isinstance(value, dict) else {}
                    decision, reason = "ACCEPTED", "Checking source and experiment configuration"
                    run_data = {
                        "state": "CHECKING_REQUEST", "actor_id": actor_id, "actor_login": login,
                        "repository_id": settings.repository.id, "repository": settings.repository.full_name,
                        "commit_sha": sha, "commit_url": f"{settings.repository.url}/commit/{sha}",
                        "ref": ref, "branch": branch, "experiment": experiment, "before_sha": payload.get("before"),
                        "forced": bool(payload.get("forced", False)), "commit_author": author("author"),
                        "commit_committer": author("committer"), "policy": settings.policy_snapshot(),
                    }
        elif payload.get("deleted"):
            reason = "Branch deletion does not create a job"
        selected = {"repository_id": settings.repository.id, "actor_id": actor_id if isinstance(actor_id, int) else None,
                    "actor_login": login if isinstance(login, str) else "", "ref": ref, "event": event}
        try:
            result = store.accept_delivery(settings.hook_id, delivery_id, hashlib.sha256(body).hexdigest(),
                                           selected, decision, reason, run_data, queue_limit=settings.queue_limit)
        except DeliveryConflict:
            raise HTTPException(409, "Delivery identity conflicts with an earlier signed payload") from None
        return JSONResponse(result, status_code=202)

    def history_data(actor=None, status=None, experiment=None):
        runs = [public_run(r, settings) for r in store.list_runs(actor=actor, status=status, experiment=experiment)]
        activity = []
        for delivery in store.deliveries():
            p = delivery["payload"]
            activity.append({"delivery_id": delivery["delivery_id"], "decision": delivery["decision"],
                             "reason": delivery["reason"], "run_id": delivery["run_id"],
                             "created_at": delivery["received_at"], "received_at": delivery["received_at"],
                             "actor_login": p.get("actor_login"), "branch": p.get("ref", "").removeprefix("refs/heads/")})
        control = store.control_state()
        if settings.mode == "observe":
            control = {"paused": False, "reason": ""}
        elif not settings.submissions_enabled:
            control = {**control, "paused": True, "reason": "Submissions are disabled in operator configuration."}
        if (settings.state_dir / "restore-pending.json").exists():
            control = {**control, "paused": True, "reason": "Restore reconciliation requires operator review."}
        with store.connection() as con:
            actors = [row[0] for row in con.execute("SELECT DISTINCT json_extract(data,'$.actor_login') AS actor FROM runs WHERE actor IS NOT NULL AND actor != '' ORDER BY actor")]
        return {"runs": runs, "stats": store.stats(), "activity": _redact(activity, settings),
                "actors": actors, "status_labels": STATUS_LABELS, "control": _redact(control, settings),
                "observation": observation_data()}

    def observation_data():
        if settings.mode != "observe":
            return None
        state = store.observation_state()
        return _redact({k: state.get(k) for k in ("observed_at", "last_attempt_at", "error", "job_count")}, settings)

    def detail_data(run_id: str):
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        jobs = [{k: job.get(k) for k in ("job_id", "state", "exit_code", "submitted_at", "started_at", "ended_at", "observed_at")}
                for job in store.jobs(run_id)]
        events = []
        for event in store.events(run_id):
            detail = event["detail"]
            if isinstance(detail, dict) and isinstance(detail.get("detail"), dict):
                detail = detail["detail"]
            transition = {key: detail[key] for key in ("from", "to", "state")
                          if isinstance(detail, dict) and isinstance(detail.get(key), str)
                          and detail[key] in STATUS_LABELS}
            events.append({"kind": event["kind"], "created_at": event["created_at"], **transition,
                           "reason": detail.get("reason", "") if isinstance(detail, dict) else str(detail)})
        return {"run": public_run(run, settings), "jobs": _redact(jobs, settings, run), "events": _redact(events, settings, run),
                "status_labels": STATUS_LABELS, "observation": observation_data()}

    @app.get("/")
    def history(request: Request, actor: str = Query(default="", max_length=100),
                status: str = Query(default="", max_length=40), experiment: str = Query(default="", max_length=200)):
        data = history_data(actor or None, status or None, experiment or None)
        return templates.TemplateResponse(request=request, name="history.html",
                                          context={**data, "settings": public_settings, "filters": {"actor": actor, "status": status, "experiment": experiment}})

    @app.get("/runs/{run_id}")
    def detail(request: Request, run_id: str):
        return templates.TemplateResponse(request=request, name="detail.html", context={**detail_data(run_id), "settings": public_settings})

    @app.get("/api/runs")
    def runs_api(actor: str = Query(default="", max_length=100), status: str = Query(default="", max_length=40),
                 experiment: str = Query(default="", max_length=200)):
        return history_data(actor or None, status or None, experiment or None)

    @app.get("/api/runs/{run_id}")
    def run_api(run_id: str):
        return detail_data(run_id)

    @app.get("/api/runs/{run_id}/logs")
    def logs_api(run_id: str):
        data = detail_data(run_id)["run"]
        return {"text": data.get("log_tail") or "", "last_observed_at": data.get("last_observed_at")}

    @app.get("/artifacts/{artifact_id}")
    def artifact(artifact_id: str):
        if not re.fullmatch(r"[a-f0-9]{64}", artifact_id):
            raise HTTPException(404, "Artifact not found")
        with store.connection() as con:
            rows = con.execute("SELECT runs.id,json_each.value AS artifact FROM runs,json_each(runs.data,'$.artifacts') WHERE json_extract(json_each.value,'$.id')=?", (artifact_id,)).fetchall()
        if len(rows) != 1:
            raise HTTPException(404, "Artifact not found")
        record = json.loads(rows[0]["artifact"])
        if record.get("cache_state") != "cached":
            raise HTTPException(410, "Artifact is not cached. Contact the operator for its retained copy.")
        base = settings.state_dir / "artifacts"
        path = base / rows[0]["id"] / artifact_id
        if (path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(base.resolve())
                or not path.is_file() or path.stat().st_size > settings.max_artifact_bytes):
            raise HTTPException(410, "Cached artifact unavailable")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record.get("sha256") or path.stat().st_size != record.get("size"):
            raise HTTPException(410, "Cached artifact failed its integrity check")
        filename = re.sub(r"[^A-Za-z0-9_.-]", "_", str(record.get("name", "result.npz")))[:200]
        return FileResponse(path, media_type="application/octet-stream", filename=filename)

    return app


def app_factory():
    return create_app()
