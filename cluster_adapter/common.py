"""Shared filesystem invariants. This module uses only the Python standard library."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone


class AdapterError(Exception):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def write_json(path: Path, data, *, exclusive: bool = False) -> None:
    """Fsync content before publishing; never replace evidence when exclusive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(data))
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temp, path)
            temp.unlink()
        else:
            os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text())


def run_id(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AdapterError("Run ID must be a UUID") from exc
    if str(parsed) != str(value):
        raise AdapterError("Run ID must use canonical UUID format")
    return str(parsed)


def job_id(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,20}", str(value)):
        raise AdapterError("Unexpected Slurm job ID")
    return str(value)


def digest(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", str(value)):
        raise AdapterError("Expected a SHA-256 digest")
    return str(value)


def checked_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise AdapterError("Unsafe relative path")
    return path


def contained(base: Path, value: str, *, must_exist: bool = True) -> Path:
    relative = checked_relative(value)
    path = base / str(relative)
    if path.is_symlink() or base.resolve() not in path.resolve().parents:
        raise AdapterError("Path escapes its managed directory")
    if must_exist and not path.is_file():
        raise AdapterError("Expected a regular file")
    return path


def safe_extract(archive: Path, destination: Path, limit_bytes: int = 50 * 1024 * 1024) -> dict:
    """Extract plain Git content; reject links, devices, duplicates and oversized data."""
    destination.mkdir(parents=True, exist_ok=False)
    total = 0
    seen = set()
    files = {}
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle:
            path = checked_relative(member.name)
            if path.parts[0] == ".git" or ".gitmodules" in path.parts:
                raise AdapterError("Git metadata and submodules are unsupported")
            name = str(path)
            if name in seen:
                raise AdapterError("Duplicate archive member")
            seen.add(name)
            if len(seen) > 20000:
                raise AdapterError("Source archive exceeds configured file-count limit")
            target = destination / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.size < 0:
                raise AdapterError("Only regular source files and directories are supported")
            total += member.size
            if total > limit_bytes or len(seen) > 20000:
                raise AdapterError("Source archive exceeds configured limits")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open("xb") as output:
                first = source.read(min(member.size, 1024))
                if first.startswith(b"version https://git-lfs.github.com/spec/v1"):
                    raise AdapterError("Git LFS pointers are unsupported")
                output.write(first)
                while block := source.read(1024 * 1024):
                    output.write(block)
            if target.stat().st_size != member.size:
                raise AdapterError("Truncated source archive")
            files[name] = sha256_file(target)
    if "affinity/extract.py" not in files:
        raise AdapterError("Source lacks affinity/extract.py")
    return files


def parse_chains(spec) -> list[str]:
    if spec is None or spec == "":
        return []
    if isinstance(spec, list):
        values = spec
    elif isinstance(spec, str):
        values = spec.split(",") if "," in spec or "." in spec else list(spec)
    else:
        raise AdapterError("Invalid chain selection")
    if not values or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.]+", value) for value in values):
        raise AdapterError("Chain labels must contain letters, numbers or periods")
    return sorted(set(values))


def input_manifest(entries: list[dict]) -> list[dict]:
    if not entries:
        raise AdapterError("A dataset must contain at least one input")
    filenames, outputs = set(), set()
    result = []
    for entry in entries:
        original = str(entry["path"])
        name = Path(original).name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("."):
            raise AdapterError("Dataset filenames must be simple, nonhidden names")
        if Path(name).suffix.lower() not in {".pdb", ".pdb1", ".ent", ".cif", ".mmcif"}:
            raise AdapterError("Unsupported structure format")
        chains = parse_chains(entry.get("chains"))
        # Mirrors affinity/extract.py naming; collisions must fail before running.
        tag = Path(name).stem + ("_" + "".join(chains).replace(".", "") if chains else "")
        output = tag + ".npz"
        if name in filenames or output in outputs:
            raise AdapterError("Duplicate dataset filename or expected output collision")
        filenames.add(name)
        outputs.add(output)
        result.append({"original_path": original, "name": name, "sha256": digest(entry["sha256"]), "chains": chains, "expected_output": output})
    return result


def claim(path: Path) -> None:
    """Persist an exclusive claim before any non-idempotent external action."""
    path.mkdir()
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def verify_tree(root: Path, files: dict[str, str]) -> None:
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise AdapterError("Pinned source contains a symbolic link")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files):
        raise AdapterError("Pinned source file inventory changed")
    for name, checksum in files.items():
        if sha256_file(contained(root, name)) != checksum:
            raise AdapterError("Pinned source content changed")
