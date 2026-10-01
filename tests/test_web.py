from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from dmasif_console.config import Settings
from dmasif_console.db import Store
from dmasif_console.web import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        state_dir=tmp_path / "state", repository={"id": 42, "full_name": "lab/research", "clone_url": str(tmp_path / "repo")},
        allowed_actor_ids=[7, 8], webhook_secret="local-test-webhook-secret-1234", viewer_password="local-viewer-password-1234",
        cluster={"host": "private-login.invalid", "user": "private-user", "root": "/private/cluster",
                 "account": "private-account", "helper_path": "/private/cluster/helper.py"},
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def payload(actor=7, login="alice", branch="runs/pocket-v1", sha="a" * 40):
    return {"repository": {"id": 42, "full_name": "untrusted-url/name"}, "sender": {"id": actor, "login": login, "type": "User"},
            "ref": "refs/heads/" + branch, "after": sha, "before": "b" * 40, "deleted": False,
            "head_commit": {"author": {"name": "Someone Else", "email": "someone@example.org"}}}


def signed(client, settings, data=None, delivery="delivery-1", body=None):
    body = body if body is not None else json.dumps(data or payload()).encode()
    return client.post("/webhooks/github", content=body, headers={
        "Content-Type": "application/json", "X-GitHub-Event": "push", "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": "sha256=" + hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest(),
    })


def test_signed_push_durable_and_identity_is_sender_not_author(client, settings):
    response = signed(client, settings)
    assert response.status_code == 202
    run = Store(settings.database_path).get_run(response.json()["run_id"])
    assert run["actor_login"] == "alice"
    assert run["actor_id"] == 7
    assert run["branch"] == "runs/pocket-v1"
    assert run["experiment"] == "pocket-v1"
    assert run["commit_author"]["name"] == "Someone Else"
    assert run["commit_url"] == "https://github.com/lab/research/commit/" + "a" * 40
    assert run["state"] == "CHECKING_REQUEST"
    assert run["body_hash"]
    assert run["policy"]["cluster"]["host"] == "private-login.invalid"
    assert "ssh_key_path" not in run["policy"]["cluster"]
    assert client.get("/").status_code == 200
    assert client.get("/runs/" + run["id"]).status_code == 200


def test_replay_and_changed_delivery_header_return_same_run(client, settings):
    first = signed(client, settings).json()
    repeated = signed(client, settings).json()
    replay = signed(client, settings, delivery="changed-header").json()
    assert repeated["duplicate"] and replay["duplicate"]
    assert first["run_id"] == repeated["run_id"] == replay["run_id"]
    assert len(Store(settings.database_path).list_runs()) == 1
    # Aliases themselves retain their byte identity, even after the replay.
    assert signed(client, settings, payload(sha="c" * 40), delivery="changed-header").status_code == 409


def test_concurrent_acceptance_is_atomic(client, settings):
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda i: signed(client, settings, delivery=f"parallel-{i}"), range(12)))
    assert {response.status_code for response in responses} == {202}
    assert len({response.json()["run_id"] for response in responses}) == 1
    assert len(Store(settings.database_path).list_runs()) == 1


def test_distinct_push_same_sha_on_other_experiment_creates_new_run(client, settings):
    first = signed(client, settings).json()
    second = signed(client, settings, payload(branch="runs/another"), delivery="delivery-2").json()
    assert first["run_id"] != second["run_id"]


@pytest.mark.parametrize("data", [payload(actor=999),
                                  {**payload(), "sender": {"id": 7, "login": "alice", "type": "Bot"}}])
def test_disallowed_actors_are_recorded_not_submitted(client, settings, data):
    response = signed(client, settings, data)
    assert response.status_code == 202
    assert response.json()["decision"] == "REJECTED"
    assert response.json()["run_id"] is None
    assert Store(settings.database_path).list_runs() == []


def test_two_researchers_on_same_branch_are_attributed_to_each_pusher(client, settings):
    first = signed(client, settings).json()
    data = payload(actor=8, login="bob", sha="c" * 40)
    data["pusher"] = {"name": "Untrusted commit identity", "email": "someone@example.invalid"}
    second = signed(client, settings, data, delivery="bob-push").json()
    store = Store(settings.database_path)
    alice, bob = store.get_run(first["run_id"]), store.get_run(second["run_id"])
    assert alice["id"] != bob["id"]
    assert alice["branch"] == bob["branch"] == "runs/pocket-v1"
    assert alice["experiment"] == bob["experiment"] == "pocket-v1"
    assert (alice["actor_id"], alice["actor_login"]) == (7, "alice")
    assert (bob["actor_id"], bob["actor_login"]) == (8, "bob")
    assert alice["commit_sha"] == "a" * 40
    assert bob["commit_sha"] == "c" * 40
    assert bob["commit_author"]["name"] == "Someone Else"
    assert signed(client, settings, data, delivery="bob-redelivery").json()["run_id"] == bob["id"]


