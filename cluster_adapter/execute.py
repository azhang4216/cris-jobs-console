#!/usr/bin/env python3
"""Fixed invocation/validator inside the pinned image, after source binding."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import runpy
import sys

try:
    from .batch import cuda_environment
    from .common import AdapterError, job_id, read_json, utcnow, write_json
    from .validate import publish
except ImportError:
    from batch import cuda_environment
    from common import AdapterError, job_id, read_json, utcnow, write_json
    from validate import publish


# These exact site GRES/model pairs are known. Arbitrary aliases are not parsed
# as model names, and a matching model does not validate PyTorch or KeOps kernels.
GPU_MODELS = {
    "nvidia_h100_80gb_hbm3": ("NVIDIA H100 80GB HBM3", (9, 0)),
    "nvidia_h200": ("NVIDIA H200", (9, 0)),
    "rtx_pro_6000_blackwell": ("NVIDIA RTX PRO 6000 Blackwell Server Edition", (12, 0)),
}


def gpu_preflight(job: Path, request: dict, stage: dict, torch) -> dict:
    """Check allocation identity before research runs, not scientific compatibility."""
    requested = request["resolved"]["cluster"].get("gpu_type")
    report = {"checked_at": utcnow(), "check": "gpu_selection", "requested_gpu_type": requested,
              "gpu_name": None, "compute_capability": None, "visible_device_count": None,
              "torch": str(torch.__version__)[:100], "cuda_runtime": torch.version.cuda,
              "runtime_sha256": stage["provenance"]["runtime_sha256"],
              "model_match_verified": False, "runtime_validation": "not_performed"}
    failure = None
    try:
        cuda = cuda_environment()
        execution = read_json(job / "execution.json")
        requested_count = request["resolved"]["cluster"].get("gpus", 1)
        counts = {"requested_gpu_count": execution.get("requested_gpu_count", 1),
                  "allocated_gpu_count": execution.get("allocated_gpu_count", 1),
                  "used_gpu_count": execution.get("used_gpu_count", 1)}
        if (type(requested_count) is not int or requested_count not in (1, 2)
                or any(type(value) is not int for value in counts.values())
                or counts["requested_gpu_count"] != requested_count
                or counts["allocated_gpu_count"] != requested_count
                or counts["used_gpu_count"] != 1
                or stage["provenance"].get("requested_gpu_count", 1) != requested_count):
            raise AdapterError("GPU allocation evidence does not match the accepted resource request")
        report.update(counts)
        if (cuda["CUDA_VISIBLE_DEVICES"] != execution.get("cuda_visible_devices")
                or cuda.get("CUDA_DEVICE_ORDER") != execution.get("cuda_device_order")):
            raise AdapterError("The container did not preserve the scheduler's CUDA device selection")
        report["cuda_visible_devices"] = cuda["CUDA_VISIBLE_DEVICES"]
        report["cuda_device_order"] = cuda.get("CUDA_DEVICE_ORDER")
        if not torch.cuda.is_available():
            raise AdapterError("CUDA is unavailable inside the pinned runtime")
        count = torch.cuda.device_count()
        report["visible_device_count"] = count
        if count != 1:
            raise AdapterError("The extractor requires exactly one CUDA-visible device")
        properties = torch.cuda.get_device_properties(0)
        name = str(properties.name)[:200]
        capability = tuple(torch.cuda.get_device_capability(0))
        report.update(gpu_name=name, compute_capability=list(capability))
        device_uuid = getattr(properties, "uuid", None)
        if "MIG" in name.upper().split() or (isinstance(device_uuid, str) and device_uuid.startswith("MIG-")):
            raise AdapterError("MIG allocations are unsupported by the full-GPU preset")
        expected = GPU_MODELS.get(requested)
        if expected is None:
            report.update(status="unverified", reason="The requested GPU type has no verified model mapping; runtime compatibility remains unvalidated")
        else:
            expected_name, expected_capability = expected
            if " ".join(name.casefold().split()) != " ".join(expected_name.casefold().split()) or capability != expected_capability:
                raise AdapterError(f"Requested GPU type {requested} does not match allocated {name}; the scheduler may have rewritten the request. Extraction was not started")
            report.update(status="passed", model_match_verified=True,
                          reason="GPU model and capability match the request; scientific runtime compatibility is not validated by this check")
    except AdapterError as exc:
        failure = str(exc)
    except Exception:
        failure = "The CUDA allocation could not be inspected; extraction was not started"
    if failure:
        report.update(status="failed", reason=failure)
    # The atomic writer uses a private 0600 temporary file and never replaces
    # earlier evidence. A failed guard leaves no scientific result manifest.
    write_json(job / "preflight.json", report, exclusive=True)
    if failure:
        print("GPU preflight failed: " + failure, file=sys.stderr)
        raise AdapterError(failure)
    if report["status"] == "unverified":
        print("GPU preflight warning: " + report["reason"], file=sys.stderr)
    return report


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

    try:
        preflight = gpu_preflight(job, request, stage, torch)
    except AdapterError:
        return 1  # The guard already recorded private evidence and a concise log.
    provenance = {**stage["provenance"], **{key: preflight[key] for key in ("requested_gpu_count", "allocated_gpu_count", "used_gpu_count")}, "project_module_paths": modules, "python": sys.version.split()[0], "torch": torch.__version__, "cuda_runtime": torch.version.cuda, "gpu": preflight["gpu_name"], "gpu_preflight": preflight, "execution": read_json(job / "execution.json")}
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
