"""The researcher console has fixed data, not a live VM file browser."""
import pytest
from fastapi.testclient import TestClient

from dmasif_console.config import Settings
from dmasif_console.web import create_app


def test_data_explorer_removed_and_views_never_query_remote_files(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Researcher views must not browse VM datasets")
    monkeypatch.setattr("dmasif_console.datasets.fetch_datasets", unexpected)
    monkeypatch.setattr("dmasif_console.datasets.fetch_dataset_files", unexpected)
    settings = Settings(state_dir=tmp_path / "state", allowed_actor_ids=[7], webhook_secret="test-webhook-secret",
                        repository={"id": 42, "full_name": "lab/research", "clone_url": str(tmp_path / "repo")})
    with TestClient(create_app(settings)) as client:
        for path in ("/api/datasets", "/api/datasets/files?scope=project&name=structures"):
            assert client.get(path).status_code == 404
        for path in ("/", "/api/runs"):
            assert client.get(path).status_code == 200
