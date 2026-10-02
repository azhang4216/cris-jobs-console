"""Exercise repeated branch pushes through the real local orchestration path."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import pytest

from cluster_adapter.validate import validate_npz
from dmasif_console.config import Settings
from dmasif_console.web import create_app
from dmasif_console.worker import Worker


def git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments],
        cwd=repository, stderr=subprocess.DEVNULL, text=True,
    ).strip()


def commit_experiment(repository: Path, revision: str, seed: int) -> str:
    (repository / "affinity/extract.py").write_text(
        f"# Revision {revision}; fake mode must never execute this source.\n"
        "raise RuntimeError('Synthetic orchestration test only')\n"
    )
    (repository / "experiments/run.yaml").write_text(
        "schema_version: 1\njob_type: dmasif_extract\ndataset_id: demo\n"
        f"preset_id: quick-test\nseed: {seed}\nrepeat_id: unchanged-label\n"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-qm", revision)
    return git(repository, "rev-parse", "HEAD")


def push(client: TestClient, settings: Settings, payload: dict, delivery: str) -> dict:
    body = json.dumps(payload, sort_keys=True).encode()
    signature = hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    response = client.post("/webhooks/github", content=body, headers={
        "Content-Type": "application/json", "X-GitHub-Event": "push",
        "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": "sha256=" + signature,
    })
    assert response.status_code == 202
    return response.json()


@pytest.mark.parametrize("force_push", [False, True], ids=["branch-advance", "force-push"])
def test_same_branch_new_commits_keep_both_runs_sources_and_outputs(tmp_path, force_push):
    repository = tmp_path / "research"
    (repository / "affinity").mkdir(parents=True)
    (repository / "experiments").mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Test Researcher")
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "checkout", "-b", "runs/same-experiment")
    base_sha = commit_experiment(repository, "base", 0)
    first_sha = commit_experiment(repository, "first", 0)
    structure = tmp_path / "1STP.pdb"
    structure.write_text("REMARK Synthetic test fixture; not a scientific structure\nEND\n")
    settings = Settings(
        mode="fake", state_dir=tmp_path / "state", submissions_enabled=True, min_free_bytes=0,
        repository={"id": 42, "full_name": "lab/research", "clone_url": str(repository)},
        allowed_actor_ids=[7], webhook_secret="same-branch-test-webhook-secret",
        datasets={"demo": {"entries": [{"path": str(structure), "sha256": hashlib.sha256(structure.read_bytes()).hexdigest()}]}},
    )
    first_payload = {
        "repository": {"id": 42}, "sender": {"id": 7, "login": "alice", "type": "User"},
        "ref": "refs/heads/runs/same-experiment", "before": "0" * 40, "after": first_sha,
        "created": True, "deleted": False, "forced": False,
    }
    with TestClient(create_app(settings)) as client, Worker(settings) as worker:
        first_response = push(client, settings, first_payload, "first-push")
        first_id = first_response["run_id"]
        worker.tick()
        assert worker.store.get_run(first_id)["state"] == "QUEUED"
        worker.tick()
        assert worker.store.get_run(first_id)["state"] == "RUNNING"

        # Move the same branch while its previous run is in progress. The
        # force-push variant replaces history, rather than advancing from A.
        if force_push:
            git(repository, "reset", "--hard", base_sha)
        second_sha = commit_experiment(repository, "second", 1)
        ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", first_sha, second_sha],
                                  cwd=repository, check=False, capture_output=True).returncode
        assert ancestry == (1 if force_push else 0)
        second_payload = {**first_payload, "before": first_sha, "after": second_sha,
                          "created": False, "forced": force_push}
        second_response = push(client, settings, second_payload, "second-push")
        second_id = second_response["run_id"]
        assert first_id != second_id
        worker.tick()
        first = worker.store.get_run(first_id)
        assert first["state"] == "SUCCEEDED"
        assert worker.store.get_run(second_id)["state"] == "QUEUED"
        first_artifact = first["artifacts"][0]
        first_cached = settings.state_dir / first_artifact["cache_path"]
        first_retained = Path(first["remote_dir"]) / first_artifact["relative_path"]
        original_bytes = first_retained.read_bytes()
        original_modified = first_retained.stat().st_mtime_ns

        # GitHub redeliveries must neither create a third run nor re-execute
        # one, including a replay with a different delivery header.
        replay = push(client, settings, first_payload, "redelivery-new-header")
        repeated = push(client, settings, second_payload, "second-push")
        assert replay["duplicate"] and replay["run_id"] == first_id
        assert repeated["duplicate"] and repeated["run_id"] == second_id
        worker.tick()
        assert worker.store.get_run(second_id)["state"] == "RUNNING"
        worker.tick()
        second = worker.store.get_run(second_id)
        assert second["state"] == "SUCCEEDED"

        runs = worker.store.list_runs()
        assert len(runs) == 2
        assert {run["branch"] for run in runs} == {"runs/same-experiment"}
        assert {run["config"]["repeat_id"] for run in runs} == {"unchanged-label"}
        assert {run["commit_sha"] for run in runs} == {first_sha, second_sha}
        assert {run["config"]["seed"] for run in runs} == {0, 1}
        assert len({run["remote_dir"] for run in runs}) == 2
        assert len({worker.store.jobs(run["id"])[0]["job_id"] for run in runs}) == 2
        assert len({run["artifacts"][0]["id"] for run in runs}) == 2
        assert first_retained.read_bytes() == first_cached.read_bytes() == original_bytes
        assert first_retained.stat().st_mtime_ns == original_modified
        assert Path(first["source"]["source_dir"], "affinity/extract.py").read_text().startswith("# Revision first;")
        assert Path(second["source"]["source_dir"], "affinity/extract.py").read_text().startswith("# Revision second;")

        # Both histories and independently validated result downloads remain
        # available through the public API after the branch's code is replaced.
        public = client.get("/api/runs").json()["runs"]
        assert {run["id"] for run in public} == {first_id, second_id}
        downloads = []
        for run in runs:
            artifact = run["artifacts"][0]
            assert artifact["name"] == "1STP.npz"
            assert validate_npz(settings.state_dir / artifact["cache_path"]) == {"points": 8, "atoms": 4}
            response = client.get("/artifacts/" + artifact["id"])
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
            downloads.append(response.content)
        assert downloads[0] != downloads[1]
