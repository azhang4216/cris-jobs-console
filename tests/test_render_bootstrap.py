from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from dmasif_console import render_bootstrap as bootstrap


@pytest.fixture
def secrets_env(tmp_path):
    root = tmp_path / "home" / ".config" / "dmasif-console"
    values = {
        "DMASIF_OPERATOR_YAML": "mode: fake\n# synthetic operator value",
        "DMASIF_SSH_PRIVATE_KEY": "-----BEGIN SYNTHETIC KEY-----\nfirst-line\nsecond-line\n-----END SYNTHETIC KEY-----",
        "DMASIF_SSH_KNOWN_HOSTS": "example.invalid ssh-ed25519 SYNTHETIC\n",
        "DMASIF_REPOSITORY_TOKEN": "synthetic-repository-token",
        "DMASIF_WEBHOOK_SECRET": "synthetic-webhook-secret",
        "PORT": "10000",
    }
    for _, variable, basename in bootstrap.REQUIRED:
        values[variable] = str(root / basename)
    values[bootstrap.TOKEN_FILE] = str(root / bootstrap.TOKEN_NAME)
    return root, values


def test_materializes_private_files_preserves_multiline_and_removes_raw_values(secrets_env):
    root, values = secrets_env
    original = dict(values)
    child = bootstrap.prepare_environment(values, private_dir=root)
    assert values == original
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for raw_name, file_name, _ in bootstrap.REQUIRED:
        path = Path(child[file_name])
        assert path.read_text() == values[raw_name].rstrip("\n") + "\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert raw_name not in child
    assert (root / bootstrap.TOKEN_NAME).read_text() == values[bootstrap.TOKEN_VALUE] + "\n"
    assert stat.S_IMODE((root / bootstrap.TOKEN_MARKER).stat().st_mode) == 0o600
    assert bootstrap.TOKEN_VALUE not in child
    assert child["DMASIF_WEBHOOK_SECRET"] == values["DMASIF_WEBHOOK_SECRET"]
    assert child["PORT"] == "10000"


def test_key_keeps_existing_newlines_and_rotation_replaces_atomically(secrets_env):
    root, values = secrets_env
    values["DMASIF_SSH_PRIVATE_KEY"] += "\n\n"
    bootstrap.prepare_environment(values, private_dir=root)
    key = root / "cluster-key"
    assert key.read_text() == values["DMASIF_SSH_PRIVATE_KEY"]
    previous_inode = key.stat().st_ino
    with key.open() as previous:
        values["DMASIF_SSH_PRIVATE_KEY"] = "new-synthetic-key"
        bootstrap.prepare_environment(values, private_dir=root)
        assert "second-line" in previous.read()
        assert key.read_text() == "new-synthetic-key\n"
        assert key.stat().st_ino != previous_inode
    assert not list(root.glob(".secret-*"))


@pytest.mark.parametrize("missing", [item[0] for item in bootstrap.REQUIRED])
def test_missing_required_input_fails_with_setting_names_only(secrets_env, missing):
    root, values = secrets_env
    del values[missing]
    file_name = next(item[1] for item in bootstrap.REQUIRED if item[0] == missing)
    with pytest.raises(bootstrap.BootstrapError) as error:
        bootstrap.prepare_environment(values, private_dir=root)
    assert missing in str(error.value) and file_name in str(error.value)
    assert "synthetic" not in str(error.value)
    assert not (root / "cluster-key").exists()


@pytest.mark.parametrize("value", ["", " \n", "x" * (bootstrap.MAX_SECRET_BYTES + 1), "key\x00fragment"])
def test_invalid_raw_secret_is_bounded_and_never_echoed(secrets_env, value):
    root, values = secrets_env
    values["DMASIF_SSH_PRIVATE_KEY"] = value
    with pytest.raises(bootstrap.BootstrapError, match="DMASIF_SSH_PRIVATE_KEY"):
        bootstrap.prepare_environment(values, private_dir=root)
    assert not (root / "cluster-key").exists()


def test_existing_mounted_secret_files_work_without_changing_their_permissions(tmp_path):
    root = tmp_path / "runtime"
    mounted = tmp_path / "mount"
    mounted.mkdir()
    environment = {}
    for _, file_name, basename in bootstrap.REQUIRED:
        path = mounted / basename
        path.write_text("synthetic existing content\n")
        path.chmod(0o644)
        environment[file_name] = str(path)
    token = mounted / "repository-token"
    token.write_text("synthetic-existing-token")
    token.chmod(0o444)
    environment[bootstrap.TOKEN_FILE] = str(token)
    child = bootstrap.prepare_environment(environment, private_dir=root)
    assert child == environment
    assert all(stat.S_IMODE(Path(child[item[1]]).stat().st_mode) == 0o644 for item in bootstrap.REQUIRED)
    assert token.read_text() == "synthetic-existing-token" and stat.S_IMODE(token.stat().st_mode) == 0o444
    assert not (root / bootstrap.TOKEN_MARKER).exists()


@pytest.mark.parametrize("raw_token", [None, "", " \n"])
def test_removing_generated_token_deletes_only_its_owned_file(secrets_env, raw_token):
    root, values = secrets_env
    bootstrap.prepare_environment(values, private_dir=root)
    values.pop(bootstrap.TOKEN_VALUE)
    if raw_token is not None:
        values[bootstrap.TOKEN_VALUE] = raw_token
    child = bootstrap.prepare_environment(values, private_dir=root)
    assert bootstrap.TOKEN_VALUE not in child and bootstrap.TOKEN_FILE not in child
    assert not (root / bootstrap.TOKEN_NAME).exists()
    assert not (root / bootstrap.TOKEN_MARKER).exists()


