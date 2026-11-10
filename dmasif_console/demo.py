"""A fully local demonstration with genuine signed webhook requests and fake GPUs."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets
import subprocess
import uuid

import yaml


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()


def prepare_demo(state_dir: Path, *, training: bool = False) -> Path:
    """Create a new local fixture; never overwrite an existing demonstration."""
    state_dir = state_dir.expanduser().resolve()
    if state_dir.exists() and any(state_dir.iterdir()):
        raise ValueError("Demo directory is not empty. Choose a new --state-dir or reuse its existing config.")
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    fixture = state_dir / "fixture-repository"
    fixture.mkdir()
    (fixture / "affinity").mkdir()
    (fixture / "experiments").mkdir()
    (fixture / "affinity" / "extract.py").write_text(
        '# Local fixture. The fake adapter generates synthetic NPZ data and never runs this file.\n'
        'raise RuntimeError("This fixture is for fake mode only; it is not dMaSIF.")\n'
    )
    if training:
        for name in ("src/run_experiment.py", "src/train.py", "src/surface_encoder.py", "dMaSIF/model.py"):
            path = fixture / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Synthetic local training fixture; never scientifically executed.\n'
                            'raise RuntimeError("This fixture is for fake mode only.")\n')
    _git(fixture, "init", "-q")
    _git(fixture, "config", "user.name", "Demo Researcher")
    _git(fixture, "config", "user.email", "demo@example.invalid")
    inputs = state_dir / "fixture-inputs"
    inputs.mkdir()
    pdb = inputs / "1STP.pdb"
    pdb.write_text("REMARK SYNTHETIC LOCAL DEMO INPUT, NOT A SCIENTIFIC STRUCTURE\nEND\n")
    secret = secrets.token_urlsafe(32)
    secret_path = state_dir / "webhook-secret"
    secret_path.write_text(secret)
    secret_path.chmod(0o600)
    config = {
        "state_dir": str(state_dir), "mode": "fake", "submissions_enabled": True,
        "repository": {"id": 42, "full_name": "demo/dmasif", "clone_url": str(fixture)},
        "allowed_actor_ids": [101, 202], "hook_id": "demo-hook", "operator_contact": "Local demo operator",
        "poll_seconds": 1, "min_free_bytes": 0,
        "datasets": {"demo-1stp-v1": {"entries": [{"path": str(pdb), "sha256": hashlib.sha256(pdb.read_bytes()).hexdigest()}]}},
    }
    if training:
        from cluster_adapter.training import DATASET_FILES
        config["datasets"] = {"dataset_v2": {"kind": "training", "project_root": str(inputs),
                                                "hashes": {name: "0" * 64 for name in DATASET_FILES}}}
        config["presets"] = {"training": {"kind": "training"}}
    config_path = state_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    config_path.chmod(0o600)
    # The webhook secret lives in a separate file, never the saved configuration.
    from .config import Settings
    settings = Settings.model_validate({**config, "webhook_secret": secret})
    from .web import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(settings))
    for index, (login, actor) in enumerate((("demo-alice", 101), ("demo-bob", 202)), start=1):
        experiment = {"schema_version": 1, "job_type": "dmasif_extract", "dataset_id": "demo-1stp-v1", "preset_id": "quick-test", "seed": index, "repeat_id": f"demo-{index}"}
        if training:
            experiment.update(job_type="dmasif_train", dataset_id="dataset_v2", preset_id="training",
                              batch_size=16 if index == 1 else 32, steps=1000, eval_every=125, save_every=125)
        (fixture / "experiments" / "run.yaml").write_text(yaml.safe_dump(experiment))
        _git(fixture, "add", ".")
        task = "training" if training else "extraction"
        _git(fixture, "commit", "-qm", f"Local fake {task} {index}")
        commit = _git(fixture, "rev-parse", "HEAD")
        branch = f"runs/surface-encoder-v{index}" if training else f"runs/pocket-v{index}"
        _git(fixture, "branch", branch, commit)
        payload = {"ref": f"refs/heads/{branch}", "after": commit, "before": "0" * 40,
                   "created": True, "deleted": False, "forced": False,
                   "repository": {"id": 42, "full_name": "demo/dmasif"},
                   "sender": {"id": actor, "login": login, "type": "User"},
                   "head_commit": {"id": commit, "message": f"Local fake {task} {index}", "author": {"name": "Demo Researcher", "email": "demo@example.invalid"}, "committer": {"name": "Demo Researcher"}}}
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        response = client.post("/webhooks/github", content=body, headers={"Content-Type": "application/json", "X-GitHub-Event": "push", "X-GitHub-Delivery": str(uuid.uuid4()), "X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Hook-ID": "demo-hook"})
        if response.status_code not in {200, 201, 202}:
            raise RuntimeError(f"Demo signed webhook was rejected ({response.status_code}).")
    return config_path
