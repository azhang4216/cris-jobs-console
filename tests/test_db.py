import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from dmasif_console.db import Store


def enqueue(store, number):
    return store.accept_delivery("hook", f"delivery-{number}", hashlib.sha256(str(number).encode()).hexdigest(), {},
                                 "ACCEPTED", "", {"state": "WAITING_FOR_CAPACITY", "commit_sha": "a" * 40})["run_id"]


def test_capacity_reservation_is_singleton_and_unknown_state_holds_it(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.initialize()
    first, second = enqueue(store, 1), enqueue(store, 2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.reserve_next(), range(4)))
    assert len([r for r in results if r]) == 1
    assert [r for r in results if r][0]["id"] == first
    store.update_run(first, {"state": "SUBMISSION_UNKNOWN"})
    assert store.reserve_next() is None
    with pytest.raises(sqlite3.IntegrityError):
        store.update_run(second, {"capacity_reserved": True})
    store.update_run(first, {"state": "NEEDS_REVIEW", "capacity_reserved": False})
    assert store.reserve_next()["id"] == second


def test_pause_blocks_new_reservation(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.initialize()
    enqueue(store, 1)
    store.set_paused(True, "operator maintenance", "collin")
    assert store.reserve_next() is None
    store.set_paused(False, "complete", "collin")
    assert store.reserve_next()


def test_restore_rebuilds_replay_mapping_and_preserves_multiple_job_ids(tmp_path):
    original = Store(tmp_path / "original.sqlite3")
    original.initialize()
    run_id = enqueue(original, 1)
    run = original.get_run(run_id)
    restored = Store(tmp_path / "restored.sqlite3")
    restored.initialize()
    restored.set_paused(True, "restore")
    restored.restore_run(run, [{"job_id": "10", "cluster": "fake", "state": "RUNNING"},
                              {"job_id": "11", "cluster": "fake", "state": "RUNNING"}])
    replay = restored.accept_delivery(run["hook_id"], "new-delivery-header", run["body_hash"], {}, "ACCEPTED", "", {})
    assert replay["duplicate"] and replay["run_id"] == run_id
    assert len(restored.jobs(run_id)) == 2
    assert restored.control_state()["paused"]
    assert restored.reserve_next() is None
