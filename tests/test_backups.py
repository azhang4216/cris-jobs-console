import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
from types import SimpleNamespace

import pytest

from dmasif_console.backups import AutomaticBackups, create_backup
from dmasif_console.cli import _restore
from dmasif_console.config import Settings
from dmasif_console.db import Store
from dmasif_console.sources import read_experiment, snapshot_source
from dmasif_console.worker import Worker


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    for name in ("DMASIF_BACKUP_DIR", "DMASIF_BACKUP_INTERVAL_SECONDS", "DMASIF_BACKUP_KEEP"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(state_dir=tmp_path / "state", min_free_bytes=0,
                        repository={"id": 1, "full_name": "lab/repo", "clone_url": str(tmp_path)},
                        allowed_actor_ids=[1], submissions_enabled=False)
    store = Store(settings.database_path)
    store.initialize()
    commit = "a" * 40
    directory = settings.state_dir / "sources" / commit
    directory.mkdir(parents=True)
    archive = directory / "source.tar.gz"
    config = b"schema_version: 1\njob_type: dmasif_extract\ndataset_id: demo\npreset_id: quick-test\nseed: 0\nrepeat_id: first\n"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("experiments/run.yaml")
        member.size = len(config)
        output.addfile(member, io.BytesIO(config))
    source = {"commit_sha": commit, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "archive_path": str(archive), "source_dir": str(directory / "source")}
    response = store.accept_delivery("hook", "one", "b" * 64, {}, "ACCEPTED", "", {
        "commit_sha": commit, "actor_login": "researcher", "state": "WAITING_FOR_CAPACITY", "source": source})
    # These unrelated files must never enter a recovery snapshot.
    (settings.state_dir / "ssh_key.txt").write_text("PRIVATE FIXTURE")
    (settings.state_dir / "artifacts").mkdir()
    (settings.state_dir / "artifacts" / "result.npz").write_bytes(b"fixture download")
    (directory / "source").mkdir()
    (directory / "source" / "unpacked.txt").write_text("regenerable fixture")
    return settings, store, response["run_id"], archive


def test_snapshot_restores_database_dedup_and_immutable_source_without_credentials(fixture, tmp_path, monkeypatch):
    settings, store, run_id, archive = fixture
    destination = tmp_path / "backups" / "first"
    with Worker(settings):
        create_backup(store, settings.state_dir, settings.mode, destination)
    files = {str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file()}
    assert files == {"console.sqlite3", "backup.json", f"sources/{'a' * 40}/source.tar.gz", f"sources/{'a' * 40}/record.json"}
    assert destination.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o077 == 0 for path in destination.rglob("*") if path.is_file())
    with sqlite3.connect(destination / "console.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.update_run(run_id, {"reason": "Changed after backup"})
    restored_settings = settings.model_copy(update={"state_dir": tmp_path / "restored"})
    monkeypatch.setattr("dmasif_console.worker.get_adapter", lambda settings: SimpleNamespace(inventory=lambda: []))
    _restore(restored_settings, destination, "operator", "test restoration")
    recovered = Store(restored_settings.database_path)
    assert recovered.get_run(run_id).get("reason") != "Changed after backup"
    assert recovered.control_state()["paused"] is True
    assert recovered.accept_delivery("hook", "new-header", "b" * 64, {}, "ACCEPTED", "", {})["duplicate"] is True
    source = snapshot_source(restored_settings, "a" * 40)
    assert read_experiment(source)[0]["repeat_id"] == "first"
    assert Path(source["archive_path"]).read_bytes() == archive.read_bytes()


def test_copy_failure_cleans_partial_and_preserves_previous_backups(fixture, tmp_path, monkeypatch, caplog):
    settings, store, _, _ = fixture
    backups = AutomaticBackups(tmp_path / "backups", settings.state_dir, "fake", 0, interval_seconds=60, keep=1)
    first = backups.run_due(store, at=1000)
    assert first

    def failing_copy(*args, **kwargs):
        raise OSError("secret-sensitive-path")

    monkeypatch.setattr("dmasif_console.backups.shutil.copyfile", failing_copy)
    assert backups.run_due(store, at=1061) is None
    assert first.exists()
    assert list(backups.directory.glob(".incomplete-dmasif-*")) == []
    assert "secret-sensitive-path" not in caplog.text
    assert "backup failed" in caplog.text


def test_retention_only_prunes_our_complete_snapshots_after_success(fixture, tmp_path):
    settings, store, _, _ = fixture
    backups = AutomaticBackups(tmp_path / "backups", settings.state_dir, "fake", 0, interval_seconds=60, keep=2)
    first = backups.run_due(store, at=1000)
    unrelated = backups.directory / "operator-export"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep")
    partial = backups.directory / ".incomplete-dmasif-old"
    partial.mkdir()
    (partial / "interrupted-copy").write_text("incomplete")
    second = backups.run_due(store, at=1061)
    third = backups.run_due(store, at=1122)
    assert not first.exists()
    assert second.exists() and third.exists()
    assert unrelated.exists()
    assert not partial.exists()


def test_restarted_backup_schedule_uses_persisted_snapshot_time(fixture, tmp_path):
    settings, store, _, _ = fixture
    directory = tmp_path / "backups"
    first = AutomaticBackups(directory, settings.state_dir, "fake", 0).run_due(store, at=1000)
    restarted = AutomaticBackups(directory, settings.state_dir, "fake", 0)
    assert restarted.run_due(store, at=1001) is None
    assert restarted.run_due(store, at=87400)
    assert first.exists()


def test_existing_destination_is_never_overwritten(fixture, tmp_path):
    settings, store, _, _ = fixture
    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        create_backup(store, settings.state_dir, settings.mode, destination)
    assert list(destination.iterdir()) == []


def test_corrupted_archive_cannot_be_published_as_complete(fixture, tmp_path):
    settings, store, _, archive = fixture
    archive.write_bytes(b"changed archive")
    destination = tmp_path / "backups" / "first"
    with pytest.raises(ValueError, match="checksum"):
        create_backup(store, settings.state_dir, settings.mode, destination)
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_low_space_keeps_monitoring_and_existing_backups(fixture, tmp_path, monkeypatch):
    settings, store, _, _ = fixture
    backups = AutomaticBackups(tmp_path / "backups", settings.state_dir, "fake", 0, interval_seconds=60)
    first = backups.run_due(store, at=1000)
    monkeypatch.setattr("dmasif_console.backups.shutil.disk_usage", lambda path: SimpleNamespace(free=0))
    assert backups.run_due(store, at=1061) is None
    assert first.exists()


def test_automatic_backup_runs_after_tick_with_lock_even_when_submissions_paused(fixture, tmp_path, monkeypatch):
    settings, _, run_id, _ = fixture
    monkeypatch.setenv("DMASIF_BACKUP_DIR", str(tmp_path / "backups"))
    worker = Worker(settings)
    worker.tick()
    assert not (tmp_path / "backups").exists()
    with worker:
        worker.tick()
    snapshots = list((tmp_path / "backups").glob("backup-*"))
    assert len(snapshots) == 1
    assert Store(snapshots[0] / "console.sqlite3").get_run(run_id)["state"] == "WAITING_FOR_CAPACITY"


@pytest.mark.parametrize("name,value", [("DMASIF_BACKUP_KEEP", "0"), ("DMASIF_BACKUP_KEEP", "31"),
                                        ("DMASIF_BACKUP_INTERVAL_SECONDS", "bad"),
                                        ("DMASIF_BACKUP_INTERVAL_SECONDS", "1")])
def test_invalid_backup_environment_does_not_interrupt_worker(fixture, tmp_path, monkeypatch, caplog, name, value):
    settings, _, _, _ = fixture
    monkeypatch.setenv("DMASIF_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv(name, value)
    worker = Worker(settings)
    with worker:
        worker.tick()
    assert worker._backups is None
    assert "backups are disabled" in caplog.text


@pytest.mark.parametrize("location", ["state", "state/backups", "."])
def test_backup_directory_must_be_separate_from_live_state(fixture, tmp_path, monkeypatch, location):
    settings, _, _, _ = fixture
    monkeypatch.setenv("DMASIF_BACKUP_DIR", str(tmp_path / location))
    assert AutomaticBackups.from_environment(settings) is None


def test_snapshot_copies_only_sources_in_consistent_database_snapshot(fixture, tmp_path, monkeypatch):
    settings, store, run_id, _ = fixture
    original = store.backup

    def backup_then_change_live_state(destination):
        original(destination)
        store.update_run(run_id, {"source": None})

    monkeypatch.setattr(store, "backup", backup_then_change_live_state)
    destination = tmp_path / "backups" / "first"
    create_backup(store, settings.state_dir, settings.mode, destination)
    assert (destination / "sources" / ("a" * 40) / "source.tar.gz").is_file()
    manifest = json.loads((destination / "backup.json").read_text())
    assert manifest["format_version"] == 1
