#!/usr/bin/env python3
"""Runs once per run UUID, on the allocated GPU node; stdlib only."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

try:
    from .common import AdapterError, claim, contained, job_id, read_json, sha256_file, utcnow, verify_tree, write_json
except ImportError:
    from common import AdapterError, claim, contained, job_id, read_json, sha256_file, utcnow, verify_tree, write_json


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
    for name in ("raw_features", "tmp", "keops_cache"):
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
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH" and not key.startswith(("APPTAINERENV_", "SINGULARITYENV_"))}
    version = subprocess.run(["apptainer", "--version"], capture_output=True, text=True, timeout=15, check=True, env=environment).stdout.strip()
    write_json(job / "execution.json", {"job_id": ident, "started_at": utcnow(), "apptainer_version": version, "provenance": stage["provenance"]}, exclusive=True)
    # The run remains writable. Scientific source and deployed adapter are bound
    # read-only; this guards mistakes, not hostile code sharing the Unix account.
    command = [
        "apptainer", "exec", "--nv", "--cleanenv", "--containall", "--no-home", "--no-mount", "cwd,hostfs",
        "--bind", f"{source}:/source:ro", "--bind", f"{adapter}:/adapter:ro",
        "--bind", f"{directory}:/run:rw", "--bind", f"{stage['checkpoint_path']}:/checkpoint/model.pt:ro",
        "--pwd", "/source", "--env", "PYTHONNOUSERSITE=1", "--env", "PYTHONPATH=/source:/source/affinity",
        "--env", f"HOME=/run/jobs/{ident}/tmp/home", "--env", f"TMPDIR=/run/jobs/{ident}/tmp",
        "--env", f"KEOPS_CACHE_FOLDER=/run/jobs/{ident}/keops_cache",
        stage["runtime_image"], "python", "-s", "/adapter/execute.py", "/run", ident,
    ]
    return subprocess.run(command, env=environment, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Execution preparation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
