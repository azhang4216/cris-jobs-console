"""Read-only observation contracts; every scheduler response is synthetic."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from dmasif_console.config import Settings
from dmasif_console.db import Store
from dmasif_console.supervisor import run_service
from dmasif_console.web import create_app, public_run
from dmasif_console.worker import Worker


OBSERVED_AT = "2026-09-30T15:00:00+00:00"


def job(**changes):
    return {"job_id": "17", "job_name": "dmasif-extract", "state": "RUNNING",
            "submitted_at": "2026-09-30T13:00:00+00:00", "started_at": "2026-09-30T13:01:00+00:00",
            "ended_at": None, "exit_code": "0:0", **changes}


def snapshot(*jobs, observed_at=OBSERVED_AT):
    return {"jobs": list(jobs), "observed_at": observed_at}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        mode="observe", state_dir=tmp_path / "state",
        repository={"id": 42, "full_name": "lab/research", "clone_url": "https://github.com/lab/research.git"},
        allowed_actor_ids=[7],
        cluster={"host": "private-login.invalid", "user": "private-user", "account": "private-account",
                 "root": "/private/cluster", "helper_path": "/private/cluster/helper.py",
                 "ssh_key_path": tmp_path / "synthetic-key", "known_hosts_path": tmp_path / "synthetic-hosts"},
    )


def test_observation_cannot_enable_submission(settings):
    data = settings.model_dump(mode="python")
    data["submissions_enabled"] = True
    with pytest.raises(ValueError, match="(?i)observ"):
        Settings.model_validate(data)


@pytest.mark.parametrize("field", ["ssh_key_path", "known_hosts_path"])
def test_observation_requires_explicit_ssh_files(settings, field):
    data = settings.model_dump(mode="python")
    data["cluster"][field] = None
    with pytest.raises(ValueError):
        Settings.model_validate(data)


def test_submission_worker_rejects_observation_before_touching_state(settings, monkeypatch):
    monkeypatch.setattr("dmasif_console.worker.get_adapter", lambda _: pytest.fail("submission adapter created"))
    with pytest.raises(ValueError, match="(?i)observ"):
        Worker(settings)
    assert not settings.state_dir.exists()


def test_observation_web_needs_no_webhook_secret_and_never_starts_remote_work(settings, monkeypatch):
    assert settings.webhook_secret == ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("browser request started a process"))
    monkeypatch.setattr("dmasif_console.worker.snapshot_source", lambda *a, **k: pytest.fail("research source fetched"))
    with TestClient(create_app(settings)) as client:
        for _ in range(3):
            assert client.get("/").status_code == 200
            response = client.get("/api/runs")
            assert response.status_code == 200
            assert response.json()["runs"] == []
            assert response.json()["control"] == {"paused": False, "reason": ""}
        assert client.get("/healthz").status_code == 200


def test_even_valid_signed_push_cannot_create_records_in_observation_mode(settings):
    settings.webhook_secret = "synthetic-secret-long-enough"
    body = json.dumps({"repository": {"id": 42}, "sender": {"id": 7, "login": "alice", "type": "User"},
                       "ref": "refs/heads/runs/test", "after": "a" * 40, "deleted": False}).encode()
    signature = hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    with TestClient(create_app(settings)) as client:
        response = client.post("/webhooks/github", content=body, headers={
            "X-GitHub-Event": "push", "X-GitHub-Delivery": "synthetic-push",
            "X-Hub-Signature-256": "sha256=" + signature,
        })
        assert response.status_code in {403, 503}
    store = Store(settings.database_path)
    assert store.list_runs() == []
    assert store.deliveries() == []


def test_supervisor_starts_observer_without_webhook_secret(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("DMASIF_MAINTENANCE", "0")
    monkeypatch.setattr("dmasif_console.supervisor.load_settings", lambda _: settings)
    commands_seen = {}

    def capture(commands, status_path, *, env):
        commands_seen.update(commands)
        return 0

    monkeypatch.setattr("dmasif_console.supervisor.supervise", capture)
    previous_umask = os.umask(0o077)
    try:
        assert run_service(str(tmp_path / "config.yaml")) == 0
    finally:
        os.umask(previous_umask)
    assert set(commands_seen) == {"web", "observer"}
    assert "observe" in commands_seen["observer"]
    assert all("worker" not in command for command in commands_seen.values())


def test_unknown_observed_provenance_never_creates_git_identity_or_commit_link(settings):
    run = public_run({"id": "observed-17", "source_kind": "slurm", "state": "COMPLETED",
                      "scheduler_job_id": "17"}, settings)
    assert run["actor_login"] is None
    assert run["actor_id"] is None
    assert run["commit_sha"] is None
    assert run["commit_url"] is None
    assert run["source_kind"] == "slurm"
    assert run["state"] == "COMPLETED"
    assert run["validation"].get("outcome") != "SUCCEEDED"
    assert run["artifacts"] == []


def test_dashboard_refresh_interval_is_independent_of_cluster_polling(settings):
    assert settings.poll_seconds == 30
    assert settings.dashboard_poll_seconds == 15
    settings.poll_seconds = 120
    with TestClient(create_app(settings)) as client:
        html = client.get("/").text
        assert 'data-poll-seconds="15"' in html
        assert 'data-poll-seconds="120"' not in html


def test_repeat_observations_update_one_run_and_do_not_fabricate_results(settings):
    from dmasif_console.observer import Observer

    response = snapshot(job())
    observer = Observer(settings, fetch=lambda: response)
    assert observer.tick() is True
    store = Store(settings.database_path)
    original = store.list_runs()[0]
    assert original["state"] == "RUNNING"
    assert not original["capacity_reserved"]

    response = snapshot(job(state="COMPLETED", ended_at="2026-09-30T14:00:00+00:00"),
                        observed_at="2026-09-30T15:01:00+00:00")
    assert observer.tick() is True
    assert observer.tick() is True
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["id"] == original["id"]
    assert runs[0]["state"] == "COMPLETED"
    assert not runs[0].get("artifacts")
    assert not runs[0].get("commit_sha")
    assert not runs[0].get("actor_login")
    assert runs[0]["source_kind"] == "slurm"
    assert not runs[0].get("source")
    assert not store.deliveries()
    assert store.observation_state()["job_count"] == 1


def test_scheduler_id_reuse_with_different_submit_time_preserves_both_runs(settings):
    from dmasif_console.observer import Observer

    response = snapshot(job(state="COMPLETED", ended_at="2026-09-30T14:00:00+00:00"))
    observer = Observer(settings, fetch=lambda: response)
    observer.tick()
    response = snapshot(job(submitted_at="2026-09-30T14:30:00+00:00", started_at="2026-09-30T14:31:00+00:00"))
    observer.tick()
    runs = Store(settings.database_path).list_runs()
    assert len(runs) == 2
    assert len({run["id"] for run in runs}) == 2
    assert {run["state"] for run in runs} == {"RUNNING", "COMPLETED"}


def test_observation_filters_unrelated_jobs_before_saving_or_validating(settings):
    from dmasif_console.observer import Observer

    response = snapshot(job(), {"job_name": "unrelated-job", "job_id": "99", "submitted_at": "malformed"})
    assert Observer(settings, fetch=lambda: response).tick() is True
    store = Store(settings.database_path)
    assert len(store.list_runs()) == 1
    assert store.observation_state()["job_count"] == 1
    assert "unrelated" not in json.dumps(store.list_runs())


def test_fetch_failure_preserves_cached_jobs_and_marks_observation_stale_without_secrets(settings):
    from dmasif_console.observer import Observer

    observer = Observer(settings, fetch=lambda: snapshot(job()))
    assert observer.tick() is True
    store = Store(settings.database_path)
    original = store.list_runs()

    def fail():
        raise RuntimeError("private-user@private-login.invalid /private/cluster SECRET-SSH-DETAILS")

    observer = Observer(settings, fetch=fail)
    assert observer.tick() is False
    assert store.list_runs() == original
    state = store.observation_state()
    assert state["observed_at"] == OBSERVED_AT
    assert state["last_attempt_at"]
    assert state["error"]
    assert state["job_count"] == 1
    for value in ("private-user", "private-login.invalid", "/private/cluster", "SECRET-SSH-DETAILS"):
        assert value not in json.dumps(state)
    with TestClient(create_app(settings)) as client:
        for path in ("/api/runs", "/api/runs/" + original[0]["id"]):
            response = client.get(path)
            assert response.status_code == 200
            observation = response.json()["observation"]
            assert observation["observed_at"] == OBSERVED_AT
            assert observation["error"]
            assert "SECRET-SSH-DETAILS" not in response.text


def test_malformed_scheduler_identity_cannot_replace_cached_observations(settings):
    from dmasif_console.observer import Observer

    response = snapshot(job())
    observer = Observer(settings, fetch=lambda: response)
    assert observer.tick() is True
    original = Store(settings.database_path).list_runs()
    # A malformed second row must not leave a partially refreshed first row.
    response = snapshot(job(state="COMPLETED", ended_at="2026-09-30T14:00:00+00:00"),
                        job(job_id="18", submitted_at="Unknown"))
    assert observer.tick() is False
    store = Store(settings.database_path)
    assert store.list_runs() == original
    assert store.observation_state()["error"]


def test_observed_detail_and_logs_only_read_cached_data(settings, monkeypatch):
    from dmasif_console.observer import Observer

    assert Observer(settings, fetch=lambda: snapshot(job())).tick() is True
    run_id = Store(settings.database_path).list_runs()[0]["id"]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("viewing spawned a remote process"))
    with TestClient(create_app(settings)) as client:
        for path in ("/", "/api/runs", "/runs/" + run_id, "/api/runs/" + run_id,
                     "/api/runs/" + run_id + "/logs"):
            assert client.get(path).status_code == 200
        data = client.get("/api/runs/" + run_id).json()
        assert data["run"]["commit_url"] is None
        assert data["run"]["actor_login"] is None
        assert data["observation"]["observed_at"] == OBSERVED_AT
        assert "private-login.invalid" not in json.dumps(data)


def test_observer_uses_exclusive_worker_lock(settings):
    from dmasif_console.observer import Observer

    with Observer(settings, fetch=lambda: snapshot()):
        with pytest.raises(RuntimeError, match="(?i)(another|lock)"):
            with Observer(settings, fetch=lambda: snapshot()):
                pytest.fail("second observer acquired the same state directory")


def test_observation_transport_is_fixed_read_only_pinned_ssh_with_private_temporary_key(settings, monkeypatch):
    from dmasif_console.observer import fetch_snapshot

    settings.cluster.ssh_key_path.write_bytes(b"synthetic key, never used for SSH")
    seen = {}

    def fake_read(argv, script, timeout):
        seen.update(argv=argv, script=script)
        assert argv[0] == "ssh"
        assert argv[-1] == "python3 -B -s"
        for option in ("StrictHostKeyChecking=yes", "IdentitiesOnly=yes", "BatchMode=yes", "ForwardAgent=no"):
            assert option in argv
        assert argv[argv.index("-F") + 1] == "/dev/null"
        assert "UserKnownHostsFile=" + str(settings.cluster.known_hosts_path) in argv
        key = Path(argv[argv.index("-i") + 1])
        assert key != settings.cluster.ssh_key_path
        assert key.stat().st_mode & 0o777 == 0o600
        assert key.read_bytes() == b"synthetic key, never used for SSH"
        assert b'"sacct"' in script and b'"squeue"' in script
        assert b'"--user", user' in script
        for forbidden in (b"sbatch", b"scancel", b"srun", b"git clone", b"affinity.extract"):
            assert forbidden not in script
        return json.dumps({"ok": True, "result": snapshot()}).encode()

    monkeypatch.setattr("dmasif_console.observer._ssh_read", fake_read)
    assert fetch_snapshot(settings) == snapshot()
    assert not Path(seen["argv"][seen["argv"].index("-i") + 1]).exists()
    assert settings.cluster.ssh_key_path.exists()


def test_observation_transport_bounds_remote_output_without_starting_ssh():
    from dmasif_console.observer import MAX_RESPONSE_BYTES, _ssh_read

    # A local synthetic process stands in for SSH and exceeds the response cap.
    command = [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'x' * {MAX_RESPONSE_BYTES + 1})"]
    with pytest.raises(ValueError, match="too large"):
        _ssh_read(command, b"", 5)
