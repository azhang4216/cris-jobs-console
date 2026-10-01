from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace
import subprocess
import tarfile
from uuid import uuid4

import pytest

from dmasif_console.transport import FakeAdapter, PreparationError, SSHAdapter, SubmissionUncertain


def fixture(tmp_path):
    settings = SimpleNamespace(state_dir=tmp_path / "state", mode="fake", max_source_bytes=1024*1024, max_input_bytes=1024*1024, max_artifact_bytes=1024*1024, min_free_bytes=0, log_tail_bytes=65536)
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        body = b"raise RuntimeError('This research source must never execute on the app host')\n"
        entry = tarfile.TarInfo("affinity/extract.py")
        entry.size = len(body)
        bundle.addfile(entry, io.BytesIO(body))
    input_path = tmp_path / "1STP.pdb"
    input_path.write_text("REMARK local fixture, not real science\n")
    source = {"archive_path": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "commit_sha": "a" * 40}
    run = {"id": str(uuid4()), "commit_sha": "a" * 40, "config": {"seed": 0}, "resolved": {"cluster": {}, "preset": {}, "dataset": {"entries": [{"path": str(input_path), "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest()}]}}, "hook_id": "hook", "delivery_id": "delivery", "body_hash": "b"*64}
    return settings, run, source


def test_simulation_survives_restart_preserves_source_and_validates_npz(tmp_path):
    settings, run, source = fixture(tmp_path)
    run.update(commit_author={"name": "Source Author", "email": "author@example.invalid"}, commit_committer={"name": "Source Committer"}, before_sha="c"*40, commit_url="https://github.com/lab/research/commit/" + "a"*40)
    adapter = FakeAdapter(settings)
    staged = adapter.stage(run, source)
    assert staged["provenance"]["simulation"]
    assert adapter.stage({**run, "state": "PREPARING"}, source) == staged
    original = adapter.submit(run)
    recovered = FakeAdapter(settings)
    assert recovered.submit(run) == original
    assert recovered.poll(run)[0]["state"] == "RUNNING"
    assert recovered.poll(run)[0]["state"] == "COMPLETED"
    result = recovered.results(run)
    assert result["outcome"] == "SUCCEEDED"
    assert result["expected_count"] == result["valid_count"] == 1
    destination = tmp_path / "download.npz"
    recovered.fetch_artifact(run, result["artifacts"][0], destination)
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == result["artifacts"][0]["sha256"]
    inventory = recovered.inventory()
    assert inventory[0]["body_hash"] == run["body_hash"]
    for field in ("commit_author", "commit_committer", "before_sha", "commit_url"):
        assert inventory[0][field] == run[field]
    assert inventory[0]["source"]["sha256"] == source["sha256"]
    assert "SIMULATION" in recovered.logs(run)


def test_same_code_dataset_distinct_run_never_share_outputs(tmp_path):
    settings, first, source = fixture(tmp_path)
    second = {**first, "id": str(uuid4())}
    adapter = FakeAdapter(settings)
    directories = []
    for run in (first, second):
        directories.append(adapter.stage(run, source)["remote_dir"])
        adapter.submit(run)
        adapter.poll(run)
        adapter.poll(run)
    assert directories[0] != directories[1]
    assert adapter.results(first)["artifacts"] != []
    assert adapter.results(second)["artifacts"] != []


def test_existing_input_integrity_checked_and_missing_receipt_not_retried(tmp_path):
    settings, run, source = fixture(tmp_path)
    adapter = FakeAdapter(settings)
    adapter.stage(run, source)
    directory = adapter.directory(run)
    (directory / "submit.claim").mkdir()
    with pytest.raises(SubmissionUncertain):
        adapter.submit(run)
    (directory / "inputs" / "1STP.pdb").write_text("changed")
    with pytest.raises(PreparationError, match="changed"):
        adapter.stage(run, source)


def test_ssh_uses_fixed_quoted_endpoint_strict_hosts_and_stdin(monkeypatch, tmp_path):
    settings, run, _ = fixture(tmp_path)
    (tmp_path / "key").write_bytes(b"synthetic test key")
    cluster = dict(host="login.example.invalid", user="researcher", root="/srv/lab/job_console", helper_path="/srv/lab/job_console/adapter/helper.py", ssh_key_path=tmp_path / "key", known_hosts_path=tmp_path / "known_hosts", ssh_timeout_seconds=5)
    settings.cluster = SimpleNamespace(**cluster)
    run["resolved"]["cluster"] = {k: v for k, v in cluster.items() if k not in {"ssh_key_path", "known_hosts_path"}}
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true,"result":[]}', stderr=b"private banner")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert SSHAdapter(settings).reconcile(run) == []
    argv, kwargs = calls[0]
    assert "StrictHostKeyChecking=yes" in argv
    assert "ForwardAgent=no" in argv
    assert "-F" in argv
    assert run["id"] not in argv[-1]
    assert run["id"].encode() in kwargs["input"]
    assert "shell" not in kwargs


def test_ssh_submit_timeout_is_uncertain_without_retry(monkeypatch, tmp_path):
    settings, run, _ = fixture(tmp_path)
    (tmp_path / "key").write_bytes(b"synthetic test key")
    cluster = dict(host="login.example.invalid", user="researcher", root="/srv/lab/job_console", helper_path="/srv/lab/helper.py", ssh_key_path=tmp_path / "key", known_hosts_path=tmp_path / "known_hosts")
    settings.cluster = SimpleNamespace(**cluster)
    run["resolved"]["cluster"] = {k: v for k, v in cluster.items() if k not in {"ssh_key_path", "known_hosts_path"}}
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 10)
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SubmissionUncertain):
        SSHAdapter(settings).submit(run)
    assert len(calls) == 1
