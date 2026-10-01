import hashlib
from pathlib import Path

import pytest

from dmasif_console.config import Settings
from dmasif_console.db import Store
from dmasif_console.worker import Worker
from dmasif_console.transport import SubmissionUncertain, TransportError
from dmasif_console.sources import ExperimentConfigError


class Adapter:
    def __init__(self):
        self.submit_count = 0
        self.stage_count = 0
        self.ambiguous = False
        self.known = True
        self.fail_poll = False
        self.download_fails = False
        self.payload = b"validated-fixture"
        self.job = {"job_id": "17", "cluster": "fake", "owner": "test", "state": "PENDING", "terminal": False}
        self.result = {"outcome": "SUCCEEDED", "expected_count": 1, "valid_count": 1,
                       "artifacts": [{"name": "1STP.npz", "sha256": hashlib.sha256(self.payload).hexdigest(), "size": len(self.payload)}]}

    def stage(self, run, source):
        self.stage_count += 1
        return {"remote_dir": f"/fake/runs/{run['id']}"}

    def submit(self, run):
        self.submit_count += 1
        if self.ambiguous:
            raise SubmissionUncertain("Connection lost after acceptance")
        return [dict(self.job)]

    def reconcile(self, run):
        return [dict(self.job)] if self.known else []

    def poll(self, run):
        if self.fail_poll:
            raise TransportError("unavailable")
        return [dict(self.job)]

    def logs(self, run):
        return "fixture log\n"

    def results(self, run):
        return self.result

    def fetch_artifact(self, run, artifact, destination):
        if self.download_fails:
            raise TransportError("unavailable")
        Path(destination).write_bytes(self.payload)

    def complete(self, state="COMPLETED", exit_code="0:0"):
        self.job.update(state=state, terminal=True, exit_code=exit_code, ended_at="2026-01-01T00:01:00+00:00")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = Settings(state_dir=tmp_path / "state", repository={"id": 1, "full_name": "lab/repo", "clone_url": str(tmp_path)},
                        allowed_actor_ids=[1], submissions_enabled=True, min_free_bytes=0,
                        datasets={"demo": {"entries": [{"path": "/fake/1STP.pdb", "sha256": "0"*64}]}})
    store = Store(settings.database_path)
    store.initialize()
    adapter = Adapter()
    monkeypatch.setattr("dmasif_console.worker.snapshot_source", lambda settings, sha: {"commit_sha": sha, "sha256": "a"*64, "archive_path": "/fake/source.tar.gz", "source_dir": "/fake/source"})
    monkeypatch.setattr("dmasif_console.worker.read_experiment", lambda source: ({"schema_version": 1, "job_type": "dmasif_extract", "dataset_id": "demo", "preset_id": "quick-test", "seed": 0, "repeat_id": "one"}, "fixture"))
    worker = Worker(settings, store, adapter)
    return settings, store, adapter, worker


def enqueue(settings, store, delivery="one"):
    response = store.accept_delivery("hook", delivery, hashlib.sha256(delivery.encode()).hexdigest(), {}, "ACCEPTED", "", {
        "commit_sha": "a"*40, "actor_login": "researcher", "policy": settings.policy_snapshot(), "state": "CHECKING_REQUEST"})
    return response["run_id"]


