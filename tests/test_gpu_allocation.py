"""Operator GPU reservations remain pinned independently of the extractor mask."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
from pathlib import Path
import tarfile
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cluster_adapter.common import AdapterError, read_json
from cluster_adapter.helper import Helper
from dmasif_console.config import ExperimentConfig, PresetConfig, Settings
from dmasif_console.transport import _prepared_run


H100 = "nvidia_h100_80gb_hbm3"
H200 = "nvidia_h200"


def operator_settings(count=1, gpu_type=None):
    return Settings(
        repository={"id": 1, "full_name": "lab/research", "clone_url": "https://github.com/lab/research.git"},
        allowed_actor_ids=[1],
        cluster={"gpu_type": gpu_type},
        presets={"quick-test": {"gpus": count}},
    )


def test_default_remains_one_gpu_and_researchers_cannot_override_it():
    assert PresetConfig().gpus == 1
    assert operator_settings().presets["quick-test"].gpus == 1
    with pytest.raises(ValidationError):
        ExperimentConfig(dataset_id="demo", repeat_id="one", gpus=2)


@pytest.mark.parametrize("count", [True, False, 1.0, 2.0, "1", "2", None, 0, -1, 3])
def test_operator_count_requires_an_integer_in_the_supported_range(count):
    with pytest.raises(ValidationError):
        PresetConfig(gpus=count)


@pytest.mark.parametrize("gpu_type", [H100, H200])
def test_two_gpu_preset_requires_a_supported_explicit_type(gpu_type):
    assert operator_settings(2, gpu_type).presets["quick-test"].gpus == 2


@pytest.mark.parametrize("gpu_type", [None, "nvidia_rtx_pro_6000_blackwell_server_edition", "h100", "other"])
def test_two_gpu_preset_rejects_untyped_or_unsupported_allocations(gpu_type):
    with pytest.raises(ValidationError, match="H100/H200"):
        operator_settings(2, gpu_type)


def test_preparation_uses_the_accepted_preset_without_mutating_its_snapshot():
    settings = operator_settings(2, H100)
    policy = settings.policy_snapshot()
    run = {"id": str(uuid4()), "policy": policy, "resolved": {
        "cluster": policy["cluster"], "preset": policy["presets"]["quick-test"],
    }}
    original = copy.deepcopy(run)
    # A later operator edit cannot change resources for an already accepted push.
    settings.presets["quick-test"] = PresetConfig(gpus=1)
    prepared = _prepared_run(run)
    assert prepared["resolved"]["cluster"]["gpus"] == 2
    assert prepared["policy"]["presets"]["quick-test"]["gpus"] == 2
    assert run == original
    assert "gpus" not in run["resolved"]["cluster"]


@pytest.mark.parametrize("count,gpu_type,expected", [
    (None, None, "gpu:1"),
    (1, H100, f"gpu:{H100}:1"),
    (2, H100, f"gpu:{H100}:2"),
    (2, H200, f"gpu:{H200}:2"),
])
def test_helper_requests_the_pinned_gpu_count_and_type(tmp_path, count, gpu_type, expected):
    cluster = {"account": "lab", "partition": "gpu", "gpu_type": gpu_type}
    if count is not None:
        cluster["gpus"] = count
    script = Helper(tmp_path).batch_script(tmp_path / "run", tmp_path / "adapter", cluster)
    assert f"#SBATCH --gres={expected}\n" in script
    assert script.count("#SBATCH --gres=") == 1


@pytest.mark.parametrize("count,gpu_type", [
    (True, H100), (2.0, H100), ("2", H100), (None, H100), (0, H100), (3, H100),
    (2, None), (2, "other"), (2, "nvidia_rtx_pro_6000_blackwell_server_edition"),
])
def test_helper_rejects_invalid_allocation_before_creating_a_run(tmp_path, count, gpu_type):
    helper = Helper(tmp_path / "managed")
    cluster = {"account": "lab", "partition": "gpu", "gpus": count, "gpu_type": gpu_type}
    with pytest.raises(AdapterError):
        helper.batch_script(tmp_path / "run", tmp_path / "adapter", cluster)
    with pytest.raises(AdapterError):
        helper.stage({"run": {"id": str(uuid4()), "resolved": {"cluster": cluster}}, "source": {}})
    assert not helper.root.exists()


@pytest.mark.parametrize("count,gpu_type", [(1, None), (2, H100), (2, H200)])
def test_staging_retains_requested_count_with_the_pinned_source_and_resources(tmp_path, count, gpu_type):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        body = b"# pinned extractor fixture\n"
        member = tarfile.TarInfo("affinity/extract.py")
        member.size = len(body)
        bundle.addfile(member, io.BytesIO(body))
    source = archive.getvalue()
    checksum = hashlib.sha256(b"immutable fixture").hexdigest()
    structure, runtime, checkpoint = (tmp_path / name for name in ("demo.pdb", "runtime.sif", "checkpoint.pt"))
    for path in (structure, runtime, checkpoint):
        path.write_bytes(b"immutable fixture")
    settings = operator_settings(count, gpu_type)
    policy = settings.policy_snapshot()
    cluster = {**policy["cluster"], "runtime_image": str(runtime), "runtime_sha256": checksum,
               "checkpoint_path": str(checkpoint), "checkpoint_sha256": checksum}
    run = {"id": str(uuid4()), "commit_sha": "a" * 40, "config": {"seed": 0}, "resolved": {
        "cluster": cluster, "preset": policy["presets"]["quick-test"],
        "dataset": {"entries": [{"path": str(structure), "sha256": checksum, "chains": "A"}]},
    }}
    payload = {"run": _prepared_run(run), "source": {"commit_sha": run["commit_sha"], "sha256": hashlib.sha256(source).hexdigest()},
               "archive_b64": base64.b64encode(source).decode(), "limits": {"min_free_bytes": 0}}
    helper = Helper(tmp_path / "managed")
    stage = helper.stage(payload)
    directory = Path(stage["remote_dir"])
    assert stage["provenance"]["requested_gpu_count"] == count
    assert read_json(directory / "request.json")["run"]["resolved"]["cluster"]["gpus"] == count
    assert read_json(directory / "stage.json")["provenance"]["requested_gpu_count"] == count
    assert "allocated_gpu_count" not in stage["provenance"]
    assert "used_gpu_count" not in stage["provenance"]
    assert (Path(stage["source_dir"]) / "affinity/extract.py").read_bytes() == body
    assert helper.stage(payload) == stage
