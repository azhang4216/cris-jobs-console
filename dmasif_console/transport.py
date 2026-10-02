"""Real SSH protocol and a persistent local simulator. Neither runs source locally."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Protocol

from cluster_adapter.common import AdapterError, canonical, claim, contained, input_manifest, job_id, read_json, run_id, safe_extract, sha256_file, utcnow, write_json
from cluster_adapter.helper import Helper
from cluster_adapter.validate import publish
from .runtime_secrets import private_key_copy


class TransportError(Exception):
    pass


class PreparationError(TransportError):
    pass


class SubmissionUncertain(TransportError):
    pass


class ClusterAdapter(Protocol):
    def stage(self, run: dict, source: dict) -> dict: ...
    def submit(self, run: dict) -> list[dict]: ...
    def reconcile(self, run: dict) -> list[dict]: ...
    def poll(self, run: dict, jobs: list[dict] | None = None) -> list[dict]: ...
    def logs(self, run: dict, jobs: list[dict] | None = None) -> str: ...
    def results(self, run: dict, jobs: list[dict] | None = None) -> dict | None: ...
    def fetch_artifact(self, run: dict, artifact: dict, destination: Path) -> None: ...
    def cancel(self, run: dict, jobs: list[dict] | None = None) -> None: ...
    def inventory(self) -> list[dict]: ...


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _plain(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _prepared_run(run: dict) -> dict:
    """Keep immutable identities, omit changing worker progress from staging ID."""
    keys = ("id", "commit_sha", "commit_url", "before_sha", "commit_author", "commit_committer", "repository", "repository_id", "repository_full_name", "ref", "branch", "experiment", "actor_id", "actor_login", "author", "committer", "forced", "created_at", "received_at", "hook_id", "delivery_id", "body_hash", "trigger", "config", "original_config", "resolved", "policy")
    result = {key: copy.deepcopy(run[key]) for key in keys if key in run}
    resolved = result["resolved"]
    cluster = resolved["cluster"]
    # Cluster credentials only exist on the worker. They cannot enter jobs.
    cluster.pop("ssh_key_path", None)
    cluster.pop("known_hosts_path", None)
    for name in ("gpus", "qos", "cpus", "memory_gb", "wall_minutes"):
        if name in resolved.get("preset", {}):
            cluster[name] = resolved["preset"][name]
    return result


class SSHAdapter:
    def __init__(self, settings):
        self.settings = settings
        self.cluster = settings.cluster

    def _cluster_for(self, run=None):
        # Connection identity and managed root are snapshotted with the accepted
        # request. Only key/known-host files come from current secret mounts.
        if run and run.get("resolved", {}).get("cluster"):
            return run["resolved"]["cluster"]
        return _plain(self.cluster)

    def _call(self, operation: str, payload: dict, *, run=None):
        cluster = self._cluster_for(run)
        host, user = cluster["host"], cluster["user"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", user):
            raise TransportError("Invalid SSH destination configuration")
        key = _get(self.cluster, "ssh_key_path")
        known_hosts = _get(self.cluster, "known_hosts_path")
        if not key or not known_hosts:
            raise TransportError("SSH key and pinned known_hosts files are required")
        helper, root = cluster["helper_path"], cluster["root"]
        # Execution stays pinned to the accepted release. Read-only monitoring
        # may use a newer operator-installed helper for that same cluster/root,
        # so accounting fixes can reconcile old jobs without rewriting their
        # immutable source, runtime, adapter identity, or submission request.
        read_operations = {"reconcile", "status", "logs", "result", "artifact"}
        if operation in read_operations and all(
            cluster.get(name) == _get(self.cluster, name) for name in ("host", "user", "root")
        ):
            helper = _get(self.cluster, "helper_path")
        for value in (helper, root):
            if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts:
                raise TransportError("Invalid managed cluster path")
        if operation not in {"stage", "submit", "reconcile", "status", "logs", "result", "artifact", "cancel", "inventory"}:
            raise TransportError("Unsupported helper operation")
        command = shlex.join(["python3", helper, "--root", root, operation])
        # -F /dev/null prevents a local user SSH config from changing the security
        # settings or proxying this fixed endpoint through an unexpected command.
        argv = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2", "-o", "UserKnownHostsFile=" + str(known_hosts), "-i", str(key), "--", user + "@" + host, command]
        timeout = int(cluster.get("ssh_timeout_seconds", 30))
        if operation == "stage":
            # Hashing the frozen image is deliberate and may exceed SSH connect
            # timeouts; the protocol invocation still has a finite bound.
            timeout = max(timeout, 300)
        try:
            with private_key_copy(key) as private_key:
                argv[argv.index("-i") + 1] = str(private_key)
                response = subprocess.run(argv, input=canonical(payload), capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            error_type = SubmissionUncertain if operation == "submit" else TransportError
            raise error_type("SSH operation did not return a verified response; operator reconciliation may be required") from exc
        except ValueError as exc:
            raise TransportError("SSH key file is invalid; inspect the protected service configuration") from exc
        if len(response.stdout) > 75 * 1024 * 1024:
            raise TransportError("Remote response exceeded the protocol size limit")
        try:
            body = json.loads(response.stdout)
        except (ValueError, UnicodeError) as exc:
            error_type = SubmissionUncertain if operation == "submit" else TransportError
            raise error_type("SSH operation returned no valid helper response") from exc
        if response.returncode or not body.get("ok"):
            # Remote errors can contain private paths. The dashboard receives a
            # category only; operators can inspect protected cluster evidence.
            error_type = SubmissionUncertain if operation == "submit" else PreparationError if operation == "stage" else TransportError
            raise error_type("Cluster " + operation + " failed; inspect protected operator evidence")
        return body["result"]

    def stage(self, run: dict, source: dict) -> dict:
        archive = Path(source["archive_path"])
        if archive.is_symlink() or archive.stat().st_size > self.settings.max_source_bytes or sha256_file(archive) != source["sha256"]:
            raise PreparationError("Source archive failed size or integrity checks")
        prepared = _prepared_run(run)
        try:
            input_manifest(prepared["resolved"]["dataset"]["entries"])
        except (AdapterError, KeyError) as exc:
            raise PreparationError(str(exc)) from exc
        payload = {"run": prepared, "source": {key: source[key] for key in ("sha256", "commit_sha")}, "archive_b64": base64.b64encode(archive.read_bytes()).decode(), "limits": {name: getattr(self.settings, name) for name in ("max_source_bytes", "max_input_bytes", "min_free_bytes")}}
        return self._call("stage", payload, run=run)

    def submit(self, run: dict) -> list[dict]:
        return self._call("submit", {"run_id": run_id(run["id"])}, run=run)

    def reconcile(self, run: dict) -> list[dict]:
        return self._call("reconcile", {"run_id": run_id(run["id"])}, run=run)

    def _jobs(self, run, jobs):
        return jobs if jobs is not None else run.get("jobs", [])

    def poll(self, run: dict, jobs=None) -> list[dict]:
        jobs = self._jobs(run, jobs)
        return self._call("status", {"run_id": run_id(run["id"]), "jobs": jobs}, run=run) if jobs else self.reconcile(run)

    def logs(self, run: dict, jobs=None) -> str:
        jobs = self._jobs(run, jobs) or self.reconcile(run)
        return self._call("logs", {"run_id": run_id(run["id"]), "jobs": jobs, "limit": self.settings.log_tail_bytes}, run=run)

    def results(self, run: dict, jobs=None):
        jobs = self._jobs(run, jobs) or self.reconcile(run)
        return self._call("result", {"run_id": run_id(run["id"]), "jobs": jobs}, run=run)

    def fetch_artifact(self, run: dict, artifact: dict, destination: Path) -> None:
        response = self._call("artifact", {"run_id": run_id(run["id"]), "artifact": artifact, "limit": self.settings.max_artifact_bytes}, run=run)
        data = base64.b64decode(response["data"], validate=True)
        if len(data) != artifact["size"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise TransportError("Downloaded artifact failed integrity checks")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as output:
            output.write(data)

    def cancel(self, run: dict, jobs=None) -> None:
        jobs = self._jobs(run, jobs) or self.reconcile(run)
        if not jobs:
            raise TransportError("No verified scheduler jobs are available to cancel")
        self._call("cancel", {"run_id": run_id(run["id"]), "jobs": jobs}, run=run)

    def inventory(self) -> list[dict]:
        evidence = self._call("inventory", {})
        result = []
        for item in evidence:
            run = item["request"]["run"]
            result.append({**run, "source": item["request"]["source"], "remote_dir": item["remote_dir"], "jobs": [item["receipt"]] if item["receipt"] else [], "remote_evidence": item, "delivery": run.get("trigger", {})})
        return result


class FakeAdapter:
    """Persistent, deterministic simulation; synthetic features, never research code.

    A fresh instance recovers prior submissions from disk, which exercises worker
    restarts without an SSH connection, GPU, PyTorch, or trusted source execution.
    """
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings.state_dir) / "fake-cluster"
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, run: dict) -> Path:
        return self.root / "runs" / run_id(run["id"])

    def stage(self, run: dict, source: dict) -> dict:
        prepared = _prepared_run(run)
        directory = self.directory(run)
        try:
            entries = input_manifest(prepared["resolved"]["dataset"]["entries"])
            archive = Path(source["archive_path"])
            if archive.stat().st_size > self.settings.max_source_bytes or sha256_file(archive) != source["sha256"]:
                raise AdapterError("Source archive failed integrity checks")
            identity = {"run": prepared, "source": {key: source[key] for key in ("sha256", "commit_sha")}, "inputs": entries}
            identity_hash = hashlib.sha256(canonical(identity)).hexdigest()
            directory.parent.mkdir(parents=True, exist_ok=True)
            try:
                directory.mkdir()
            except FileExistsError:
                if read_json(directory / "request.json")["identity_sha256"] != identity_hash:
                    raise AdapterError("Existing run identity differs")
                if (directory / "stage.json").exists():
                    for entry in entries:
                        if sha256_file(contained(directory / "inputs", entry["name"])) != entry["sha256"]:
                            raise AdapterError("Staged input changed")
                    return read_json(directory / "stage.json")
            else:
                write_json(directory / "request.json", {**identity, "identity_sha256": identity_hash, "created_at": utcnow()}, exclusive=True)
            source_target = directory / "source"
            if not source_target.exists():
                safe_extract(archive, source_target, self.settings.max_source_bytes)
            (directory / "inputs").mkdir(exist_ok=True)
            total = 0
            for entry in entries:
                original = Path(entry["original_path"])
                if original.is_symlink() or not original.is_file():
                    raise AdapterError("Dataset input is unavailable")
                total += original.stat().st_size
                if total > self.settings.max_input_bytes:
                    raise AdapterError("Dataset exceeds input limit")
                if sha256_file(original) != entry["sha256"]:
                    raise AdapterError("Dataset checksum mismatch")
                target = directory / "inputs" / entry["name"]
                if not target.exists():
                    with original.open("rb") as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                if sha256_file(target) != entry["sha256"]:
                    raise AdapterError("Copied input changed")
            write_json(directory / "inputs.json", entries)
            (directory / "logs").mkdir(exist_ok=True)
            (directory / "jobs").mkdir(exist_ok=True)
            provenance = {"simulation": True, "source_sha256": source["sha256"], "commit_sha": source["commit_sha"], "inputs": entries, "runtime": "local synthetic result generator; dMaSIF was not executed"}
            stage = {"remote_dir": str(directory), "provenance": provenance}
            write_json(directory / "stage.json", stage, exclusive=True)
            return stage
        except (AdapterError, OSError, KeyError, ValueError) as exc:
            raise PreparationError(str(exc)) from exc

    def submit(self, run: dict) -> list[dict]:
        directory = self.directory(run)
        receipt = directory / "submission-receipt.json"
        if receipt.exists():
            return [read_json(receipt)]
        if not (directory / "stage.json").exists():
            raise PreparationError("Run has not been staged")
        intent = directory / "submission-intent.json"
        if not intent.exists():
            write_json(intent, {"run_id": run["id"], "created_at": utcnow()}, exclusive=True)
        try:
            claim(directory / "submit.claim")
        except FileExistsError as exc:
            raise SubmissionUncertain("Submission claim exists without a receipt; it will not be retried") from exc
        ident = str(int(hashlib.sha256(run["id"].encode()).hexdigest()[:15], 16))
        job = {"job_id": ident, "cluster": "simulation", "state": "PENDING", "exit_code": None, "submitted_at": utcnow(), "started_at": None, "ended_at": None, "observed_at": utcnow(), "terminal": False, "owner": "simulated-account", "polls": 0}
        write_json(receipt, job, exclusive=True)
        (directory / "logs" / f"slurm-{ident}.out").write_text("LOCAL SIMULATION: synthetic results; research code is never executed.\nWaiting for simulated GPU allocation.\n")
        return [job]

    def reconcile(self, run: dict) -> list[dict]:
        receipt = self.directory(run) / "submission-receipt.json"
        return [read_json(receipt)] if receipt.exists() else []

    def poll(self, run: dict, jobs=None) -> list[dict]:
        directory = self.directory(run)
        receipts = self.reconcile(run)
        if not receipts:
            return []
        job = receipts[0]
        if job["terminal"]:
            return [job]
        job["polls"] += 1
        job["observed_at"] = utcnow()
        if job["polls"] == 1:
            try:
                claim(directory / "execution.claim")
            except FileExistsError as exc:
                raise TransportError("Duplicate simulated execution claim") from exc
            write_json(directory / "execution.claim" / "owner.json", {"job_id": job["job_id"]}, exclusive=True)
            job.update(state="RUNNING", started_at=utcnow())
            with (directory / "logs" / f"slurm-{job['job_id']}.out").open("a") as stream:
                stream.write("Generating small synthetic features for protocol validation.\n")
        elif job["polls"] >= 2:
            self._produce(run, job)
            job.update(state="COMPLETED", exit_code="0:0", terminal=True, ended_at=utcnow())
        write_json(directory / "submission-receipt.json", job)
        return [job]

    def _produce(self, run, job):
        import numpy as np

        directory = self.directory(run)
        job_dir = directory / "jobs" / job_id(job["job_id"])
        # A crash after publication but before receipt update must not replace it.
        if (job_dir / "result-manifest.json").exists():
            return
        raw = job_dir / "raw_features"
        raw.mkdir(parents=True, exist_ok=True)
        entries = read_json(directory / "inputs.json")
        for entry in entries:
            target = raw / entry["expected_output"]
            if target.exists():
                continue
            seed = int(hashlib.sha256((entry["sha256"] + str(run["config"]["seed"])).encode()).hexdigest()[:8], 16)
            rng = np.random.default_rng(seed)
            n, m = 8, 4
            np.savez_compressed(target, xyz=rng.random((n, 3), dtype=np.float32), normals=np.tile(np.array([[0, 0, 1]], dtype=np.float32), (n, 1)), input_feats=rng.random((n, 16), dtype=np.float32), emb1=rng.random((n, 16), dtype=np.float32), emb2=rng.random((n, 16), dtype=np.float32), nearest_atom=np.arange(n, dtype=np.int64) % m, atom_xyz=rng.random((m, 3), dtype=np.float32), atom_type=np.arange(m, dtype=np.int64), atom_chain=np.array(["A"] * m), atom_resnum=np.arange(m, dtype=np.int64), atom_icode=np.array([""] * m), atom_resname=np.array(["ALA"] * m), atom_name=np.array(["CA"] * m))
        provenance = read_json(directory / "stage.json")["provenance"]
        publish(raw, job_dir / "published", [entry["expected_output"] for entry in entries], provenance, job_dir / "result-manifest.json")
        with (directory / "logs" / f"slurm-{job['job_id']}.out").open("a") as stream:
            stream.write("Simulation complete. All synthetic NPZ files passed the deployed validator.\n")

    def logs(self, run: dict, jobs=None) -> str:
        jobs = jobs if jobs is not None else self.reconcile(run)
        return Helper(self.root.resolve()).logs({"run_id": run["id"], "jobs": jobs, "limit": self.settings.log_tail_bytes})

    def results(self, run: dict, jobs=None):
        jobs = jobs if jobs is not None else self.reconcile(run)
        return Helper(self.root.resolve()).result({"run_id": run["id"], "jobs": jobs})

    def fetch_artifact(self, run: dict, artifact: dict, destination: Path) -> None:
        try:
            response = Helper(self.root.resolve()).artifact({"run_id": run["id"], "artifact": artifact, "limit": self.settings.max_artifact_bytes})
            data = base64.b64decode(response["data"], validate=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(data)
        except (AdapterError, OSError, ValueError) as exc:
            raise TransportError(str(exc)) from exc

    def cancel(self, run: dict, jobs=None) -> None:
        current = self.reconcile(run)
        for job in current:
            if not job["terminal"]:
                job.update(state="CANCELLED", terminal=True, exit_code="0:15", ended_at=utcnow(), observed_at=utcnow())
                write_json(self.directory(run) / "submission-receipt.json", job)

    def inventory(self) -> list[dict]:
        result = []
        for item in Helper(self.root.resolve()).inventory({}):
            run = item["request"]["run"]
            result.append({**run, "source": item["request"]["source"], "remote_dir": item["remote_dir"], "jobs": [item["receipt"]] if item["receipt"] else [], "remote_evidence": item, "delivery": run.get("trigger", {})})
        return result


def get_adapter(settings) -> ClusterAdapter:
    return FakeAdapter(settings) if settings.mode == "fake" else SSHAdapter(settings)
