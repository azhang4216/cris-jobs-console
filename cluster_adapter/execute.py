#!/usr/bin/env python3
"""Fixed invocation/validator inside the pinned image, after source binding."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import runpy
import sys

try:
    from .common import AdapterError, job_id, read_json
    from .validate import publish
except ImportError:
    from common import AdapterError, job_id, read_json
    from validate import publish


def project_paths(source: Path) -> dict:
    result = {}
    for name in ("dmasif_compat", "Arguments", "model"):
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin or source.resolve() not in Path(spec.origin).resolve().parents:
            raise AdapterError(f"Project module {name} does not resolve to the pinned snapshot")
        result[name] = str(Path(spec.origin).resolve())
    return result


def main() -> int:
    directory, ident = Path(sys.argv[1]), job_id(sys.argv[2])
    request = read_json(directory / "request.json")["run"]
    stage = read_json(directory / "stage.json")
    job = directory / "jobs" / ident
    source = Path("/source")
    os.chdir(source)
    # Keep installed runtime libraries but remove inherited project paths.
    sys.path[:] = ["/source/affinity", "/source", "/adapter"] + [value for value in sys.path if value.startswith(("/usr/", "/opt/")) and "site-packages" in value or value.startswith(("/usr/lib/python", "/usr/local/lib/python", "/opt/conda/lib/python"))]
    modules = project_paths(source)
    import torch

    provenance = {**stage["provenance"], "project_module_paths": modules, "python": sys.version.split()[0], "torch": torch.__version__, "cuda_runtime": torch.version.cuda, "gpu": torch.cuda.get_device_name(0), "execution": read_json(job / "execution.json")}
    arguments = [str(source / "affinity" / "extract.py"), "--inputs", str(directory / "input-list.txt"), "--out", str(job / "raw_features"), "--repo", str(source), "--ckpt", "/checkpoint/model.pt", "--seed", str(int(request["config"]["seed"])), "--device", "cuda:0"]
    preset = request["resolved"].get("preset", {})
    arguments.extend(["--max_atoms", str(int(preset.get("max_atoms", 150000))), "--max_proteins", str(int(preset.get("max_proteins", 16)))])
    if request["resolved"]["dataset"].get("merge_models", False):
        arguments.append("--merge_models")
    sys.argv = arguments
    try:
        runpy.run_path(arguments[0], run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise
    # Resolve imported project modules again after research execution.
    for name, expected in modules.items():
        imported = sys.modules.get(name)
        if imported is not None and str(Path(imported.__file__).resolve()) != expected:
            raise AdapterError(f"Project module {name} changed origin during extraction")
    expected = [entry["expected_output"] for entry in read_json(directory / "inputs.json")]
    publish(job / "raw_features", job / "published", expected, provenance, job / "result-manifest.json")
    # Scheduler success still requires worker-side complete-result validation.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
