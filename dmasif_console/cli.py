"""Protected local operator commands. There are no browser write actions."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading

from .config import load_settings
from .db import Store
from .worker import Worker, is_terminal
from .transport import TransportError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="dmasif-console")
    root.add_argument("--config", help="Operator YAML configuration (or DMASIF_CONFIG).")
    sub = root.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the website and one worker together (single Render service).")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=os.environ.get("PORT", "10000"))
    web = sub.add_parser("web", help="Serve the read-only dashboard and signed webhook receiver.")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    worker = sub.add_parser("worker", help="Run the single durable worker.")
    worker.add_argument("--once", action="store_true")
    for name in ("pause", "resume", "reconcile", "cancel", "resolve"):
        command = sub.add_parser(name)
        if name in {"reconcile", "cancel", "resolve"}:
            command.add_argument("run_id")
        command.add_argument("--reason", required=True)
        command.add_argument("--actor", default=getpass.getuser())
        if name == "resolve":
            command.add_argument("--evidence", required=True, help="External verification or site ticket reference; retained privately.")
            command.add_argument("--confirm-no-live-job", action="store_true", help="Attest that no helper can submit and no associated job remains live.")
    backup = sub.add_parser("backup", help="Back up SQLite and retained sources, excluding credentials.")
    backup.add_argument("destination", type=Path)
    restore = sub.add_parser("restore", help="Restore into fresh local state, remain paused, and reconcile remote inventory.")
    restore.add_argument("--backup", type=Path)
    restore.add_argument("--actor", default=getpass.getuser())
    restore.add_argument("--reason", required=True)
    demo = sub.add_parser("demo", help="Create two signed example pushes using local fixtures; no SSH or GPUs.")
    demo.add_argument("--state-dir", type=Path, default=Path(".state-demo"))
    demo.add_argument("--serve", action="store_true")
    demo.add_argument("--port", type=int, default=8000)
    return root


def _backup(settings, destination: Path):
    from .backups import create_backup
    with Worker(settings) as worker:
        create_backup(worker.store, settings.state_dir, settings.mode, destination,
                      min_free_bytes=settings.min_free_bytes)
    print(f"Backup written to {destination}. Keep operator configuration and credentials separately.")


def _restore(settings, backup: Path | None, actor: str, reason: str):
    state = settings.state_dir
    state.mkdir(parents=True, exist_ok=True)
    marker = state / "restore-pending.json"
    # Marker also makes the receiver fail closed until dedup evidence is rebuilt.
    with Worker(settings) as worker:
        if backup:
            if worker.store.list_runs(limit=1):
                raise ValueError("Restore a backup into a fresh state directory; existing history cannot be replaced.")
            manifest_path = backup / "backup.json"
            if not manifest_path.is_file() or json.loads(manifest_path.read_text()).get("format_version") != 1:
                raise ValueError("Backup is incomplete or uses an unsupported format.")
            source_db = backup / "console.sqlite3"
            if not source_db.is_file():
                raise ValueError("Backup contains no console.sqlite3.")
            marker.write_text(json.dumps({"reason": reason, "actor": actor}))
            with sqlite3.connect(f"file:{source_db.resolve()}?mode=ro", uri=True) as source:
                with sqlite3.connect(settings.database_path) as target:
                    source.backup(target)
            if (backup / "sources").exists():
                shutil.copytree(backup / "sources", state / "sources", dirs_exist_ok=True)
        else:
            marker.write_text(json.dumps({"reason": reason, "actor": actor}))
        worker.store.set_paused(True, "Restore reconciliation in progress: " + reason, actor=actor)
        for run in worker.store.list_runs(limit=100000):
            source = run.get("source")
            if source:
                base = state / "sources" / source["commit_sha"]
                source.update(archive_path=str(base / "source.tar.gz"), source_dir=str(base / "source"))
                worker.store.update_run(run["id"], {"source": source})
        inventory = worker.adapter.inventory()
        for recovered in inventory:
            run = recovered.get("run", recovered)
            jobs = recovered.get("jobs", run.get("jobs", []))
            worker.store.restore_run(run, jobs)
        # The global restore gate blocks scheduling while each recovered run is
        # examined. Do not reserve every historical run: there is only one slot.
        unresolved, live = [], []
        for run in worker.store.list_runs(limit=100000):
            jobs = worker.store.jobs(run["id"])
            attestation = run.get("operator_resolution")
            if attestation and run["state"] in {"SUBMISSION_UNKNOWN", "NEEDS_REVIEW"}:
                worker.resolve(run["id"], attestation["actor"], attestation["reason"], attestation["evidence"], confirm_no_live_job=True)
                continue
            if run.get("submission_intent_at") or run.get("capacity_reserved") or run.get("remote_dir") or jobs:
                worker.reconcile(run["id"], actor, "Restore: " + reason, reserve_unknown=False)
                current = worker.store.get_run(run["id"])
                jobs = worker.store.jobs(run["id"])
                if not jobs:
                    unresolved.append(run["id"])
                elif any(not is_terminal(job) for job in jobs):
                    live.append(current)
                    if current.get("monitor_error"):
                        unresolved.append(run["id"])
        reserved = [run for run in worker.store.list_runs(limit=100000) if run.get("capacity_reserved")]
        if not reserved and (live or unresolved):
            worker.store.update_run((live[0]["id"] if live else unresolved[0]), {"capacity_reserved": True})
        if unresolved or len(live) > 1:
            raise ValueError("Restore found ambiguous or multiple live submissions. Submissions and webhook acceptance remain paused; reconcile evidence and rerun restore without --backup.")
        marker.unlink(missing_ok=True)
        worker.store.set_paused(True, "Restore inventory reconciled; operator review and explicit resume required.", actor=actor)
    print("Restore reconciled. Submissions remain paused; inspect history before an explicit resume.")


def main(argv: list[str] | None = None):
    args = parser().parse_args(argv)
    if args.command == "serve":
        from .supervisor import run_service
        try:
            code = run_service(args.config, host=args.host, port=args.port)
        except Exception as exc:
            # Configuration validation can include secret input values. Never
            # dump it into the hosting provider's build/runtime logs.
            print(f"Service startup failed ({type(exc).__name__}). Check the private operator configuration, secrets, and disk permissions.", file=sys.stderr)
            raise SystemExit(1) from None
        raise SystemExit(code)
    try:
        if args.command == "demo":
            from .demo import prepare_demo
            config_path, password = prepare_demo(args.state_dir)
            os.environ["DMASIF_WEBHOOK_SECRET_FILE"] = str(config_path.parent / "webhook-secret")
            os.environ["DMASIF_VIEWER_PASSWORD_FILE"] = str(config_path.parent / "viewer-password")
            os.environ.pop("DMASIF_WEBHOOK_SECRET", None)
            os.environ.pop("DMASIF_VIEWER_PASSWORD", None)
            settings = load_settings(config_path)
            print(f"Local fake demo created: {config_path}")
            print("Synthetic data only; no dMaSIF code, SSH, or GPUs are executed.")
            print(f"Viewer username: lab\nViewer password: {password}")
            if not args.serve:
                print("Set DMASIF_WEBHOOK_SECRET_FILE and DMASIF_VIEWER_PASSWORD_FILE to the files in this directory.")
                print(f"Then run dmasif-console --config {config_path} worker and dmasif-console --config {config_path} web.")
                return
            from .web import create_app
            import uvicorn
            worker = Worker(settings)
            threading.Thread(target=worker.run_forever, daemon=True).start()
            print(f"Dashboard: http://127.0.0.1:{args.port}")
            uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
            return
        settings = load_settings(args.config)
        if args.command == "web":
            from .web import create_app
            import uvicorn
            uvicorn.run(create_app(settings), host=args.host, port=args.port)
        elif args.command == "worker":
            worker = Worker(settings)
            if args.once:
                with worker:
                    worker.tick()
            else:
                worker.run_forever()
        elif args.command in {"pause", "resume"}:
            store = Store(settings.database_path)
            store.initialize()
            if args.command == "resume" and (settings.state_dir / "restore-pending.json").exists():
                raise ValueError("Restore evidence has not been reconciled. Complete restore before resuming.")
            store.set_paused(args.command == "pause", args.reason, actor=args.actor)
            print("Submissions paused; monitoring continues." if args.command == "pause" else "Pause cleared. Configuration submissions_enabled must also be true.")
        elif args.command == "resolve":
            with Worker(settings) as worker:
                worker.resolve(args.run_id, args.actor, args.reason, args.evidence, confirm_no_live_job=args.confirm_no_live_job)
            print("Resolution evidence recorded privately. Run closed as failed; claims remain intact and no job was resubmitted.")
        elif args.command in {"reconcile", "cancel"}:
            with Worker(settings) as worker:
                getattr(worker, args.command)(args.run_id, args.actor, args.reason)
            print("Operator action recorded. Consult the dashboard for verified status.")
        elif args.command == "backup":
            _backup(settings, args.destination)
        elif args.command == "restore":
            _restore(settings, args.backup, args.actor, args.reason)
    except (ValueError, RuntimeError, OSError, TransportError) as exc:
        # All expected errors are authored, credential-free messages.
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
