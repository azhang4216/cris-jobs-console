"""Validation and no-overwrite publication, executed inside the frozen runtime."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import zipfile

try:
    from .common import AdapterError, sha256_file, write_json, utcnow
except ImportError:
    from common import AdapterError, sha256_file, write_json, utcnow

REQUIRED = {"xyz", "normals", "input_feats", "emb1", "emb2", "nearest_atom", "atom_xyz", "atom_type", "atom_chain", "atom_resnum", "atom_icode", "atom_resname", "atom_name"}


def validate_npz(path: Path, max_uncompressed_bytes: int = 512 * 1024 * 1024) -> dict:
    import numpy as np

    if path.is_symlink() or not path.is_file():
        raise AdapterError("Output is missing or is not a regular file")
    with zipfile.ZipFile(path) as bundle:
        members = bundle.infolist()
        if len({member.filename for member in members}) != len(members):
            raise AdapterError("Duplicate NPZ members")
        if sum(member.file_size for member in members) > max_uncompressed_bytes:
            raise AdapterError("Uncompressed NPZ exceeds validation limit")
    with np.load(path, allow_pickle=False) as arrays:
        missing = REQUIRED - set(arrays.files)
        if missing:
            raise AdapterError("Missing result arrays: " + ", ".join(sorted(missing)))
        xyz, atoms = arrays["xyz"], arrays["atom_xyz"]
        if xyz.ndim != 2 or xyz.shape[1] != 3 or not len(xyz):
            raise AdapterError("xyz must be a nonempty N×3 array")
        if atoms.ndim != 2 or atoms.shape[1] != 3 or not len(atoms):
            raise AdapterError("atom_xyz must be a nonempty M×3 array")
        n, m = len(xyz), len(atoms)
        shapes = {"xyz": (n, 3), "normals": (n, 3), "input_feats": (n, 16), "emb1": (n, 16), "emb2": (n, 16), "atom_xyz": (m, 3)}
        for name, shape in shapes.items():
            value = arrays[name]
            if value.shape != shape or not np.issubdtype(value.dtype, np.number) or not np.isfinite(value).all():
                raise AdapterError(f"Invalid shape, dtype or nonfinite values in {name}")
        for name in ("nearest_atom", "atom_type", "atom_resnum"):
            value = arrays[name]
            if value.shape != ((n,) if name == "nearest_atom" else (m,)) or not np.issubdtype(value.dtype, np.integer):
                raise AdapterError(f"Invalid integer metadata in {name}")
        if np.any(arrays["nearest_atom"] < 0) or np.any(arrays["nearest_atom"] >= m):
            raise AdapterError("nearest_atom contains invalid atom indices")
        if np.any(arrays["atom_type"] < 0) or np.any(arrays["atom_type"] > 5):
            raise AdapterError("atom_type contains unsupported element codes")
        for name in ("atom_chain", "atom_icode", "atom_resname", "atom_name"):
            if arrays[name].shape != (m,) or arrays[name].dtype.kind not in "US":
                raise AdapterError(f"Invalid atom text metadata in {name}")
        # Read every array with pickle disabled, including optional metadata.
        for name in arrays.files:
            if arrays[name].dtype.hasobject:
                raise AdapterError("Object arrays are unsupported")
    return {"points": n, "atoms": m}


def publish(raw: Path, destination: Path, expected: list[str], provenance: dict, manifest_path: Path) -> dict:
    if manifest_path.exists() or destination.exists():
        raise AdapterError("Publication already exists; outputs will not be replaced")
    destination.mkdir(parents=True, exist_ok=False)
    artifacts, errors = [], []
    for name in expected:
        if Path(name).name != name or not name.endswith(".npz"):
            raise AdapterError("Invalid expected result filename")
        source = raw / name
        try:
            if source.is_symlink() or not source.is_file():
                raise AdapterError("Output is missing or is not a regular file")
            descriptor, temporary = tempfile.mkstemp(prefix=".result-", dir=destination)
            try:
                with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output)
                    output.flush()
                    os.fsync(output.fileno())
                # Validate precisely the retained bytes. Raw files may change;
                # a checksum of a later copy cannot establish earlier validity.
                dimensions = validate_npz(Path(temporary))
                checksum = sha256_file(Path(temporary))
                size = Path(temporary).stat().st_size
                os.link(temporary, destination / name)
            finally:
                Path(temporary).unlink(missing_ok=True)
            relative = str((destination / name).relative_to(manifest_path.parent.parent.parent))
            artifacts.append({"id": checksum, "name": name, "relative_path": relative, "path": relative, "size": size, "sha256": checksum, **dimensions})
        except (AdapterError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            errors.append({"name": name, "error": str(exc)[:500]})
    result = {"expected": len(expected), "valid": len(artifacts), "expected_count": len(expected), "valid_count": len(artifacts), "outcome": "SUCCEEDED" if len(artifacts) == len(expected) and expected else "PARTIAL" if artifacts else "FAILED", "artifacts": artifacts, "errors": errors, "provenance": provenance, "validated_at": utcnow()}
    write_json(manifest_path, result, exclusive=True)
    return result