def test_submission_unknown_reconciles_after_restart_without_resubmitting(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    adapter.ambiguous = True
    worker.tick()
    assert store.get_run(run_id)["state"] == "SUBMISSION_UNKNOWN"
    assert store.get_run(run_id)["capacity_reserved"] is True
    restarted = Worker(settings, store, adapter)
    restarted.tick()
    assert adapter.submit_count == 1
    assert store.get_run(run_id)["state"] == "QUEUED"


def test_rejected_config_keeps_source_and_private_original_without_submitting(setup, monkeypatch):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    errors = [{"sample": "experiments/run.yaml.seed", "reason": "Must be an integer from 0 to 4294967295."}]

    def reject(source):
        raise ExperimentConfigError(errors, "seed: private-value\n")

    monkeypatch.setattr("dmasif_console.worker.read_experiment", reject)
    worker.tick()
    run = store.get_run(run_id)
    assert run["state"] == "REJECTED"
    assert run["source"]["commit_sha"] == "a" * 40
    assert run["source"]["sha256"] == "a" * 64
    assert run["original_config"] == "seed: private-value\n"
    assert run["validation"] == {"outcome": "REJECTED", "errors": errors}
    assert "private-value" not in run["reason"]
    assert not run["capacity_reserved"]
    assert adapter.stage_count == adapter.submit_count == 0
    from dmasif_console.web import public_run
    public = public_run(run, settings)
    assert "private-value" not in str(public)
    assert public["provenance"]["source_sha256"] == "a" * 64


def test_unknown_dataset_keeps_validated_config_and_explains_rejection(setup, monkeypatch):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    config = {"schema_version": 1, "job_type": "dmasif_extract", "dataset_id": "unregistered", "preset_id": "quick-test", "seed": 0, "repeat_id": "one"}
    monkeypatch.setattr("dmasif_console.worker.read_experiment", lambda source: (config, "dataset_id: unregistered\n"))
    worker.tick()
    run = store.get_run(run_id)
    assert run["state"] == "REJECTED"
    assert run["config"] == config
    assert "Unknown dataset_id" in run["reason"]
    assert "registered ID" in run["reason"]
    assert run["validation"]["errors"][0]["sample"] == "experiments/run.yaml.dataset_id"
    assert adapter.stage_count == adapter.submit_count == 0


def test_ambiguous_submission_holds_capacity_for_later_push(setup):
    settings, store, adapter, worker = setup
    first, second = enqueue(settings, store), enqueue(settings, store, "two")
    adapter.ambiguous, adapter.known = True, False
    worker.tick()
    worker.tick()
    assert adapter.submit_count == 1
    assert store.get_run(first)["capacity_reserved"] is True
    assert store.get_run(second)["state"] == "WAITING_FOR_CAPACITY"


def test_worker_restart_from_submitting_does_not_submit(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    store.update_run(run_id, {"state": "SUBMITTING", "capacity_reserved": True})
    worker.tick()
    assert adapter.submit_count == 0
    assert store.get_run(run_id)["state"] == "QUEUED"


def test_zero_exit_with_no_outputs_is_failed(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    worker.tick()
    adapter.complete()
    adapter.result = {"outcome": "FAILED", "expected_count": 1, "valid_count": 0, "artifacts": []}
    worker.tick()
    assert store.get_run(run_id)["state"] == "FAILED"
    assert not store.get_run(run_id)["capacity_reserved"]


def test_nonzero_scheduler_outcome_cannot_be_success(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    worker.tick()
    adapter.complete("FAILED", "1:0")
    worker.tick()
    assert store.get_run(run_id)["state"] == "FAILED"


def test_download_failure_does_not_change_scientific_success(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    worker.tick()
    adapter.complete()
    adapter.download_fails = True
    worker.tick()
    run = store.get_run(run_id)
    assert run["state"] == "SUCCEEDED"
    assert run["artifacts"][0]["cache_state"] == "unavailable"


def test_missing_result_manifest_releases_slot_after_bounded_checks(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    worker.tick()
    adapter.complete()
    adapter.result = None
    for _ in range(3):
        worker.tick()
    run = store.get_run(run_id)
    assert run["state"] == "NEEDS_REVIEW"
    assert not run["capacity_reserved"]


def test_pause_keeps_monitoring_and_does_not_submit_waiting_run(setup):
    settings, store, adapter, worker = setup
    first, second = enqueue(settings, store), enqueue(settings, store, "two")
    worker.tick()
    store.set_paused(True, "operator pause")
    adapter.complete()
    worker.tick()
    assert store.get_run(first)["state"] == "SUCCEEDED"
    assert store.get_run(second)["state"] == "WAITING_FOR_CAPACITY"
    assert adapter.submit_count == 1


def test_monitoring_failure_preserves_known_state(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    worker.tick()
    adapter.fail_poll = True
    worker.tick()
    run = store.get_run(run_id)
    assert run["state"] == "QUEUED"
    assert run["monitor_error"]
    assert run["last_checked_at"]


def test_single_worker_lock(setup):
    settings, store, adapter, worker = setup
    with worker:
        with pytest.raises(RuntimeError, match="Another worker"):
            with Worker(settings, store, adapter):
                pass


def test_restore_marker_prevents_new_submission(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    (settings.state_dir / "restore-pending.json").write_text("{}")
    worker.tick()
    assert adapter.submit_count == 0
    assert store.get_run(run_id)["state"] == "WAITING_FOR_CAPACITY"


def test_real_local_demo_runs_two_researchers_with_isolated_outputs(tmp_path, monkeypatch):
    from dmasif_console.config import load_settings
    from dmasif_console.demo import prepare_demo
    config = prepare_demo(tmp_path / "demo")
    monkeypatch.setenv("DMASIF_WEBHOOK_SECRET_FILE", str(config.parent / "webhook-secret"))
    assert not (config.parent / "viewer-password").exists()
    monkeypatch.delenv("DMASIF_VIEWER_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("DMASIF_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("DMASIF_VIEWER_PASSWORD", raising=False)
    settings = load_settings(config)
    with Worker(settings) as worker:
        for _ in range(7):
            worker.tick()
        runs = worker.store.list_runs()
    assert len(runs) == 2
    assert {run["actor_login"] for run in runs} == {"demo-alice", "demo-bob"}
    assert all(run["state"] == "SUCCEEDED" for run in runs)
    assert len({run["remote_dir"] for run in runs}) == 2
    assert len({run["source"]["commit_sha"] for run in runs}) == 2
    for run in runs:
        artifact = run["artifacts"][0]
        assert artifact["name"] == "1STP.npz"
        assert artifact["cache_state"] == "cached"
        cached = settings.state_dir / artifact["cache_path"]
        assert hashlib.sha256(cached.read_bytes()).hexdigest() == artifact["sha256"]


def test_restore_recovers_newer_remote_runs_and_delivery_dedup(tmp_path, monkeypatch):
    from dmasif_console.cli import _backup, _restore
    from dmasif_console.config import load_settings
    from dmasif_console.demo import prepare_demo
    from dmasif_console.transport import FakeAdapter
    config = prepare_demo(tmp_path / "demo")
    settings = load_settings(config)
    remote = FakeAdapter(settings)
    original_store = Store(settings.database_path)
    backup = tmp_path / "backup"
    _backup(settings, backup)
    # Advance jobs after the backup was taken, then accept a further run.
    worker = Worker(settings, original_store, remote)
    first = original_store.list_runs()[-1]
    recovered = original_store.accept_delivery("hook", "newer", "d"*64, {}, "ACCEPTED", "", {
        "commit_sha": first["commit_sha"], "actor_login": "demo-alice", "actor_id": 101,
        "policy": settings.policy_snapshot(), "state": "CHECKING_REQUEST"})
    for _ in range(10):
        worker.tick()
    assert all(run["state"] == "SUCCEEDED" for run in original_store.list_runs())
    restored_settings = settings.model_copy(update={"state_dir": tmp_path / "restored"})
    monkeypatch.setattr("dmasif_console.worker.get_adapter", lambda settings: remote)
    _restore(restored_settings, backup, "operator", "exercise restore")
    restored = Store(restored_settings.database_path)
    assert restored.control_state()["paused"] is True
    assert not (restored_settings.state_dir / "restore-pending.json").exists()
    assert len(restored.list_runs()) == 3
    duplicate = restored.accept_delivery("hook", "new-header", "d"*64, {}, "ACCEPTED", "", {"state": "CHECKING_REQUEST"})
    assert duplicate["duplicate"] is True
    assert duplicate["run_id"] == recovered["run_id"]


def test_download_recovers_after_worker_dies_following_scientific_success(setup, monkeypatch):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    worker.tick()
    adapter.complete()
    # Simulate process death just after the durable science outcome/manifest.
    monkeypatch.setattr(worker, "_cache_artifacts", lambda *args: None)
    worker.tick()
    assert store.get_run(run_id)["state"] == "SUCCEEDED"
    assert store.get_run(run_id)["artifacts"][0]["cache_state"] == "pending"
    restarted = Worker(settings, store, adapter)
    restarted.tick()
    assert store.get_run(run_id)["artifacts"][0]["cache_state"] == "cached"
    assert adapter.submit_count == 1


def test_restore_reconciles_terminal_history_and_one_live_job(tmp_path, monkeypatch):
    from dmasif_console.cli import _backup, _restore
    from dmasif_console.config import load_settings
    from dmasif_console.demo import prepare_demo
    from dmasif_console.transport import FakeAdapter
    config = prepare_demo(tmp_path / "demo")
    settings = load_settings(config)
    remote = FakeAdapter(settings)
    store = Store(settings.database_path)
    backup = tmp_path / "backup"
    _backup(settings, backup)
    worker = Worker(settings, store, remote)
    # First run finishes and the second is submitted but remains live.
    for _ in range(3):
        worker.tick()
    assert {run["state"] for run in store.list_runs()} == {"SUCCEEDED", "QUEUED"}
    restored_settings = settings.model_copy(update={"state_dir": tmp_path / "restored"})
    monkeypatch.setattr("dmasif_console.worker.get_adapter", lambda settings: remote)
    _restore(restored_settings, backup, "operator", "live recovery")
    recovered = Store(restored_settings.database_path)
    runs = recovered.list_runs()
    assert len([run for run in runs if run["capacity_reserved"]]) == 1
    assert len([run for run in runs if run["state"] == "SUCCEEDED"]) == 1
    assert recovered.control_state()["paused"] is True
    assert not (restored_settings.state_dir / "restore-pending.json").exists()


def test_resolution_requires_explicit_attestation_and_unknown_state(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    with pytest.raises(ValueError, match="Only a submission-unknown"):
        worker.resolve(run_id, "operator", "verified", "site ticket 123", confirm_no_live_job=True)
    store.update_run(run_id, {"state": "SUBMISSION_UNKNOWN", "capacity_reserved": True})
    with pytest.raises(ValueError, match="confirm-no-live-job"):
        worker.resolve(run_id, "operator", "verified", "site ticket 123")
    assert store.get_run(run_id)["capacity_reserved"] is True


def test_resolution_refuses_verified_live_jobs(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    store.update_run(run_id, {"state": "SUBMISSION_UNKNOWN", "capacity_reserved": True})
    with pytest.raises(ValueError, match="associated job is live"):
        worker.resolve(run_id, "operator", "investigated", "site ticket 123", confirm_no_live_job=True)
    assert store.get_run(run_id)["state"] == "SUBMISSION_UNKNOWN"
    assert store.get_run(run_id)["capacity_reserved"] is True
    assert adapter.submit_count == 0


def test_resolution_refuses_unavailable_scheduler(setup, monkeypatch):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    store.update_run(run_id, {"state": "NEEDS_REVIEW", "capacity_reserved": True})
    def unavailable(_):
        raise TransportError("unavailable")
    monkeypatch.setattr(adapter, "reconcile", unavailable)
    with pytest.raises(TransportError):
        worker.resolve(run_id, "operator", "investigated", "site ticket 123", confirm_no_live_job=True)
    assert store.get_run(run_id)["capacity_reserved"] is True


def test_resolution_uses_private_evidence_without_resubmission(setup):
    from fastapi.testclient import TestClient
    from dmasif_console.web import create_app
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    store.update_run(run_id, {"state": "SUBMISSION_UNKNOWN", "capacity_reserved": True})
    adapter.known = False
    worker.resolve(run_id, "operator", "private-investigation-details", "private-ticket-reference", confirm_no_live_job=True)
    run = store.get_run(run_id)
    assert run["state"] == "FAILED"
    assert run["capacity_reserved"] is False
    assert run["operator_resolution"]["evidence"] == "private-ticket-reference"
    assert adapter.submit_count == 0
    events = store.events(run_id)
    assert any(event["detail"].get("private_attestation", {}).get("evidence") == "private-ticket-reference" for event in events)
    settings = settings.model_copy(update={"webhook_secret": "webhook-secret-value-123"})
    client = TestClient(create_app(settings))
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert "private-ticket-reference" not in response.text
    assert "private-investigation-details" not in response.text


def test_resolution_accepts_verified_terminal_failure_without_fabricating_success(setup):
    settings, store, adapter, worker = setup
    run_id = enqueue(settings, store)
    store.update_run(run_id, {"state": "NEEDS_REVIEW", "capacity_reserved": True})
    adapter.complete()
    worker.resolve(run_id, "operator", "manifest unrecoverable", "site ticket 123", confirm_no_live_job=True)
    assert store.get_run(run_id)["state"] == "FAILED"
    assert not store.get_run(run_id)["capacity_reserved"]
    assert store.jobs(run_id)[0]["state"] == "COMPLETED"
    assert adapter.submit_count == 0


def test_restore_accepts_audited_resolution_after_rechecking_scheduler(tmp_path, monkeypatch):
    from dmasif_console.cli import _restore
    from dmasif_console.config import load_settings
    from dmasif_console.demo import prepare_demo
    from dmasif_console.transport import FakeAdapter
    config = prepare_demo(tmp_path / "demo")
    settings = load_settings(config)
    remote = FakeAdapter(settings)
    def lost_before_submit(run):
        (remote.directory(run) / "submit.claim").mkdir()
        raise SubmissionUncertain("simulate helper termination before submission")
    monkeypatch.setattr(remote, "submit", lost_before_submit)
    monkeypatch.setattr("dmasif_console.worker.get_adapter", lambda settings: remote)
    worker = Worker(settings, adapter=remote)
    worker.tick()
    run = worker.store.list_runs(states=["SUBMISSION_UNKNOWN"])[0]
    with pytest.raises(ValueError, match="ambiguous"):
        _restore(settings, None, "operator", "exercise unresolved restore")
    with Worker(settings) as operator:
        operator.resolve(run["id"], "operator", "helper confirmed stopped", "site ticket 456", confirm_no_live_job=True)
    _restore(settings, None, "operator", "resume resolved restore")
    recovered = worker.store.get_run(run["id"])
    assert recovered["state"] == "FAILED"
    assert not recovered["capacity_reserved"]
    assert (remote.directory(run) / "submit.claim").is_dir()
    assert not (remote.directory(run) / "submission-receipt.json").exists()
    assert not (settings.state_dir / "restore-pending.json").exists()
    assert worker.store.control_state()["paused"] is True
