"""Materialize Render environment secrets privately, then replace this process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
import sys


MAX_SECRET_BYTES = 1024 * 1024
REQUIRED = (
    ("DMASIF_OPERATOR_YAML", "DMASIF_CONFIG", "operator.yaml"),
    ("DMASIF_SSH_PRIVATE_KEY", "DMASIF_SSH_KEY", "cluster-key"),
    ("DMASIF_SSH_KNOWN_HOSTS", "DMASIF_KNOWN_HOSTS", "known-hosts"),
)
TOKEN_VALUE = "DMASIF_REPOSITORY_TOKEN"
TOKEN_FILE = "DMASIF_REPOSITORY_TOKEN_FILE"
TOKEN_NAME = "repository-token"
TOKEN_MARKER = ".repository-token-owner.json"


class BootstrapError(ValueError):
    """Messages contain fixed setting names only, never submitted values."""


def _private_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise BootstrapError("Private runtime secret directory must be an absolute, safe directory")
    for parent in reversed((path, *path.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            parent.mkdir(mode=0o700)
            info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise BootstrapError("Private runtime secret directory cannot contain symlinks or non-directories")
        if info.st_mode & 0o022 and not (parent != path and info.st_mode & stat.S_ISVTX):
            raise BootstrapError("Private runtime secret directory cannot be writable by other users")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(descriptor).st_uid != os.getuid():
            raise BootstrapError("Private runtime secret directory must belong to the service user")
        os.fchmod(descriptor, 0o700)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_file(path: Path, label: str) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SECRET_BYTES:
                raise BootstrapError(f"{label} must name a regular file of at most 1 MiB")
            data = stream.read(MAX_SECRET_BYTES + 1)
        if not data.strip() or len(data) > MAX_SECRET_BYTES:
            raise BootstrapError(f"{label} must name a nonempty file of at most 1 MiB")
        return data, info
    except OSError:
        raise BootstrapError(f"{label} must name a readable regular file, not a symlink") from None


def _content(value: str, label: str) -> bytes:
    if not value.strip():
        raise BootstrapError(f"{label} must contain a nonempty value")
    if "\x00" in value:
        raise BootstrapError(f"{label} cannot contain NUL characters")
    try:
        data = (value if value.endswith("\n") else value + "\n").encode("utf-8")
    except UnicodeError:
        raise BootstrapError(f"{label} must contain valid UTF-8 text") from None
    if len(data) > MAX_SECRET_BYTES:
        raise BootstrapError(f"{label} must contain at most 1 MiB")
    return data


def _replace(directory: int, name: str, data: bytes, before_replace=None) -> None:
    try:
        previous = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        previous = None
    if previous is not None and (not stat.S_ISREG(previous.st_mode) or previous.st_uid != os.getuid()):
        raise BootstrapError("Generated secret destination must be a regular file owned by the service user")
    temporary = ".secret-" + secrets.token_hex(16)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if before_replace is not None:
                before_replace(os.fstat(stream.fileno()))
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _token_owners(root: Path) -> list[list[int]]:
    marker = root / TOKEN_MARKER
    if not marker.exists() and not marker.is_symlink():
        return []
    data, info = _read_file(marker, TOKEN_FILE)
    try:
        owners = json.loads(data)
    except (ValueError, UnicodeError):
        raise BootstrapError("Repository token ownership record is invalid") from None
    if (info.st_uid != os.getuid() or info.st_mode & 0o077 or not isinstance(owners, list)
            or not 1 <= len(owners) <= 2 or any(
                not isinstance(item, list) or len(item) != 2
                or any(type(number) is not int or number < 0 for number in item) for item in owners)):
        raise BootstrapError("Repository token ownership record is invalid")
    return owners


def _retire_token(directory: int, owners: list[list[int]]) -> None:
    if not owners:
        return
    try:
        info = os.stat(TOKEN_NAME, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        info = None
    if info is not None and [info.st_dev, info.st_ino] in owners:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise BootstrapError("Generated repository token is no longer a safe regular file")
        os.unlink(TOKEN_NAME, dir_fd=directory)
    # A replacement with another inode could be an externally mounted file;
    # leave it untouched. The caller also disables its inherited token path.
    os.unlink(TOKEN_MARKER, dir_fd=directory)
    os.fsync(directory)


def prepare_environment(environ: dict[str, str], *, private_dir: Path | None = None) -> dict[str, str]:
    """Return an exec environment; mounted secret files remain supported."""
    result = dict(environ)
    root = private_dir if private_dir is not None else Path.home() / ".config" / "dmasif-console"
    try:
        directory = _private_directory(root)
    except OSError:
        raise BootstrapError("Cannot create the private runtime secret directory") from None
    try:
        pending = []
        for raw_name, file_name, basename in REQUIRED:
            destination = Path(result.get(file_name) or root / basename)
            if raw_name in result:
                data = _content(result.pop(raw_name), raw_name)
                if destination != root / basename:
                    raise BootstrapError(f"{file_name} must use its private runtime path when {raw_name} is supplied")
                pending.append((basename, data, raw_name))
            else:
                try:
                    _read_file(destination, file_name)
                except BootstrapError:
                    raise BootstrapError(f"Provide {raw_name} or a nonempty readable regular file in {file_name}") from None
            result[file_name] = str(destination)
        owners = _token_owners(root)
        raw_token = result.pop(TOKEN_VALUE, None)
        if raw_token is not None and len(raw_token.encode("utf-8")) > MAX_SECRET_BYTES:
            raise BootstrapError(f"{TOKEN_VALUE} must contain at most 1 MiB")
        if not result.get(TOKEN_FILE):
            result.pop(TOKEN_FILE, None)
        token_destination = Path(result.get(TOKEN_FILE) or root / TOKEN_NAME)
        token = None
        if raw_token is not None and raw_token.strip():
            if token_destination != root / TOKEN_NAME:
                raise BootstrapError(f"{TOKEN_FILE} must use its private runtime path when {TOKEN_VALUE} is supplied")
            if "\n" in raw_token.strip() or "\r" in raw_token.strip():
                raise BootstrapError(f"{TOKEN_VALUE} must contain a single-line token")
            token = _content(raw_token, TOKEN_VALUE)
        for name, data, label in pending:
            try:
                _replace(directory, name, data)
            except OSError:
                raise BootstrapError(f"Cannot prepare the private file for {label}") from None
        if token is not None:
            def remember(info):
                # Record the replacement inode before publishing it. Keeping
                # the prior inode covers interruption during token rotation.
                try:
                    current = os.stat(TOKEN_NAME, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                prior = [[current.st_dev, current.st_ino]] if current and [current.st_dev, current.st_ino] in owners else []
                _replace(directory, TOKEN_MARKER, json.dumps([*prior, [info.st_dev, info.st_ino]]).encode())
            _replace(directory, TOKEN_NAME, token, before_replace=remember)
            result[TOKEN_FILE] = str(token_destination)
        else:
            _retire_token(directory, owners)
            if raw_token is not None or (owners and token_destination == root / TOKEN_NAME):
                result.pop(TOKEN_FILE, None)
            elif TOKEN_FILE in result:
                if token_destination.exists() or token_destination.is_symlink():
                    _read_file(token_destination, TOKEN_FILE)
                elif token_destination == root / TOKEN_NAME:
                    result.pop(TOKEN_FILE, None)  # Optional Blueprint placeholder.
                else:
                    raise BootstrapError(f"{TOKEN_FILE} must name a readable token file when configured")
        return result
    except OSError:
        raise BootstrapError("Cannot prepare private runtime secret files") from None
    finally:
        os.close(directory)


def main(argv: list[str] | None = None) -> int:
    try:
        environment = prepare_environment(dict(os.environ))
        os.execvpe("dmasif-console", ["dmasif-console", *(sys.argv[1:] if argv is None else argv)], environment)
    except BootstrapError as exc:
        print(f"Secret bootstrap failed: {exc}", file=sys.stderr)
    except Exception:
        print("Secret bootstrap failed: unable to start dmasif-console; check runtime files and permissions", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
