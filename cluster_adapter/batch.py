#!/usr/bin/env python3
"""Runs once per run UUID, on the allocated GPU node; stdlib only."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

try:
    from .common import AdapterError, claim, contained, job_id, read_json, sha256_file, utcnow, verify_tree, write_json
except ImportError:
    from common import AdapterError, claim, contained, job_id, read_json, sha256_file, utcnow, verify_tree, write_json


@contextmanager
def _runtime_scratch(job: Path, runtime: Path, unsquash: bool):
    if not unsquash:
        yield job / "tmp"
        return
    # The operator verifies that the scheduler's scratch root (or /tmp) is on
    # node-local disk. Avoid inherited TMPDIR, which often points to shared FS.
    root = Path(os.environ.get("SLURM_TMPDIR") or "/tmp")
    if not root.is_absolute() or ".." in root.parts or root.is_symlink() or not root.is_dir():
        raise AdapterError("Node-local runtime scratch must be an existing absolute directory, not a symlink")
    # Compression ratios vary. This is a conservative admission floor, not an
    # exact prediction of the image's expanded size or a disk-space reservation.
    required = runtime.stat().st_size * 4 + 1024**3
    if shutil.disk_usage(root).free < required:
        raise AdapterError("Node-local runtime scratch needs at least four times the SIF size plus 1 GiB free")
    prefix = f"dmasif-{job.parent.parent.name}-{job.name}-"
    with tempfile.TemporaryDirectory(prefix=prefix, dir=root) as temporary:
        yield Path(temporary)


def main() -> int:
    directory, ident = Path(sys.argv[1]).resolve(), job_id(sys.argv[2])
    try:
        claim(directory / "execution.claim")
    except FileExistsError:
        print("This run already has an execution claim; refusing duplicate execution.", file=sys.stderr)
        return 73
    write_json(directory / "execution.claim" / "owner.json", {"job_id": ident, "claimed_at": utcnow()}, exclusive=True)
    stage = read_json(directory / "stage.json")
    job = directory / "jobs" / ident
    job.mkdir(exist_ok=False)
    for name in ("raw_features", "tmp", "keops_cache", "apptainer_cache", "cache", "torch_cache", "torch_extensions", "cuda_cache"):
        (job / name).mkdir()
    (job / "tmp" / "home").mkdir()
    source = Path(stage["source_dir"])
    adapter = Path(stage["adapter_dir"])
    verify_tree(source, read_json(source.parent / "manifest.json")["files"])
    for name, checksum in read_json(adapter / "manifest.json").items():
        if sha256_file(contained(adapter, name)) != checksum:
            raise AdapterError("Pinned adapter changed before execution")
    for entry in read_json(directory / "inputs.json"):
        if sha256_file(contained(directory / "inputs", entry["name"])) != entry["sha256"]:
            raise AdapterError("Pinned input changed before execution")
    for path_key, hash_key in (("runtime_image", "runtime_sha256"), ("checkpoint_path", "checkpoint_sha256")):
        if sha256_file(Path(stage[path_key])) != stage["provenance"][hash_key]:
            raise AdapterError("Pinned runtime/checkpoint changed before execution")
    unsquash = stage.get("runtime_unsquash", False)
    if type(unsquash) is not bool:
        raise AdapterError("runtime_unsquash must be an operator-configured boolean")
    with _runtime_scratch(job, Path(stage["runtime_image"]), unsquash) as scratch:
        return _execute(directory, ident, stage, unsquash, scratch)


def _execute(directory: Path, ident: str, stage: dict, unsquash: bool, scratch: Path) -> int:
    job = directory / "jobs" / ident
    source, adapter = Path(stage["source_dir"]), Path(stage["adapter_dir"])
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH" and not key.startswith(("APPTAINERENV_", "SINGULARITYENV_"))}
    # These paths belong to this allocation, including temporary SIF extraction
    # for sites without squashfuse. Never inherit a shared host scratch/cache.
    environment.update(TMPDIR=str(scratch), APPTAINER_TMPDIR=str(scratch),
                       SINGULARITY_TMPDIR=str(scratch),
                       APPTAINER_CACHEDIR=str(job / "apptainer_cache"),
                       SINGULARITY_CACHEDIR=str(job / "apptainer_cache"))
    version = subprocess.run(["apptainer", "--version"], capture_output=True, text=True, timeout=15, check=True, env=environment).stdout.strip()
    write_json(job / "execution.json", {"job_id": ident, "started_at": utcnow(), "apptainer_version": version, "runtime_unsquash": unsquash, "provenance": stage["provenance"]}, exclusive=True)
    # The run remains writable. Scientific source and deployed adapter are bound
    # read-only; this guards mistakes, not hostile code sharing the Unix account.
    command = [
        "apptainer", "exec", *(["--unsquash"] if unsquash else []), "--nv", "--cleanenv", "--containall", "--no-home", "--no-mount", "cwd,hostfs",
        "--bind", f"{source}:/source:ro", "--bind", f"{adapter}:/adapter:ro",
        "--bind", f"{directory}:/run:rw", "--bind", f"{stage['checkpoint_path']}:/checkpoint/model.pt:ro",
        "--pwd", "/source", "--env", "PYTHONNOUSERSITE=1", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTHONUNBUFFERED=1", "--env", "PYTHONPATH=/source:/source/affinity",
        "--env", f"HOME=/run/jobs/{ident}/tmp/home", "--env", f"TMPDIR=/run/jobs/{ident}/tmp",
        "--env", f"KEOPS_CACHE_FOLDER=/run/jobs/{ident}/keops_cache",
        "--env", f"XDG_CACHE_HOME=/run/jobs/{ident}/cache", "--env", f"TORCH_HOME=/run/jobs/{ident}/torch_cache",
        "--env", f"TORCH_EXTENSIONS_DIR=/run/jobs/{ident}/torch_extensions", "--env", f"CUDA_CACHE_PATH=/run/jobs/{ident}/cuda_cache",
        stage["runtime_image"], "python", "-s", "/adapter/execute.py", "/run", ident,
    ]
    return subprocess.run(command, env=environment, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Execution preparation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
