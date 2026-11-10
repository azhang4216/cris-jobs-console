from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient

from dmasif_console.config import Settings
from dmasif_console.db import Store
from dmasif_console.training import TelemetryError, event_page, ingest_chunk, iter_events, normalize_event, sync_training_telemetry
from dmasif_console.web import create_app


@pytest.fixture
def setup(tmp_path):
    settings = Settings(
        state_dir=tmp_path / "state", repository={"id": 42, "full_name": "lab/research", "clone_url": str(tmp_path / "repo")},
        allowed_actor_ids=[7], webhook_secret="private-webhook-secret",
        cluster={"host": "hidden-host.invalid", "user": "hidden-user", "root": "/private/cluster"},
    )
    store = Store(settings.database_path)
    store.initialize()
    accepted = store.accept_delivery("test", "d1", "a" * 64, {}, "ACCEPTED", "test", {
        "state": "RUNNING", "config": {"job_type": "dmasif_train", "batch_size": 32, "steps": 10000},
        "log_tail": "only the last lines", "commit_sha": "b" * 40,
    })
    return settings, store, store.get_run(accepted["run_id"])


def event(seq, kind="step", **fields):
    return {"schema_version": 1, "seq": seq, "kind": kind, "timestamp": datetime.now(timezone.utc).isoformat(),
            "step": seq, "total_steps": 10000, "metrics": {"loss": 1 / seq, "nce": .7}, **fields}


def chunk(events, offset=0, eof=True):
    text = "".join(json.dumps(e) + "\n" for e in events)
    return {"offset": offset, "next_offset": offset + len(text.encode()), "text": text, "eof": eof}


def test_cursor_events_and_summary_survive_restart_and_repeated_poll(setup):
    settings, store, run = setup
    response = chunk([event(1), event(2, "evaluation", metrics={"mrr": .15}),
                      event(3, "checkpoint", name="best.pt", size=6000000)])
    first = ingest_chunk(store, run["id"], response)
    restarted = Store(settings.database_path)
    restarted.initialize()
    second = ingest_chunk(restarted, run["id"], response)
    assert first["training_cursor"] == second["training_cursor"] == response["next_offset"]
    assert second["training"]["event_count"] == 3
    assert second["training"]["latest_evaluation"]["metrics"]["mrr"] == .15
    assert second["training"]["checkpoints"][0]["name"] == "best.pt"
    assert second["state"] == "RUNNING"


def test_full_step_history_remains_available_after_raw_tail_changes(setup):
    _, store, run = setup
    ingest_chunk(store, run["id"], chunk([event(i) for i in range(1, 301)]))
    store.update_run(run["id"], {"log_tail": "a newer truncated tail"})
    page = event_page(store, run["id"], kind="step", limit=50)
    assert page["total"] == 300
    assert [e["seq"] for e in page["events"]] == list(range(300, 250, -1))
    earlier = event_page(store, run["id"], before_seq=page["next_before_seq"], limit=50)
    assert earlier["events"][0]["seq"] == 250
    assert event_page(store, run["id"], step=42)["events"][0]["metrics"]["loss"] == 1 / 42
    assert [e["seq"] for e in iter_events(store, run["id"])] == list(range(1, 301))


@pytest.mark.parametrize("change", [{"next_offset": 999}, {"offset": -1}, {"offset": True}, {"eof": "yes"},
                                    {"text": '{"seq": 1}', "next_offset": 10}])
def test_invalid_or_partial_response_never_advances_cursor(setup, change):
    _, store, run = setup
    with pytest.raises(TelemetryError):
        ingest_chunk(store, run["id"], {**chunk([event(1)]), **change})
    assert store.get_run(run["id"]).get("training_cursor", 0) == 0
    assert event_page(store, run["id"])["total"] == 0


def test_malformed_event_does_not_block_following_valid_event(setup):
    _, store, run = setup
    text = "not json\n" + json.dumps(event(1)) + "\n"
    result = ingest_chunk(store, run["id"], {"offset": 0, "next_offset": len(text.encode()), "text": text, "eof": True})
    assert result["training"]["invalid_events"] == 1
    assert result["training"]["event_count"] == 1


