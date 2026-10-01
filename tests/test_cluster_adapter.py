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


def test_cancel_refuses_unverified_job_mapping(tmp_path):
    helper, ident, _ = staged_helper(tmp_path)
    helper.status = lambda _: []
    with pytest.raises(AdapterError, match="Cannot verify"):
        helper.cancel({"run_id": ident, "jobs": [{"job_id": "999"}]})


def test_stage_pins_source_inputs_adapter_and_refuses_mutation(tmp_path):
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
    helper = Helper(tmp_path / "managed")
    payload = {"run": run, "source": {"commit_sha": "a"*40, "sha256": source_hash}, "archive_b64": base64.b64encode(archive.read_bytes()).decode(), "limits": {"min_free_bytes": 0}}
    stage = helper.stage(payload)
    assert helper.stage(payload) == stage
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
