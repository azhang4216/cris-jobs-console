from __future__ import annotations

import io
from pathlib import Path
import tarfile
from uuid import uuid4

import numpy as np
import pytest

from cluster_adapter.common import AdapterError, input_manifest, read_json, safe_extract, write_json
from cluster_adapter.helper import Helper
from cluster_adapter.validate import publish, validate_npz


def valid_npz(path: Path, **changes):
    n, m = 3, 2
    values = dict(xyz=np.zeros((n, 3)), normals=np.ones((n, 3)), input_feats=np.ones((n, 16)), emb1=np.ones((n, 16)), emb2=np.ones((n, 16)), nearest_atom=np.array([0, 1, 0]), atom_xyz=np.zeros((m, 3)), atom_type=np.array([0, 3]), atom_chain=np.array(["A", "A"]), atom_resnum=np.array([1, 2]), atom_icode=np.array(["", ""]), atom_resname=np.array(["ALA", "GLY"]), atom_name=np.array(["CA", "CA"]))
    values.update(changes)
    np.savez_compressed(path, **values)


def test_npz_rejects_invalid_indices_dimensions_nonfinite_and_pickle(tmp_path):
    path = tmp_path / "valid.npz"
    valid_npz(path)
    assert validate_npz(path) == {"points": 3, "atoms": 2}
    for change in ({"nearest_atom": np.array([0, 1, 2])}, {"emb1": np.ones((3, 8))}, {"normals": np.full((3, 3), np.nan)}, {"atom_name": np.array([object(), object()])}):
        valid_npz(path, **change)
        with pytest.raises((AdapterError, ValueError)):
            validate_npz(path)


def test_missing_corrupt_and_partial_results_never_succeed(tmp_path):
    run = tmp_path / "run"
    job = run / "jobs" / "123"
    raw = job / "raw_features"
    raw.mkdir(parents=True)
    valid_npz(raw / "good.npz")
    (raw / "broken.npz").write_bytes(b"truncated")
    result = publish(raw, job / "published", ["good.npz", "broken.npz", "missing.npz"], {}, job / "result-manifest.json")
    assert result["outcome"] == "PARTIAL"
    assert (result["expected_count"], result["valid_count"]) == (3, 1)
    assert result["artifacts"][0]["relative_path"] == "jobs/123/published/good.npz"
    old = (job / "published" / "good.npz").read_bytes()
    with pytest.raises(AdapterError):
        publish(raw, job / "published", ["good.npz"], {}, job / "result-manifest.json")
    assert (job / "published" / "good.npz").read_bytes() == old


def test_zero_outputs_is_failed(tmp_path):
    job = tmp_path / "run" / "jobs" / "1"
    raw = job / "raw_features"
    raw.mkdir(parents=True)
    result = publish(raw, job / "published", ["absent.npz"], {}, job / "result-manifest.json")
    assert result["outcome"] == "FAILED"
    assert result["valid_count"] == 0


def test_archive_rejects_traversal_symlink_lfs_and_duplicate_members(tmp_path):
    cases = [("../escape", b"bad", None), ("link", b"", "../../escape"), ("large.dat", b"version https://git-lfs.github.com/spec/v1\noid sha256:x", None)]
    for index, (name, body, link) in enumerate(cases):
        archive = tmp_path / f"{index}.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            if link:
                entry.type, entry.linkname = tarfile.SYMTYPE, link
            bundle.addfile(entry, io.BytesIO(body))
        with pytest.raises(AdapterError):
            safe_extract(archive, tmp_path / f"out{index}")
    assert not (tmp_path / "escape").exists()


def test_chain_name_collision_rejected_before_submission():
    checksum = "a" * 64
    # Upstream removes periods, causing these distinct selections to collide.
    with pytest.raises(AdapterError):
        input_manifest([{"path": "/data/one/target.pdb", "sha256": checksum, "chains": "1.A"}, {"path": "/data/two/target.cif", "sha256": checksum, "chains": "1A"}])
    assert input_manifest([{"path": "/data/target.cif", "sha256": checksum, "chains": "2.A,1.A"}])[0]["expected_output"] == "target_1A2A.npz"


