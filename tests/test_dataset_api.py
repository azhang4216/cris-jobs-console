from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient

from dmasif_console.config import Settings
from dmasif_console.datasets import DatasetDiscoveryError
from dmasif_console.web import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        state_dir=tmp_path / "state",
        repository={"id": 42, "full_name": "lab/research", "clone_url": str(tmp_path / "repo")},
        allowed_actor_ids=[7], webhook_secret="local-test-webhook-secret",
    )
    with TestClient(create_app(settings)) as client:
        yield client


def catalog(name="structures"):
    return {
        "datasets": [{"name": name, "scope": "project", "dataset_id": None, "description": "Atomic coordinates",
                      "kind": "collection", "file_count": 3, "available": True, "enabled": False}],
        "checked_at": "2026-10-09T17:00:00+00:00", "source": "ssh", "truncated": False,
    }


def test_each_dataset_request_discovers_fresh_data_without_run_history(client, monkeypatch):
    calls = []

    def discover(settings):
        calls.append(settings)
        return catalog("structures" if len(calls) == 1 else "new-collection")

    monkeypatch.setattr("dmasif_console.web.fetch_datasets", discover)
    first = client.get("/api/datasets")
    second = client.get("/api/datasets")
    assert first.status_code == second.status_code == 200
    assert first.json()["datasets"][0]["name"] == "structures"
    assert second.json()["datasets"][0]["name"] == "new-collection"
    assert first.headers["cache-control"] == second.headers["cache-control"] == "no-store"
    assert len(calls) == 2


def test_runs_and_history_do_not_wait_for_dataset_ssh(client, monkeypatch):
    def unexpected(settings):
        pytest.fail("Run views must not initiate dataset SSH discovery")

    monkeypatch.setattr("dmasif_console.web.fetch_datasets", unexpected)
    for path in ("/", "/?experiment=no-match", "/api/runs", "/api/runs?status=RUNNING"):
        assert client.get(path).status_code == 200


def test_failed_refresh_returns_safe_error_without_cached_catalog_and_can_retry(client, monkeypatch):
    calls = 0

    def discover(settings):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DatasetDiscoveryError("private-user@private-host /private/key")
        return catalog()

    monkeypatch.setattr("dmasif_console.web.fetch_datasets", discover)
    assert client.get("/api/datasets").status_code == 200
    failed = client.get("/api/datasets")
    assert failed.status_code == 503
    assert "datasets" not in failed.json()
    assert "private" not in failed.text
    assert failed.headers["cache-control"] == "no-store"
    assert client.get("/api/datasets").status_code == 200


def test_file_browser_fetches_only_requested_collection_page(client, monkeypatch):
    calls = []

    def discover(settings, scope, name, page, page_size):
        calls.append((scope, name, page, page_size))
        return {"files": [{"name": "model.cif", "model_id": "model", "relative_path": "model.cif", "format": "CIF"}],
                "page": page, "page_size": page_size, "has_more": True, "source": "ssh",
                "checked_at": "2026-10-09T17:00:00+00:00", "truncated": False}

    monkeypatch.setattr("dmasif_console.web.fetch_dataset_files", discover)
    for page in (1, 2):
        response = client.get("/api/datasets/files", params={"scope": "project", "name": "structures", "page": page})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["page"] == page
    assert calls == [("project", "structures", 1, 50), ("project", "structures", 2, 50)]


@pytest.mark.parametrize("changes", [
    {"scope": "arbitrary"}, {"name": "../private"}, {"name": "/absolute"}, {"name": "a\\b"},
    {"name": "."}, {"name": ".."}, {"name": ".git"},
    {"name": ""}, {"name": "x" * 129}, {"page": 0}, {"page_size": 101},
])
def test_file_browser_rejects_invalid_selectors_before_ssh(client, monkeypatch, changes):
    monkeypatch.setattr("dmasif_console.web.fetch_dataset_files", lambda *args: pytest.fail("Invalid selector reached SSH"))
    response = client.get("/api/datasets/files", params={"scope": "project", "name": "structures", **changes})
    assert response.status_code == 422


def test_dataset_ssh_concurrency_is_bounded_without_blocking_runs(client, monkeypatch):
    occupied, release = Event(), Event()
    lock = Lock()
    calls = 0

    def discover(settings):
        nonlocal calls
        with lock:
            calls += 1
            if calls == 2:
                occupied.set()
        assert release.wait(10), "Test did not release dataset queries"
        return catalog()

    monkeypatch.setattr("dmasif_console.web.fetch_datasets", discover)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(client.get, "/api/datasets") for _ in range(2)]
        try:
            assert occupied.wait(10)
            rejected = client.get("/api/datasets")
            assert rejected.status_code == 503
            assert rejected.headers["retry-after"] == "5"
            assert calls == 2
            assert client.get("/api/runs").status_code == 200
        finally:
            release.set()
        assert all(request.result().status_code == 200 for request in pending)
    assert client.get("/api/datasets").status_code == 200
