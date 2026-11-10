"""Fixed dataset_v2 staging, training invocation and retained-result validation."""
from __future__ import annotations

import csv
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

try:
    from .common import AdapterError, contained, digest, read_json, sha256_file, utcnow, write_json
except ImportError:
    from common import AdapterError, contained, digest, read_json, sha256_file, utcnow, write_json

DATASET_FILES = {
    "index": "data/datasets/dataset_v2/index.parquet",
    "inputs": "data/datasets/dataset_v2/inputs.txt",
    "items": "out/runs/dataset_v2/items.tsv",
    "ligand_feats": "out/runs/dataset_v2/ligand_feats.npz",
    "val_set": "out/valset_v1v2/val_set.tsv",
    "holdout_groups": "out/valset_v1v2/holdout_groups.txt",
    "val_provenance": "out/valset_v1v2/provenance.json",
    "head": "out/encoder_test/head_pretrained_300.pt",
}
TRAINING_OUTPUTS = ("best.pt", "last.pt", "history.json", "training-summary.json", "evaluation.json")
TRAINING_SOURCE_FILES = ("src/run_experiment.py", "src/train.py", "src/surface_encoder.py", "dMaSIF/model.py")


def is_training(run: dict) -> bool:
    return run.get("config", {}).get("job_type") == "dmasif_train"


def dataset_manifest(dataset: dict) -> list[dict]:
    root = Path(dataset["project_root"])
    if (not root.is_absolute() or root == Path("/") or ".." in root.parts or root.is_symlink()
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root))):
        raise AdapterError("Training dataset root must be an absolute registered directory")
    if set(dataset.get("hashes", {})) != set(DATASET_FILES):
        raise AdapterError("Every fixed training dataset identity must be registered")
    return [{"role": role, "original_path": str(root / relative), "name": Path(relative).name,
             "sha256": digest(dataset["hashes"][role])} for role, relative in DATASET_FILES.items()]


def normalize_items(text: str, prefix: str) -> str:
    rows = csv.DictReader(io.StringIO(text), delimiter="\t")
    if not rows.fieldnames or not {"input_path", "sdf_paths"} <= set(rows.fieldnames):
        raise AdapterError("Registered items.tsv has no structure/ligand path columns")
    count = 0
    for row in rows:
        count += 1
        paths = [row.get("input_path", ""), *row.get("sdf_paths", "").split(",")]
        for value in paths:
            path = Path(value)
            if not value or ".." in path.parts:
                raise AdapterError("Training item paths must stay inside the fixed dataset")
            if path.is_absolute() and not value.startswith(prefix + "/"):
                raise AdapterError("Training item absolute path is outside its registered source prefix")
    if not count:
        raise AdapterError("Registered training items.tsv is empty")
    return text.replace(prefix + "/", "/dataset/")


def stage_inputs(directory: Path, dataset: dict, manifest: list[dict]) -> dict:
    root = Path(dataset["project_root"])
    for entry in manifest:
        path = contained(root, DATASET_FILES[entry["role"]])
        if sha256_file(path) != entry["sha256"]:
            raise AdapterError("Registered training dataset file is missing or changed")
    for relative in ("data/surfaces", "data/pockets/fpocket", "data/plip", "systems"):
        path = root / relative
        if not path.is_dir() or path.is_symlink():
            raise AdapterError("Registered training data/cache directory is unavailable")
    original = root / DATASET_FILES["items"]
    if original.stat().st_size > 128 * 1024 * 1024:
        raise AdapterError("Training items manifest exceeds 128 MiB")
    normalized = normalize_items(original.read_text(), dataset.get("source_prefix") or str(root))
    target = directory / "inputs" / "items.tsv"
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        if target.is_symlink() or target.read_text() != normalized:
            raise AdapterError("Staged training item manifest changed")
    else:
        with target.open("x") as stream:
            stream.write(normalized)
    write_json(directory / "inputs.json", manifest)
    return {"project_root": str(root), "items_path": str(target),
            "items_sha256": sha256_file(target), "original_items_sha256": dataset["hashes"]["items"],
            "dataset_hashes": dict(dataset["hashes"])}