def staged_helper(tmp_path):
    helper = Helper(tmp_path)
    ident = str(uuid4())
    directory = tmp_path / "runs" / ident
    directory.mkdir(parents=True)
    write_json(directory / "stage.json", {"source_dir": "unused"})
    (directory / "submit.sbatch").write_text("#!/bin/bash\ntrue\n")
    helper.verify_stage = lambda *_: None
    return helper, ident, directory


def test_lost_sbatch_response_leaves_claim_and_never_resubmits(tmp_path):
    helper, ident, directory = staged_helper(tmp_path)
    calls = []
    def command(argv, **_):
        calls.append(argv)
        if argv[0] == "sbatch":
            raise TimeoutError("connection gone after Slurm acceptance")
        return ""
    helper.command = command
    with pytest.raises(AdapterError, match="SUBMISSION_UNCERTAIN"):
        helper.submit({"run_id": ident})
    assert (directory / "submit.claim").is_dir()
    with pytest.raises(AdapterError, match="SUBMISSION_UNCERTAIN"):
        helper.submit({"run_id": ident})
    assert sum(call[0] == "sbatch" for call in calls) == 1


def test_receipt_persists_and_duplicate_submit_recovers(tmp_path):
    helper, ident, directory = staged_helper(tmp_path)
    calls = []
    def command(argv, **_):
        calls.append(argv)
        return "123;testcluster\n"
    helper.command = command
    first = helper.submit({"run_id": ident})
    assert helper.submit({"run_id": ident}) == first
    assert len(calls) == 1
    assert read_json(directory / "submission-receipt.json")["job_id"] == "123"
    assert "--no-requeue" in calls[0] and "--open-mode=append" in calls[0]


def test_reconciliation_matches_owner_full_tag_and_records_duplicates(tmp_path):
    helper, ident, directory = staged_helper(tmp_path)
    tag = "dm-" + ident
    write_json(directory / "submission-intent.json", {"owner": "lab", "tag": tag, "created_at": "2026-01-01T00:00:00+00:00"})
    def command(argv, **_):
        if argv[0] == "squeue":
            return f"10|RUNNING|2026-01-01T00:00:00|2026-01-01T00:01:00|{tag}|lab|{tag}|None\n11|PENDING|2026-01-01T00:00:00|N/A|{tag}|lab|{tag}|Resources\n12|RUNNING|2026-01-01T00:00:00|N/A|{tag}|other|{tag}|None\n"
        return ""
    helper.command = command
    matches = helper.reconcile({"run_id": ident})
    assert {job["job_id"] for job in matches} == {"10", "11"}
    assert next(job for job in matches if job["job_id"] == "11")["started_at"] is None


def test_status_finishes_when_job_is_purged_from_live_queue(tmp_path):
    helper, ident, directory = staged_helper(tmp_path)
    tag = "dm-" + ident
    write_json(directory / "submission-intent.json", {"owner": "lab", "tag": tag, "created_at": "2026-01-01T00:00:00+00:00"})
    calls = []

    def command(argv, **_):
        calls.append(argv)
        if argv[0] == "sacct":
            return f"10|COMPLETED|0:0|2026-01-01T00:00:00|2026-01-01T00:01:00|2026-01-01T00:02:00|{tag}|lab|{tag}\n"
        if any(arg.startswith("--jobs=") for arg in argv):
            raise AdapterError("slurm_load_jobs error: Invalid job id specified")
        # Even a different live job with the same owner/tag cannot replace the
        # explicitly requested allocation's terminal accounting evidence.
        return f"11|RUNNING|2026-01-01T00:00:00|2026-01-01T00:01:00|{tag}|lab|{tag}|None\n"

    helper.command = command
    jobs = helper.status({"run_id": ident, "jobs": [{"job_id": "10"}]})
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "10"
    assert jobs[0]["state"] == "COMPLETED" and jobs[0]["terminal"] is True
    assert jobs[0]["exit_code"] == "0:0"
    assert "--jobs=10" in calls[0]
    assert "--user=lab" in calls[1]
    assert not any(arg.startswith("--jobs=") for arg in calls[1])