def test_blank_token_disables_but_does_not_delete_external_mount(secrets_env, tmp_path):
    root, values = secrets_env
    token = tmp_path / "mounted-token"
    token.write_text("external-synthetic-token")
    values[bootstrap.TOKEN_FILE] = str(token)
    values[bootstrap.TOKEN_VALUE] = ""
    child = bootstrap.prepare_environment(values, private_dir=root)
    assert bootstrap.TOKEN_FILE not in child
    assert token.read_text() == "external-synthetic-token"


def test_removal_does_not_delete_a_replacement_with_a_different_inode(secrets_env, tmp_path):
    root, values = secrets_env
    bootstrap.prepare_environment(values, private_dir=root)
    replacement = tmp_path / "replacement-token"
    replacement.write_text("external replacement")
    replacement.replace(root / bootstrap.TOKEN_NAME)
    values.pop(bootstrap.TOKEN_VALUE)
    child = bootstrap.prepare_environment(values, private_dir=root)
    assert bootstrap.TOKEN_FILE not in child
    assert (root / bootstrap.TOKEN_NAME).read_text() == "external replacement"


def test_interrupted_token_rotation_cannot_reuse_the_previous_token(secrets_env, monkeypatch):
    root, values = secrets_env
    bootstrap.prepare_environment(values, private_dir=root)
    original_replace = bootstrap.os.replace
    def fail_token_replace(source, destination, **kwargs):
        if destination == bootstrap.TOKEN_NAME:
            raise OSError("synthetic-sensitive-error")
        return original_replace(source, destination, **kwargs)
    monkeypatch.setattr(bootstrap.os, "replace", fail_token_replace)
    values[bootstrap.TOKEN_VALUE] = "rotated-synthetic-token"
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.prepare_environment(values, private_dir=root)
    monkeypatch.setattr(bootstrap.os, "replace", original_replace)
    values.pop(bootstrap.TOKEN_VALUE)
    child = bootstrap.prepare_environment(values, private_dir=root)
    assert bootstrap.TOKEN_FILE not in child
    assert not (root / bootstrap.TOKEN_NAME).exists()
    assert not list(root.glob(".secret-*"))


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
def test_generated_destination_cannot_be_a_nonregular_file(secrets_env, tmp_path, kind):
    root, values = secrets_env
    root.mkdir(parents=True, mode=0o700)
    target = root / "cluster-key"
    outside = tmp_path / "untouched"
    outside.write_text("external file")
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(outside)
    else:
        os.mkfifo(target)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.prepare_environment(values, private_dir=root)
    assert outside.read_text() == "external file"


@pytest.mark.parametrize("kind", ["symlink", "writable"])
def test_unsafe_private_directory_is_rejected(secrets_env, tmp_path, kind):
    root, values = secrets_env
    root.parent.mkdir(parents=True)
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir(mode=0o777)
        root.chmod(0o777)
    with pytest.raises(bootstrap.BootstrapError, match="directory"):
        bootstrap.prepare_environment(values, private_dir=root)


def test_raw_values_cannot_overwrite_external_files(secrets_env, tmp_path):
    root, values = secrets_env
    outside = tmp_path / "mounted-key"
    outside.write_text("external key")
    values["DMASIF_SSH_KEY"] = str(outside)
    with pytest.raises(bootstrap.BootstrapError, match="DMASIF_SSH_KEY"):
        bootstrap.prepare_environment(values, private_dir=root)
    assert outside.read_text() == "external key"


def test_main_execs_requested_command_with_sanitized_environment(secrets_env, monkeypatch):
    root, values = secrets_env
    prepare = bootstrap.prepare_environment
    monkeypatch.setattr(bootstrap, "prepare_environment", lambda env: prepare(env, private_dir=root))
    monkeypatch.setattr(bootstrap.os, "environ", values)
    calls = []
    def execute(program, arguments, environment):
        calls.append((program, arguments, environment))
        raise SystemExit(0)
    monkeypatch.setattr(bootstrap.os, "execvpe", execute)
    with pytest.raises(SystemExit) as error:
        bootstrap.main(["serve", "--host", "0.0.0.0"])
    assert error.value.code == 0
    program, arguments, environment = calls[0]
    assert program == "dmasif-console" and arguments == ["dmasif-console", "serve", "--host", "0.0.0.0"]
    assert not {item[0] for item in bootstrap.REQUIRED} & environment.keys()
    assert bootstrap.TOKEN_VALUE not in environment


def test_startup_failure_logs_no_values_or_tracebacks(secrets_env, monkeypatch, capsys):
    root, values = secrets_env
    prepare = bootstrap.prepare_environment
    monkeypatch.setattr(bootstrap, "prepare_environment", lambda env: prepare(env, private_dir=root))
    monkeypatch.setattr(bootstrap.os, "environ", values)
    def fail_exec(*_):
        raise OSError("synthetic-secret-must-never-appear")
    monkeypatch.setattr(bootstrap.os, "execvpe", fail_exec)
    assert bootstrap.main(["serve"]) == 1
    output = capsys.readouterr()
    assert not output.out and "Secret bootstrap failed" in output.err
    assert "synthetic" not in output.err and "Traceback" not in output.err