def test_numeric_normalization_preserves_unavailable_metrics_and_discards_objects():
    normalized = normalize_event(event(1, metrics={"loss": float("nan"), "auroc": float("inf"), "mrr": .2,
                                                  "bad": {"private": "value"}, "truth": True, "huge": 10**999,
                                                  "unknown_metric": 17, "/private/key": 9}))
    assert normalized["metrics"] == {"loss": None, "auroc": None, "mrr": .2, "huge": None, "unknown_metric": 17}
    assert normalize_event(event(1, kind={"invalid": True})) is None
    assert normalize_event(event(True)) is None
    assert normalize_event(event(1, timestamp="2026-11-10T12:00:00")) is None


def test_out_of_order_repeated_sequences_do_not_rewind_progress(setup):
    _, store, run = setup
    first = chunk([event(3), event(1)])
    ingest_chunk(store, run["id"], first)
    latest = ingest_chunk(store, run["id"], chunk([event(3), event(2)], offset=first["next_offset"]))
    assert latest["training"]["step"] == 3
    assert latest["training"]["event_count"] == 3


def test_final_evaluation_of_best_checkpoint_does_not_rewind_training_step(setup):
    _, store, run = setup
    latest = ingest_chunk(store, run["id"], chunk([
        event(1, step=10000), event(2, "evaluation", step=2500, phase="final_evaluation", metrics={"mrr": .19}),
        event(3, "checkpoint", step=2500, name="best.pt"),
    ]))
    assert latest["training"]["step"] == 10000
    assert latest["training"]["latest_evaluation"]["step"] == 2500


def test_sync_drains_chunks_without_finishing_scheduler_run(setup):
    settings, store, run = setup
    first = chunk([event(1)], eof=False)
    second = chunk([event(2, "complete", phase="budget_exhausted", reason="Time budget reached")], offset=first["next_offset"])
    class Adapter:
        def telemetry(self, r, *, offset, max_bytes):
            assert max_bytes <= 262144
            return first if offset == 0 else second
    result = sync_training_telemetry(settings, store, Adapter(), run)
    assert result["training"]["phase"] == "budget_exhausted"
    assert result["state"] == "RUNNING"
    assert result["telemetry_eof"] is True


def test_public_api_pages_filters_exports_and_redacts(setup):
    settings, store, run = setup
    ingest_chunk(store, run["id"], chunk([event(1), event(2, "evaluation", metrics={"mrr": .12}),
                                         event(3, "complete", reason="hidden-user /private/cluster private-webhook-secret")]))
    with TestClient(create_app(settings)) as client:
        base = "/api/runs/" + run["id"]
        public = client.get(base).json()["run"]
        assert public["config"]["batch_size"] == 32
        assert public["training"]["latest_evaluation"]["metrics"] == {"mrr": .12}
        assert len(client.get(base + "/training-events?kind=evaluation&step=2").json()["events"]) == 1
        assert client.get(base + "/training-events?limit=201").status_code == 422
        assert client.get(base + "/training-events?kind=unknown").status_code == 422
        export = client.get(base + "/training-events/download")
        assert len(export.text.splitlines()) == 3
        for hidden in ("hidden-user", "/private/cluster", "private-webhook-secret"):
            assert hidden not in export.text and hidden not in json.dumps(public)
        assert client.get("/api/runs/missing/training-events").status_code == 404


def test_schema_one_upgrade_preserves_existing_history(tmp_path):
    from pathlib import Path
    import dmasif_console.db as db_module
    store = Store(tmp_path / "old.sqlite3")
    with store.connection() as con:
        con.executescript((Path(db_module.__file__).parent / "migrations/001_initial.sql").read_text())
    accepted = store.accept_delivery("hook", "old", "a" * 64, {}, "ACCEPTED", "existing", {"state": "SUCCEEDED"})
    store.initialize()
    assert store.get_run(accepted["run_id"])["state"] == "SUCCEEDED"
    assert event_page(store, accepted["run_id"])["total"] == 0