def test_branch_name_never_supplies_researcher_identity(client, settings):
    response = signed(client, settings, payload(actor=8, login="bob", branch="runs/alice/pocket-v1"))
    run = Store(settings.database_path).get_run(response.json()["run_id"])
    assert (run["actor_id"], run["actor_login"]) == (8, "bob")
    assert run["experiment"] == "alice/pocket-v1"


def test_run_branch_requires_an_experiment_name(client, settings):
    response = signed(client, settings, payload(branch="runs/"))
    assert response.status_code == 202
    assert response.json()["decision"] == "REJECTED"
    assert Store(settings.database_path).list_runs() == []


@pytest.mark.parametrize("data", [payload(branch="modern-stack"), {**payload(), "deleted": True},
                                  {**payload(), "ref": "refs/tags/v1"}])
def test_non_run_events_never_create_run(client, settings, data):
    assert signed(client, settings, data).json()["decision"] == "IGNORED"
    assert Store(settings.database_path).list_runs() == []


def test_bad_signature_malformed_event_and_wrong_repository_fail_closed(client, settings):
    assert client.post("/webhooks/github", json=payload()).status_code == 401
    wrong = {**payload(), "repository": {"id": 43}}
    assert signed(client, settings, wrong).status_code == 403
    assert signed(client, settings, body=b"[]").status_code == 400
    assert signed(client, settings, {**payload(), "after": "HEAD"}).status_code == 400
    assert Store(settings.database_path).deliveries() == []


def test_raw_body_signature_is_not_normalized(client, settings):
    body = json.dumps(payload()).encode()
    signature = hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    response = client.post("/webhooks/github", content=body + b" ", headers={
        "X-Hub-Signature-256": "sha256=" + signature, "X-GitHub-Event": "push", "X-GitHub-Delivery": "body-change"})
    assert response.status_code == 401


def test_body_size_limit(client, settings):
    assert signed(client, settings, body=b" " * (settings.max_webhook_bytes + 1)).status_code == 413


def test_restore_gate_prevents_accepting_stale_deduplication_state(client, settings):
    (settings.state_dir / "restore-pending.json").write_text("{}")
    assert signed(client, settings).status_code == 503
    assert not Store(settings.database_path).list_runs()


def test_queue_limit_rejection_remains_visible(client, settings):
    settings.queue_limit = 1
    signed(client, settings)
    second = signed(client, settings, payload(sha="c" * 40), delivery="second").json()
    assert Store(settings.database_path).get_run(second["run_id"])["state"] == "REJECTED"


