#!/usr/bin/env python3
"""Installed, fixed SSH endpoint. Receives one JSON request on stdin.

No service runs persistently on the login node. Never install or invoke this
against a real cluster as part of the local demonstration.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

try:
    from .common import AdapterError, canonical, claim, contained, digest, input_manifest, job_id, read_json, run_id, safe_extract, sha256_file, utcnow, verify_tree, write_json
except ImportError:
    from common import AdapterError, canonical, claim, contained, digest, input_manifest, job_id, read_json, run_id, safe_extract, sha256_file, utcnow, verify_tree, write_json

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED"}
MODULE_FILES = ("__init__.py", "common.py", "helper.py", "batch.py", "execute.py", "validate.py")


def absolute(value: str) -> Path:
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts:
        raise AdapterError("Cluster paths must be absolute, without spaces or shell characters")
    return Path(value)


def scheduler_token(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(value)):
        raise AdapterError("Invalid scheduler setting")
    return str(value)


def state_name(value: str) -> str:
    return value.split(" ", 1)[0].rstrip("+")


def timestamp(value: str):
    if not value or value in {"Unknown", "None", "N/A"}:
        return None
    # Slurm uses local timestamps; every command is launched with TZ=UTC.
    return value if value.endswith(("Z", "+00:00")) else value + "+00:00"


class Helper:
    def __init__(self, root: Path):
        self.root = absolute(str(root))

    def directory(self, value: str) -> Path:
        path = self.root / "runs" / run_id(value)
        if path.is_symlink() or self.root.resolve() not in path.resolve().parents:
            raise AdapterError("Unsafe run directory")
        return path

    def command(self, argv: list[str], timeout: int = 45) -> str:
        environment = dict(os.environ, TZ="UTC", LC_ALL="C")
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env=environment)
        if result.returncode:
            raise AdapterError(f"{Path(argv[0]).name} failed with exit {result.returncode}: {result.stderr[-1000:]}")
        return result.stdout

    def stage(self, payload: dict) -> dict:
        run, source = payload["run"], payload["source"]
        directory = self.directory(run["id"])
        cluster = run["resolved"]["cluster"]
        unsquash = cluster.get("runtime_unsquash", False)
        if type(unsquash) is not bool:
            raise AdapterError("runtime_unsquash must be an operator-configured boolean")
        source_hash = digest(source["sha256"])
        limits = payload.get("limits", {})
        source_limit = int(limits.get("max_source_bytes", 50 * 1024 * 1024))
        input_limit = int(limits.get("max_input_bytes", 50 * 1024 * 1024))
        self.root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.root).free < int(limits.get("min_free_bytes", 1024 * 1024 * 1024)):
            raise AdapterError("Cluster storage is below the configured free-space threshold")
        manifest = input_manifest(run["resolved"]["dataset"]["entries"])
        identity = {"run": run, "source": {"sha256": source_hash, "commit_sha": source["commit_sha"]}, "inputs": manifest}
        identity_hash = hashlib.sha256(canonical(identity)).hexdigest()
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            if not (directory / "request.json").is_file():
                raise AdapterError("Existing run directory has no immutable identity; operator review required")
            if read_json(directory / "request.json").get("identity_sha256") != identity_hash:
                raise AdapterError("Existing run directory has a different identity")
            if (directory / "stage.json").exists():
                stage = read_json(directory / "stage.json")
                self.verify_stage(directory, stage)
                return stage
            if (directory / "submit.claim").exists():
                raise AdapterError("Submitted run cannot be restaged")
        else:
            write_json(directory / "request.json", {**identity, "identity_sha256": identity_hash, "created_at": utcnow()}, exclusive=True)
        # A staging crash can be resumed only under the identity recorded above.
        source_root = self.root / "releases" / "sources" / source_hash
        if not source_root.exists():
            source_root.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".source-", dir=source_root.parent))
            try:
                data = base64.b64decode(payload["archive_b64"], validate=True)
                if len(data) > source_limit or hashlib.sha256(data).hexdigest() != source_hash:
                    raise AdapterError("Source archive size or checksum mismatch")
                archive = temporary / "source.tar.gz"
                archive.write_bytes(data)
                files = safe_extract(archive, temporary / "tree", source_limit)
                write_json(temporary / "manifest.json", {"archive_sha256": source_hash, "files": files}, exclusive=True)
                try:
                    temporary.rename(source_root)
                except FileExistsError:
                    pass
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        source_manifest = read_json(source_root / "manifest.json")
        if source_manifest["archive_sha256"] != source_hash or sha256_file(source_root / "source.tar.gz") != source_hash:
            raise AdapterError("Saved source archive checksum mismatch")
        verify_tree(source_root / "tree", source_manifest["files"])
        runtime = absolute(cluster["runtime_image"])
        checkpoint = absolute(cluster["checkpoint_path"])
        for path, checksum in ((runtime, cluster["runtime_sha256"]), (checkpoint, cluster["checkpoint_sha256"])):
            if not path.is_file() or path.is_symlink() or sha256_file(path) != digest(checksum):
                raise AdapterError("Runtime or checkpoint checksum mismatch")
        (directory / "inputs").mkdir(exist_ok=True)
        total = 0
        for entry in manifest:
            original = absolute(entry["original_path"])
            if not original.is_file() or original.is_symlink():
                raise AdapterError("Dataset input is missing or is not a regular file")
            total += original.stat().st_size
            if total > input_limit:
                raise AdapterError("Dataset exceeds configured input limit")
            target = directory / "inputs" / entry["name"]
            if not target.exists():
                descriptor, name = tempfile.mkstemp(prefix=".input-", dir=target.parent)
                try:
                    with original.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if sha256_file(Path(name)) != entry["sha256"]:
                        raise AdapterError("Dataset checksum mismatch")
                    os.link(name, target)
                finally:
                    Path(name).unlink(missing_ok=True)
            if target.is_symlink() or sha256_file(target) != entry["sha256"]:
                raise AdapterError("Copied input checksum mismatch")
        write_json(directory / "inputs.json", manifest)
        lines = []
        for entry in manifest:
            # Comma notation preserves multi-character labels; one single label
            # must be followed by a comma to avoid upstream character splitting.
            chain_spec = ",".join(entry["chains"]) + ("," if len(entry["chains"]) == 1 else "")
            lines.append("/run/inputs/" + entry["name"] + (" " + chain_spec if chain_spec else ""))
        (directory / "input-list.txt").write_text("\n".join(lines) + "\n")
        adapter_source = Path(__file__).resolve().parent
        adapter_files = {name: sha256_file(adapter_source / name) for name in MODULE_FILES}
        adapter_hash = hashlib.sha256(canonical(adapter_files)).hexdigest()
        adapter = self.root / "releases" / "adapters" / adapter_hash
        if not adapter.exists():
            adapter.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".adapter-", dir=adapter.parent))
            try:
                for name in MODULE_FILES:
                    shutil.copyfile(adapter_source / name, temporary / name)
                write_json(temporary / "manifest.json", adapter_files, exclusive=True)
                temporary.rename(adapter)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        for name, checksum in adapter_files.items():
            if sha256_file(adapter / name) != checksum:
                raise AdapterError("Pinned adapter changed")
        provenance = {"source_sha256": source_hash, "commit_sha": source["commit_sha"], "runtime_sha256": cluster["runtime_sha256"], "checkpoint_sha256": cluster["checkpoint_sha256"], "adapter_sha256": adapter_hash, "adapter_version": cluster.get("adapter_version", "1.0.0"), "inputs": manifest}
        stage = {"remote_dir": str(directory), "source_dir": str(source_root / "tree"), "adapter_dir": str(adapter), "runtime_image": str(runtime), "runtime_unsquash": unsquash, "checkpoint_path": str(checkpoint), "provenance": provenance}
        (directory / "logs").mkdir(exist_ok=True)
        (directory / "jobs").mkdir(exist_ok=True)
        (directory / "submit.sbatch").write_text(self.batch_script(directory, adapter, cluster))
        write_json(directory / "stage.json", stage, exclusive=True)
        return stage

    def verify_stage(self, directory: Path, stage: dict) -> None:
        source_root = Path(stage["source_dir"]).parent
        source = read_json(source_root / "manifest.json")
        verify_tree(source_root / "tree", source["files"])
        for entry in read_json(directory / "inputs.json"):
            if sha256_file(contained(directory / "inputs", entry["name"])) != entry["sha256"]:
                raise AdapterError("Staged dataset changed")

    def batch_script(self, directory: Path, adapter: Path, cluster: dict) -> str:
        # Only operator-controlled preset values can reach this template.
        cpus, memory, minutes = int(cluster.get("cpus", 8)), int(cluster.get("memory_gb", 64)), int(cluster.get("wall_minutes", 15))
        if not (1 <= cpus <= 8 and 1 <= memory <= 64 and 1 <= minutes <= 15 and cluster.get("qos", "test") == "test"):
            raise AdapterError("Resources exceed the tested quick-test limits")
        gpu_type = cluster.get("gpu_type")
        if gpu_type is not None and (
            type(gpu_type) is not str or not 1 <= len(gpu_type) <= 100
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", gpu_type) is None
        ):
            raise AdapterError("GPU type must be an operator-configured scheduler token")
        gpu_request = "gpu:1" if gpu_type is None else f"gpu:{gpu_type}:1"
        return "\n".join([
            "#!/bin/bash", f"#SBATCH --account={scheduler_token(cluster['account'])}",
            f"#SBATCH --partition={scheduler_token(cluster['partition'])}", "#SBATCH --qos=test", f"#SBATCH --gres={gpu_request}",
            f"#SBATCH --cpus-per-task={cpus}", f"#SBATCH --mem={memory}G", f"#SBATCH --time=00:{minutes:02d}:00",
            f"#SBATCH --output={directory}/logs/slurm-%j.out", f"#SBATCH --error={directory}/logs/slurm-%j.err",
            "#SBATCH --open-mode=append", "#SBATCH --no-requeue", "set -euo pipefail", "umask 077", "unset PYTHONPATH PYTHONHOME", "export PYTHONNOUSERSITE=1", "module load apptainer",
            "exec python3 " + shlex.quote(str(adapter / "batch.py")) + " " + shlex.quote(str(directory)) + ' "$SLURM_JOB_ID"', "",
        ])

    def submit(self, payload: dict) -> list[dict]:
        directory = self.directory(payload["run_id"])
        stage = read_json(directory / "stage.json")
        self.verify_stage(directory, stage)
        receipt_path = directory / "submission-receipt.json"
        if receipt_path.exists():
            return [read_json(receipt_path)]
        intent = directory / "submission-intent.json"
        tag = "dm-" + directory.name
        if not intent.exists():
            write_json(intent, {"run_id": directory.name, "tag": tag, "created_at": utcnow(), "owner": getpass.getuser(), "stage_sha256": sha256_file(directory / "stage.json")}, exclusive=True)
        try:
            claim(directory / "submit.claim")
        except FileExistsError:
            matches = self.reconcile(payload)
            if matches:
                return matches
            raise AdapterError("SUBMISSION_UNCERTAIN: Persistent submission claim exists without resolved scheduler evidence")
        # No code path may remove this claim or retry this potentially executed call.
        try:
            output = self.command(["sbatch", "--parsable", "--job-name=" + tag, "--comment=" + tag, "--no-requeue", "--open-mode=append", str(directory / "submit.sbatch")])
            response = output.strip().splitlines()[-1]
            match = re.fullmatch(r"([0-9]+)(?:;([A-Za-z0-9_-]+))?", response)
            if not match:
                raise AdapterError("Invalid sbatch response")
            receipt = {"job_id": job_id(match.group(1)), "cluster": "cluster", "reported_cluster": match.group(2), "state": "PENDING", "exit_code": None, "submitted_at": utcnow(), "started_at": None, "ended_at": None, "observed_at": utcnow(), "terminal": False, "owner": getpass.getuser(), "tag": tag}
            write_json(receipt_path, receipt, exclusive=True)
            return [receipt]
        except Exception as exc:
            raise AdapterError("SUBMISSION_UNCERTAIN: Scheduler call or receipt publication failed; reconcile before proceeding") from exc

    def _query(self, directory: Path, ids: list[str] | None = None) -> list[dict]:
        intent = read_json(directory / "submission-intent.json")
        owner, tag = intent["owner"], intent["tag"]
        start = intent["created_at"][:19]
        accounting = ["sacct", "--local", "--noheader", "--parsable2", "--allocations", "--user=" + owner, "--starttime=" + start, "--format=JobIDRaw,State%64,ExitCode,Submit,Start,End,JobName%100,User%100,Comment%100"]
        queue = ["squeue", "--local", "--noheader", "--user=" + owner, "--format=%i|%T|%V|%S|%j|%u|%k|%r"]
        requested = None
        if ids:
            valid_ids = [job_id(value) for value in ids]
            requested = set(valid_ids)
            joined = ",".join(valid_ids)
            accounting.append("--jobs=" + joined)
        # Finished jobs leave squeue before their accounting record expires.
        # squeue --jobs then fails with "Invalid job id specified", even when
        # sacct conclusively reports completion. Query this owner's live queue
        # and filter IDs below; genuine command failures still propagate.
        observed, found = utcnow(), {}
        # Accounting errors are deliberately not treated as an empty completed queue.
        for line in self.command(accounting).splitlines():
            values = [item.strip() for item in line.split("|")]
            if len(values) < 9:
                continue
            ident, state, exit_code, submitted, started, ended, name, user, comment = values[:9]
            # Sites without AccountingStoreFlags=job_comment omit the comment
            # from accounting even though squeue exposes it while the job is
            # live. The owner, complete UUID name, and known ID still identify
            # our allocation; a nonempty conflicting comment remains invalid.
            if not ident.isdigit() or name != tag or user != owner or comment not in {"", tag} or (requested is not None and ident not in requested):
                continue
            state = state_name(state)
            found[ident] = {"job_id": ident, "cluster": "cluster", "state": state, "exit_code": exit_code, "submitted_at": timestamp(submitted), "started_at": timestamp(started), "ended_at": timestamp(ended), "observed_at": observed, "terminal": state in TERMINAL, "owner": owner, "tag": tag}
        for line in self.command(queue).splitlines():
            values = [item.strip() for item in line.split("|")]
            if len(values) < 8:
                continue
            ident, state, submitted, started, name, user, comment, reason = values[:8]
            if not ident.isdigit() or name != tag or user != owner or comment != tag or (requested is not None and ident not in requested):
                continue
            # A live observation supersedes delayed terminal accounting.
            found[ident] = {"job_id": ident, "cluster": "cluster", "state": state_name(state), "exit_code": None, "submitted_at": timestamp(submitted), "started_at": timestamp(started) if state_name(state) == "RUNNING" else None, "ended_at": None, "observed_at": observed, "terminal": False, "owner": owner, "tag": tag, "reason": reason}
        return list(found.values())

    def reconcile(self, payload: dict) -> list[dict]:
        directory = self.directory(payload["run_id"])
        if not (directory / "submission-intent.json").exists():
            return []
        jobs = {job["job_id"]: job for job in self._query(directory)}
        receipt_path = directory / "submission-receipt.json"
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            jobs.setdefault(receipt["job_id"], receipt)
        return list(jobs.values())

    def status(self, payload: dict) -> list[dict]:
        directory = self.directory(payload["run_id"])
        return self._query(directory, [job_id(job["job_id"]) for job in payload["jobs"]])

    def logs(self, payload: dict) -> str:
        directory = self.directory(payload["run_id"])
        cap = min(int(payload.get("limit", 65536)), 256 * 1024)
        pieces = []
        for job in payload["jobs"][:8]:
            ident = job_id(job["job_id"])
            for suffix in ("out", "err"):
                path = directory / "logs" / f"slurm-{ident}.{suffix}"
                if path.exists():
                    if path.is_symlink():
                        raise AdapterError("Unsafe log file")
                    with path.open("rb") as stream:
                        stream.seek(max(0, path.stat().st_size - cap))
                        pieces.append(f"--- Job {ident} {suffix} ---\n" + stream.read(cap).decode("utf-8", errors="replace"))
        return "\n".join(pieces)[-cap:]

    def result(self, payload: dict):
        directory = self.directory(payload["run_id"])
        manifests = []
        for job in payload["jobs"]:
            path = directory / "jobs" / job_id(job["job_id"]) / "result-manifest.json"
            if path.is_file() and not path.is_symlink():
                result = read_json(path)
                result["job_id"] = job["job_id"]
                manifests.append(result)
        if len(manifests) > 1:
            raise AdapterError("Multiple execution manifests require operator review")
        return manifests[0] if manifests else None

    def artifact(self, payload: dict) -> dict:
        directory = self.directory(payload["run_id"])
        artifact = payload["artifact"]
        relative = artifact.get("relative_path", artifact.get("path", ""))
        path = contained(directory, relative)
        parts = Path(relative).parts
        if len(parts) != 4 or parts[0] != "jobs" or parts[2] != "published":
            raise AdapterError("Only published result artifacts can be fetched")
        manifest = read_json(directory / "jobs" / job_id(parts[1]) / "result-manifest.json")
        matches = [item for item in manifest["artifacts"] if item["relative_path"] == relative and item["sha256"] == artifact["sha256"]]
        if len(matches) != 1 or path.stat().st_size != matches[0]["size"] or sha256_file(path) != matches[0]["sha256"]:
            raise AdapterError("Artifact is absent from committed manifest or changed")
        limit = min(int(payload.get("limit", 10 * 1024 * 1024)), 50 * 1024 * 1024)
        if path.stat().st_size > limit:
            raise AdapterError("Artifact exceeds web download limit")
        return {"data": base64.b64encode(path.read_bytes()).decode(), "sha256": matches[0]["sha256"]}

    def cancel(self, payload: dict) -> None:
        known = {item["job_id"]: item for item in self.status(payload)}
        ids = [job_id(item["job_id"]) for item in payload["jobs"]]
        if any(value not in known for value in ids):
            raise AdapterError("Cannot verify ownership and run mapping for cancellation")
        live = [value for value in ids if not known[value]["terminal"]]
        if live:
            self.command(["scancel", *live])

    def inventory(self, payload: dict) -> list[dict]:
        result = []
        parent = self.root / "runs"
        if not parent.exists():
            return result
        for directory in sorted(parent.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            run_id(directory.name)
            request = read_json(directory / "request.json")
            result.append({"run_id": directory.name, "request": request, "receipt": read_json(directory / "submission-receipt.json") if (directory / "submission-receipt.json").exists() else None, "has_submit_claim": (directory / "submit.claim").exists(), "has_execution_claim": (directory / "execution.claim").exists(), "remote_dir": str(directory)})
        return result


def main() -> None:
    # New application-managed files belong only to this cluster Unix identity.
    # Existing research files are neither chmodded nor modified.
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("operation", choices=("stage", "submit", "reconcile", "status", "logs", "result", "artifact", "cancel", "inventory"))
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(75 * 1024 * 1024 + 1)
        if len(raw) > 75 * 1024 * 1024:
            raise AdapterError("Request exceeds the protocol limit")
        payload = json.loads(raw)
        result = getattr(Helper(Path(args.root)), args.operation)(payload)
        print(json.dumps({"ok": True, "result": result}))
    except Exception as exc:
        # No request body, key, tokens, environment, or traceback goes to clients.
        print(json.dumps({"ok": False, "error": str(exc)[:1000]}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