@pytest.mark.parametrize("failed_command", ["sacct", "squeue"])
def test_status_does_not_hide_genuine_scheduler_failures(tmp_path, failed_command):
    helper, ident, directory = staged_helper(tmp_path)
    tag = "dm-" + ident
    write_json(directory / "submission-intent.json", {"owner": "lab", "tag": tag, "created_at": "2026-01-01T00:00:00+00:00"})

    def command(argv, **_):
        if argv[0] == failed_command:
            raise AdapterError("Scheduler service unavailable")
        return f"10|COMPLETED|0:0|2026-01-01T00:00:00|2026-01-01T00:01:00|2026-01-01T00:02:00|{tag}|lab|{tag}\n"

    helper.command = command
    with pytest.raises(AdapterError, match="Scheduler service unavailable"):
        helper.status({"run_id": ident, "jobs": [{"job_id": "10"}]})


def test_cancel_refuses_unverified_job_mapping(tmp_path):
    helper, ident, _ = staged_helper(tmp_path)
    helper.status = lambda _: []
    with pytest.raises(AdapterError, match="Cannot verify"):
        helper.cancel({"run_id": ident, "jobs": [{"job_id": "999"}]})


@pytest.mark.parametrize("unsquash", [False, True])
def test_stage_pins_source_inputs_adapter_and_refuses_mutation(tmp_path, unsquash):
    import base64
    import hashlib

    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        body = b"# source fixture; never executed\n"
        entry = tarfile.TarInfo("affinity/extract.py")
        entry.size = len(body)
        bundle.addfile(entry, io.BytesIO(body))
    structure, runtime, checkpoint = (tmp_path / name for name in ("demo.pdb", "runtime.sif", "checkpoint.pt"))
    for path in (structure, runtime, checkpoint):
        path.write_bytes(b"immutable fixture")
    checksum = hashlib.sha256(b"immutable fixture").hexdigest()
    source_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    run = {"id": str(uuid4()), "commit_sha": "a"*40, "config": {"seed": 0}, "body_hash": "b"*64, "delivery_id": "delivery", "hook_id": "hook", "resolved": {"cluster": {"runtime_image": str(runtime), "runtime_sha256": checksum, "checkpoint_path": str(checkpoint), "checkpoint_sha256": checksum, "account": "lab-account", "partition": "gpu"}, "dataset": {"entries": [{"path": str(structure), "sha256": checksum, "chains": "A"}]}}}
    run["resolved"]["cluster"]["runtime_unsquash"] = unsquash
    helper = Helper(tmp_path / "managed")
    payload = {"run": run, "source": {"commit_sha": "a"*40, "sha256": source_hash}, "archive_b64": base64.b64encode(archive.read_bytes()).decode(), "limits": {"min_free_bytes": 0}}
    stage = helper.stage(payload)
    assert helper.stage(payload) == stage
    assert stage["runtime_unsquash"] is unsquash
    directory = Path(stage["remote_dir"])
    assert "/run/inputs/demo.pdb A," in (directory / "input-list.txt").read_text()
    assert "--no-requeue" in (directory / "submit.sbatch").read_text()
    assert "--open-mode=append" in (directory / "submit.sbatch").read_text()
    assert read_json(directory / "request.json")["run"]["body_hash"] == run["body_hash"]
    (Path(stage["source_dir"]) / "extra.py").write_text("unexpected")
    with pytest.raises(AdapterError, match="inventory changed"):
        helper.stage(payload)


