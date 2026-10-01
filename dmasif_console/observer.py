"""Read existing Slurm allocations over SSH without installing or running jobs.

Only the observer contacts the cluster. Website requests read its local cache.
The remote program runs from stdin, writes no files, and invokes only squeue
and sacct. Existing jobs provide no verified GitHub identity or source commit.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

from .db import Store, utcnow
from .runtime_secrets import private_key_copy


MAX_RESPONSE_BYTES = 1024 * 1024
MAX_JOBS = 200

# All code sent to the login node is fixed application code. The small request
# is JSON encoded as base64 and is never interpreted as shell or Python code.
REMOTE_SCRIPT = r'''
import base64
from datetime import datetime, timedelta, timezone
import getpass
import json
import os
import re
import selectors
import subprocess
import time

LIMIT = 1024 * 1024

def read_command(argv, env):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=env)
    output = bytearray()
    deadline = time.monotonic() + 15
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("query timeout")
                for key, unused in ready.select(min(remaining, 1)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        ready.unregister(key.fileobj)
                    else:
                        output.extend(data)
                        if len(output) > LIMIT:
                            raise RuntimeError("query limit")
        if process.wait(timeout=max(0.1, deadline-time.monotonic())):
            raise RuntimeError("query failed")
        return output.decode("utf-8", errors="strict")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()

def collect(request):
    user, prefix = request["user"], request["prefix"]
    if getpass.getuser() != user or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", user):
        raise ValueError("identity mismatch")
    if not isinstance(prefix, str) or not prefix or len(prefix) > 100:
        raise ValueError("invalid prefix")
    env = dict(os.environ, TZ="UTC", SLURM_TIME_FORMAT="%Y-%m-%dT%H:%M:%S")
    start = (datetime.now(timezone.utc)-timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    accounting = read_command([
        "sacct", "--local", "--allocations", "--duplicates", "--noheader", "--parsable2",
        "--user", user, "--starttime", start,
        "--format=JobIDRaw,JobName%200,State%64,Submit,Start,End,ExitCode",
    ], env)
    # %A is the actual allocation ID, including the unique ID of each array
    # element; %i is a display expression and is not the same as JobIDRaw.
    queue = read_command([
        "squeue", "--local", "--array", "--noheader", "--user", user,
        "--format=%A|%j|%T|%V|%S",
    ], env)
    jobs = {}
    for line in accounting.splitlines():
        cells = [value.strip() for value in line.split("|")]
        if len(cells) != 7:
            continue
        job_id, name, state, submitted, started, ended, exit_code = cells
        if not name.startswith(prefix) or not re.fullmatch(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?", job_id):
            continue
        jobs[(job_id, submitted)] = dict(job_id=job_id, job_name=name, state=state,
            submitted_at=submitted, started_at=started, ended_at=ended, exit_code=exit_code)
    for line in queue.splitlines():
        cells = [value.strip() for value in line.split("|")]
        if len(cells) != 5:
            continue
        job_id, name, state, submitted, started = cells
        if not name.startswith(prefix) or not re.fullmatch(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?", job_id):
            continue
        # Queue observations are fresher than the earlier accounting query.
        # The start time of a pending job is an estimate, not an actual start.
        jobs[(job_id, submitted)] = dict(job_id=job_id, job_name=name, state=state,
            submitted_at=submitted,
            started_at=started if state in {"RUNNING", "COMPLETING", "SUSPENDED", "STOPPED", "SIGNALING", "STAGE_OUT"} else None,
            ended_at=None, exit_code=None, live=True)
    selected = sorted(jobs.values(), key=lambda job: (bool(job.get("live")), job["submitted_at"]), reverse=True)
    return {"jobs": selected[:200], "observed_at": datetime.now(timezone.utc).isoformat(),
            "truncated": len(selected) > 200}

try:
    request = json.loads(base64.b64decode(REQUEST_B64))
    result = {"ok": True, "result": collect(request)}
    encoded = json.dumps(result, separators=(",", ":"))
    if len(encoded.encode()) > LIMIT:
        raise ValueError("response limit")
    print(encoded)
except Exception:
    print('{"ok":false,"error":"Read-only scheduler query failed"}')
'''


def _ssh_read(argv: list[str], script: bytes, timeout: int) -> bytes:
    """Bound SSH output while reading; banners/errors are never published."""
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        process.stdin.write(script)
        process.stdin.close()
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Read-only SSH query timed out")
                for key, _ in ready.select(min(remaining, 1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        ready.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_RESPONSE_BYTES:
                            raise ValueError("Read-only SSH response is too large")
        if process.wait(timeout=max(0.1, deadline-time.monotonic())):
            raise ValueError("Read-only SSH query failed")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()


def fetch_snapshot(settings) -> dict:
    """Run the fixed read-only script through a pinned, noninteractive SSH hop."""
    cluster = settings.cluster
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", cluster.host):
        raise ValueError("Invalid SSH destination")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", cluster.user):
        raise ValueError("Invalid SSH account")
    if not cluster.ssh_key_path or not cluster.known_hosts_path:
        raise ValueError("Pinned SSH credentials are required")
    request = json.dumps({"user": cluster.user, "prefix": settings.observation_job_prefix}).encode()
    encoded = base64.b64encode(request).decode("ascii")
    script = ("REQUEST_B64 = '" + encoded + "'\n" + REMOTE_SCRIPT).encode()
    argv = [
        "ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
        "-o", "UserKnownHostsFile=" + str(cluster.known_hosts_path), "-i", "",
        "--", cluster.user + "@" + cluster.host, "python3 -B -s",
    ]
    with private_key_copy(cluster.ssh_key_path) as key:
        argv[argv.index("-i") + 1] = str(key)
        raw = _ssh_read(argv, script, max(40, cluster.ssh_timeout_seconds))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Read-only SSH response is too large")
    response = json.loads(raw)
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise ValueError("Read-only scheduler query failed")
    return response["result"]


def _timestamp(value, *, required: bool = False) -> str | None:
    if value is None or value in {"", "Unknown", "None", "N/A", "UNLIMITED"}:
        if required:
            raise ValueError("Scheduler observation lacks a submission timestamp")
        return None
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError("Invalid scheduler timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalize_job(job: dict) -> dict:
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or len(job_id) > 64 or not re.fullmatch(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?", job_id):
        raise ValueError("Invalid allocation identity")
    name = job.get("job_name")
    if not isinstance(name, str) or not name or len(name) > 200 or any(ord(c) < 32 for c in name):
        raise ValueError("Invalid scheduler job name")
    raw = job.get("state")
    if not isinstance(raw, str) or not raw or len(raw) > 100:
        raise ValueError("Invalid scheduler state")
    scheduler_state = raw.upper().split()[0].rstrip("+")
    if scheduler_state in {"PENDING", "CONFIGURING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD"}:
        state, reason = "QUEUED", "Waiting on the cluster."
    elif scheduler_state in {"RUNNING", "COMPLETING", "SUSPENDED", "STOPPED", "SIGNALING", "STAGE_OUT", "RESIZING"}:
        state, reason = "RUNNING", "The cluster reports an active allocation."
    elif scheduler_state == "COMPLETED":
        state, reason = "COMPLETED", "Slurm reports completion. Research outputs have not been validated."
    elif scheduler_state == "CANCELLED":
        state, reason = "CANCELLED", "Slurm reports this allocation was cancelled."
    elif scheduler_state in {"TIMEOUT", "DEADLINE"}:
        state, reason = "TIMED_OUT", "Slurm reports this allocation reached its time limit."
    elif scheduler_state in {"FAILED", "BOOT_FAIL", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "REVOKED"}:
        state, reason = "FAILED", "Slurm reports " + scheduler_state.lower().replace("_", " ") + "."
    else:
        state, reason = "NEEDS_REVIEW", "Slurm reports an unrecognized allocation state."
    exit_code = job.get("exit_code")
    if exit_code is not None and (not isinstance(exit_code, str) or not re.fullmatch(r"[0-9]+(?::[0-9]+)?", exit_code)):
        exit_code = None
    return {
        "job_id": job_id, "job_name": name, "state": state, "scheduler_state": scheduler_state,
        "reason": reason, "submitted_at": _timestamp(job.get("submitted_at"), required=True),
        "started_at": _timestamp(job.get("started_at")) if state != "QUEUED" else None,
        "ended_at": _timestamp(job.get("ended_at")) if state not in {"QUEUED", "RUNNING"} else None,
        "exit_code": exit_code,
    }


class Observer:
    def __init__(self, settings, *, fetch=None, store: Store | None = None):
        if settings.mode != "observe" or settings.submissions_enabled:
            raise ValueError("The observer requires observation mode with submissions disabled")
        prefix = settings.observation_job_prefix
        if not isinstance(prefix, str) or not prefix or len(prefix) > 100:
            raise ValueError("Configure a nonempty job-name prefix for observation")
        self.settings = settings
        Path(settings.state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = store or Store(settings.database_path)
        self.store.initialize()
        self.fetch = fetch or (lambda: fetch_snapshot(settings))
        self._lock = None

    def __enter__(self):
        self._lock = (Path(self.settings.state_dir) / "worker.lock").open("a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            self._lock = None
            raise RuntimeError("Another worker or observer already holds this state directory's lock") from exc
        return self

    def __exit__(self, *_):
        if self._lock:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def tick(self) -> bool:
        """Refresh a shared cache; failure keeps previously observed jobs intact."""
        try:
            snapshot = self.fetch()
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("jobs"), list):
                raise ValueError("Invalid scheduler response")
            if len(snapshot["jobs"]) > MAX_JOBS:
                raise ValueError("Scheduler response exceeds job limit")
            observed_at = _timestamp(snapshot.get("observed_at") or utcnow(), required=True)
            jobs = []
            for job in snapshot["jobs"]:
                if not isinstance(job, dict):
                    raise ValueError("Invalid scheduler record")
                name = job.get("job_name")
                if isinstance(name, str) and name.startswith(self.settings.observation_job_prefix):
                    jobs.append(_normalize_job(job))
            # Validate the entire response before touching cached job records.
            for job in jobs:
                self.store.upsert_observed_run(self.settings.cluster.host, job, observed_at)
            self.store.record_observation(observed_at=observed_at, job_count=len(jobs))
            return True
        except Exception:
            # SSH diagnostics and tracebacks can contain login details. The
            # public site gets a fixed message while keeping its previous data.
            self.store.record_observation(
                observed_at=None,
                error="Cluster refresh failed. Showing the last successful observation; retrying automatically.",
            )
            return False

    def run_forever(self):
        with self:
            while True:
                self.tick()
                time.sleep(max(60, self.settings.poll_seconds))
