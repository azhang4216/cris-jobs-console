#!/usr/bin/env python3
"""Verify two real acceptance pushes using only public cached HTTP GET routes.

Run from the checkout with its installed Python environment. Defaults identify
the October 2026 pilot cases; override repository, branches, commits, and actor
when repeating that acceptance recipe. Exit 0 = complete pass, 2 = pending or
unavailable, 1 = failed verification. No SSH, source execution, Git operation,
or job mutation is performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import ssl
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler

# Public identities of the current pilot acceptance cases, not credentials.
VALID_SHA = "347623c02d64e440bc4ef61cbce787a95b923531"
INVALID_SHA = "7a8ac87600b5fbf089bb77d7dc3e00bbc4d452a6"
DEFAULT_REPO_URL = "https://github.com/azhang4216/dmasif-experiments"
MAX_JSON = 4 * 1024 * 1024
MAX_ARTIFACT = 10 * 1024 * 1024
TERMINAL = {"SUCCEEDED", "REJECTED", "PREPARATION_FAILED", "FAILED", "CANCELLED", "TIMED_OUT", "PARTIAL", "NEEDS_REVIEW", "COMPLETED"}


class VerificationError(Exception):
    """A fixed, non-sensitive verification failure."""


class Pending(Exception):
    def __init__(self, states, checked_results=None):
        self.states = states
        self.checked_results = checked_results or {}


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def timestamp(value, label):
    require(isinstance(value, str) and bool(value), label + " timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise VerificationError(label + " timestamp is invalid") from None
    require(parsed.tzinfo is not None, label + " timestamp has no timezone")
    require(parsed <= datetime.now(timezone.utc), label + " timestamp is in the future")
    return parsed


def digest(value, label):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None,
            label + " SHA-256 is missing or malformed")
    require(value != "0" * 64, label + " SHA-256 is a placeholder")
    return value


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise VerificationError("Cross-origin redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, base_url):
        parsed = urlsplit(base_url)
        require(parsed.scheme in {"http", "https"} and bool(parsed.netloc), "Base URL must use HTTP or HTTPS")
        require(not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                "Base URL must not contain credentials, query parameters, or a fragment")
        self.base_url = base_url.rstrip("/")
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        self.opener = build_opener(HTTPSHandler(context=context), SameOriginRedirect())

    def get(self, path, limit, destination=None):
        require(path.startswith("/") and not path.startswith("//"), "Invalid API path")
        request = Request(self.base_url + path, method="GET", headers={"User-Agent": "dmasif-acceptance-verifier/1"})
        with self.opener.open(request, timeout=30) as response:
            require(response.status == 200, "An HTTP GET did not return success")
            length = response.headers.get("Content-Length")
            if length is not None:
                require(length.isdigit() and int(length) <= limit, "HTTP response exceeds its size limit")
            size = 0
            checksum = hashlib.sha256()
            body = bytearray()
            while True:
                chunk = response.read(min(65536, limit - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                require(size <= limit, "HTTP response exceeds its size limit")
                checksum.update(chunk)
                if destination is None:
                    body.extend(chunk)
                else:
                    destination.write(chunk)
            if length is not None:
                require(size == int(length), "HTTP response length is inconsistent")
        return (size, checksum.hexdigest()) if destination is not None else bytes(body)

    def json(self, path):
        try:
            result = json.loads(self.get(path, MAX_JSON))
        except (ValueError, UnicodeError):
            raise VerificationError("An API response is not valid JSON") from None
        require(isinstance(result, dict), "An API response is not a JSON object")
        return result


def identify(run, sha, actor, actor_id, branch, repo_url):
    require(run.get("commit_sha") == sha, "Run commit does not match the pushed acceptance commit")
    require(run.get("actor_login") == actor and run.get("actor_id") == actor_id,
            "Run does not identify the expected GitHub pusher")
    require(run.get("source_kind") == "github", "Run was not recorded as a GitHub submission")
    require(run.get("branch") == branch, "Run branch does not match the acceptance branch")
    require(run.get("commit_url") == repo_url + "/commit/" + sha, "Commit link is not the expected research commit")
    require(not run.get("capacity_reserved"), "A finished acceptance run still reserves submission capacity")
    provenance = run.get("provenance") or {}
    require(provenance.get("source_commit") == sha, "Retained source commit is not the acceptance commit")
    digest(provenance.get("source_sha256"), "Retained source")
    for field in ("commit_author", "commit_committer"):
        identity = provenance.get(field)
        require(isinstance(identity, dict) and bool(identity.get("name")), "Source " + field + " provenance is missing")
    return provenance


def verify_events(events):
    require(isinstance(events, list), "Run events are missing")
    # API order is newest first. Retain actual event order, including equal-time
    # transitions; do not invent states from the final scheduler record.
    chronological = list(reversed(events))
    states = []
    times = []
    for event in chronological:
        state = event.get("to") or event.get("state")
        if state:
            states.append(state)
            times.append(timestamp(event.get("created_at"), "State event"))
    require(all(a <= b for a, b in zip(times, times[1:])), "State event timestamps are out of order")
    position = -1
    for expected in ("QUEUED", "RUNNING", "SUCCEEDED"):
        matches = [i for i, state in enumerate(states) if state == expected and i > position]
        require(bool(matches), "No recorded " + expected + " transition in the required sequence")
        position = matches[0]
    return states


def verify_valid(client, detail, options):
    run = detail["run"]
    provenance = identify(run, options.valid_sha, options.actor, options.actor_id,
                          options.valid_branch, options.repo_url)
    require(run.get("state") == "SUCCEEDED", "Valid acceptance run did not succeed")
    config = run.get("config") or {}
    expected = {"schema_version": 1, "job_type": "dmasif_extract", "dataset_id": "demo-1stp-v1",
                "preset_id": "quick-test", "seed": 0, "repeat_id": options.valid_repeat_id}
    require(config == expected, "Valid run configuration differs from the acceptance request")
    require(run.get("dataset_id") == expected["dataset_id"], "Valid run dataset is incorrect")
    hashes = {name: digest(provenance.get(name), name) for name in
              ("source_sha256", "runtime_sha256", "checkpoint_sha256", "adapter_sha256")}
    for optional in ("archive_sha256", "input_manifest_sha256"):
        if provenance.get(optional) is not None:
            hashes[optional] = digest(provenance[optional], optional)
    for field in ("python", "torch", "cuda_runtime", "gpu_name"):
        require(isinstance(provenance.get(field), str) and bool(provenance[field].strip()),
                "Real execution provenance lacks " + field)
    gpu = provenance["gpu_name"]
    require(not any(word in gpu.lower() for word in ("fake", "simulat", "synthetic")), "GPU provenance indicates a simulation")
    jobs = detail.get("jobs")
    require(isinstance(jobs, list) and len(jobs) == 1, "Valid run must have exactly one scheduler job")
    job = jobs[0]
    require(re.fullmatch(r"[0-9]+", str(job.get("job_id", ""))) is not None, "Scheduler job ID is invalid")
    require(job.get("state") == "COMPLETED" and str(job.get("exit_code")) in {"0", "0:0"},
            "Scheduler job did not complete with exit zero")
    times = {name: timestamp(run.get(name), name) for name in ("created_at", "submitted_at", "started_at", "ended_at")}
    require(times["created_at"] <= times["submitted_at"], "Run submission predates the received push")
    # Slurm timestamps have second precision; the receipt can have fractions.
    require((times["started_at"] - times["submitted_at"]).total_seconds() >= -1,
            "Run starts before its scheduler submission")
    require(times["ended_at"] > times["started_at"], "Run lacks a positive execution duration")
    for name in ("submitted_at", "started_at", "ended_at"):
        actual = timestamp(job.get(name), "Scheduler " + name)
        require(abs((actual - times[name]).total_seconds()) <= 1, "Run and scheduler timing disagree")
    duration = (times["ended_at"] - times["started_at"]).total_seconds()
    require(isinstance(run.get("runtime_seconds"), (int, float)) and abs(run["runtime_seconds"] - duration) < 1,
            "Displayed runtime is inconsistent")
    transitions = verify_events(detail.get("events"))
    logs = client.json("/api/runs/" + quote(run["id"], safe="") + "/logs")
    log_text = logs.get("text")
    require(isinstance(log_text, str) and bool(log_text.strip()), "Valid run has no captured execution logs")
    require("LOCAL SIMULATION" not in log_text, "Execution log is a synthetic demo log")
    validation = run.get("validation") or {}
    require(validation.get("outcome") == "SUCCEEDED" and not validation.get("errors"), "Result validation did not pass")
    require(validation.get("expected_count", validation.get("expected")) == 1
            and validation.get("valid_count", validation.get("valid")) == 1, "Expected exactly one validated 1STP output")
    artifacts = run.get("artifacts")
    require(isinstance(artifacts, list) and len(artifacts) == 1, "Expected one cached NPZ result")
    artifact = artifacts[0]
    require(artifact.get("cached") is True, "Validated artifact has not been cached yet")
    artifact_id = digest(artifact.get("id"), "Artifact identity")
    expected_hash = digest(artifact.get("sha256"), "Artifact content")
    size = artifact.get("size")
    require(type(size) is int and 0 < size <= MAX_ARTIFACT, "Artifact size is invalid or exceeds 10 MiB")
    name = artifact.get("name")
    require(isinstance(name, str) and Path(name).name == name and name.endswith(".npz"), "Artifact filename is not an NPZ basename")
    from cluster_adapter.validate import validate_npz
    with tempfile.TemporaryDirectory(prefix="dmasif-acceptance-") as temp:
        file = Path(temp) / "result.npz"
        with file.open("xb") as output:
            downloaded, actual_hash = client.get("/artifacts/" + artifact_id, MAX_ARTIFACT, output)
        require(downloaded == size and actual_hash == expected_hash, "Downloaded NPZ size or checksum differs from the validated artifact")
        try:
            dimensions = validate_npz(file)
        except Exception:
            raise VerificationError("Downloaded NPZ failed the scientific result schema") from None
    require(dimensions["points"] > 0 and dimensions["atoms"] > 0, "NPZ has no surface points or protein atoms")
    for field in ("points", "atoms"):
        require(artifact.get(field) == dimensions[field], "Published artifact dimensions differ from the downloaded NPZ")
    return {"id": run["id"], "state": run["state"], "commit": run["commit_sha"],
            "actor": run["actor_login"], "actor_id": run["actor_id"], "job_id": job["job_id"],
            "scheduler_state": job["state"], "exit_code": job["exit_code"],
            "timings": {key: run[key] for key in times}, "runtime_seconds": duration,
            "recorded_transitions": transitions, "gpu": gpu, "provenance_hashes": hashes,
            "log_bytes": len(log_text.encode()), "artifact": {"name": name, "bytes": downloaded,
            "sha256": actual_hash, **dimensions, "feature_dimensions": 16}}


def verify_invalid(detail, options):
    run = detail["run"]
    provenance = identify(run, options.invalid_sha, options.actor, options.actor_id,
                          options.invalid_branch, options.repo_url)
    require(run.get("state") == "REJECTED", "Invalid acceptance run was not rejected")
    require(detail.get("jobs") == [], "Invalid configuration reached a scheduler job")
    require(run.get("artifacts") == [], "Invalid configuration has output artifacts")
    require(not any(run.get(key) for key in ("submitted_at", "started_at", "ended_at")),
            "Rejected configuration has scheduler execution timestamps")
    reason = run.get("reason") or ""
    require("seed" in reason.lower() and "integer" in reason.lower(), "Rejection reason does not identify the invalid seed type")
    validation = run.get("validation") or {}
    errors = validation.get("errors") or []
    require(validation.get("outcome") == "REJECTED" and isinstance(errors, list) and bool(errors),
            "Invalid configuration lacks structured rejection diagnostics")
    require(any(isinstance(error, dict) and "seed" in error.get("sample", "").lower()
                and "integer" in error.get("reason", "").lower() for error in errors),
            "Structured validation errors do not explain the seed type")
    events = detail.get("events") or []
    require(any(event.get("to") == "REJECTED" for event in events), "Rejection event is missing")
    require(not any((event.get("to") or event.get("state")) in {"SUBMITTING", "QUEUED", "RUNNING", "SUCCEEDED"}
                    for event in events), "Rejected configuration contains execution transitions")
    return {"id": run["id"], "state": run["state"], "commit": run["commit_sha"],
            "actor": run["actor_login"], "actor_id": run["actor_id"], "scheduler_jobs": 0,
            "artifacts": 0, "rejected_field": "seed", "source_sha256": provenance["source_sha256"]}


def verify(client, options):
    history = client.json("/api/runs")
    runs = history.get("runs")
    require(isinstance(runs, list), "Run history is missing")
    selected, states = {}, {}
    for label, sha in (("valid", options.valid_sha), ("invalid", options.invalid_sha)):
        matches = [run for run in runs if run.get("commit_sha") == sha]
        require(len(matches) <= 1, "An acceptance commit created duplicate runs")
        if not matches:
            states[label] = "NOT_RECEIVED"
            continue
        detail = client.json("/api/runs/" + quote(matches[0]["id"], safe=""))
        require(isinstance(detail.get("run"), dict), "Run detail is missing")
        selected[label] = detail
        states[label] = detail["run"].get("state")
    for label, expected in (("valid", "SUCCEEDED"), ("invalid", "REJECTED")):
        if states[label] in TERMINAL and states[label] != expected:
            raise VerificationError(label.capitalize() + " acceptance run reached an unexpected terminal state")
    checked_results = {}
    if states["invalid"] == "REJECTED":
        checked_results["invalid"] = verify_invalid(selected["invalid"], options)
    if any(states[label] != expected for label, expected in (("valid", "SUCCEEDED"), ("invalid", "REJECTED"))):
        raise Pending(states, checked_results)
    artifacts = selected["valid"]["run"].get("artifacts") or []
    if artifacts and any(item.get("cached") is not True for item in artifacts):
        raise Pending({**states, "artifact": "NOT_CACHED"}, checked_results)
    return {"status": "passed", "checked_at": datetime.now(timezone.utc).isoformat(),
            "valid": verify_valid(client, selected["valid"], options),
            "invalid": checked_results["invalid"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="Expected GitHub repository URL (no .git suffix)")
    parser.add_argument("--valid-branch", default="runs/1stp-smoke-check")
    parser.add_argument("--valid-repeat-id", default="acceptance-valid-20261001")
    parser.add_argument("--invalid-branch", default="runs/config-rejection-check")
    parser.add_argument("--valid-sha", default=VALID_SHA, help="Full valid-case commit; defaults to the current pilot")
    parser.add_argument("--invalid-sha", default=INVALID_SHA, help="Full rejected-case commit; defaults to the current pilot")
    parser.add_argument("--actor", default="azhang4216")
    parser.add_argument("--actor-id", type=int, default=64555467)
    parser.add_argument("--output", type=Path, help="Optionally save this same non-sensitive JSON summary")
    options = parser.parse_args()
    try:
        for value in (options.valid_sha, options.invalid_sha):
            require(re.fullmatch(r"[a-f0-9]{40}", value) is not None, "Acceptance commit must be a full Git SHA")
        options.repo_url = options.repo_url.rstrip("/")
        require(re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", options.repo_url)
                is not None and not options.repo_url.endswith(".git"), "Expected repository must be a GitHub repository URL")
        for branch in (options.valid_branch, options.invalid_branch):
            require(branch.startswith("runs/") and len(branch) > 5 and not any(ord(c) < 32 for c in branch),
                    "Acceptance branches must be named runs/experiment")
        require(options.valid_sha != options.invalid_sha and options.valid_branch != options.invalid_branch,
                "Acceptance cases require distinct commits and branches")
        summary = verify(Client(options.base_url), options)
        exit_code = 0
    except Pending as exc:
        summary, exit_code = {"status": "pending", "runs": exc.states}, 2
        if exc.checked_results:
            summary["checked_results"] = exc.checked_results
    except VerificationError as exc:
        summary, exit_code = {"status": "failed", "reason": str(exc)}, 1
    except (HTTPError, URLError, TimeoutError, OSError):
        summary, exit_code = {"status": "unavailable", "reason": "A cached HTTP GET or local output operation failed"}, 2
    except Exception as exc:
        summary, exit_code = {"status": "failed", "reason": "Unexpected response structure or missing runtime dependency", "error_type": type(exc).__name__}, 1
    text = json.dumps(summary, separators=(",", ":"), sort_keys=True)
    print(text)
    if options.output:
        try:
            options.output.write_text(text + "\n")
        except OSError:
            return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