def test_duplicate_execution_claim_stops_before_loading_runtime(monkeypatch, tmp_path):
    from cluster_adapter import batch
    import sys

    directory = tmp_path / "run"
    (directory / "execution.claim").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["batch.py", str(directory), "10"])
    assert batch.main() == 73
    assert not (directory / "jobs").exists()


def batch_fixture(tmp_path, unsquash=None):
    from cluster_adapter.common import sha256_file

    directory = tmp_path / "run"
    (directory / "jobs").mkdir(parents=True)
    source = tmp_path / "source" / "tree"
    source.mkdir(parents=True)
    (source / "extract.py").write_text("# source fixture\n")
    write_json(source.parent / "manifest.json", {"files": {"extract.py": sha256_file(source / "extract.py")}})
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    write_json(adapter / "manifest.json", {})
    runtime, checkpoint = tmp_path / "runtime.sif", tmp_path / "checkpoint.pt"
    runtime.write_bytes(b"pinned SIF fixture")
    checkpoint.write_bytes(b"pinned checkpoint fixture")
    stage = {"source_dir": str(source), "adapter_dir": str(adapter), "runtime_image": str(runtime),
             "checkpoint_path": str(checkpoint), "provenance": {"runtime_sha256": sha256_file(runtime),
                                                                  "checkpoint_sha256": sha256_file(checkpoint)}}
    if unsquash is not None:
        stage["runtime_unsquash"] = unsquash
    write_json(directory / "stage.json", stage)
    write_json(directory / "inputs.json", [])
    return directory, runtime


@pytest.mark.parametrize("unsquash", [None, False, True])
def test_batch_uses_pinned_sif_with_optional_unpack_and_private_scratch(monkeypatch, tmp_path, unsquash):
    import os
    import sys
    from types import SimpleNamespace
    from cluster_adapter import batch

    directory, runtime = batch_fixture(tmp_path, unsquash)
    monkeypatch.setattr(sys, "argv", ["batch.py", str(directory), "10"])
    host_home = os.environ.get("HOME")
    monkeypatch.setenv("TMPDIR", "/shared/tmp")
    monkeypatch.setenv("APPTAINER_TMPDIR", "/shared/apptainer-tmp")
    monkeypatch.setenv("APPTAINER_CACHEDIR", "/shared/apptainer-cache")
    monkeypatch.setenv("APPTAINERENV_HOME", "/shared/container-home")
    scratch_root = tmp_path / "node-local"
    scratch_root.mkdir()
    monkeypatch.setenv("SLURM_TMPDIR", str(scratch_root))
    monkeypatch.setattr(batch.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024**3))
    calls = []

    def execute(argv, **options):
        calls.append((argv, options))
        scratch = Path(options["env"]["APPTAINER_TMPDIR"])
        assert scratch.is_dir()
        if unsquash:
            assert scratch.parent == scratch_root
            assert scratch.name.startswith("dmasif-run-10-")
            assert scratch.stat().st_mode & 0o777 == 0o700
            (scratch / "expanded-container-fixture").write_bytes(b"temporary only")
        return SimpleNamespace(returncode=0, stdout="apptainer version 1.1.9\n")

    monkeypatch.setattr(batch.subprocess, "run", execute)
    assert batch.main() == 0
    argv, options = calls[-1]
    assert ("--unsquash" in argv) is bool(unsquash)
    assert argv[-6] == str(runtime)
    assert "--containall" in argv and "--no-home" in argv
    for _, call_options in calls:
        environment = call_options["env"]
        assert environment["TMPDIR"] == environment["APPTAINER_TMPDIR"] == environment["SINGULARITY_TMPDIR"]
        if unsquash:
            assert not Path(environment["APPTAINER_TMPDIR"]).exists()
        else:
            assert environment["TMPDIR"] == str(directory / "jobs/10/tmp")
        assert environment["APPTAINER_CACHEDIR"] == str(directory / "jobs/10/apptainer_cache")
        assert environment.get("HOME") == host_home
        assert "APPTAINERENV_HOME" not in environment
    for variable in ("HOME", "TMPDIR", "KEOPS_CACHE_FOLDER", "XDG_CACHE_HOME", "TORCH_HOME", "TORCH_EXTENSIONS_DIR", "CUDA_CACHE_PATH"):
        assert any(arg.startswith(variable + "=/run/jobs/10/") for arg in argv)
    assert os.environ.get("HOME") == host_home
    assert os.environ["TMPDIR"] == "/shared/tmp"
    evidence = read_json(directory / "jobs/10/execution.json")
    assert evidence["runtime_unsquash"] is bool(unsquash)
    assert list(scratch_root.iterdir()) == []
    assert (directory / "jobs/10/tmp").is_dir()


