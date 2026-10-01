import stat

import pytest

from dmasif_console.runtime_secrets import private_key_copy


def test_provider_key_copied_privately_and_removed_after_failure(tmp_path):
    source = tmp_path / "mounted-key"
    source.write_bytes(b"synthetic fixture; not an SSH credential\n")
    source.chmod(0o644)
    with pytest.raises(RuntimeError):
        with private_key_copy(source) as private:
            assert private.read_bytes() == source.read_bytes()
            assert stat.S_IMODE(private.stat().st_mode) == 0o600
            assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700
            assert private.parent != tmp_path
            raise RuntimeError("simulated failed SSH call")
    assert not private.exists()
    assert stat.S_IMODE(source.stat().st_mode) == 0o644


@pytest.mark.parametrize("value", [b"", b" " * 12, b"x" * (128 * 1024 + 1)])
def test_invalid_or_oversized_key_rejected(tmp_path, value):
    source = tmp_path / "key"
    source.write_bytes(value)
    with pytest.raises(ValueError):
        with private_key_copy(source):
            pytest.fail("Invalid key should not be yielded")
