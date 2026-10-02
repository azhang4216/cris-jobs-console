"""GPU selection guard tests use synthetic CUDA objects, never a real GPU."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from cluster_adapter import batch, execute
from cluster_adapter.common import AdapterError, read_json, write_json


def synthetic_torch(name="NVIDIA H100 80GB HBM3", capability=(9, 0), count=1, available=True, uuid=None):
    return SimpleNamespace(
        __version__="2.5.0a0", version=SimpleNamespace(cuda="12.6"),
        cuda=SimpleNamespace(
            is_available=lambda: available,
            device_count=lambda: count,
            get_device_properties=lambda index: SimpleNamespace(name=name, uuid=uuid),
            get_device_capability=lambda index: capability,
            get_arch_list=lambda: pytest.fail("PyTorch's architecture list must not certify runtime compatibility"),
        ),
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    job = tmp_path / "run" / "jobs" / "17"
    job.mkdir(parents=True)
    write_json(job / "execution.json", {"cuda_visible_devices": "0", "cuda_device_order": None})
    request = {"resolved": {"cluster": {"gpu_type": "nvidia_h100_80gb_hbm3"},
                            "dataset": {"entries": []}}, "config": {"seed": 0}}
    stage = {"provenance": {"runtime_sha256": "a" * 64, "source_sha256": "b" * 64}}
    return job, request, stage


@pytest.mark.parametrize("mask", ["0", "3", "GPU-8932f937-d72c-4106-c12f-20bd9faed9f6"])
@pytest.mark.parametrize("order", [None, "PCI_BUS_ID", "FASTEST_FIRST"])
def test_scheduler_mask_is_forwarded_exactly_through_clean_environment(tmp_path, monkeypatch, mask, order):
    directory = tmp_path / "run"
    job = directory / "jobs" / "17"
    job.mkdir(parents=True)
    stage = {"source_dir": str(tmp_path / "source"), "adapter_dir": str(tmp_path / "adapter"),
             "runtime_image": str(tmp_path / "runtime.sif"), "checkpoint_path": str(tmp_path / "model.pt"),
             "provenance": {"runtime_sha256": "a" * 64}}
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", mask)
    monkeypatch.setenv("APPTAINERENV_CUDA_VISIBLE_DEVICES", "all")
    monkeypatch.setenv("SINGULARITYENV_CUDA_VISIBLE_DEVICES", "all")
    if order is None:
        monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    else:
        monkeypatch.setenv("CUDA_DEVICE_ORDER", order)
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(returncode=0, stdout="Apptainer fixture")

    monkeypatch.setattr(batch.subprocess, "run", run)
    assert batch._execute(directory, "17", stage, False, job / "tmp") == 0
    command, options = calls[-1]
    assert "--cleanenv" in command
    assert "CUDA_VISIBLE_DEVICES=" + mask in command
    assert "CUDA_VISIBLE_DEVICES=all" not in command
    assert "APPTAINERENV_CUDA_VISIBLE_DEVICES" not in options["env"]
    assert "SINGULARITYENV_CUDA_VISIBLE_DEVICES" not in options["env"]
    if order:
        assert "CUDA_DEVICE_ORDER=" + order in command
    else:
        assert not any(value.startswith("CUDA_DEVICE_ORDER=") for value in command)
    evidence = read_json(job / "execution.json")
    assert evidence["cuda_visible_devices"] == mask
    assert evidence["cuda_device_order"] == order
    assert stage["provenance"]["runtime_sha256"] == "a" * 64


@pytest.mark.parametrize("mask", [None, "", " ", "all", "-1", "0,1", "0\n", "0;false", "$(false)",
                                  "GPU-abbreviated", "MIG-GPU-8932f937-d72c-4106-c12f-20bd9faed9f6/1/0",
                                  "0" * 101])
def test_unsafe_or_ambiguous_masks_fail_before_apptainer_launch(tmp_path, monkeypatch, mask):
    if mask is None:
        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    else:
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", mask)
    monkeypatch.setattr(batch.subprocess, "run", lambda *args, **kwargs: pytest.fail("Apptainer started with an invalid GPU mask"))
    with pytest.raises(AdapterError, match="CUDA_VISIBLE_DEVICES"):
        batch._execute(tmp_path, "17", {"source_dir": "source", "adapter_dir": "adapter"}, False, tmp_path)


@pytest.mark.parametrize("order", ["", "pci_bus_id", "PCI_BUS_ID,OTHER=1", "PCI_BUS_ID\n"])
def test_invalid_device_order_is_rejected(monkeypatch, order):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("CUDA_DEVICE_ORDER", order)
    with pytest.raises(AdapterError, match="CUDA_DEVICE_ORDER"):
        batch.cuda_environment()


@pytest.mark.parametrize("gpu_type,name,capability", [
    ("nvidia_h100_80gb_hbm3", "NVIDIA H100 80GB HBM3", (9, 0)),
    ("nvidia_h200", "NVIDIA H200", (9, 0)),
    ("rtx_pro_6000_blackwell", "NVIDIA RTX PRO 6000 Blackwell Server Edition", (12, 0)),
])
def test_known_gpu_match_records_identity_without_claiming_runtime_compatibility(setup, gpu_type, name, capability):
    job, request, stage = setup
    request["resolved"]["cluster"]["gpu_type"] = gpu_type
    report = execute.gpu_preflight(job, request, stage, synthetic_torch(name, capability))
    assert report == read_json(job / "preflight.json")
    assert report["status"] == "passed" and report["model_match_verified"] is True
    assert report["runtime_validation"] == "not_performed"
    assert report["gpu_name"] == name and report["compute_capability"] == list(capability)
    assert report["torch"] == "2.5.0a0" and report["cuda_runtime"] == "12.6"
    assert report["runtime_sha256"] == "a" * 64
    assert (job / "preflight.json").stat().st_mode & 0o777 == 0o600
    assert not (job / "result-manifest.json").exists()


@pytest.mark.parametrize("name,capability", [("NVIDIA RTX PRO 6000 Blackwell Server Edition", (12, 0)),
                                            ("NVIDIA H200", (9, 0)), ("NVIDIA H100 80GB HBM3", (12, 0))])
def test_known_request_checks_model_as_well_as_capability(setup, capsys, name, capability):
    job, request, stage = setup
    with pytest.raises(AdapterError, match="scheduler may have rewritten"):
        execute.gpu_preflight(job, request, stage, synthetic_torch(name, capability))
    evidence = read_json(job / "preflight.json")
    assert evidence["status"] == "failed"
    assert evidence["gpu_name"] == name
    assert not evidence["model_match_verified"]
    assert "Extraction was not started" in capsys.readouterr().err
    assert not (job / "result-manifest.json").exists()


@pytest.mark.parametrize("gpu_type", [None, "site_alias", "h100", "nvidia_h100_80gb_hbm3_fake"])
def test_unknown_or_untyped_policy_is_explicitly_unverified(setup, capsys, gpu_type):
    job, request, stage = setup
    request["resolved"]["cluster"]["gpu_type"] = gpu_type
    report = execute.gpu_preflight(job, request, stage, synthetic_torch())
    assert report["status"] == "unverified" and report["model_match_verified"] is False
    assert report["requested_gpu_type"] == gpu_type
    assert report["runtime_validation"] == "not_performed"
    assert "GPU preflight warning" in capsys.readouterr().err


@pytest.mark.parametrize("kwargs,reason", [({"available": False}, "unavailable"), ({"count": 0}, "exactly one"),
                                          ({"count": 2}, "exactly one"),
                                          ({"name": "NVIDIA H100 MIG 1g.10gb"}, "MIG"),
                                          ({"uuid": "MIG-8932f937-d72c-4106-c12f-20bd9faed9f6"}, "MIG")])
def test_missing_multiple_and_mig_devices_are_recorded_as_failures(setup, kwargs, reason):
    job, request, stage = setup
    with pytest.raises(AdapterError, match=reason):
        execute.gpu_preflight(job, request, stage, synthetic_torch(**kwargs))
    assert read_json(job / "preflight.json")["status"] == "failed"


def test_container_cannot_silently_change_the_slurm_device_mask(setup, monkeypatch):
    job, request, stage = setup
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    with pytest.raises(AdapterError, match="did not preserve"):
        execute.gpu_preflight(job, request, stage, synthetic_torch())
    assert read_json(job / "preflight.json")["status"] == "failed"


def test_cuda_diagnostic_failures_do_not_persist_unbounded_exception_details(setup):
    job, request, stage = setup
    torch = synthetic_torch()

    def fail():
        raise RuntimeError("unexpected private runtime diagnostic")

    torch.cuda.device_count = fail
    with pytest.raises(AdapterError, match="could not be inspected"):
        execute.gpu_preflight(job, request, stage, torch)
    assert "private runtime diagnostic" not in (job / "preflight.json").read_text()


def test_gpu_mismatch_prevents_extractor_invocation_and_returns_failure(setup, monkeypatch):
    job, request, stage = setup
    directory = job.parent.parent
    write_json(directory / "request.json", {"run": request})
    write_json(directory / "stage.json", stage)
    monkeypatch.setattr(sys, "argv", ["execute.py", str(directory), "17"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(execute.os, "chdir", lambda _: None)
    monkeypatch.setattr(execute, "project_paths", lambda _: {})
    monkeypatch.setitem(sys.modules, "torch", synthetic_torch("NVIDIA RTX PRO 6000 Blackwell Server Edition", (12, 0)))
    monkeypatch.setattr(execute.runpy, "run_path", lambda *args, **kwargs: pytest.fail("Research extractor executed after GPU mismatch"))
    monkeypatch.setattr(execute, "publish", lambda *args, **kwargs: pytest.fail("GPU mismatch published results"))
    assert execute.main() == 1
    assert read_json(job / "preflight.json")["requested_gpu_type"] == "nvidia_h100_80gb_hbm3"


def test_prior_preflight_evidence_is_never_overwritten(setup):
    job, request, stage = setup
    write_json(job / "preflight.json", {"retained": True})
    with pytest.raises(FileExistsError):
        execute.gpu_preflight(job, request, stage, synthetic_torch())
    assert read_json(job / "preflight.json") == {"retained": True}


@pytest.mark.parametrize("mask", ["3,5", "GPU-8932f937-d72c-4106-c12f-20bd9faed9f6,GPU-7932f937-d72c-4106-c12f-20bd9faed9f6"])
def test_two_gpu_reservation_exposes_only_first_assigned_device(tmp_path, monkeypatch, mask):
    directory = tmp_path / "run"
    job = directory / "jobs" / "17"
    job.mkdir(parents=True)
    stage = {"source_dir": "source", "adapter_dir": "adapter", "runtime_image": "image",
             "checkpoint_path": "checkpoint", "provenance": {"requested_gpu_count": 2}}
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", mask)
    calls = []

    def run(argv, **options):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="Apptainer fixture")

    monkeypatch.setattr(batch.subprocess, "run", run)
    assert batch._execute(directory, "17", stage, False, tmp_path) == 0
    assert "CUDA_VISIBLE_DEVICES=" + mask.split(",")[0] in calls[-1]
    assert "CUDA_VISIBLE_DEVICES=" + mask not in calls[-1]
    evidence = read_json(job / "execution.json")
    assert evidence["allocated_cuda_visible_devices"] == mask
    assert evidence["requested_gpu_count"] == evidence["allocated_gpu_count"] == 2
    assert evidence["used_gpu_count"] == 1


@pytest.mark.parametrize("mask", ["0", "0,1,2", "3,3", "03,3", "0,all", "0,MIG-1", "0,GPU-8932f937-d72c-4106-c12f-20bd9faed9f6"])
def test_two_gpu_mask_rejects_wrong_count_duplicates_and_mixed_formats(monkeypatch, mask):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", mask)
    with pytest.raises(AdapterError, match="CUDA_VISIBLE_DEVICES"):
        batch.cuda_environment(2)


def test_two_reserved_one_used_preflight_preserves_resource_truth(setup):
    job, request, stage = setup
    request["resolved"]["cluster"]["gpus"] = 2
    stage["provenance"]["requested_gpu_count"] = 2
    write_json(job / "execution.json", {"cuda_visible_devices": "0", "cuda_device_order": None,
               "requested_gpu_count": 2, "allocated_gpu_count": 2, "used_gpu_count": 1})
    report = execute.gpu_preflight(job, request, stage, synthetic_torch())
    assert report["status"] == "passed"
    assert report["allocated_gpu_count"] == 2 and report["used_gpu_count"] == 1
    assert report["visible_device_count"] == 1


def test_two_gpu_request_rejects_inconsistent_allocation_evidence(setup):
    job, request, stage = setup
    request["resolved"]["cluster"]["gpus"] = 2
    stage["provenance"]["requested_gpu_count"] = 2
    with pytest.raises(AdapterError, match="allocation evidence"):
        execute.gpu_preflight(job, request, stage, synthetic_torch())
