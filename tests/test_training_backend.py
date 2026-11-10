import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cluster_adapter.common import AdapterError, read_json, sha256_file
from cluster_adapter.helper import Helper
from cluster_adapter.training import DATASET_FILES, invocation, normalize_items, publish_training, telemetry_chunk
from dmasif_console.config import Settings, TrainingExperimentConfig
from dmasif_console.db import Store
from dmasif_console.sources import SourceError, read_experiment, snapshot_source
from dmasif_console.transport import FakeAdapter, _prepared_run
from dmasif_console.worker import Worker


def config(**changes):
    return TrainingExperimentConfig(repeat_id="training-one", **changes).model_dump()


def dataset(tmp_path):
    root = tmp_path / "project"
    hashes = {}
    for key, relative in DATASET_FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        value = b"registered fixture " + key.encode()
        if key == "items":
            value = b"input_path\tsdf_paths\n/original/project/data/receptor.pdb\tsystems/one/ligand.sdf\n"
        path.write_bytes(value)
        hashes[key] = sha256_file(path)
    for relative in ("data/surfaces", "data/pockets/fpocket", "data/plip", "systems"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return {"kind": "training", "project_root": str(root), "source_prefix": "/original/project", "hashes": hashes}


def source_archive(tmp_path):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("src/run_experiment.py", "src/train.py", "src/surface_encoder.py", "dMaSIF/model.py"):
            data = b"# submitted source; never run by tests\n"
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
    return {"archive_path": str(archive), "sha256": sha256_file(archive), "commit_sha": "a" * 40}


def settings_for(tmp_path):
    data = dataset(tmp_path)
    runtime = tmp_path / "runtime.sif"
    runtime.write_bytes(b"pinned runtime fixture")
    return Settings(state_dir=tmp_path / "state", repository={"id": 1, "full_name": "lab/research", "clone_url": str(tmp_path)},
                    allowed_actor_ids=[1], min_free_bytes=0, submissions_enabled=True,
                    datasets={"dataset_v2": data}, presets={"training": {"kind": "training"}},
                    cluster={"root": str(tmp_path / "cluster"), "runtime_image": str(runtime), "runtime_sha256": sha256_file(runtime),
                             "partition": "alpha"})


def run_for(settings, cfg=None):
    policy = settings.policy_snapshot()
    cluster = {**policy["cluster"], "checkpoint_path": policy["datasets"]["dataset_v2"]["project_root"] + "/" + DATASET_FILES["head"],
               "checkpoint_sha256": policy["datasets"]["dataset_v2"]["hashes"]["head"]}
    return {"id": str(uuid4()), "commit_sha": "a" * 40, "config": cfg or config(), "policy": policy,
            "resolved": {"dataset": policy["datasets"]["dataset_v2"], "preset": policy["presets"]["training"], "cluster": cluster}}


@pytest.mark.parametrize("field,value", [("batch_size", True), ("batch_size", "32"), ("batch_size", 1), ("batch_size", 129),
                                         ("steps", 0), ("seed", -1), ("schema_version", True),
                                         ("dataset_id", "other"), ("preset_id", "quick-test"), ("command", "anything")])
def test_training_schema_rejects_unsafe_or_unregistered_values(field, value):
    with pytest.raises(ValidationError):
        config(**{field: value})


def test_training_source_requires_wrapper_without_extraction_entrypoint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    for name in ("src/run_experiment.py", "src/train.py", "src/surface_encoder.py", "dMaSIF/model.py"):
        path = repo / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("# pinned source\n")
    (repo / "experiments").mkdir()
    (repo / "experiments/run.yaml").write_text("job_type: dmasif_train\nrepeat_id: one\nbatch_size: 16\n")
    git("add", ".")
    git("commit", "-qm", "training source")
    settings = settings_for(tmp_path)
    settings.repository.clone_url = str(repo)
    snapshot = snapshot_source(settings, git("rev-parse", "HEAD"))
    assert read_experiment(snapshot)[0]["batch_size"] == 16
    (repo / "src/run_experiment.py").unlink()
    git("add", ".")
    git("commit", "-qm", "missing wrapper")
    broken = snapshot_source(settings, git("rev-parse", "HEAD"))
    with pytest.raises(SourceError, match="src/run_experiment.py"):
        read_experiment(broken)
    assert read_experiment(snapshot)[0]["batch_size"] == 16


@pytest.mark.parametrize("missing,setting", [("datasets", "dataset_id: dataset_v2"), ("presets", "preset_id: training")])
def test_missing_training_registration_explains_server_configuration_without_submitting(tmp_path, monkeypatch, missing, setting):
    settings = settings_for(tmp_path)
    policy = settings.policy_snapshot()
    policy[missing] = {}
    source = source_archive(tmp_path)
    monkeypatch.setattr("dmasif_console.worker.snapshot_source", lambda *_: source)
    monkeypatch.setattr("dmasif_console.worker.read_experiment", lambda *_: (config(steps=10), "fixture"))
    store = Store(settings.database_path)
    store.initialize()
    adapter = FakeAdapter(settings)
    def must_not_stage(*args):
        pytest.fail("An unregistered training request must never reach the cluster")
    monkeypatch.setattr(adapter, "stage", must_not_stage)
    response = store.accept_delivery("hook", "missing-registration", hashlib.sha256(b"missing").hexdigest(), {},
                                    "ACCEPTED", "", {"commit_sha": "a" * 40, "actor_login": "scientist",
                                                     "policy": policy, "state": "CHECKING_REQUEST"})
    worker = Worker(settings, store, adapter)
    worker.tick()
    run = store.get_run(response["run_id"])
    assert run["state"] == "REJECTED"
    assert "not registered on the server" in run["reason"]
    assert "operator must update deployment configuration" in run["reason"]
    assert "Keep " + setting in run["reason"]
    assert "Invalid experiments/run.yaml" not in run["reason"]
    assert store.jobs(run["id"]) == [] and not run.get("capacity_reserved")


def test_helper_training_stages_only_normalized_items_and_detects_mutation(tmp_path):
    settings = settings_for(tmp_path)
    run = _prepared_run(run_for(settings))
    source = source_archive(tmp_path)
    helper = Helper(Path(settings.cluster.root))
    payload = {"run": run, "source": source, "archive_b64": base64.b64encode(Path(source["archive_path"]).read_bytes()).decode(), "limits": {"min_free_bytes": 0}}
    stage = helper.stage(payload)
    directory = helper.directory(run["id"])
    assert {path.name for path in (directory / "inputs").iterdir()} == {"items.tsv"}
    assert "/dataset/data/receptor.pdb" in (directory / "inputs/items.tsv").read_text()
    assert stage["training"]["original_items_sha256"] != stage["training"]["items_sha256"]
    script = (directory / "submit.sbatch").read_text()
    assert "--time=12:00:00" in script and "--mem=96G" in script and "--partition=alpha" in script
    assert "--qos=" not in script
    assert helper.stage(payload) == stage
    (Path(run["resolved"]["dataset"]["project_root"]) / DATASET_FILES["val_set"]).write_text("mutated")
    with pytest.raises(AdapterError, match="changed"):
        helper.verify_stage(directory, stage)


def test_item_rewrite_refuses_escape_and_preserves_relative_ligand_paths():
    text = "input_path\tsdf_paths\n/old/data/a.pdb\tsystems/x.sdf\n"
    assert normalize_items(text, "/old") == text.replace("/old/", "/dataset/")
    for value in ("/unapproved/a.pdb", "../a.pdb"):
        with pytest.raises(AdapterError):
            normalize_items(text.replace("/old/data/a.pdb", value), "/old")


def output_files(raw, cfg, status="completed", completed=None):
    raw.mkdir(parents=True)
    step = cfg["steps"] if completed is None else completed
    checkpoints = []
    for name in ("best.pt", "last.pt"):
        path = raw / name
        path.write_text(json.dumps({"step": step}))
        checkpoints.append({"name": name, "step": step, "size": path.stat().st_size, "sha256": sha256_file(path)})
    (raw / "history.json").write_text(json.dumps([{"step": step, "mrr": 0.25}]))
    (raw / "training-summary.json").write_text(json.dumps({"schema_version": 1, "dataset_id": "dataset_v2", "status": status,
        "requested_steps": cfg["steps"], "completed_steps": step, "optimizer_updates": step, "batch_size": cfg["batch_size"], "seed": cfg["seed"], "checkpoints": checkpoints}))
    (raw / "evaluation.json").write_text(json.dumps({"schema_version": 1, "dataset_id": "dataset_v2", "validation_set": "valset_v1v2",
        "checkpoint": "best.pt", "step": step, "metrics": {"mrr": 0.25}}))


@pytest.mark.parametrize("status,completed,expected", [("completed", 100, "SUCCEEDED"), ("budget_exhausted", 30, "PARTIAL"),
                                                      ("completed", 30, "FAILED")])
def test_training_publication_requires_step_target_and_heldout_outputs(tmp_path, status, completed, expected):
    cfg = config(steps=100)
    job = tmp_path / "run/jobs/17"
    raw = job / "training"
    output_files(raw, cfg, status, completed)
    result = publish_training(raw, job / "published", {}, job / "result-manifest.json", cfg,
                              checkpoint_validator=lambda path: read_json(path))
    assert result["outcome"] == expected
    assert len(result["artifacts"]) == 5
    for item in result["artifacts"]:
        assert sha256_file(tmp_path / "run" / item["relative_path"]) == item["sha256"]


def test_training_publication_missing_evaluation_cannot_succeed(tmp_path):
    cfg = config(steps=100)
    job = tmp_path / "run/jobs/17"
    output_files(job / "training", cfg)
    (job / "training/evaluation.json").unlink()
    result = publish_training(job / "training", job / "published", {}, job / "result-manifest.json", cfg,
                              checkpoint_validator=lambda path: read_json(path))
    assert result["outcome"] == "FAILED"


def test_telemetry_protocol_reads_complete_lines_without_loss(tmp_path):
    path = tmp_path / "jobs/17/training/telemetry.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"seq":1}\n{"seq":')
    first = telemetry_chunk(tmp_path, [{"job_id": "17"}], 0, 100)
    assert first["text"] == '{"seq":1}\n' and not first["eof"]
    with path.open("ab") as stream:
        stream.write(b"2}\n")
    second = telemetry_chunk(tmp_path, [{"job_id": "17"}], first["next_offset"], 100)
    assert second["text"] == '{"seq":2}\n' and second["eof"]
    with pytest.raises(AdapterError):
        telemetry_chunk(tmp_path, [{"job_id": "17"}], 999, 100)


def test_training_invocation_uses_fixed_source_and_isolated_output(tmp_path):
    settings = settings_for(tmp_path)
    run = run_for(settings)
    command = invocation(run, Path("/run/jobs/17/training"))
    assert command[0] == "/source/src/run_experiment.py"
    assert command[command.index("--out") + 1] == "/run/jobs/17/training"
    assert command[command.index("--project-root") + 1] == "/dataset"
    assert command[command.index("--batch-size") + 1] == "32"
    assert command[command.index("--max-hours") + 1] == "11.0"


def test_fake_training_keeps_real_lifecycle_and_synthetic_outputs(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    source = source_archive(tmp_path)
    monkeypatch.setattr("dmasif_console.worker.snapshot_source", lambda *_: source)
    monkeypatch.setattr("dmasif_console.worker.read_experiment", lambda *_: (config(steps=100), "fixture"))
    store = Store(settings.database_path)
    store.initialize()
    worker = Worker(settings, store, FakeAdapter(settings))
    response = store.accept_delivery("hook", "training", hashlib.sha256(b"training").hexdigest(), {}, "ACCEPTED", "",
                                    {"commit_sha": "a" * 40, "actor_login": "scientist", "policy": settings.policy_snapshot(), "state": "CHECKING_REQUEST"})
    for _ in range(3):
        worker.tick()
    run = store.get_run(response["run_id"])
    assert run["state"] == "SUCCEEDED", run
    assert len(run["artifacts"]) == 5
    assert all(item["cache_state"] == "cached" for item in run["artifacts"])
    assert not run["capacity_reserved"]
    telemetry = worker.adapter.telemetry({**run, "jobs": store.jobs(run["id"])})
    events = [json.loads(line) for line in telemetry["text"].splitlines()]
    assert {item["kind"] for item in events} == {"phase", "step", "evaluation", "checkpoint", "complete"}
    assert [item["seq"] for item in events] == list(range(1, len(events) + 1))
    assert run["result"]["provenance"]["simulation"]


def test_training_container_binds_pinned_source_readonly_data_and_isolated_output(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cluster_adapter.batch import _execute
    settings = settings_for(tmp_path)
    run = _prepared_run(run_for(settings))
    source = source_archive(tmp_path)
    helper = Helper(Path(settings.cluster.root))
    stage = helper.stage({"run": run, "source": source, "archive_b64": base64.b64encode(Path(source["archive_path"]).read_bytes()).decode(), "limits": {"min_free_bytes": 0}})
    directory = helper.directory(run["id"])
    job = directory / "jobs/17"
    (job / "tmp").mkdir(parents=True)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("PYTHONPATH", "/old/mutable/research")
    calls = []
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="apptainer fixture")
    monkeypatch.setattr("cluster_adapter.batch.subprocess.run", command)
    assert _execute(directory, "17", stage, False, job / "tmp") == 0
    argv, kwargs = calls[-1]
    assert str(Path(stage["source_dir"])) + ":/source:ro" in argv
    assert stage["training"]["project_root"] + ":/dataset:ro" in argv
    assert stage["training"]["items_path"] + ":/dataset/out/runs/dataset_v2/items.tsv:ro" in argv
    assert "CRIS_READONLY_DATA=1" in argv
    assert argv[argv.index("--pwd") + 1] == "/dataset"
    assert "PYTHONPATH" not in kwargs["env"]
    assert argv[-3:] == ["/adapter/execute.py", "/run", "17"]


def test_scheduler_zero_exit_with_budget_manifest_is_partial_and_keeps_downloads(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    source = source_archive(tmp_path)
    monkeypatch.setattr("dmasif_console.worker.snapshot_source", lambda *_: source)
    monkeypatch.setattr("dmasif_console.worker.read_experiment", lambda *_: (config(steps=100), "fixture"))
    store = Store(settings.database_path)
    store.initialize()
    adapter = FakeAdapter(settings)
    original = adapter.results
    def budget_result(run):
        result = original(run)
        if result:
            result["outcome"] = "PARTIAL"
            result["training"].update(status="budget_exhausted", completed_steps=30)
        return result
    monkeypatch.setattr(adapter, "results", budget_result)
    worker = Worker(settings, store, adapter)
    response = store.accept_delivery("hook", "budget", hashlib.sha256(b"budget").hexdigest(), {}, "ACCEPTED", "",
                                    {"commit_sha": "a" * 40, "actor_login": "scientist", "policy": settings.policy_snapshot(), "state": "CHECKING_REQUEST"})
    for _ in range(3):
        worker.tick()
    run = store.get_run(response["run_id"])
    assert store.jobs(run["id"])[0]["exit_code"] == "0:0"
    assert run["state"] == "PARTIAL" and "step target" in run["reason"]
    assert len(run["artifacts"]) == 5 and all(item["cache_state"] == "cached" for item in run["artifacts"])
    assert not run["capacity_reserved"]


def test_terminal_training_backlog_drains_across_ticks_without_resubmission(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    source = source_archive(tmp_path)
    monkeypatch.setattr("dmasif_console.worker.snapshot_source", lambda *_: source)
    monkeypatch.setattr("dmasif_console.worker.read_experiment", lambda *_: (config(steps=100), "fixture"))
    store = Store(settings.database_path)
    store.initialize()
    adapter = FakeAdapter(settings)
    original_produce, original_telemetry = adapter._produce_training, adapter.telemetry
    def long_training(run, job):
        original_produce(run, job)
        path = adapter.directory(run) / "jobs" / job["job_id"] / "training/telemetry.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        last = rows[-1]
        with path.open("a") as output:
            for index in range(200):
                event = {**last, "seq": last["seq"] + index + 1, "kind": "step", "phase": "training",
                         "metrics": {"loss": 0.5, "mrr": 0.3}}
                output.write(json.dumps(event) + "\n")
    monkeypatch.setattr(adapter, "_produce_training", long_training)
    # A compliant endpoint may return less than the upper bound. Small chunks
    # create a real multi-poll backlog without a multi-megabyte test fixture.
    monkeypatch.setattr(adapter, "telemetry", lambda run, offset=0, max_bytes=262144:
                        original_telemetry(run, offset=offset, max_bytes=min(max_bytes, 1024)))
    worker = Worker(settings, store, adapter)
    response = store.accept_delivery("hook", "backlog", hashlib.sha256(b"backlog").hexdigest(), {}, "ACCEPTED", "",
                                    {"commit_sha": "a" * 40, "actor_login": "scientist", "policy": settings.policy_snapshot(), "state": "CHECKING_REQUEST"})
    for _ in range(3):
        worker.tick()
    run = store.get_run(response["run_id"])
    assert run["state"] == "SUCCEEDED" and not run["capacity_reserved"]
    assert not run["telemetry_eof"]
    first_cursor = run["training_cursor"]
    for _ in range(30):
        worker.tick()
        run = store.get_run(run["id"])
        if run.get("telemetry_eof"):
            break
    job = store.jobs(run["id"])[0]
    path = adapter.directory(run) / "jobs" / job["job_id"] / "training/telemetry.jsonl"
    assert run["telemetry_eof"] and run["training_cursor"] == path.stat().st_size > first_cursor
    with store.connection() as connection:
        count = connection.execute("SELECT count(*) FROM training_events WHERE run_id=?", (run["id"],)).fetchone()[0]
    assert count == len(path.read_text().splitlines())
    assert len(store.jobs(run["id"])) == 1 and run["state"] == "SUCCEEDED"


def test_optional_retained_checkpoints_and_evaluation_rows_are_downloadable(tmp_path):
    cfg = config(steps=100)
    job = tmp_path / "run/jobs/17"
    raw = job / "training"
    output_files(raw, cfg)
    retained = raw / "step50.pt"
    retained.write_text(json.dumps({"step": 50}))
    summary = read_json(raw / "training-summary.json")
    summary["checkpoints"].append({"name": retained.name, "step": 50,
                                    "size": retained.stat().st_size, "sha256": sha256_file(retained)})
    (raw / "training-summary.json").write_text(json.dumps(summary))
    (raw / "evaluation-rows.tsv").write_text("# /run/jobs/17/training/best.pt step 100\nrow\tsystem_id\tgroup\trank\tn_cand\n0\tsystem-one\tgroup-one\t2\t10\n")
    (raw / "unrequested.txt").write_text("This arbitrary file is not published")
    result = publish_training(raw, job / "published", {}, job / "result-manifest.json", cfg,
                              checkpoint_validator=lambda path: read_json(path))
    assert result["outcome"] == "SUCCEEDED"
    assert result["expected_count"] == result["valid_count"] == len(result["artifacts"]) == 7
    optional = {item["name"]: item for item in result["artifacts"]}
    assert optional["step50.pt"]["step"] == 50
    assert optional["evaluation-rows.tsv"]["rows"] == 1
    assert "unrequested.txt" not in optional
    helper = Helper(tmp_path)
    # The fixed artifact endpoint accepts only these manifest-listed retained
    # bytes, including optional output formats.
    run_id = str(uuid4())
    final = tmp_path / "runs" / run_id
    final.parent.mkdir()
    (tmp_path / "run").rename(final)
    for name in ("step50.pt", "evaluation-rows.tsv"):
        response = helper.artifact({"run_id": run_id, "artifact": optional[name], "limit": 1024 * 1024})
        data = base64.b64decode(response["data"])
        assert hashlib.sha256(data).hexdigest() == optional[name]["sha256"]


@pytest.mark.parametrize("kind", ["symlink", "corrupt_tsv", "wrong_step", "too_many"])
def test_unsafe_or_corrupt_optional_training_outputs_fail_publication(tmp_path, kind):
    cfg = config(steps=100)
    job = tmp_path / "run/jobs/17"
    raw = job / "training"
    output_files(raw, cfg)
    if kind == "symlink":
        (raw / "step50.pt").symlink_to(raw / "best.pt")
    elif kind == "corrupt_tsv":
        (raw / "evaluation-rows.tsv").write_text("row\tsystem_id\tgroup\trank\tn_cand\n0\tx\tg\t11\t10\n")
    elif kind == "wrong_step":
        (raw / "step50.pt").write_text(json.dumps({"step": 49}))
    else:
        for step in range(101):
            (raw / f"step{step}.pt").write_text(json.dumps({"step": step}))
    result = publish_training(raw, job / "published", {}, job / "result-manifest.json", cfg,
                              checkpoint_validator=lambda path: read_json(path))
    assert result["outcome"] == "FAILED" and result["errors"]
