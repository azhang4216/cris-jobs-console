"""Single-worker durable orchestration. Submission is never retried blindly."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import time

from .db import Store
from .backups import AutomaticBackups
from .sources import SourceError, read_experiment, sha256_file, snapshot_source
from .transport import PreparationError, SubmissionUncertain, get_adapter

ACTIVE = {"PREPARING", "SUBMITTING", "SUBMISSION_UNKNOWN", "QUEUED", "RUNNING", "VALIDATING_RESULTS", "NEEDS_REVIEW"}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_terminal(job: dict) -> bool:
    return bool(job.get("terminal")) or str(job.get("state", "")).upper().split()[0].split("+")[0] in TERMINAL


def succeeded(job: dict) -> bool:
    return str(job.get("state", "")).upper() == "COMPLETED" and str(job.get("exit_code", "")) in {"0", "0:0"}


class Worker:
    def __init__(self, settings, store: Store | None = None, adapter=None):
        if settings.mode == "observe":
            raise ValueError("Observe mode is read-only; use the observe command instead of a worker")
        self.settings = settings
        Path(settings.state_dir).mkdir(parents=True, exist_ok=True)
        self.store = store or Store(settings.database_path)
        self.store.initialize()
        self.adapter = adapter or get_adapter(settings)
        self._lock = None
        self._backups = AutomaticBackups.from_environment(settings)

    def __enter__(self):
        self._lock = (Path(self.settings.state_dir) / "worker.lock").open("a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            self._lock = None
            raise RuntimeError("Another worker/operator already holds this state directory's lock.") from exc
        return self

    def __exit__(self, *_):
        if self._lock:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def run_forever(self):
        with self:
            while True:
                self.tick()
                time.sleep(self.settings.poll_seconds)

    def _update(self, run: dict, **patch) -> dict:
        state = patch.get("state")
        event = None
        if state and state != run.get("state"):
            event = {"kind": "state_changed", "detail": {"from": run.get("state"), "to": state, "reason": patch.get("reason")}}
        return self.store.update_run(run["id"], patch, event=event)

    def tick(self):
        """Perform one bounded pass; callers running concurrently must hold the lock."""
        try:
            self._tick()
        finally:
            if self._lock is not None and self._backups is not None:
                self._backups.run_due(self.store)

    def _tick(self):
        # Monitoring never stops when new submissions are paused.
        for run in self.store.list_runs(limit=10000):
            if run.get("capacity_reserved") and run["state"] in ACTIVE and run["state"] != "PREPARING":
                self._monitor(run)
        for run in self.store.list_runs(limit=10000):
            if not run.get("capacity_reserved") and any(item.get("cache_state") == "pending" for item in run.get("artifacts", [])):
                if run.get("cache_attempts", 0) < 3:
                    self._cache_artifacts(run, run.get("result", {}).get("artifacts", []))
                else:
                    artifacts = run.get("artifacts", [])
                    for item in artifacts:
                        if item.get("cache_state") == "pending":
                            item.update(cache_state="unavailable", cache_reason="Download recovery limit reached; contact the operator for the retained cluster copy.")
                    self.store.update_run(run["id"], {"artifacts": artifacts})
        for run in reversed(self.store.list_runs(states=["RECEIVED", "CHECKING_REQUEST"], limit=100)):
            self._check_request(run)
        if ((Path(self.settings.state_dir) / "restore-pending.json").exists()
                or self.store.control_state().get("paused") or not self.settings.submissions_enabled):
            return
        if shutil.disk_usage(self.settings.state_dir).free < self.settings.min_free_bytes:
            self.store.set_paused(True, "Local free space is below the configured threshold.", actor="worker")
            return
        preparing = self.store.list_runs(states=["PREPARING"], limit=10)
        if preparing:
            self._stage_and_submit(preparing[-1])
            return
        run = self.store.reserve_next()
        if run:
            self._stage_and_submit(run)

    def _check_request(self, run: dict):
        run = self._update(run, state="CHECKING_REQUEST", reason="Fetching the exact pushed commit and checking its configuration.")
        if shutil.disk_usage(self.settings.state_dir).free < self.settings.min_free_bytes + self.settings.max_source_bytes * 4:
            self.store.set_paused(True, "Insufficient local space for bounded source preparation.", actor="worker")
            self._update(run, state="PREPARATION_FAILED", capacity_reserved=False, reason="Insufficient source storage. The operator must free space before a new push.")
            return
        try:
            from .config import RepositoryConfig
            policy = run.get("policy") or self.settings.policy_snapshot()
            acquisition = self.settings.model_copy(update={"repository": RepositoryConfig.model_validate(policy.get("repository", self.settings.repository.model_dump()))})
            source = snapshot_source(acquisition, run["commit_sha"])
            config, original = read_experiment(source)
            policy = run.get("policy") or self.settings.policy_snapshot()
            datasets, presets = policy["datasets"], policy["presets"]
            if config["dataset_id"] not in datasets:
                raise SourceError("Unknown dataset_id; select an operator-approved immutable dataset.")
            if config["preset_id"] not in presets:
                raise SourceError("Unknown preset_id; select an operator-approved resource preset.")
            if config["preset_id"] != "quick-test":
                raise SourceError("Only the tested quick-test preset is enabled in this release.")
            resolved = {"dataset": datasets[config["dataset_id"]], "preset": presets[config["preset_id"]], "cluster": policy.get("cluster", {})}
            self._update(run, source=source, config=config, original_config=original, resolved=resolved,
                         state="WAITING_FOR_CAPACITY", capacity_reserved=False,
                         reason="Validated request; waiting for the application submission slot.")
        except SourceError as exc:
            text = str(exc)
            rejection = "experiments/run.yaml" in text or "dataset_id" in text or "preset" in text
            self._update(run, state="REJECTED" if rejection else "PREPARATION_FAILED", capacity_reserved=False, reason=text)
        except Exception:
            self._update(run, state="PREPARATION_FAILED", capacity_reserved=False,
                         reason="Source preparation failed. Contact the operator; no cluster job was submitted.")

    def _stage_and_submit(self, run: dict):
        try:
            staged = self.adapter.stage(run, run["source"])
            run = self._update(run, **staged, state="SUBMITTING", submission_intent_at=now(),
                               reason="Submission intent saved; contacting the scheduler once.")
        except (PreparationError, SourceError) as exc:
            self._update(run, state="PREPARATION_FAILED", capacity_reserved=False, reason=str(exc))
            return
        except Exception:
            count = run.get("preparation_retries", 0) + 1
            self._update(run, state="PREPARATION_FAILED" if count >= 3 else "PREPARING",
                         capacity_reserved=count < 3, preparation_retries=count,
                         reason="Cluster staging is unavailable; safe preparation will retry." if count < 3 else "Cluster staging failed after three attempts. Contact the operator.")
            return
        try:
            jobs = self.adapter.submit(run)
            if not jobs:
                raise SubmissionUncertain("No scheduler receipt returned.")
            self._record_jobs(run, jobs)
        except Exception:
            # Catch even programming errors here: submission may already have happened.
            self._update(run, state="SUBMISSION_UNKNOWN", capacity_reserved=True,
                         reason="Submission may have reached Slurm. Reconciliation is required; this run will not be resubmitted.")

    def _record_jobs(self, run: dict, jobs: list[dict]):
        for job in jobs:
            self.store.upsert_job(run["id"], job)
        # Include every previously associated job; an omitted job is not proof it ended.
        all_jobs = self.store.jobs(run["id"])
        patch = {"last_checked_at": now(), "monitor_error": None,
                 "submitted_at": run.get("submitted_at") or next((j.get("submitted_at") for j in jobs if j.get("submitted_at")), None)}
        starts = [j["started_at"] for j in all_jobs if j.get("started_at")]
        if starts:
            patch["started_at"] = min(starts)
        if len(all_jobs) > 1:
            patch.update(state="NEEDS_REVIEW", reason="Multiple scheduler jobs are associated with this run. Contact the operator.", duplicate_jobs=True)
        elif all(is_terminal(j) for j in all_jobs):
            patch.update(state="VALIDATING_RESULTS", reason="Scheduler finished; checking the committed result manifest.")
        elif any(str(j.get("state", "")).upper() in {"RUNNING", "COMPLETING"} for j in all_jobs):
            patch.update(state="RUNNING", reason="Running on the cluster.")
        else:
            patch.update(state="QUEUED", reason=next((j.get("reason") for j in all_jobs if j.get("reason")), "Waiting in Slurm's queue."))
        run = self._update(run, **patch)
        if all_jobs and all(is_terminal(j) for j in all_jobs):
            self._finish(run, all_jobs)
        return run

    def _monitor(self, run: dict, *, reserve_unknown: bool = True):
        run = {**run, "jobs": self.store.jobs(run["id"])}
        try:
            if run["state"] in {"SUBMITTING", "SUBMISSION_UNKNOWN", "NEEDS_REVIEW"}:
                jobs = self.adapter.reconcile(run)
            else:
                jobs = self.adapter.poll(run)
            if not jobs:
                if not self.store.jobs(run["id"]):
                    self._update(run, state="SUBMISSION_UNKNOWN", capacity_reserved=run.get("capacity_reserved", False) or reserve_unknown,
                                 last_checked_at=now(), monitor_error=None,
                                 reason="No conclusive submission evidence yet. Capacity remains reserved; contact the operator.")
                else:
                    self._update(run, monitor_error="Scheduler returned no conclusive evidence; last known state retained.")
                return
            self._record_jobs(run, jobs)
            refreshed = {**self.store.get_run(run["id"]), "jobs": self.store.jobs(run["id"])}
            try:
                tail = self.adapter.logs(refreshed)
                if isinstance(tail, dict):
                    tail = tail.get("text", "")
                tail = str(tail).encode("utf-8")[-self.settings.log_tail_bytes:].decode("utf-8", errors="replace")
                self.store.update_run(run["id"], {"log_tail": tail, "logs_checked_at": now()})
            except Exception:
                pass  # Failure to fetch a log is not a scheduler or scientific failure.
        except Exception:
            self._update(run, monitor_error="Cluster monitoring is temporarily unavailable; last known state retained.")

    def _finish(self, run: dict, jobs: list[dict]):
        run = {**run, "jobs": jobs}
        try:
            result = self.adapter.results(run)
        except Exception:
            result = None
        ended = [job["ended_at"] for job in jobs if job.get("ended_at")]
        common = {"ended_at": max(ended) if ended else run.get("ended_at"), "last_checked_at": now()}
        if result is None and (len(jobs) > 1 or not all(succeeded(job) for job in jobs)):
            result = {"outcome": "FAILED", "expected_count": len(run.get("resolved", {}).get("dataset", {}).get("entries", [])),
                      "valid_count": 0, "artifacts": [], "errors": ["No result manifest; scheduler failure is authoritative."]}
        if result is None:
            checks = run.get("result_checks", 0) + 1
            if checks < 3:
                self._update(run, state="VALIDATING_RESULTS", result_checks=checks, **common)
            else:
                self._update(run, state="NEEDS_REVIEW", capacity_reserved=False, result_checks=checks,
                             reason="All associated jobs ended, but no usable result manifest is available. Contact the operator.", **common)
            return
        raw_states = {str(job.get("state", "")).upper().split()[0] for job in jobs}
        if len(jobs) > 1:
            state, reason = "NEEDS_REVIEW", "Duplicate jobs are terminal. Operator review is required; the capacity slot is released."
        elif "CANCELLED" in raw_states:
            state, reason = "CANCELLED", "Slurm confirmed cancellation."
        elif "TIMEOUT" in raw_states:
            state, reason = "TIMED_OUT", "The job exceeded its Slurm time limit."
        elif not all(succeeded(job) for job in jobs):
            state, reason = "FAILED", "Slurm reported an unsuccessful job; valid files do not turn it into a success."
        else:
            expected = result.get("expected_count", result.get("expected", 0))
            valid = result.get("valid_count", result.get("valid", 0))
            artifacts = result.get("artifacts", [])
            if expected > 0 and valid == expected and len(artifacts) == valid and result.get("outcome") == "SUCCEEDED":
                state, reason = "SUCCEEDED", "Every expected output passed validation."
            elif valid > 0 and valid < expected:
                state, reason = "PARTIAL", "Only some expected outputs passed validation; this is not a successful run."
            else:
                state, reason = "FAILED", "No complete valid result set was produced."
        # Scientific outcome is committed before optional downloads and does not depend on them.
        pending_artifacts = [{**item, "id": hashlib.sha256(f"{run['id']}:{item.get('name')}:{item.get('sha256')}".encode()).hexdigest(),
                              "cache_state": "pending"} for item in result.get("artifacts", [])]
        run = self._update(run, state=state, capacity_reserved=False, reason=reason, result=result,
                           validation=result, artifacts=pending_artifacts, **common)
        self._cache_artifacts(run, result.get("artifacts", []))

    def _cache_artifacts(self, run: dict, artifacts: list[dict]):
        root = Path(self.settings.state_dir)
        cache = root / "artifacts" / run["id"]
        cache.mkdir(parents=True, exist_ok=True)
        self.store.update_run(run["id"], {"cache_attempts": run.get("cache_attempts", 0) + 1})
        saved = []
        run_total = 0
        for item in artifacts:
            artifact = dict(item)
            aid = hashlib.sha256(f"{run['id']}:{artifact.get('name')}:{artifact.get('sha256')}".encode()).hexdigest()
            artifact.update(id=aid, cache_state="unavailable")
            size = artifact.get("size", -1)
            if not isinstance(size, int) or size < 0 or size > self.settings.max_artifact_bytes or run_total + size > self.settings.max_run_artifact_bytes:
                artifact["cache_reason"] = "Result exceeds the website download limit; contact the operator for its retained cluster copy."
                saved.append(artifact)
                continue
            destination = cache / aid
            if destination.is_file() and not destination.is_symlink() and destination.stat().st_size == size and sha256_file(destination) == artifact.get("sha256"):
                artifact.update(cache_state="cached", cache_path=str(destination.relative_to(root)), cached_at=now())
                run_total += size
                saved.append(artifact)
                continue
            if not self._make_cache_room(size):
                artifact["cache_reason"] = "Local download cache is full or below its free-space threshold."
                saved.append(artifact)
                continue
            destination = cache / aid
            temporary = cache / f".{aid}.part"
            try:
                temporary.unlink(missing_ok=True)
                self.adapter.fetch_artifact(run, item, temporary)
                if temporary.is_symlink() or temporary.stat().st_size != size or sha256_file(temporary) != artifact.get("sha256"):
                    raise ValueError("Artifact failed integrity verification")
                os.replace(temporary, destination)
                artifact.update(cache_state="cached", cache_path=str(destination.relative_to(root)), cached_at=now())
                run_total += size
            except Exception:
                temporary.unlink(missing_ok=True)
                artifact["cache_reason"] = "Download failed integrity checks or transfer; its cluster copy is retained. Contact the operator."
            saved.append(artifact)
        self.store.update_run(run["id"], {"artifacts": saved})

    def _make_cache_room(self, wanted: int) -> bool:
        cache = Path(self.settings.state_dir) / "artifacts"
        entries = sorted((path for path in cache.glob("*/*") if path.is_file() and not path.name.startswith(".")), key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in entries)
        if wanted > self.settings.max_cache_bytes:
            return False
        for path in entries:
            if total + wanted <= self.settings.max_cache_bytes:
                break
            size = path.stat().st_size
            path.unlink()
            total -= size
            old = self.store.get_run(path.parent.name)
            if old:
                artifacts = old.get("artifacts", [])
                for item in artifacts:
                    if item.get("id") == path.name:
                        item.update(cache_state="unavailable", cache_reason="Local copy was evicted to stay within the cache limit; cluster copy retained.")
                        item.pop("cache_path", None)
                self.store.update_run(old["id"], {"artifacts": artifacts})
        return total + wanted <= self.settings.max_cache_bytes and shutil.disk_usage(self.settings.state_dir).free - wanted >= self.settings.min_free_bytes

    def reconcile(self, run_id: str, actor: str, reason: str, *, reserve_unknown: bool = True):
        run = self.store.get_run(run_id)
        if not run:
            raise ValueError("Unknown run ID.")
        self.store.add_event(run_id, "operator_reconcile", {"reason": reason}, actor=actor)
        self._monitor(run, reserve_unknown=reserve_unknown)

    def resolve(self, run_id: str, actor: str, reason: str, evidence: str, *, confirm_no_live_job: bool = False):
        """Close an uncertain run using audited operator evidence, never retry it.

        The scheduler must be reachable. Absence of jobs is only actionable in
        combination with the operator's explicit attestation, for example a
        site's confirmation that the previous submit helper is no longer alive.
        """
        run = self.store.get_run(run_id)
        if not run:
            raise ValueError("Unknown run ID.")
        if run["state"] not in {"SUBMISSION_UNKNOWN", "NEEDS_REVIEW"}:
            raise ValueError("Only a submission-unknown or needs-review run can be resolved.")
        if not confirm_no_live_job:
            raise ValueError("Resolution requires --confirm-no-live-job and external verification evidence.")
        if not actor.strip() or not reason.strip() or not evidence.strip() or max(len(reason), len(evidence)) > 4096:
            raise ValueError("Supply an operator identity, reason, and verification reference (at most 4096 characters each).")
        public_reason = "Operator verified that no associated job remains live. The run is closed as failed; repeat through a new commit."
        self.store.add_event(run_id, "operator_resolution_requested", {
            "reason": "Operator requested an evidence-backed resolution of this run.",
            "private_operator_reason": reason, "private_evidence": evidence,
            "confirm_no_live_job": True,
        }, actor=actor)
        # Do not use _monitor here: it deliberately swallows transport failures
        # for ordinary polling, while resolution must refuse an unavailable site.
        known = self.store.jobs(run_id)
        observed = self.adapter.reconcile({**run, "jobs": known})
        for job in observed:
            self.store.upsert_job(run_id, job)
        known = self.store.jobs(run_id)
        if known:
            exact = self.adapter.poll({**run, "jobs": known})
            for job in exact:
                self.store.upsert_job(run_id, job)
            known = self.store.jobs(run_id)
        if any(not is_terminal(job) for job in known):
            self.store.add_event(run_id, "operator_resolution_refused", {
                "reason": "Resolution refused because an associated scheduler job may still be live.",
            }, actor=actor)
            raise ValueError("An associated job is live or lacks terminal evidence. Resolve/cancel it and reconcile before releasing capacity.")
        resolved_at = now()
        attestation = {"actor": actor, "reason": reason, "evidence": evidence,
                       "confirmed_at": resolved_at, "job_ids": [str(job["job_id"]) for job in known]}
        self.store.add_event(run_id, "operator_resolved_no_live_job", {
            "reason": public_reason, "private_attestation": attestation,
        }, actor=actor)
        self._update(run, state="FAILED", capacity_reserved=False, reason=public_reason,
                     last_checked_at=resolved_at, monitor_error=None, operator_resolution=attestation)

    def cancel(self, run_id: str, actor: str, reason: str):
        run = self.store.get_run(run_id)
        if not run:
            raise ValueError("Unknown run ID.")
        self.store.add_event(run_id, "operator_cancel", {"reason": reason}, actor=actor)
        if run["state"] in {"RECEIVED", "CHECKING_REQUEST", "WAITING_FOR_CAPACITY", "PREPARING"}:
            self._update(run, state="CANCELLED", capacity_reserved=False, ended_at=now(), reason=reason)
            return
        jobs = self.adapter.reconcile(run)
        if not jobs:
            raise ValueError("No verified job mapping; cancellation cannot resolve an ambiguous submission.")
        for job in jobs:
            self.store.upsert_job(run_id, job)
        self.adapter.cancel({**self.store.get_run(run_id), "jobs": self.store.jobs(run_id)})
        self._update(run, cancel_requested_at=now(), reason="Cancellation requested; waiting for terminal scheduler evidence.")
        self._monitor(self.store.get_run(run_id))
