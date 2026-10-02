"""GPU reservation/use counts remain useful without publishing device identity."""
import hashlib

from fastapi.testclient import TestClient

from dmasif_console.config import Settings
from dmasif_console.db import Store
from dmasif_console.web import create_app


def populated_app(tmp_path, counts):
    settings = Settings(
        state_dir=tmp_path / "state", webhook_secret="gpu-count-test-secret-only",
        repository={"id": 42, "full_name": "lab/research", "clone_url": str(tmp_path / "repo")},
        allowed_actor_ids=[7],
    )
    app = create_app(settings)
    store = Store(settings.database_path)
    private = {
        "cuda_visible_devices": "GPU-private-first,GPU-private-second",
        "gpu_uuid": "GPU-private-hardware-identity",
        "runtime_image": "/unpublished/runtime/image.sif",
        "project_module_paths": {"model": "/unpublished/source/model.py"},
        "execution": {"cuda_visible_devices": "GPU-private-first,GPU-private-second"},
        "gpu_preflight": {"device_uuid": "GPU-private-hardware-identity"},
    }
    run_id = store.accept_delivery(
        "test", "counts", hashlib.sha256(b"gpu-count-fixture").hexdigest(), {}, "ACCEPTED", "", {
            "state": "SUCCEEDED", "commit_sha": "a" * 40, "actor_login": "researcher",
            "experiment": "two-reserved-one-used", "provenance": {key: 1 for key in counts},
            "validation": {"outcome": "SUCCEEDED", "provenance": {**private, **counts}},
        },
    )["run_id"]
    return app, run_id


def test_validated_gpu_counts_are_public_but_masks_uuids_and_paths_are_private(tmp_path):
    counts = {"requested_gpu_count": 2, "allocated_gpu_count": 2, "used_gpu_count": 1}
    app, run_id = populated_app(tmp_path, counts)
    with TestClient(app) as client:
        for path in ("/api/runs", f"/api/runs/{run_id}"):
            response = client.get(path)
            assert response.status_code == 200
            body = response.json()
            run = body["runs"][0] if "runs" in body else body["run"]
            assert {key: run["provenance"][key] for key in counts} == counts
            assert all(type(run["provenance"][key]) is int for key in counts)
            assert "cuda_visible_devices" not in response.text
            assert "GPU-private" not in response.text
            assert "/unpublished/" not in response.text
        page = client.get(f"/runs/{run_id}").text
        for key, value in counts.items():
            assert f'"{key}": {value}' in page
        assert "GPU-private" not in page and "/unpublished/" not in page


def test_gpu_count_fields_cannot_publish_a_device_record_or_coerce_boolean(tmp_path):
    counts = {
        "requested_gpu_count": "GPU-count-field-secret",
        "allocated_gpu_count": {"cuda_visible_devices": "GPU-count-field-secret"},
        "used_gpu_count": True,
    }
    app, run_id = populated_app(tmp_path, counts)
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200
        assert not set(counts).intersection(response.json()["run"]["provenance"])
        assert "GPU-count-field-secret" not in response.text
        page = client.get(f"/runs/{run_id}").text
        assert "GPU-count-field-secret" not in page