def test_unpack_flag_does_not_allow_a_changed_sif(monkeypatch, tmp_path):
    import sys
    from cluster_adapter import batch

    directory, runtime = batch_fixture(tmp_path, True)
    runtime.write_bytes(b"mutated image")
    monkeypatch.setattr(sys, "argv", ["batch.py", str(directory), "10"])
    monkeypatch.setattr(batch.subprocess, "run", lambda *args, **kwargs: pytest.fail("Modified SIF must fail before Apptainer starts"))
    with pytest.raises(AdapterError, match="Pinned runtime/checkpoint changed"):
        batch.main()


@pytest.mark.parametrize("failure", ["nonzero", "exception"])
def test_unpack_scratch_is_removed_when_runtime_fails(monkeypatch, tmp_path, failure):
    import sys
    from types import SimpleNamespace
    from cluster_adapter import batch

    directory, _ = batch_fixture(tmp_path, True)
    root = tmp_path / "node-local"
    root.mkdir()
    sentinel = root / "other-job-data"
    sentinel.write_text("preserve")
    monkeypatch.setenv("SLURM_TMPDIR", str(root))
    monkeypatch.setattr(sys, "argv", ["batch.py", str(directory), "10"])
    monkeypatch.setattr(batch.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024**3))
    scratch_paths = []

    def execute(argv, **options):
        scratch = Path(options["env"]["APPTAINER_TMPDIR"])
        scratch_paths.append(scratch)
        (scratch / "temporary-image").write_text("unpacked bytes")
        if argv[1] == "--version":
            return SimpleNamespace(stdout="apptainer version 1.1.9\n", returncode=0)
        if failure == "exception":
            raise OSError("Runtime launch failed")
        return SimpleNamespace(returncode=9)

    monkeypatch.setattr(batch.subprocess, "run", execute)
    if failure == "exception":
        with pytest.raises(OSError, match="Runtime launch failed"):
            batch.main()
    else:
        assert batch.main() == 9
    assert scratch_paths and all(not path.exists() for path in scratch_paths)
    assert sentinel.read_text() == "preserve"
    assert (directory / "jobs/10/execution.json").is_file()
    assert (directory / "jobs/10/raw_features").is_dir()