def verify_inputs(directory: Path, training: dict) -> None:
    for entry in read_json(directory / "inputs.json"):
        path = contained(Path(training["project_root"]), DATASET_FILES[entry["role"]])
        if sha256_file(path) != entry["sha256"]:
            raise AdapterError("Registered training dataset identity changed")
    path = directory / "inputs" / "items.tsv"
    if path.is_symlink() or sha256_file(path) != training["items_sha256"]:
        raise AdapterError("Normalized training items changed")


def invocation(run: dict, output: Path) -> list[str]:
    config = run["config"]
    limits = {"batch_size": (2, 128), "steps": (1, 1000000), "eval_every": (1, 1000000),
              "save_every": (1, 1000000), "seed": (0, 2**32 - 1)}
    for key, (low, high) in limits.items():
        value = config.get(key)
        if type(value) is not int or not low <= value <= high:
            raise AdapterError("Invalid bounded training integer: " + key)
    if config.get("dataset_id") != "dataset_v2" or config.get("preset_id") != "training":
        raise AdapterError("Training requires the fixed dataset_v2 and training preset")
    minutes = run["resolved"]["preset"]["wall_minutes"]
    # Reserve one hour for held-out evaluation and final publication.
    hours = max(0.1, (minutes - min(60, minutes / 4)) / 60)
    return ["/source/src/run_experiment.py", "--project-root", "/dataset", "--out", str(output),
            "--batch-size", str(config["batch_size"]), "--steps", str(config["steps"]),
            "--seed", str(config["seed"]), "--eval-every", str(config["eval_every"]),
            "--save-every", str(config["save_every"]), "--max-hours", str(hours)]


def _finite_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_finite_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    return value


def validate_checkpoint(path: Path) -> dict:
    # Already executing trusted research code in the isolated scientific runtime;
    # checkpoints are never deserialized on the web/worker application host.
    import torch
    value = torch.load(path, map_location="cpu", weights_only=False)
    if not isinstance(value, dict) or type(value.get("step")) is not int or value["step"] < 0:
        raise AdapterError("Checkpoint has no valid training step")
    tensors = value.get("state_dict")
    if not isinstance(tensors, dict) or not tensors:
        raise AdapterError("Checkpoint has no model state")
    for tensor in tensors.values():
        if not isinstance(tensor, torch.Tensor) or not torch.isfinite(tensor).all():
            raise AdapterError("Checkpoint model state contains invalid tensors")
    if not isinstance(value.get("train_rows"), list) or not isinstance(value.get("val_rows"), list):
        raise AdapterError("Checkpoint has no retained training/validation split")
    if set(value["train_rows"]) & set(value["val_rows"]):
        raise AdapterError("Checkpoint training and validation rows overlap")
    return {"step": value["step"]}


def validate_evaluation_rows(path: Path) -> dict:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise AdapterError("Per-row evaluation exceeds the 32 MiB validation limit")
    columns = ("row", "system_id", "group", "rank", "n_cand")
    seen, header = set(), None
    with path.open(encoding="utf-8", newline="") as stream:
        for line in stream:
            if not line.strip() or line.startswith("#"):
                continue
            values = line.rstrip("\r\n").split("\t")
            if header is None:
                if tuple(values) != columns:
                    raise AdapterError("Per-row evaluation has an unsupported TSV header")
                header = values
                continue
            if len(values) != len(columns) or not values[1] or not values[2]:
                raise AdapterError("Per-row evaluation contains an incomplete record")
            if any(re.fullmatch(r"[0-9]+", values[index]) is None for index in (0, 3, 4)):
                raise AdapterError("Per-row evaluation row, rank and candidate count must be integers")
            row, rank, candidates = (int(values[index]) for index in (0, 3, 4))
            if row in seen or not 1 <= rank <= candidates:
                raise AdapterError("Per-row evaluation contains duplicate rows or invalid ranks")
            seen.add(row)
    if not seen:
        raise AdapterError("Per-row evaluation contains no scored rows")
    return {"rows": len(seen)}


