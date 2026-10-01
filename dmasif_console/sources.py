"""Acquire plain Git snapshots without executing research code or Git hooks."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

import yaml

SHA = re.compile(r"^[0-9a-f]{40}$")
LFS_MARKER = b"version https://git-lfs.github.com/spec/v1"


class SourceError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_ATTR_NOSYSTEM="1")
    # A calling shell must not redirect Git to another repository.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        env.pop(name, None)
    return env


def _git(args: list[str], cwd: Path, *, timeout: int = 120) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", *args],
            cwd=cwd, env=_git_env(), capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError("Git acquisition failed or timed out; contact the operator.") from exc
    if result.returncode:
        # Remote errors can contain credentials embedded in URLs. Never persist them.
        raise SourceError("The exact commit could not be fetched from the configured repository.")
    return result.stdout



def _fetch(repo: Path, url: str, commit: str, source_cap: int) -> None:
    # Bound temporary Git objects as well as the final unpacked snapshot. The
    # source limit alone would otherwise allow a large fetch to fill the host.
    command = ["git", "-c", "core.hooksPath=/dev/null", "fetch", "--no-tags", "--depth=1", "--", url, commit]
    try:
        process = subprocess.Popen(command, cwd=repo, env=_git_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise SourceError("Git acquisition could not start.") from exc
    deadline = time.monotonic() + 120
    limit = source_cap * 2 + 8 * 1024 * 1024
    try:
        while process.poll() is None:
            size = sum(item.stat().st_size for item in repo.rglob("*") if item.is_file())
            if size > limit:
                raise SourceError("Source fetch exceeds the configured temporary-storage limit.")
            if time.monotonic() > deadline:
                raise SourceError("The exact commit fetch timed out.")
            time.sleep(0.05)
        if process.returncode:
            raise SourceError("The exact commit could not be fetched from the configured repository.")
        if sum(item.stat().st_size for item in repo.rglob("*") if item.is_file()) > limit:
            raise SourceError("Source fetch exceeds the configured temporary-storage limit.")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _archive(repo: Path, commit: str, destination: Path, cap: int) -> None:
    with destination.open("wb") as output:
        proc = subprocess.Popen(
            ["git", "-c", "core.attributesFile=/dev/null", "archive", "--format=tar", commit],
            cwd=repo, env=_git_env(), stdout=output, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 120
        try:
            while proc.poll() is None:
                if destination.stat().st_size > cap + 8 * 1024 * 1024:
                    raise SourceError("Source snapshot exceeds the configured size limit.")
                if time.monotonic() > deadline:
                    raise SourceError("Source archiving timed out.")
                time.sleep(0.03)
            if proc.returncode:
                raise SourceError("Source archiving failed.")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    if destination.stat().st_size > cap + 8 * 1024 * 1024:
        raise SourceError("Source snapshot exceeds the configured size limit.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_plain(archive: Path, destination: Path, cap: int) -> None:
    size = 0
    seen: set[str] = set()
    with tarfile.open(archive) as source:
        for member in source:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or ".git" in path.parts
                    or "\\" in member.name or not path.parts or member.name in seen):
                raise SourceError("Source contains an unsafe or duplicate path.")
            seen.add(member.name)
            if not (member.isdir() or member.isfile()):
                raise SourceError("Symlinks and special files are unsupported in source snapshots.")
            if path.name == ".gitmodules":
                raise SourceError("Git submodules are unsupported; use a plain Git source tree.")
            size += member.size
            if size > cap or len(seen) > 10000:
                raise SourceError("Source snapshot exceeds the configured size limit.")
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            if stream is None:
                raise SourceError("Source archive contains an unreadable file.")
            with stream, target.open("xb") as output:
                prefix = stream.read(256)
                if prefix.startswith(LFS_MARKER):
                    raise SourceError("Git LFS pointers are unsupported; register large assets separately.")
                output.write(prefix)
                shutil.copyfileobj(stream, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def snapshot_source(settings, commit_sha: str) -> dict:
    """Save the exact SHA and a deterministic, safe, root-relative tar.gz archive."""
    if not SHA.fullmatch(commit_sha):
        raise SourceError("A full 40-character Git commit SHA is required.")
    root = Path(settings.state_dir) / "sources"
    root.mkdir(parents=True, exist_ok=True)
    final = root / commit_sha
    if final.exists():
        try:
            record = json.loads((final / "record.json").read_text())
            if record["commit_sha"] != commit_sha or sha256_file(final / "source.tar.gz") != record["sha256"]:
                raise ValueError("checksum mismatch")
            # Re-read from the retained archive, not a mutable local checkout.
            return {**record, "archive_path": str(final / "source.tar.gz"), "source_dir": str(final / "source")}
        except (ValueError, OSError, KeyError) as exc:
            raise SourceError("Retained source snapshot failed its integrity check.") from exc
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=root) as temp:
        stage = Path(temp)
        repo = stage / "git"
        repo.mkdir()
        _git(["init", "--bare", "."], repo)
        # info/attributes overrides export-ignore/subst in submitted .gitattributes.
        (repo / "info" / "attributes").write_text("* -export-ignore -export-subst\n")
        _fetch(repo, settings.repository.clone_url, commit_sha, settings.max_source_bytes)
        resolved = _git(["rev-parse", "FETCH_HEAD^{commit}"], repo).decode().strip()
        if resolved != commit_sha:
            raise SourceError("Repository returned a different commit; refusing substitution.")
        tree = _git(["ls-tree", "-r", "-z", commit_sha], repo)
        if any(entry.startswith(b"160000 ") for entry in tree.split(b"\0")):
            raise SourceError("Git submodules are unsupported; use a plain Git source tree.")
        tar_path = stage / "source.tar"
        _archive(repo, commit_sha, tar_path, settings.max_source_bytes)
        bundle = stage / "bundle"
        source_dir = bundle / "source"
        source_dir.mkdir(parents=True)
        _extract_plain(tar_path, source_dir, settings.max_source_bytes)
        for required in ("affinity/extract.py", "experiments/run.yaml"):
            if not (source_dir / required).is_file():
                raise SourceError(f"Commit is missing required file: {required}.")
        archive = bundle / "source.tar.gz"
        with tar_path.open("rb") as stream, archive.open("wb") as target:
            with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
                shutil.copyfileobj(stream, compressed)
        record = {"commit_sha": commit_sha, "sha256": sha256_file(archive), "archive_bytes": archive.stat().st_size}
        (bundle / "record.json").write_text(json.dumps(record, sort_keys=True))
        os.rename(bundle, final)
        return {**record, "archive_path": str(final / "source.tar.gz"), "source_dir": str(final / "source")}


def read_experiment(source: dict) -> tuple[dict, str]:
    """Read bounded configuration from the retained archive (not mutable extraction)."""
    from .config import ExperimentConfig

    try:
        with tarfile.open(source["archive_path"], "r:gz") as archive:
            member = archive.getmember("experiments/run.yaml")
            if member.size > 32768:
                raise SourceError("Experiment configuration exceeds 32 KiB.")
            stream = archive.extractfile(member)
            if stream is None:
                raise SourceError("Experiment configuration is unreadable.")
            original = stream.read().decode("utf-8")
        parsed = yaml.safe_load(original)
        if not isinstance(parsed, dict):
            raise SourceError("experiments/run.yaml must contain a YAML mapping.")
        config = ExperimentConfig.model_validate(parsed)
    except SourceError:
        raise
    except Exception as exc:
        # Pydantic errors may echo submitted input. Show safe field paths only.
        paths = []
        if hasattr(exc, "errors"):
            paths = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
        suffix = f" Check: {', '.join(paths)}." if paths else ""
        raise SourceError("Invalid experiments/run.yaml; use the documented extraction schema." + suffix) from exc
    return config.model_dump(), original