def test_unpack_rejects_insufficient_space_before_creating_scratch(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from cluster_adapter import batch

    directory, runtime = batch_fixture(tmp_path, True)
    root = tmp_path / "node-local"
    root.mkdir()
    monkeypatch.setenv("SLURM_TMPDIR", str(root))
    monkeypatch.setattr(sys, "argv", ["batch.py", str(directory), "10"])
    floor = runtime.stat().st_size * 4 + 1024**3
    monkeypatch.setattr(batch.shutil, "disk_usage", lambda path: SimpleNamespace(free=floor - 1))
    monkeypatch.setattr(batch.subprocess, "run", lambda *args, **kwargs: pytest.fail("Low space must fail before Apptainer starts"))
    with pytest.raises(AdapterError, match="four times the SIF size plus 1 GiB"):
        batch.main()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("kind", ["relative", "missing", "file", "symlink"])
def test_unpack_rejects_unsafe_scratch_roots(monkeypatch, tmp_path, kind):
    from cluster_adapter import batch

    directory, runtime = batch_fixture(tmp_path, True)
    root = tmp_path / "scratch"
    if kind == "file":
        root.write_text("not a directory")
    elif kind == "symlink":
        root.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv("SLURM_TMPDIR", "relative/path" if kind == "relative" else str(root))
    with pytest.raises(AdapterError, match="existing absolute directory"):
        with batch._runtime_scratch(directory / "jobs/10", runtime, True):
            pytest.fail("Unsafe scratch must not be used")


def test_unpack_scratch_is_unique_for_each_allocation(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from cluster_adapter import batch

    directory, runtime = batch_fixture(tmp_path, True)
    root = tmp_path / "node-local"
    root.mkdir()
    monkeypatch.setenv("SLURM_TMPDIR", str(root))
    monkeypatch.setattr(batch.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024**3))
    with batch._runtime_scratch(directory / "jobs/10", runtime, True) as first:
        with batch._runtime_scratch(directory / "jobs/11", runtime, True) as second:
            assert first != second and first.parent == second.parent == root
            assert first.is_dir() and second.is_dir()
        assert first.is_dir() and not second.exists()
    assert not first.exists()


def test_unsquash_is_only_an_operator_boolean():
    from dmasif_console.config import ClusterConfig, ExperimentConfig
    from pydantic import ValidationError

    assert ClusterConfig().runtime_unsquash is False
    assert ClusterConfig(runtime_unsquash=True).public_snapshot()["runtime_unsquash"] is True
    with pytest.raises(ValidationError):
        ClusterConfig(runtime_unsquash="false")
    with pytest.raises(ValidationError):
        ExperimentConfig(dataset_id="demo", repeat_id="one", runtime_unsquash=True)


def test_reported_slurm_cluster_does_not_change_database_job_identity(tmp_path):
    helper, ident, _ = staged_helper(tmp_path)
    tag = "dm-" + ident
    import getpass
    def command(argv, **_):
        if argv[0] == "sbatch":
            return "123;sitecluster\n"
        if argv[0] == "sacct":
            return f"123|COMPLETED|0:0|2026-01-01T00:00:00|2026-01-01T00:01:00|2026-01-01T00:02:00|{tag}|{getpass.getuser()}|{tag}\n"
        return ""
    helper.command = command
    submitted = helper.submit({"run_id": ident})[0]
    polled = helper.status({"run_id": ident, "jobs": [submitted]})[0]
    assert submitted["reported_cluster"] == "sitecluster"
    assert (submitted["cluster"], submitted["job_id"]) == (polled["cluster"], polled["job_id"])


def test_publication_validates_retained_bytes_not_a_mutating_raw_file(monkeypatch, tmp_path):
    from cluster_adapter import validate

    job = tmp_path / "run" / "jobs" / "1"
    raw = job / "raw_features"
    raw.mkdir(parents=True)
    raw_file = raw / "result.npz"
    valid_npz(raw_file)
    original = raw_file.read_bytes()
    real_validator = validate.validate_npz
    def mutate_after_validation(path):
        dimensions = real_validator(path)
        raw_file.write_bytes(b"corrupted after validation")
        return dimensions
    monkeypatch.setattr(validate, "validate_npz", mutate_after_validation)
    result = publish(raw, job / "published", ["result.npz"], {}, job / "result-manifest.json")
    assert result["outcome"] == "SUCCEEDED"
    assert (job / "published" / "result.npz").read_bytes() == original
    assert result["artifacts"][0]["size"] == len(original)
    assert real_validator(job / "published" / "result.npz") == {"points": 3, "atoms": 2}