def test_viewing_is_public_even_with_legacy_credentials_and_has_no_mutations(client, settings):
    run_id = signed(client, settings).json()["run_id"]
    for path in ("/", "/api/runs", "/runs/" + run_id, "/api/runs/" + run_id,
                 "/api/runs/" + run_id + "/logs", "/static/console.js", "/static/console.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert "www-authenticate" not in response.headers
        # A stale browser Basic header must not reintroduce an authentication gate.
        assert client.get(path, auth=("lab", "wrong")).status_code == 200
    for path in ("/runs/nope", "/api/runs/nope", "/api/runs/nope/logs", "/artifacts/nope", "/artifacts/" + "f" * 64):
        assert client.get(path).status_code == 404
    assert client.get("/healthz").json() == {"status": "ok"}
    for method in ("POST", "PUT", "DELETE"):
        assert client.request(method, "/api/runs").status_code == 405
        assert client.request(method, "/api/runs/" + run_id).status_code == 405
    for action in ("submit", "cancel", "retry"):
        assert client.post("/api/runs/" + run_id + "/" + action).status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert len(Store(settings.database_path).list_runs()) == 1


def test_viewing_needs_no_password_configuration(settings):
    settings.viewer_password = ""
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/runs").status_code == 200
        assert client.post("/webhooks/github", json=payload()).status_code == 401
        assert signed(client, settings).status_code == 202


@pytest.mark.parametrize("secret", ["", "too-short"])
def test_web_startup_still_requires_a_strong_webhook_secret(settings, secret):
    settings.webhook_secret = secret
    settings.viewer_password = ""
    with pytest.raises(ValueError, match="webhook secret"):
        create_app(settings)
    assert not settings.database_path.exists()


def test_public_projection_excludes_login_details_and_redacts_logs(client, settings):
    run_id = signed(client, settings).json()["run_id"]
    store = Store(settings.database_path)
    store.update_run(run_id, {
        "remote_dir": "/private/cluster/runs/secret", "log_tail": "private-user@private-login.invalid /private/cluster/results private-account "
                    + settings.webhook_secret + " " + settings.viewer_password
                    + " -----BEGIN OPENSSH PRIVATE KEY-----synthetic-private-key-----END OPENSSH PRIVATE KEY-----",
        "provenance": {"runtime_sha256": "c" * 64, "module_paths": {"model": "/private/cluster/model.py"}},
        "result": {"expected": 1, "valid": 0, "provenance": {"checkpoint_path": "/private/checkpoint"},
                   "artifacts": [{"relative_path": "/private/cluster/results"}]},
    })
    for route in ("/api/runs", "/api/runs/" + run_id, "/api/runs/" + run_id + "/logs", "/runs/" + run_id):
        response = client.get(route)
        assert response.status_code == 200
        for private in ("private-login.invalid", "private-user", "private-account", "/private/cluster", "ssh_key_path", "checkpoint_path", "module_paths",
                        settings.webhook_secret, settings.viewer_password, "synthetic-private-key"):
            assert private not in response.text


def test_artifact_download_requires_cached_checksum_and_contained_path(client, settings, tmp_path):
    run_id = signed(client, settings).json()["run_id"]
    aid = "d" * 64
    target = settings.state_dir / "artifacts" / run_id / aid
    target.parent.mkdir(parents=True)
    content = b"verified-test-bytes"
    target.write_bytes(content)
    metadata = {"id": aid, "name": "1STP.npz", "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                "cache_state": "cached", "cache_path": "/should/not/be/trusted"}
    store = Store(settings.database_path)
    store.update_run(run_id, {"artifacts": [metadata]})
    assert client.get("/artifacts/" + aid).content == content
    target.write_bytes(b"tampered")
    assert client.get("/artifacts/" + aid).status_code == 410
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(content)
    target.symlink_to(outside)
    assert client.get("/artifacts/" + aid).status_code == 410


def test_old_cluster_identity_remains_private_after_configuration_rotation(client, settings):
    run_id = signed(client, settings).json()["run_id"]
    store = Store(settings.database_path)
    run = store.get_run(run_id)
    old = {"host": "old-private.example", "user": "old-private-user", "root": "/old-private-root", "account": "old-private-account"}
    run["policy"]["cluster"].update(old)
    message = " ".join(old.values())
    store.update_run(run_id, {"policy": run["policy"], "log_tail": message, "reason": message})
    store.add_event(run_id, "FAILED", {"reason": message})
    for route in ("/api/runs", "/api/runs/" + run_id, "/api/runs/" + run_id + "/logs", "/runs/" + run_id):
        text = client.get(route).text
        assert all(secret not in text for secret in old.values())


def test_effective_pause_and_full_history_filtering(client, settings):
    data = client.get("/api/runs").json()
    assert data["control"]["paused"]
    settings.submissions_enabled = True
    assert not client.get("/api/runs").json()["control"]["paused"]
    store = Store(settings.database_path)
    for i in range(205):
        store.accept_delivery("hook", f"old-{i}", hashlib.sha256(f"old-{i}".encode()).hexdigest(), {},
                              "ACCEPTED", "", {"state": "SUCCEEDED", "commit_sha": "a" * 40,
                                               "actor_login": "alice", "experiment": "old-target" if i == 0 else "newer"})
    response = client.get("/api/runs?experiment=old-target").json()
    assert len(response["runs"]) == 1
    assert response["runs"][0]["experiment"] == "old-target"
    assert response["actors"] == ["alice"]


def test_terminal_unknown_end_time_does_not_invent_runtime(client, settings):
    run_id = signed(client, settings).json()["run_id"]
    store = Store(settings.database_path)
    store.update_run(run_id, {"state": "FAILED", "started_at": "2026-09-30T12:00:00+00:00"},
                     event={"kind": "state_changed", "detail": {"reason": "Scheduler failed; end time unavailable."}})
    data = client.get("/api/runs/" + run_id).json()
    assert data["run"]["runtime_seconds"] is None
    assert data["events"][0]["reason"] == "Scheduler failed; end time unavailable."
