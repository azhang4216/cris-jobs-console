"""Live dataset discovery uses synthetic files and processes, never real SSH."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from dmasif_console.config import Settings
from dmasif_console import datasets


@pytest.fixture
def settings(tmp_path):
    project = tmp_path / "project"
    root = project / "job_console"
    root.mkdir(parents=True)
    return Settings(
        repository={"id": 42, "full_name": "lab/research", "clone_url": "https://github.com/lab/research.git"},
        allowed_actor_ids=[7], state_dir=tmp_path / "state",
        cluster={"root": str(root), "host": "private-login.invalid", "user": "private-user",
                 "ssh_key_path": tmp_path / "key", "known_hosts_path": tmp_path / "known-hosts"},
    )


def write(path, value="ATOM coordinates"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


def configure(settings, **entries):
    from dmasif_console.config import DatasetConfig
    settings.datasets = {
        name: DatasetConfig(entries=[{"path": str(path), "sha256": "a" * 64} for path in paths],
                            description="A configured fixture.")
        for name, paths in entries.items()
    }


def by_identity(snapshot):
    return {row["dataset_id"] or row["name"]: row for row in snapshot["datasets"]}


def remote_response(rows=(), **changes):
    return json.dumps({"ok": True, "result": {
        "datasets": list(rows), "checked_at": "2026-10-09T12:00:00+00:00", "truncated": False, **changes,
    }}).encode()


def use_ssh(settings, monkeypatch, response):
    settings.mode = "observe"
    settings.cluster.ssh_key_path.write_bytes(b"synthetic private key")
    monkeypatch.setattr(datasets, "_ssh_read", lambda *args: response)


def test_additions_and_removals_are_discovered_without_a_cache(settings):
    project = Path(settings.cluster.root).parent
    coordinates = write(project / "new_collection" / "model_1" / "system.cif")
    write(project / "meta" / "manifest.parquet", "synthetic manifest")
    write(project / "systems" / "model" / "receptors.txt", "input list")
    write(project / "test_pdbs" / "1STP.pdb1")
    first = datasets.fetch_datasets(settings)
    rows = by_identity(first)
    assert first["source"] == "demo"
    assert first["truncated"] is False
    assert rows["new_collection"]["file_count"] == 1
    assert rows["new_collection"]["dataset_id"] is None
    assert rows["new_collection"]["enabled"] is False
    assert rows["meta"]["kind"] == "manifest"
    assert rows["meta"]["file_count"] is None
    assert rows["systems"]["available"] is True
    coordinates.unlink()
    write(project / "another_collection" / "MODEL.MMCIF")
    second = by_identity(datasets.fetch_datasets(settings))
    assert "new_collection" not in second
    assert second["another_collection"]["file_count"] == 1
    assert "private-login.invalid" not in json.dumps(first)
    assert str(project) not in json.dumps(first)


def test_coordinate_formats_and_runtime_exclusions(settings):
    project = Path(settings.cluster.root).parent
    for suffix in ("cif", "mmcif", "pdb", "pdb1", "ent", "pdb.gz"):
        write(project / "structures" / ("model." + suffix))
    write(project / "structures" / "container.sif")
    for excluded in ("runtime", "outputs", "feats", "dmasif_sandbox", "job_console_tests"):
        write(project / excluded / "model.pdb")
    write(project / ".hidden" / "model.pdb")
    rows = by_identity(datasets.fetch_datasets(settings))
    assert set(rows) == {"structures"}
    assert rows["structures"]["file_count"] == 6


def test_observe_project_root_finds_collections_and_console_release_ids(settings):
    root = Path(settings.cluster.root)
    write(root / "structures" / "model.cif")
    write(root / "systems" / "model_1" / "system.cif")
    write(root / "job_console" / "releases" / "datasets" / "demo-id" / "model.pdb")
    rows = by_identity(datasets.fetch_datasets(settings))
    assert set(rows) == {"structures", "systems", "demo-id"}
    assert rows["structures"]["file_count"] == 1
    assert rows["demo-id"]["enabled"] is False


def test_known_collection_rows_survive_an_exhausted_deadline(settings, monkeypatch):
    project = Path(settings.cluster.root).parent
    names = {"structures", "systems", "test_pdbs", "meta", "meta_strict_v0"}
    for name in names:
        write(project / name / "model.cif")
    ticks = iter([0] + [16] * 100)
    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: next(ticks))
        snapshot = datasets.fetch_datasets(settings)
    rows = by_identity(snapshot)
    assert set(rows) == names
    assert snapshot["truncated"] is True
    assert all(row["available"] and row["file_count"] is None for row in rows.values())


def test_registered_ids_match_experiment_grammar_and_descriptions_do_not_leak(settings, tmp_path):
    valid = write(tmp_path / "private-input.pdb")
    configure(settings, **{"_fixture": [valid], ".fixture": [valid], "-fixture": [valid]})
    for configured in settings.datasets.values():
        configured.description = str(valid) + " private-user private-login.invalid PRIVATE_SECRET"
    result = datasets.fetch_datasets(settings)
    assert all(row["enabled"] for row in result["datasets"])
    public = json.dumps(result)
    for secret in (str(valid), "private-user", "private-login.invalid", "PRIVATE_SECRET"):
        assert secret not in public


def test_registered_ids_include_external_inputs_and_require_every_input(settings, tmp_path):
    external = write(tmp_path / "external" / "valid.pdb")
    missing = tmp_path / "external" / "missing.cif"
    configure(settings, external_fixture=[external], missing_fixture=[external, missing])
    rows = by_identity(datasets.fetch_datasets(settings))
    assert rows["external_fixture"]["available"] is True
    assert rows["external_fixture"]["enabled"] is True
    assert rows["external_fixture"]["kind"] == "registered"
    assert rows["missing_fixture"]["file_count"] == 1
    assert rows["missing_fixture"]["available"] is False
    assert rows["missing_fixture"]["enabled"] is False
    write(missing)
    assert by_identity(datasets.fetch_datasets(settings))["missing_fixture"]["enabled"] is True
    external.unlink()
    assert by_identity(datasets.fetch_datasets(settings))["external_fixture"]["enabled"] is False


def test_discovered_release_ids_do_not_enable_console_runs(settings):
    root = Path(settings.cluster.root)
    first = write(root / "releases" / "datasets" / "registered" / "input.pdb")
    write(root / "releases" / "datasets" / "new-id" / "input.cif")
    configure(settings, registered=[first])
    rows = by_identity(datasets.fetch_datasets(settings))
    assert set(rows) == {"registered", "new-id"}
    assert rows["registered"]["enabled"] is True
    assert rows["new-id"]["available"] is True
    assert rows["new-id"]["dataset_id"] == "new-id"
    assert rows["new-id"]["enabled"] is False


def test_missing_unreadable_symlink_and_oversized_registered_inputs(settings, tmp_path):
    valid = write(tmp_path / "inputs" / "valid.pdb", "x" * 600)
    second = write(tmp_path / "inputs" / "second.pdb", "x" * 600)
    linked = tmp_path / "inputs" / "linked.pdb"
    linked.symlink_to(valid)
    unreadable = write(tmp_path / "inputs" / "unreadable.pdb")
    unreadable.chmod(0o000)
    configure(settings, linked=[linked], unreadable=[unreadable], oversized=[valid, second])
    settings.max_input_bytes = 1024
    rows = by_identity(datasets.fetch_datasets(settings))
    assert all(not row["available"] and not row["enabled"] for row in rows.values())


def test_collection_symlinks_are_not_followed(settings, tmp_path):
    project = Path(settings.cluster.root).parent
    outside = write(tmp_path / "outside" / "model.cif")
    (project / "structures").mkdir()
    (project / "structures" / "linked.cif").symlink_to(outside)
    (project / "linked_collection").symlink_to(outside.parent, target_is_directory=True)
    rows = by_identity(datasets.fetch_datasets(settings))
    assert set(rows) == {"structures"}
    assert rows["structures"]["file_count"] == 0
    assert rows["structures"]["available"] is False


def test_depth_and_row_limits_are_explicit(settings):
    project = Path(settings.cluster.root).parent
    write(project / "structures" / Path(*(["nested"] * 8)) / "model.cif")
    snapshot = datasets.fetch_datasets(settings)
    assert snapshot["truncated"] is True
    assert by_identity(snapshot)["structures"]["file_count"] is None
    for index in range(205):
        write(Path(settings.cluster.root) / "releases" / "datasets" / f"dataset-{index}" / "model.cif")
    snapshot = datasets.fetch_datasets(settings)
    assert len(snapshot["datasets"]) == datasets.MAX_DATASETS
    assert snapshot["truncated"] is True


def test_remote_script_executes_the_same_live_scan(settings):
    project = Path(settings.cluster.root).parent
    write(project / "structures" / "model.cif")
    request = {"root": settings.cluster.root, "registered": {}, "max_input_bytes": 1024}
    encoded = base64.b64encode(json.dumps(request).encode()).decode()
    script = "REQUEST_B64 = '" + encoded + "'\n" + datasets.REMOTE_SCRIPT
    completed = subprocess.run([sys.executable, "-B", "-s"], input=script, text=True,
                               capture_output=True, check=True, timeout=5)
    response = json.loads(completed.stdout)
    assert response["ok"] is True
    assert response["result"]["datasets"][0]["name"] == "structures"


def test_ssh_is_pinned_read_only_and_uses_an_ephemeral_private_key(settings, monkeypatch):
    use_ssh(settings, monkeypatch, remote_response())
    settings.cluster.ssh_timeout_seconds = 300
    seen = {}

    def read(argv, script, timeout):
        seen["key"] = Path(argv[argv.index("-i") + 1])
        assert seen["key"] != settings.cluster.ssh_key_path
        assert seen["key"].stat().st_mode & 0o777 == 0o600
        assert seen["key"].read_bytes() == b"synthetic private key"
        assert timeout <= 35
        assert argv[-1] == "python3 -B -s"
        assert argv[argv.index("-F") + 1] == "/dev/null"
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes",
                       "ForwardAgent=no", "ClearAllForwardings=yes"):
            assert option in argv
        for forbidden in (b"sbatch", b"scancel", b"srun", b"helper.py", b"write_text", b"write_bytes"):
            assert forbidden not in script
        assert b"synthetic private key" not in script
        return remote_response()

    monkeypatch.setattr(datasets, "_ssh_read", read)
    assert datasets.fetch_datasets(settings)["source"] == "ssh"
    assert not seen["key"].exists()


@pytest.mark.parametrize("raw", [
    b"private SSH diagnostic /secret/path", b"[]", b'{"ok":false}',
    remote_response(truncated="false"), remote_response(checked_at="2026-10-09"),
    remote_response(extra="/private/path"), b"x" * (datasets.MAX_RESPONSE_BYTES + 1),
])
def test_remote_errors_are_fixed_and_credential_safe(settings, monkeypatch, raw):
    use_ssh(settings, monkeypatch, raw)
    with pytest.raises(datasets.DatasetDiscoveryError) as error:
        datasets.fetch_datasets(settings)
    assert str(error.value) == datasets.DISCOVERY_ERROR


@pytest.mark.parametrize("changes", [
    {"dataset_id": "../private"}, {"kind": "registered"}, {"enabled": True},
    {"path": "/private/path"}, {"file_count": True}, {"file_count": 50001},
    {"available": "yes"}, {"name": "/private/path"}, {"kind": []},
])
def test_remote_records_are_allowlisted_and_cannot_enable_unknown_ids(settings, monkeypatch, changes):
    row = {"name": "new-id", "dataset_id": "new-id", "kind": "collection",
           "scope": "release", "file_count": 1, "available": True, **changes}
    use_ssh(settings, monkeypatch, remote_response([row]))
    with pytest.raises(datasets.DatasetDiscoveryError):
        datasets.fetch_datasets(settings)


def test_timeout_is_safe_and_cleans_up_the_temporary_key(settings, monkeypatch):
    use_ssh(settings, monkeypatch, remote_response())
    seen = {}

    def timeout(argv, *_):
        seen["key"] = Path(argv[argv.index("-i") + 1])
        raise TimeoutError("private-user@private-host /private/path")

    monkeypatch.setattr(datasets, "_ssh_read", timeout)
    with pytest.raises(datasets.DatasetDiscoveryError, match="Live dataset discovery failed"):
        datasets.fetch_datasets(settings)
    assert not seen["key"].exists()


def test_catalog_presence_checks_do_not_count_large_collections(settings):
    project = Path(settings.cluster.root).parent
    for index in range(300):
        write(project / "structures" / f"model-{index:04}.cif")
    result = datasets.fetch_datasets(settings)
    row = by_identity(result)["structures"]
    assert row["scope"] == "project"
    assert row["available"] is True
    assert row["file_count"] is None
    assert result["truncated"] is True


@pytest.mark.parametrize("collection", ["structures", "systems"])
def test_deep_model_pages_slice_before_metadata_checks(settings, monkeypatch, collection):
    base = Path(settings.cluster.root).parent / collection
    for index in range(501):
        relative = f"model-{index:04}.cif" if collection == "structures" else f"model-{index:04}/system.cif"
        write(base / relative)
    original_stat = os.lstat
    checked = []

    def stat(path, *args, **kwargs):
        if str(path).startswith(str(base) + "/"):
            checked.append(str(path))
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", stat)
        result = datasets.fetch_dataset_files(settings, "project", collection, page=10)
    assert result["page"] == 10
    assert result["page_size"] == 50
    assert result["has_more"] is True
    assert result["truncated"] is False
    assert result["source"] == "demo"
    assert len(result["files"]) == 50
    assert result["files"][0]["model_id"] == "model-0450"
    assert result["files"][-1]["model_id"] == "model-0499"
    assert len(checked) <= 100
    assert all("model-0000" not in path for path in checked)
    final = datasets.fetch_dataset_files(settings, "project", collection, page=11)
    assert len(final["files"]) == 1
    assert final["has_more"] is False


def test_file_pages_refresh_and_show_relative_coordinate_names_only(settings):
    project = Path(settings.cluster.root).parent
    first = write(project / "test_pdbs" / "A.pdb")
    write(project / "test_pdbs" / "assembly" / "B.pdb1")
    write(project / "test_pdbs" / "other" / "model.cif.gz")
    write(project / "test_pdbs" / "runtime.sif")
    result = datasets.fetch_dataset_files(settings, "project", "test_pdbs", page_size=2)
    assert result["has_more"] is True
    assert result["files"] == [
        {"name": "A.pdb", "model_id": "A", "relative_path": "A.pdb", "format": "PDB"},
        {"name": "B.pdb1", "model_id": "B", "relative_path": "assembly/B.pdb1", "format": "PDB"},
    ]
    assert str(project) not in json.dumps(result)
    second = datasets.fetch_dataset_files(settings, "project", "test_pdbs", page=2, page_size=2)
    assert second["files"][0]["name"] == "model.cif.gz"
    assert second["files"][0]["format"] == "CIF"
    assert second["has_more"] is False
    first.unlink()
    assert datasets.fetch_dataset_files(settings, "project", "test_pdbs")["files"][0]["name"] == "B.pdb1"


def test_registered_and_release_pages_resolve_pinned_inputs(settings, tmp_path):
    external = write(tmp_path / "private-parent" / "input.cif")
    configure(settings, fixture=[external])
    write(Path(settings.cluster.root) / "releases" / "datasets" / "fixture" / "another.pdb")
    registered = datasets.fetch_dataset_files(settings, "registered", "fixture")
    release = datasets.fetch_dataset_files(settings, "release", "fixture")
    assert registered["files"][0]["relative_path"] == "input.cif"
    assert release["files"][0]["relative_path"] == "another.pdb"
    assert "private-parent" not in json.dumps(registered)


@pytest.mark.parametrize("scope,name", [
    ("project", "."), ("project", ".."), ("project", "../structures"), ("project", "/structures"),
    ("project", "runtime"), ("project", "dmasif_sandbox"), ("project", "job_console"),
    ("project", ".hidden"), ("release", ".hidden"), ("release", "../../private"),
    ("registered", "unknown"), ("unexpected", "structures"),
])
def test_file_selectors_cannot_browse_unlisted_private_directories(settings, scope, name):
    project = Path(settings.cluster.root).parent
    for private in ("runtime", "dmasif_sandbox", "job_console", ".hidden"):
        write(project / private / "private.cif")
    write(Path(settings.cluster.root) / "releases" / "datasets" / ".hidden" / "private.cif")
    with pytest.raises(datasets.DatasetDiscoveryError):
        datasets.fetch_dataset_files(settings, scope, name)


def test_file_pages_reject_symlink_collections_ancestors_and_files(settings, tmp_path):
    project = Path(settings.cluster.root).parent
    external = write(tmp_path / "private" / "secret.cif")
    (project / "linked").symlink_to(external.parent, target_is_directory=True)
    with pytest.raises(datasets.DatasetDiscoveryError):
        datasets.fetch_dataset_files(settings, "project", "linked")
    (project / "structures").mkdir()
    (project / "structures" / "linked.cif").symlink_to(external)
    assert datasets.fetch_dataset_files(settings, "project", "structures")["files"] == []
    (Path(settings.cluster.root) / "releases").symlink_to(external.parent, target_is_directory=True)
    write(external.parent / "datasets" / "linked" / "secret.cif")
    with pytest.raises(datasets.DatasetDiscoveryError):
        datasets.fetch_dataset_files(settings, "release", "linked")


@pytest.mark.parametrize("changes", [
    {"relative_path": "/private/file.cif"}, {"relative_path": "../file.cif"},
    {"path": "/private/file.cif"}, {"model_id": "../file"}, {"format": "PRIVATE_SECRET"},
])
def test_file_response_schema_rejects_private_paths(settings, monkeypatch, changes):
    row = {"name": "file.cif", "relative_path": "file.cif", "model_id": "file", "format": "CIF", **changes}
    result = {"files": [row], "page": 1, "page_size": 50, "has_more": False,
              "truncated": False, "checked_at": "2026-10-09T12:00:00+00:00"}
    use_ssh(settings, monkeypatch, json.dumps({"ok": True, "result": result}).encode())
    with pytest.raises(datasets.DatasetDiscoveryError) as error:
        datasets.fetch_dataset_files(settings, "project", "structures")
    assert str(error.value) == datasets.DISCOVERY_ERROR
