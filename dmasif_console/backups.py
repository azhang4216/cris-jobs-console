"""Private, atomic recovery snapshots on the persistent disk.

The worker calls this only between ticks while holding its lifetime lock. These
copies help with operator mistakes; exporting them off the disk remains necessary
for recovery after losing that disk or the hosting account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
import uuid

from .db import Store

LOG = logging.getLogger(__name__)
MANAGED_BY = "dmasif-console"
NAME = re.compile(r"backup-\d{8}T\d{12}Z-[0-9a-f]{32}$")
SHA = re.compile(r"[0-9a-f]{40}$")
DIGEST = re.compile(r"[0-9a-f]{64}$")
HEADROOM = 8 * 1024 * 1024


def _records(database: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    with closing(sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)) as connection:
        for (data,) in connection.execute("SELECT data FROM runs"):
            source = json.loads(data).get("source")
            if not source:
                continue
            commit, digest = source.get("commit_sha", ""), source.get("sha256", "")
            if not SHA.fullmatch(commit) or not DIGEST.fullmatch(digest):
                raise ValueError("Invalid retained source metadata.")
            if commit in records and records[commit]["sha256"] != digest:
                raise ValueError("Conflicting retained source checksums.")
            records[commit] = source
    return records


def _archive_path(state: Path, commit: str) -> Path:
    root = state / "sources"
    directory = root / commit
    archive = directory / "source.tar.gz"
    if root.is_symlink() or directory.is_symlink() or archive.is_symlink() or not archive.is_file():
        raise ValueError("A required retained source archive is unavailable.")
    return archive


def _sync(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_space(parent: Path, required: int, reserve: int) -> None:
    if shutil.disk_usage(parent).free < required + reserve + HEADROOM:
        raise OSError("Insufficient space for a recovery snapshot.")


def create_backup(store: Store, state_dir: Path, mode: str, destination: Path, *,
                  min_free_bytes: int = 0, created_at: float | None = None) -> Path:
    """Publish a format-v1 backup, or leave no completed backup on failure.

    Caller must hold the worker lock. Source archives are immutable; the database
    snapshot determines which archives to copy. Unpacked trees, downloads, worker
    credentials and configuration files are intentionally outside this format.
    """
    state = state_dir.resolve()
    destination = destination.expanduser().absolute()
    if destination.is_symlink() or destination.exists():
        raise FileExistsError("Backup destination already exists.")
    destination = destination.resolve()
    if destination == state or state in destination.parents:
        raise ValueError("Place backups outside the live state directory.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = _records(store.path)
    with store.connection() as connection:
        database_bytes = (connection.execute("PRAGMA page_count").fetchone()[0]
                          * connection.execute("PRAGMA page_size").fetchone()[0])
    source_bytes = sum(_archive_path(state, commit).stat().st_size for commit in records)
    _require_space(destination.parent, database_bytes + source_bytes, min_free_bytes)
    stage = Path(tempfile.mkdtemp(prefix=".incomplete-dmasif-", dir=destination.parent))
    try:
        store.backup(stage / "console.sqlite3")
        records = _records(stage / "console.sqlite3")
        for commit, source in records.items():
            archive = _archive_path(state, commit)
            _require_space(destination.parent, archive.stat().st_size, min_free_bytes)
            target = stage / "sources" / commit
            target.mkdir(parents=True, mode=0o700)
            saved_archive = target / "source.tar.gz"
            shutil.copyfile(archive, saved_archive)
            saved_archive.chmod(0o600)
            with saved_archive.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != source["sha256"]:
                raise ValueError("Retained source checksum does not match the database.")
            record = target / "record.json"
            record.write_text(json.dumps({"commit_sha": commit, "sha256": digest,
                                          "archive_bytes": saved_archive.stat().st_size}, sort_keys=True))
            record.chmod(0o600)
            _sync(saved_archive)
            _sync(record)
            _sync_directory(target)
        if (stage / "sources").exists():
            (stage / "sources").chmod(0o700)
            _sync_directory(stage / "sources")
        manifest = stage / "backup.json"
        manifest.write_text(json.dumps({
            "created_at": datetime.fromtimestamp(created_at if created_at is not None else time.time(), timezone.utc).isoformat(),
            "format_version": 1, "managed_by": MANAGED_BY, "source_state_dir": str(state), "mode": mode,
        }, indent=2))
        manifest.chmod(0o600)
        _sync(stage / "console.sqlite3")
        _sync(manifest)
        _sync_directory(stage)
        # The worker lock serializes publication. Never overwrite an existing
        # destination, even when a caller accidentally reuses a snapshot name.
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("Backup destination already exists.")
        stage.rename(destination)
        _sync_directory(destination.parent)
        return destination
    finally:
        if stage.exists():
            shutil.rmtree(stage)


@dataclass
class AutomaticBackups:
    directory: Path
    state_dir: Path
    mode: str
    min_free_bytes: int
    interval_seconds: int = 86400
    keep: int = 3
    _next_attempt: float = field(default=0, init=False)

    @classmethod
    def from_environment(cls, settings) -> AutomaticBackups | None:
        directory = os.environ.get("DMASIF_BACKUP_DIR")
        if not directory:
            return None
        try:
            directory = Path(directory).expanduser().resolve()
            state = Path(settings.state_dir).resolve()
            if directory == state or state in directory.parents or directory in state.parents:
                raise ValueError("Backup and state directories must be separate.")
            interval = int(os.environ.get("DMASIF_BACKUP_INTERVAL_SECONDS", "86400"))
            keep = int(os.environ.get("DMASIF_BACKUP_KEEP", "3"))
            if not 60 <= interval <= 31 * 86400 or not 1 <= keep <= 30:
                raise ValueError("Invalid backup interval or retention.")
            return cls(directory, state, settings.mode, settings.min_free_bytes, interval, keep)
        except (OSError, ValueError):
            LOG.error("Automatic backup configuration is invalid; backups are disabled. Check the backup environment settings.")
            return None

    def _completed(self) -> list[tuple[float, Path]]:
        completed = []
        for path in self.directory.iterdir():
            if not NAME.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
                continue
            manifest = path / "backup.json"
            if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 4096:
                continue
            try:
                info = json.loads(manifest.read_text())
                if info.get("format_version") != 1 or info.get("managed_by") != MANAGED_BY:
                    continue
                stamp = datetime.fromisoformat(info["created_at"]).timestamp()
                if not (path / "console.sqlite3").is_file():
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            completed.append((stamp, path))
        return sorted(completed)

    def run_due(self, store: Store, *, at: float | None = None) -> Path | None:
        """Try one due backup; a backup failure never interrupts job monitoring."""
        moment = time.time() if at is None else at
        if moment < self._next_attempt:
            return None
        self._next_attempt = moment + min(self.interval_seconds, 300)
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            completed = self._completed()
            if completed and moment - completed[-1][0] < self.interval_seconds:
                self._next_attempt = min(completed[-1][0], moment) + self.interval_seconds
                return None
            # A killed process can leave an unpublished directory. No other
            # worker can own one while our lifetime lock is held.
            for path in self.directory.glob(".incomplete-dmasif-*"):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
            stamp = datetime.fromtimestamp(moment, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            destination = self.directory / f"backup-{stamp}-{uuid.uuid4().hex}"
            result = create_backup(store, self.state_dir, self.mode, destination,
                                   min_free_bytes=self.min_free_bytes, created_at=moment)
            # Retain old known-good copies until the new copy is complete.
            for _, path in self._completed()[:-self.keep]:
                shutil.rmtree(path)
            self._next_attempt = moment + self.interval_seconds
            LOG.info("Automatic recovery backup completed.")
            return result
        except Exception:
            # Paths, source code and exception text can contain private data.
            LOG.warning("Automatic recovery backup failed; existing backups retained. Check backup storage and retained sources. Job monitoring continues.")
            return None
