import hashlib
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from dmasif_console.sources import ExperimentConfigError, SourceError, read_experiment, snapshot_source


def git(repo, *args):
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo, stderr=subprocess.DEVNULL, text=True).strip()


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "affinity").mkdir()
    (repo / "experiments").mkdir()
    (repo / "affinity/extract.py").write_text("# first source\n")
    (repo / "experiments/run.yaml").write_text("schema_version: 1\njob_type: dmasif_extract\ndataset_id: demo\npreset_id: quick-test\nseed: 0\nrepeat_id: first\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "first")
    settings = SimpleNamespace(state_dir=tmp_path / "state", max_source_bytes=1024*1024, repository=SimpleNamespace(clone_url=str(repo)))
    return repo, settings


def test_snapshot_remains_exact_after_branch_moves(source_repo):
    repo, settings = source_repo
    old_sha = git(repo, "rev-parse", "HEAD")
    (repo / "affinity/extract.py").write_text("# second source\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "second")
    snapshot = snapshot_source(settings, old_sha)
    assert Path(snapshot["source_dir"], "affinity/extract.py").read_text() == "# first source\n"
    assert snapshot["commit_sha"] == old_sha
    assert hashlib.sha256(Path(snapshot["archive_path"]).read_bytes()).hexdigest() == snapshot["sha256"]
    config, _ = read_experiment(snapshot)
    assert config["repeat_id"] == "first"


@pytest.mark.parametrize("kind", ["symlink", "lfs", "submodule"])
def test_unsupported_sources_rejected(source_repo, kind):
    repo, settings = source_repo
    if kind == "symlink":
        (repo / "escape").symlink_to("../../secret")
    elif kind == "lfs":
        (repo / "large.data").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:" + "0"*64 + "\nsize 1234\n")
    else:
        (repo / ".gitmodules").write_text('[submodule "foo"]\npath = foo\nurl = https://example.invalid/repo\n')
    git(repo, "add", ".")
    git(repo, "commit", "-qm", kind)
    with pytest.raises(SourceError):
        snapshot_source(settings, git(repo, "rev-parse", "HEAD"))


def test_export_attributes_cannot_hide_submitted_config(source_repo):
    repo, settings = source_repo
    (repo / ".gitattributes").write_text("experiments/run.yaml export-ignore\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "attributes")
    snapshot = snapshot_source(settings, git(repo, "rev-parse", "HEAD"))
    assert read_experiment(snapshot)[0]["repeat_id"] == "first"


def test_source_size_bound(source_repo):
    repo, settings = source_repo
    (repo / "large.data").write_bytes(b"x" * 4096)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "large")
    settings.max_source_bytes = 1024
    with pytest.raises(SourceError, match="size limit"):
        snapshot_source(settings, git(repo, "rev-parse", "HEAD"))


def test_yaml_cannot_choose_commands(source_repo):
    repo, settings = source_repo
    with (repo / "experiments/run.yaml").open("a") as output:
        output.write("command: curl https://example.invalid\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "unsupported command")
    snapshot = snapshot_source(settings, git(repo, "rev-parse", "HEAD"))
    with pytest.raises(SourceError, match="command"):
        read_experiment(snapshot)


@pytest.mark.parametrize(("config", "expected"), [
    ("dataset_id: demo\nrepeat_id: first\nschema_version: true\n", "schema_version: Must be the integer 1"),
    ("dataset_id: demo\nrepeat_id: first\nschema_version: 1.0\n", "schema_version: Must be the integer 1"),
    ("dataset_id: demo\nrepeat_id: first\nseed: -1\n", "integer from 0 to 4294967295"),
    ("dataset_id: demo\nrepeat_id: first\nseed: 'private-value'\n", "integer from 0 to 4294967295"),
    ("dataset_id: demo\nrepeat_id: first\njob_type: private-value\n", "Must be dmasif_extract"),
    ("dataset_id: demo\n", "repeat_id: Required"),
    ("dataset_id: demo\nrepeat_id: first\nprivate-value: private-value\n", "Only schema_version"),
    ("dataset_id: demo\nrepeat_id: first\nseed: [private-value\n", "line 4, column 1"),
    ("dataset_id: demo\nrepeat_id: first\nseed: 0\nseed: 1\n", "Duplicate YAML fields"),
    ("- private-value\n", "Use a YAML mapping"),
])
def test_invalid_yaml_gives_actionable_safe_diagnostics_and_retains_original(source_repo, config, expected):
    repo, settings = source_repo
    (repo / "experiments/run.yaml").write_text(config)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "invalid request")
    snapshot = snapshot_source(settings, git(repo, "rev-parse", "HEAD"))
    with pytest.raises(ExperimentConfigError) as caught:
        read_experiment(snapshot)
    assert expected in str(caught.value)
    assert "private-value" not in str(caught.value)
    assert "private-value" not in str(caught.value.errors)
    assert caught.value.original_config == config


def test_missing_config_is_a_rejected_request_with_a_retained_source(source_repo):
    repo, settings = source_repo
    (repo / "experiments/run.yaml").unlink()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "missing request")
    snapshot = snapshot_source(settings, git(repo, "rev-parse", "HEAD"))
    assert Path(snapshot["archive_path"]).is_file()
    with pytest.raises(ExperimentConfigError, match="Required file is missing"):
        read_experiment(snapshot)
