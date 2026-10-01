"""Adapt provider-mounted SSH keys to OpenSSH's private-file permissions."""
from contextlib import contextmanager
from pathlib import Path
import tempfile


@contextmanager
def private_key_copy(source):
    """Keep key material in an ephemeral 0700 directory, never the state disk.

    Render secret mounts may not be owned by our unprivileged UID. Copying avoids
    changing the provider's mount and also supports protected operator commands.
    """
    with tempfile.TemporaryDirectory(prefix="dmasif-ssh-") as directory:
        destination = Path(directory) / "identity"
        with Path(source).open("rb") as stream:
            data = stream.read(128 * 1024 + 1)
        if not data.strip() or len(data) > 128 * 1024:
            raise ValueError("SSH key file is empty or too large")
        destination.touch(mode=0o600, exist_ok=False)
        destination.write_bytes(data)
        destination.chmod(0o600)
        yield destination
