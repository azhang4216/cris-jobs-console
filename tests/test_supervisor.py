"""Process lifecycle tests use synthetic children; never SSH or a real cluster."""
import json
import os
import signal
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from dmasif_console.config import Settings
from dmasif_console.db import Store
from dmasif_console.supervisor import maintenance_enabled, run_service, supervise, supervisor_healthy
from dmasif_console.web import create_app


def settings(tmp_path):
    return Settings(state_dir=tmp_path / "state", repository={"id": 1, "full_name": "lab/research",
                    "clone_url": str(tmp_path)}, allowed_actor_ids=[1],
                    webhook_secret="synthetic-webhook-secret")


@pytest.mark.parametrize("failed", ["web", "worker"])
def test_child_exit_stops_sibling_and_returns_failure(tmp_path, failed):
    status = tmp_path / "status.json"
    commands = {name: [sys.executable, "-c", "import time; time.sleep(0.3); raise SystemExit(7)"
                      if name == failed else "import time; time.sleep(60)"]
                for name in ("web", "worker")}
    assert supervise(commands, status, env=dict(os.environ), shutdown_seconds=1) == 1
    record = json.loads(status.read_text())
    assert record["ok"] is False
    for pid in record["pids"].values():
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_sigterm_stops_both_children(tmp_path):
    status = tmp_path / "status.json"
    script = """
import os, sys
from pathlib import Path
from dmasif_console.supervisor import supervise
command = [sys.executable, '-c', 'import time; time.sleep(60)']
raise SystemExit(supervise({'web': command, 'worker': command}, Path(sys.argv[1]), env=dict(os.environ), shutdown_seconds=1))
"""
    parent = subprocess.Popen([sys.executable, "-c", script, str(status)])
    try:
        deadline = time.monotonic() + 10
        while not supervisor_healthy(str(status)) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert supervisor_healthy(str(status))
        parent.send_signal(signal.SIGTERM)
        assert parent.wait(timeout=5) == 0
        record = json.loads(status.read_text())
        assert record["ok"] is False
        for pid in record["pids"].values():
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=5)


def test_health_fails_if_supervisor_dies_or_database_breaks(tmp_path, monkeypatch):
    config = settings(tmp_path)
    status = tmp_path / "status.json"
    monkeypatch.setenv("DMASIF_SUPERVISOR_STATUS", str(status))
    with TestClient(create_app(config)) as client:
        assert client.get("/healthz").status_code == 503
        status.write_text(json.dumps({"heartbeat": time.monotonic(), "ok": True}))
        assert client.get("/healthz").json() == {"status": "ok"}
        status.write_text(json.dumps({"heartbeat": time.monotonic() - 11, "ok": True}))
        assert client.get("/healthz").status_code == 503
        status.write_text(json.dumps({"heartbeat": time.monotonic(), "ok": True}))
        with Store(config.database_path).connection(write=True) as connection:
            connection.execute("DROP TABLE runs")
        assert client.get("/healthz").status_code == 503


def test_maintenance_blocks_webhooks_and_database_routes_but_allows_health(tmp_path, monkeypatch):
    config = settings(tmp_path)
    monkeypatch.setenv("DMASIF_MAINTENANCE", "1")
    config.state_dir.mkdir()
    config.database_path.write_bytes(b"synthetic corrupt SQLite file")
    with TestClient(create_app(config)) as client:
        assert config.database_path.read_bytes() == b"synthetic corrupt SQLite file"
        config.database_path.unlink()  # A restore can move/replace the DB now.
        assert client.get("/healthz").json() == {"status": "maintenance"}
        for path in ("/", "/api/runs", "/artifacts/anything"):
            response = client.get(path)
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "60"
        assert client.post("/webhooks/github", json={}).status_code == 503
        assert not config.database_path.exists()


def test_invalid_maintenance_value_fails_closed(monkeypatch):
    monkeypatch.setenv("DMASIF_MAINTENANCE", "yes")
    with pytest.raises(ValueError):
        maintenance_enabled()


def test_serve_startup_error_does_not_print_configuration_values(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("repository: do-not-print-this-private-value\n")
    result = subprocess.run([sys.executable, "-m", "dmasif_console.cli", "--config", str(config), "serve"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "do-not-print-this-private-value" not in result.stdout + result.stderr
    assert "Service startup failed" in result.stderr


def test_serve_uses_render_port(monkeypatch):
    from dmasif_console.cli import parser
    monkeypatch.setenv("PORT", "12345")
    assert parser().parse_args(["serve"]).port == 12345


@pytest.mark.parametrize("maintenance", ["0", "1"])
def test_serve_starts_without_viewer_credentials(tmp_path, monkeypatch, maintenance):
    config = settings(tmp_path)
    assert config.viewer_password == ""
    monkeypatch.setenv("DMASIF_MAINTENANCE", maintenance)
    monkeypatch.setattr("dmasif_console.supervisor.load_settings", lambda _: config)
    observed = {}

    def capture(commands, status_path, *, env):
        observed.update(commands)
        assert status_path.parent.is_dir()
        return 0

    monkeypatch.setattr("dmasif_console.supervisor.supervise", capture)
    previous_umask = os.umask(0o077)
    try:
        assert run_service(str(tmp_path / "config.yaml")) == 0
    finally:
        os.umask(previous_umask)
    assert set(observed) == ({"web"} if maintenance == "1" else {"web", "worker"})


@pytest.mark.parametrize("secret", ["", "too-short"])
def test_serve_requires_webhook_secret_before_starting_children(tmp_path, monkeypatch, secret):
    config = settings(tmp_path)
    config.webhook_secret = secret
    monkeypatch.setattr("dmasif_console.supervisor.load_settings", lambda _: config)
    monkeypatch.setattr("dmasif_console.supervisor.supervise", lambda *a, **k: pytest.fail("children started"))
    previous_umask = os.umask(0o077)
    try:
        with pytest.raises(ValueError, match="webhook secret"):
            run_service(str(tmp_path / "config.yaml"))
    finally:
        os.umask(previous_umask)
    assert not config.state_dir.exists()
