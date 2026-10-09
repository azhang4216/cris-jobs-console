"""Live, bounded, read-only discovery of project datasets and coordinate files."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import re

from .observer import _ssh_read
from .runtime_secrets import private_key_copy


MAX_RESPONSE_BYTES = 1024 * 1024
MAX_DATASETS = 200
DISCOVERY_ERROR = "Live dataset discovery failed. Refresh to try again."


class DatasetDiscoveryError(RuntimeError):
    """A safe public error; never includes SSH diagnostics or private paths."""


def _scan(request):
    # Keep this function self-contained: its fixed source is sent to python on
    # the login node, and the same scanner handles local demo fixtures.
    from datetime import datetime, timezone
    import os
    import re
    import stat
    import time

    max_rows, max_entries, max_depth = 200, 50000, 6
    deadline = time.monotonic() + 15
    rows, seen = [], set()
    truncated, examined, incomplete_walks = False, 0, 0
    excluded = {
        "__pycache__", "node_modules", "venv", "env", "site-packages",
        "runtime", "runtimes", "containers", "images", "checkpoints",
        "cache", "caches", "tmp", "temp", "logs", "runs", "jobs", "state",
        "output", "outputs", "results", "artifacts", "features", "feats", "embeddings",
        "src", "code", "scripts", "tests", "docs", "bin", "lib", "include",
        "releases", "adapters", "dmasif", "dmasif_console", "cluster_adapter", "job_console", "job_console_tests",
    }
    known = ("structures", "systems", "test_pdbs", "meta", "meta_strict_v0")
    markers = {"manifest.parquet", "receptors.txt", "extract_inputs.txt"}
    suffixes = (".cif", ".mmcif", ".pdb", ".pdb1", ".ent")

    def excluded_directory(name):
        return name.startswith(".") or name.lower() in excluded or name.lower().endswith("_sandbox")

    def budget():
        nonlocal truncated
        if examined >= max_entries or time.monotonic() >= deadline:
            truncated = True
            return False
        return True

    def valid_name(name):
        return (isinstance(name, str) and 0 < len(name) <= 128
                and not any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name))

    def add(row):
        nonlocal truncated
        if len(rows) >= max_rows or not valid_name(row["name"]):
            truncated = True
            return
        rows.append(row)

    def readable_file(path):
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o444 or not os.access(path, os.R_OK):
                return None
            return info.st_size
        except OSError:
            return None

    def directory(path):
        try:
            return stat.S_ISDIR(os.lstat(path).st_mode)
        except OSError:
            return False

    def iter_children(path):
        nonlocal examined, truncated, incomplete_walks
        if not directory(path):
            return
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if not budget():
                        incomplete_walks += 1
                        break
                    examined += 1
                    yield entry
        except OSError:
            truncated = True
            incomplete_walks += 1

    def children(path):
        return sorted(iter_children(path), key=lambda entry: entry.name.casefold())

    def count_collection(path):
        nonlocal truncated
        count, coordinates, manifest, complete = 0, False, False, True
        initial_incomplete_walks = incomplete_walks
        inspected = 0

        def walk(current, depth):
            nonlocal count, coordinates, manifest, complete, inspected, truncated
            if not budget():
                complete = False
                return
            for entry in iter_children(current):
                inspected += 1
                if inspected > 256:
                    # The catalog only establishes presence. Large coordinate
                    # collections are browsed separately, without counting
                    # hundreds of thousands of files on every page load.
                    truncated, complete = True, False
                    return
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if excluded_directory(entry.name):
                            continue
                        if depth >= max_depth:
                            truncated, complete = True, False
                        else:
                            walk(entry.path, depth + 1)
                            if inspected > 256:
                                return
                    elif entry.is_file(follow_symlinks=False):
                        name = entry.name.lower()
                        if name.endswith(".gz"):
                            name = name[:-3]
                        if name.endswith(suffixes):
                            count += 1
                            # Count regular coordinate files without issuing
                            # lstat/access twice for every file on shared NFS.
                            # One readable input establishes collection availability.
                            if not coordinates:
                                coordinates = readable_file(entry.path) is not None
                        elif entry.name in markers and readable_file(entry.path) is not None:
                            manifest = True
                except OSError:
                    truncated, complete = True, False
        walk(path, 0)
        # A partial count is not an exact count, so keep it explicitly unknown.
        return count if complete and initial_incomplete_walks == incomplete_walks else None, coordinates, manifest

    registered = request["registered"]
    for dataset_id, paths in registered.items():
        if len(rows) >= max_rows or not budget():
            truncated = True
            break
        sizes = []
        for path in paths:
            if not budget():
                break
            sizes.append(readable_file(path))
        available = (len(sizes) == len(paths) and bool(paths)
                     and all(size is not None for size in sizes)
                     and sum(size or 0 for size in sizes) <= request["max_input_bytes"])
        add(dict(name=dataset_id, dataset_id=dataset_id, kind="registered",
                 scope="registered", file_count=sum(size is not None for size in sizes), available=available))
        seen.add(dataset_id)

    root = request["root"]
    # Operators use either the console root or the project itself in observe
    # mode. Detect the latter by its established collection directories.
    project = root if any(directory(os.path.join(root, name)) for name in known) else (
        os.path.dirname(root.rstrip(os.sep)) or os.sep)
    candidates = {name: os.path.join(project, name) for name in known
                  if directory(os.path.join(project, name))}
    known_count = len(candidates)
    for entry in children(project):
        if entry.path == root or excluded_directory(entry.name) or not directory(entry.path):
            continue
        candidates[entry.name] = entry.path

    release_root = os.path.join(root, "releases", "datasets")
    if project == root:
        release_root = os.path.join(root, "job_console", "releases", "datasets")
    for entry in children(release_root):
        if not budget() or len(rows) >= max_rows - known_count:
            truncated = True
            break
        if entry.name in seen or entry.name.startswith(".") or not directory(entry.path):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", entry.name):
            truncated = True
            continue
        count, coordinates, manifest = count_collection(entry.path)
        add(dict(name=entry.name, dataset_id=entry.name, kind="collection",
                 scope="release", file_count=count, available=coordinates or manifest))

    ordered = sorted(candidates, key=lambda name: (name not in known, name.casefold()))
    for name in ordered:
        if not budget() or len(rows) >= max_rows:
            truncated = True
            if name not in known:
                continue
            count, coordinates = None, False
            manifest = any(readable_file(os.path.join(candidates[name], marker)) is not None for marker in markers)
        else:
            count, coordinates, manifest = count_collection(candidates[name])
        if coordinates or manifest or name in known:
            kind = "manifest" if manifest and not coordinates else "collection"
            add(dict(name=name, dataset_id=None, kind=kind, scope="project",
                     file_count=count if kind != "manifest" else None,
                     available=coordinates or manifest or (count is None and os.access(candidates[name], os.R_OK))))
    return dict(datasets=rows, checked_at=datetime.now(timezone.utc).isoformat(), truncated=truncated)


def _scan_files(request):
    """List a bounded page of coordinate names, never file contents."""
    from datetime import datetime, timezone
    import os
    import stat
    import time

    deadline = time.monotonic() + 20
    listed, inspected, name_bytes, truncated = 0, 0, 0, False
    page, page_size = request["page"], request["page_size"]
    offset = (page - 1) * page_size
    known = ("structures", "systems", "test_pdbs", "meta", "meta_strict_v0")
    excluded = {
        "__pycache__", "node_modules", "venv", "env", "site-packages",
        "runtime", "runtimes", "containers", "images", "checkpoints",
        "cache", "caches", "tmp", "temp", "logs", "runs", "jobs", "state",
        "output", "outputs", "results", "artifacts", "features", "feats", "embeddings",
        "src", "code", "scripts", "tests", "docs", "bin", "lib", "include",
        "releases", "adapters", "dmasif", "dmasif_console", "cluster_adapter", "job_console", "job_console_tests",
    }

    def excluded_directory(name):
        return name.startswith(".") or name.lower() in excluded or name.lower().endswith("_sandbox")

    def coordinate_format(name):
        lower = name.lower()
        if lower.endswith(".gz"):
            lower = lower[:-3]
        for suffix in (".mmcif", ".cif", ".pdb1", ".pdb", ".ent"):
            if lower.endswith(suffix):
                return "CIF" if suffix in {".cif", ".mmcif"} else "PDB"
        return None

    def safe_name(name):
        return (isinstance(name, str) and 0 < len(name) <= 255 and name not in {".", ".."}
                and not any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name))

    def directory(path):
        try:
            return stat.S_ISDIR(os.lstat(path).st_mode)
        except OSError:
            return False

    def directory_chain(path, anchor):
        if not directory(anchor):
            return False
        current = anchor
        for component in os.path.relpath(path, anchor).split(os.sep):
            if component in {".", ".."}:
                return False
            current = os.path.join(current, component)
            if not directory(current):
                return False
        return True

    def names(path):
        nonlocal listed, name_bytes, truncated
        result = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    listed += 1
                    name_bytes += len(entry.name.encode("utf-8", errors="replace"))
                    if listed > 600000 or name_bytes > 64 * 1024 * 1024 or time.monotonic() >= deadline:
                        truncated = True
                        break
                    if safe_name(entry.name) and not entry.name.startswith("."):
                        result.append(entry.name)
        except OSError:
            truncated = True
        return sorted(result, key=lambda name: (name.casefold(), name))

    def entry(path, relative):
        nonlocal inspected, truncated
        inspected += 1
        if inspected > 20000 or time.monotonic() >= deadline:
            truncated = True
            return None
        file_format = coordinate_format(os.path.basename(path))
        if file_format is None:
            return None
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o444 or not os.access(path, os.R_OK):
                return None
        except OSError:
            return None
        parts = relative.split("/")
        if not all(safe_name(part) for part in parts):
            truncated = True
            return None
        filename = parts[-1]
        model = filename[:-3] if filename.lower().endswith(".gz") else filename
        model = model.rsplit(".", 1)[0]
        if model.lower() == "system" and len(parts) > 1:
            model = parts[-2]
        return dict(name=filename, model_id=model, relative_path=relative, format=file_format)

    scope, selected = request["scope"], request["name"]
    if not safe_name(selected):
        raise ValueError("Invalid collection selector")
    files, has_more = [], False
    if scope == "registered":
        paths = request["registered"][selected]
        ordered = sorted(paths, key=lambda path: (os.path.basename(path).casefold(), os.path.basename(path)))
        has_more = len(ordered) > offset + page_size
        for path in ordered[offset:offset + page_size]:
            value = entry(path, os.path.basename(path))
            if value is not None:
                files.append(value)
    else:
        root = request["root"]
        project = root if any(directory(os.path.join(root, name)) for name in known) else (
            os.path.dirname(root.rstrip(os.sep)) or os.sep)
        if scope == "project":
            if excluded_directory(selected):
                raise ValueError("Invalid collection selector")
            base = os.path.join(project, selected)
            if base == root:
                raise ValueError("Invalid collection selector")
        elif scope == "release":
            if selected.startswith("."):
                raise ValueError("Invalid collection selector")
            console = os.path.join(project, "job_console") if project == root else root
            base = os.path.join(console, "releases", "datasets", selected)
        else:
            raise ValueError("Invalid collection scope")
        if not directory_chain(base, project):
            raise ValueError("Collection is unavailable")

        if scope == "project" and selected in {"structures", "systems"}:
            # These project collections have a stable one-model-per-name
            # layout. Slice names before stat calls so page 4000 costs about
            # the same as page 1, even with hundreds of thousands of models.
            candidates = names(base)
            if selected == "structures":
                candidates = [name for name in candidates if coordinate_format(name)]
            else:
                candidates = [name for name in candidates if not excluded_directory(name)]
            has_more = len(candidates) > offset + page_size
            for name in candidates[offset:offset + page_size]:
                if selected == "structures":
                    value = entry(os.path.join(base, name), name)
                elif directory(os.path.join(base, name)):
                    relative = name + "/system.cif"
                    value = entry(os.path.join(base, name, "system.cif"), relative)
                else:
                    value = None
                if value is not None:
                    files.append(value)
        else:
            matched = 0

            def walk(current, prefix="", depth=0):
                nonlocal matched, has_more, inspected, truncated
                for name in names(current):
                    if has_more or inspected >= 20000 or time.monotonic() >= deadline:
                        if not has_more:
                            truncated = True
                        return
                    path = os.path.join(current, name)
                    relative = prefix + name
                    value = entry(path, relative)
                    if value is not None:
                        matched += 1
                        if matched > offset + page_size:
                            has_more = True
                            return
                        if matched > offset:
                            files.append(value)
                    elif not excluded_directory(name) and directory(path):
                        if depth >= 6:
                            truncated = True
                        else:
                            walk(path, relative + "/", depth + 1)
            walk(base)
    return dict(files=files, page=page, page_size=page_size, has_more=has_more,
                checked_at=datetime.now(timezone.utc).isoformat(), truncated=truncated)


# No cluster helper is installed or invoked, and no user value becomes code.
REMOTE_SCRIPT = inspect.getsource(_scan) + "\n" + inspect.getsource(_scan_files) + r'''
import base64
import json
try:
    request = json.loads(base64.b64decode(REQUEST_B64))
    result = _scan_files(request) if request.get("operation") == "files" else _scan(request)
    encoded = json.dumps({"ok": True, "result": result}, separators=(",", ":"))
    if len(encoded.encode()) > 1024 * 1024:
        raise ValueError("response limit")
    print(encoded)
except Exception:
    print('{"ok":false}')
'''


def _public_result(result, settings, source):
    if not isinstance(result, dict) or set(result) != {"datasets", "checked_at", "truncated"}:
        raise ValueError("Invalid dataset response")
    if not isinstance(result["datasets"], list) or len(result["datasets"]) > MAX_DATASETS:
        raise ValueError("Invalid dataset list")
    if type(result["truncated"]) is not bool:
        raise ValueError("Invalid truncation flag")
    timestamp = result["checked_at"]
    if not isinstance(timestamp, str) or len(timestamp) > 50:
        raise ValueError("Invalid dataset timestamp")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Dataset timestamp requires a timezone")
    rows, identities = [], set()
    fields = {"name", "dataset_id", "kind", "scope", "file_count", "available"}
    for row in result["datasets"]:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("Invalid dataset record")
        name, dataset_id, kind, scope = row["name"], row["dataset_id"], row["kind"], row["scope"]
        if (not isinstance(name, str) or not 0 < len(name) <= 128
                or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name)):
            raise ValueError("Invalid dataset name")
        if dataset_id is not None and (not isinstance(dataset_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", dataset_id)):
            raise ValueError("Invalid dataset ID")
        if kind not in {"registered", "collection", "manifest"}:
            raise ValueError("Invalid dataset kind")
        if scope not in {"registered", "release", "project"}:
            raise ValueError("Invalid dataset scope")
        if ((scope == "registered") != (kind == "registered")
                or (scope == "project") != (dataset_id is None)):
            raise ValueError("Inconsistent dataset scope")
        if type(row["available"]) is not bool:
            raise ValueError("Invalid availability")
        count = row["file_count"]
        if count is not None and (type(count) is not int or not 0 <= count <= 50000):
            raise ValueError("Invalid file count")
        identity = (dataset_id, name if dataset_id is None else None)
        if identity in identities:
            raise ValueError("Duplicate dataset")
        identities.add(identity)
        configured = settings.datasets.get(dataset_id)
        if kind == "registered" and (configured is None or name != dataset_id):
            raise ValueError("Unknown registered dataset")
        if configured is not None and kind != "registered":
            raise ValueError("Configured dataset has invalid kind")
        if configured is not None:
            total = len(configured.entries)
            description = f"Registered dataset with {total} input file{'s' if total != 1 else ''}."
        elif dataset_id is not None:
            description = "Dataset collection found on the cluster; not registered for console runs."
        elif kind == "manifest":
            description = "Metadata tables, receptor lists, or ligand fingerprints; not atomic coordinate files."
        elif scope == "project" and name == "structures":
            description = "Protein receptor coordinates; ligand files are stored separately in systems."
        elif scope == "project" and name == "systems":
            description = "Protein–ligand complex coordinates, with separate ligand SDF files alongside the systems."
        elif scope == "project" and name == "test_pdbs":
            description = "Small PDB structure fixtures for extraction tests."
        else:
            description = "Collection of coordinate filenames; molecular contents are not checked by this listing."
        rows.append({**row, "description": description,
                     "enabled": configured is not None and row["available"]})
    if not result["truncated"] and not set(settings.datasets).issubset(
            {row["dataset_id"] for row in rows if row["kind"] == "registered"}):
        raise ValueError("Missing registered datasets")
    return {"datasets": rows, "checked_at": parsed.astimezone(timezone.utc).isoformat(),
            "source": source, "truncated": result["truncated"]}


def _request(settings, **operation):
    return {"root": str(Path(settings.cluster.root)),
            "registered": {name: [entry.path for entry in dataset.entries]
                           for name, dataset in settings.datasets.items()},
            "max_input_bytes": settings.max_input_bytes, **operation}


def _query(settings, request):
    if settings.mode == "fake":
        scanner = _scan_files if request.get("operation") == "files" else _scan
        return scanner(request), "demo"
    cluster = settings.cluster
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", cluster.host)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", cluster.user)
            or not cluster.ssh_key_path or not cluster.known_hosts_path):
        raise ValueError("Pinned SSH credentials are required")
    encoded = base64.b64encode(json.dumps(request).encode()).decode("ascii")
    script = ("REQUEST_B64 = '" + encoded + "'\n" + REMOTE_SCRIPT).encode()
    argv = ["ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2",
            "-o", "UserKnownHostsFile=" + str(cluster.known_hosts_path), "-i", "",
            "--", cluster.user + "@" + cluster.host, "python3 -B -s"]
    with private_key_copy(cluster.ssh_key_path) as key:
        argv[argv.index("-i") + 1] = str(key)
        raw = _ssh_read(argv, script, min(35, cluster.ssh_timeout_seconds))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Dataset response is too large")
    response = json.loads(raw)
    if not isinstance(response, dict) or response.get("ok") is not True or set(response) != {"ok", "result"}:
        raise ValueError("Dataset query failed")
    return response["result"], "ssh"


def fetch_datasets(settings) -> dict:
    """Fetch a new snapshot for every call; only fake mode uses local inputs."""
    try:
        result, source = _query(settings, _request(settings))
        return _public_result(result, settings, source)
    except Exception:
        raise DatasetDiscoveryError(DISCOVERY_ERROR) from None


def _public_files(result, source, page, page_size):
    fields = {"files", "page", "page_size", "has_more", "checked_at", "truncated"}
    if not isinstance(result, dict) or set(result) != fields:
        raise ValueError("Invalid file response")
    if (type(result["page"]) is not int or result["page"] != page
            or type(result["page_size"]) is not int or result["page_size"] != page_size
            or type(result["has_more"]) is not bool or type(result["truncated"]) is not bool):
        raise ValueError("Invalid file pagination")
    if not isinstance(result["files"], list) or len(result["files"]) > page_size:
        raise ValueError("Invalid file list")
    for row in result["files"]:
        if not isinstance(row, dict) or set(row) != {"name", "model_id", "relative_path", "format"}:
            raise ValueError("Invalid file record")
        for field in ("name", "model_id", "relative_path"):
            value = row[field]
            if (not isinstance(value, str) or not 0 < len(value) <= (2048 if field == "relative_path" else 255)
                    or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError("Invalid file label")
        parts = row["relative_path"].split("/")
        if (any(not part or part in {".", ".."} for part in parts)
                or row["name"] != parts[-1] or "/" in row["model_id"]
                or row["format"] not in {"CIF", "PDB"}):
            raise ValueError("Invalid relative file path")
    timestamp = result["checked_at"]
    if not isinstance(timestamp, str) or len(timestamp) > 50:
        raise ValueError("Invalid file timestamp")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("File timestamp requires a timezone")
    return {**result, "checked_at": parsed.astimezone(timezone.utc).isoformat(), "source": source}


def fetch_dataset_files(settings, scope, name, page=1, page_size=50) -> dict:
    """Read one live page selected by a collection name, never a browser path."""
    try:
        if (scope not in {"registered", "release", "project"}
                or not isinstance(name, str) or not 0 < len(name) <= 128 or name in {".", ".."}
                or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name)
                or type(page) is not int or not 1 <= page <= 100000
                or type(page_size) is not int or not 1 <= page_size <= 100):
            raise ValueError("Invalid file selector")
        if scope == "registered" and name not in settings.datasets:
            raise ValueError("Unknown registered dataset")
        if scope != "registered" and name.startswith("."):
            raise ValueError("Invalid collection selector")
        request = _request(settings, operation="files", scope=scope, name=name, page=page, page_size=page_size)
        result, source = _query(settings, request)
        return _public_files(result, source, page, page_size)
    except Exception:
        raise DatasetDiscoveryError(DISCOVERY_ERROR) from None