def publish_training(raw: Path, destination: Path, provenance: dict, manifest_path: Path,
                     config: dict, checkpoint_validator=validate_checkpoint) -> dict:
    if destination.exists() or manifest_path.exists():
        raise AdapterError("Training publication already exists")
    if raw.is_symlink() or not raw.is_dir():
        raise AdapterError("Training output must be a regular run-owned directory")
    destination.mkdir(parents=True, exist_ok=False)
    artifacts, errors, documents, checkpoints = [], [], {}, {}
    optional = []
    for path in raw.iterdir():
        if re.fullmatch(r"step[0-9]+\.pt", path.name):
            if len(optional) == 100:
                errors.append({"name": "retained checkpoints", "error": "More than 100 optional step checkpoints were produced"})
                break
            optional.append(path.name)
    rows_path = raw / "evaluation-rows.tsv"
    if rows_path.exists() or rows_path.is_symlink():
        optional.append("evaluation-rows.tsv")
    names = (*TRAINING_OUTPUTS, *sorted(optional))
    for name in names:
        source = raw / name
        try:
            if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
                raise AdapterError("Required training output is missing or empty")
            if source.stat().st_size > 512 * 1024 * 1024:
                raise AdapterError("Training output exceeds the 512 MiB publication limit")
            fd, temporary = tempfile.mkstemp(prefix=".training-", dir=destination)
            try:
                with source.open("rb") as incoming, os.fdopen(fd, "wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                retained = Path(temporary)
                info = {}
                if name.endswith(".pt"):
                    info = checkpoint_validator(retained)
                    if name.startswith("step") and (len(name) > 14 or info.get("step") != int(name[4:-3])
                                                    or not 0 <= info["step"] <= config["steps"]):
                        raise AdapterError("Retained checkpoint step does not match its filename or accepted target")
                    checkpoints[name] = info
                elif name == "evaluation-rows.tsv":
                    info = validate_evaluation_rows(retained)
                else:
                    if retained.stat().st_size > 32 * 1024 * 1024:
                        raise AdapterError("Training JSON exceeds validation size limit")
                    value = json.loads(retained.read_text())
                    if name == "history.json":
                        if not isinstance(value, list) or not value:
                            raise AdapterError("Training history must contain evaluation records")
                    elif not isinstance(value, dict):
                        raise AdapterError("Training result must be a JSON object")
                    documents[name] = value
                checksum, size = sha256_file(retained), retained.stat().st_size
                os.link(temporary, destination / name)
            finally:
                Path(temporary).unlink(missing_ok=True)
            relative = str((destination / name).relative_to(manifest_path.parent.parent.parent))
            artifacts.append({"name": name, "relative_path": relative, "path": relative,
                              "size": size, "sha256": checksum,
                              "kind": "checkpoint" if name.endswith(".pt") else "metrics", **info})
        except (AdapterError, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            errors.append({"name": name, "error": str(exc)[:500]})
    summary = documents.get("training-summary.json", {})
    requested, completed = summary.get("requested_steps"), summary.get("completed_steps")
    summary_valid = (type(requested) is int and requested == config["steps"]
                     and type(completed) is int and 0 <= completed <= requested
                     and summary.get("schema_version") == 1 and summary.get("dataset_id") == "dataset_v2"
                     and type(summary.get("batch_size")) is int and summary["batch_size"] == config["batch_size"]
                     and type(summary.get("seed")) is int and summary["seed"] == config["seed"]
                     and type(summary.get("optimizer_updates")) is int and 0 < summary["optimizer_updates"] <= completed
                     and checkpoints.get("last.pt", {}).get("step") == completed
                     and checkpoints.get("best.pt", {}).get("step", requested + 1) <= completed
                     and summary.get("status") in {"completed", "budget_exhausted"}
                     and (summary["status"] == "completed") == (completed == requested))
    if not summary_valid:
        errors.append({"name": "training-summary.json", "error": "Training completion evidence does not match the accepted request and checkpoint"})
    evaluation = documents.get("evaluation.json", {})
    metrics = evaluation.get("metrics", {})
    mrr = metrics.get("mrr") if isinstance(metrics, dict) else None
    if (evaluation.get("schema_version") != 1 or evaluation.get("dataset_id") != "dataset_v2"
            or evaluation.get("validation_set") != "valset_v1v2" or evaluation.get("checkpoint") != "best.pt"
            or type(evaluation.get("step")) is not int
            or evaluation["step"] != checkpoints.get("best.pt", {}).get("step")
            or type(mrr) not in (int, float) or not math.isfinite(mrr) or not 0 <= mrr <= 1):
        errors.append({"name": "evaluation.json", "error": "Held-out evaluation metrics are missing"})
    declared = summary.get("checkpoints", [])
    for artifact in (item for item in artifacts if item["kind"] == "checkpoint"):
        matching = [item for item in declared if isinstance(item, dict) and item.get("name") == artifact["name"]] if isinstance(declared, list) else []
        if len(matching) != 1 or any(matching[0].get(key) != artifact.get(key) for key in ("step", "size", "sha256")):
            errors.append({"name": artifact["name"], "error": "Checkpoint metadata does not match the retained bytes"})
        if type(completed) is not int or artifact.get("step", -1) > completed:
            errors.append({"name": artifact["name"], "error": "Checkpoint step exceeds the reported completed training step"})
    complete = summary_valid and completed == requested and summary.get("status") == "completed"
    outcome = "FAILED" if errors else "SUCCEEDED" if complete else "PARTIAL"
    result = {"job_type": "dmasif_train", "outcome": outcome,
              "expected_count": len(names), "valid_count": len(artifacts),
              "artifacts": artifacts, "errors": errors, "provenance": provenance,
              "training": _finite_json(summary), "evaluation": _finite_json(evaluation),
              "reason": "Training step target reached and held-out evaluation validated." if outcome == "SUCCEEDED" else
                        "Training budget ended before the requested step target; checkpoints and evaluation are retained." if outcome == "PARTIAL" else
                        "Training outputs failed validation.", "validated_at": utcnow()}
    write_json(manifest_path, result, exclusive=True)
    return result


def telemetry_chunk(directory: Path, jobs: list[dict], offset: int, max_bytes: int) -> dict:
    if type(offset) is not int or offset < 0 or type(max_bytes) is not int or not 1 <= max_bytes <= 262144:
        raise AdapterError("Invalid telemetry offset or byte limit")
    paths = []
    for job in jobs:
        from_id = str(job["job_id"])
        if not from_id.isdecimal():
            raise AdapterError("Invalid telemetry job identity")
        path = directory / "jobs" / from_id / "training" / "telemetry.jsonl"
        if path.exists():
            paths.append(contained(directory, str(path.relative_to(directory))))
    if len(paths) > 1:
        raise AdapterError("Multiple telemetry streams require operator review")
    if not paths:
        return {"offset": offset, "next_offset": offset, "text": "", "eof": True}
    path = paths[0]
    if offset > path.stat().st_size:
        raise AdapterError("Telemetry stream was truncated")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(max_bytes)
    boundary = data.rfind(b"\n") + 1
    if not boundary and len(data) == max_bytes:
        raise AdapterError("Telemetry line exceeds the protocol bound")
    data = data[:boundary]
    next_offset = offset + len(data)
    return {"offset": offset, "next_offset": next_offset, "text": data.decode("utf-8"),
            "eof": next_offset >= path.stat().st_size}
