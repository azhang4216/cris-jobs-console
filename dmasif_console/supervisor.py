"""Run the web process and one worker together in a single managed service."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from .config import load_settings


def maintenance_enabled() -> bool:
    value = os.environ.get("DMASIF_MAINTENANCE", "0")
    if value not in {"0", "1"}:
        raise ValueError("DMASIF_MAINTENANCE must be 0 or 1")
    return value == "1"


def supervisor_healthy(path: str | None) -> bool:
    """No process identities, settings or paths are returned by the health API."""
    if not path:
        return True  # Standalone web/Compose mode has no combined supervisor.
    try:
        with Path(path).open() as stream:
            status = json.loads(stream.read(4096))
        if not isinstance(status, dict):
            return False
        age = time.monotonic() - status["heartbeat"]
        return status.get("ok") is True and 0 <= age < 10
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _publish(path: Path, children: dict[str, subprocess.Popen], *, ok: bool) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "ok": ok, "heartbeat": time.monotonic(),
        "pids": {name: child.pid for name, child in children.items()},
    }))
    temporary.chmod(0o600)
    temporary.replace(path)


def _signal_group(child: subprocess.Popen, signum: int) -> None:
    # Include git/SSH descendants even when the immediate child already exited.
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def supervise(commands: dict[str, list[str]], status_path: Path, *, env: dict[str, str],
              shutdown_seconds: float = 20) -> int:
    """Any child exit stops its sibling; Render then restarts the whole service.

    A worker is never restarted beside a potentially orphaned predecessor.
    Remote scheduler jobs intentionally outlive these local processes.
    """
    children: dict[str, subprocess.Popen] = {}
    stopping = False
    previous_handlers = {}

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, stop)
        _publish(status_path, children, ok=False)
        for name, command in commands.items():
            child_env = dict(env)
            if name == "worker":
                for variable in ("DMASIF_WEBHOOK_SECRET", "DMASIF_WEBHOOK_SECRET_FILE",
                                 "DMASIF_VIEWER_PASSWORD", "DMASIF_VIEWER_PASSWORD_FILE"):
                    child_env.pop(variable, None)
            children[name] = subprocess.Popen(command, env=child_env, start_new_session=True)
        while not stopping:
            failed = next((name for name, child in children.items() if child.poll() is not None), None)
            if failed:
                print(f"The {failed} process exited; stopping this service for a clean restart.",
                      file=sys.stderr, flush=True)
                return 1
            _publish(status_path, children, ok=True)
            time.sleep(0.5)
        return 0
    finally:
        try:
            _publish(status_path, children, ok=False)
        except OSError:
            pass
        for child in children.values():
            _signal_group(child, signal.SIGTERM)
        deadline = time.monotonic() + shutdown_seconds
        while any(child.poll() is None for child in children.values()) and time.monotonic() < deadline:
            time.sleep(0.05)
        for child in children.values():
            _signal_group(child, signal.SIGKILL)
            child.wait()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def run_service(config_path: str | None, *, host: str = "0.0.0.0", port: int = 10000) -> int:
    os.umask(0o077)
    maintenance = maintenance_enabled()
    if not 1 <= port <= 65535:
        raise ValueError("The HTTP port must be between 1 and 65535")
    path = Path(config_path or os.environ.get("DMASIF_CONFIG", "config/local.yaml")).resolve()
    settings = load_settings(path)
    if len(settings.webhook_secret) < 16:
        raise ValueError("Configure a webhook secret (16+ characters) before starting")
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.state_dir.chmod(0o700)
    with (settings.state_dir / "service.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another combined service already uses this state directory") from None
        with tempfile.TemporaryDirectory(prefix="dmasif-service-") as runtime:
            status_path = Path(runtime) / "status.json"
            env = {**os.environ, "DMASIF_SUPERVISOR_STATUS": str(status_path)}
            base = [sys.executable, "-m", "dmasif_console.cli", "--config", str(path)]
            commands = {"web": [*base, "web", "--host", host, "--port", str(port)]}
            if not maintenance:
                commands["worker"] = [*base, "worker"]
            print("Starting console in maintenance mode." if maintenance else
                  "Starting console and one job monitor.", flush=True)
            return supervise(commands, status_path, env=env)
