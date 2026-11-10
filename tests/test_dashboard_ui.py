"""Template safety and usability contracts; these tests require no cluster access."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


@pytest.fixture
def templates():
    directory = Path(__file__).resolve().parents[1] / "dmasif_console" / "templates"
    return Environment(loader=FileSystemLoader(directory), autoescape=select_autoescape(["html"]))


@pytest.fixture
def common():
    return {
        "settings": {
            "mode": "fake",
            "operator_contact": "the lab operator",
            "poll_seconds": 8,
            "repository_url": "https://github.com/example/research",
            "instructions_url": "https://github.com/azhang4216/dmasif-experiments#readme",
        },
        "status_labels": {"CHECKING_REQUEST": "Checking request", "SUCCEEDED": "Succeeded"},
    }


def test_empty_history_links_to_repository_instructions_without_browser_write_controls(templates, common):
    page = templates.get_template("history.html").render(**common, runs=[], activity=[], stats={}, filters={})
    assert "Demo · simulated data" in page
    assert 'href="https://github.com/azhang4216/dmasif-experiments#readme"' in page
    assert "How to run an experiment" in page
    assert 'method="post"' not in page.lower()
    assert 'type="file"' not in page.lower()
    assert "Training progress" in page
    assert "Available datasets" not in page
    assert "datasets-panel" not in page


def test_untrusted_run_strings_and_logs_are_escaped(templates, common):
    attack = '<script>alert("unsafe")</script>'
    run = {
        "id": "abc",
        "experiment": attack,
        "actor_login": "researcher",
        "reason": attack,
        "log_tail": attack,
        "state": "CHECKING_REQUEST",
        "commit_sha": "abc123",
        "commit_url": "javascript:alert(1)",
    }
    history = templates.get_template("history.html").render(**common, runs=[run], activity=[], stats={}, filters={})
    detail = templates.get_template("detail.html").render(**common, run=run, jobs=[], events=[])
    for page in (history, detail):
        assert attack not in page
        assert "&lt;script&gt;" in page
        assert 'href="javascript:' not in page


def test_new_run_with_unknown_fields_renders_and_does_not_leak_private_provenance(templates, common):
    run = {
        "id": "abc",
        "commit_url": None,
        "commit_sha": "abc123",
        "provenance": {
            "source_sha256": "saved-source-hash",
            "runtime_sha256": "saved-runtime-hash",
            "python": "3.12.10",
            "torch": "2.6.0",
            "cuda_runtime": "12.4",
            "private_key_path": "/secret/private-key",
            "remote_dir": "/private/cluster/root",
            "ssh_host": "private-host.invalid",
        },
    }
    page = templates.get_template("detail.html").render(**common, run=run, jobs=[], events=[])
    assert 'data-timing="submitted_at"><span class="muted">—</span>' in page
    assert "saved-source-hash" in page
    assert "saved-runtime-hash" in page
    for runtime_version in ("3.12.10", "2.6.0", "12.4"):
        assert runtime_version in page
    for private_value in ("/secret/private-key", "/private/cluster/root", "private-host.invalid"):
        assert private_value not in page


def test_history_survives_initial_run_without_actor_metadata(templates, common):
    page = templates.get_template("history.html").render(
        **common, runs=[{"id": "abc"}, {"id": "def", "actor_login": "researcher"}],
        activity=[], stats={}, filters={"actor": "researcher"},
    )
    assert "Unknown researcher" in page
    assert 'value="researcher" selected' in page


def test_history_omits_global_operator_pause_notice(templates, common):
    page = templates.get_template("history.html").render(
        **common, runs=[], activity=[], stats={}, filters={},
        control={"paused": True, "reason": "Inspecting <script>unsafe()</script>"},
    )
    assert 'id="submission-notice"' not in page
    assert "New submissions paused." not in page
    assert "Inspecting" not in page
    assert "Up to 200 matches" in page


def test_training_detail_prioritizes_steps_evaluation_and_checkpoints(templates, common):
    run = {
        "id": "train-1", "state": "RUNNING", "dataset_id": "dataset_v2",
        "config": {"job_type": "dmasif_train", "batch_size": 8, "steps": 1000},
        "training": {
            "phase": "training", "step": 250, "total_steps": 1000,
            "latest_metrics": {"loss": 0.32, "custom_head_loss": None},
            "latest_evaluation": {"step": 200, "timestamp": "2026-11-10T17:00:00Z", "metrics": {"recall_at_1": 0.48}},
            "checkpoints": [{"name": "latest.pt", "step": 200, "timestamp": "2026-11-10T17:00:01Z"}],
        },
        "artifacts": [{"name": "latest.pt", "cached": False, "size": 123}],
    }
    page = templates.get_template("detail.html").render(**common, run=run, jobs=[], events=[])
    assert page.index('id="training-title"') < page.index('id="logs-title"')
    assert page.index('id="evaluation-title"') < page.index('id="logs-title"')
    assert page.index('id="checkpoints-title"') < page.index('id="logs-title"')
    for text in ("custom_head_loss", "recall_at_1", "latest.pt", "Batch size", "Go to step", "Download all metrics"):
        assert text in page
    assert 'id="training-progress" max="1000" value="250"' in page
    assert 'class="file-icon" aria-hidden="true">PT</div>' in page
    assert "recent output only" in page


@pytest.mark.parametrize("job_type", [None, "dmasif_train"])
def test_training_detail_handles_queued_and_older_runs(templates, common, job_type):
    run = {"id": "new", "state": "QUEUED", "config": {"job_type": job_type}, "training": {}}
    page = templates.get_template("detail.html").render(**common, run=run, jobs=[], events=[])
    assert "Metrics will appear when the first training step is reported." in page
    assert "No checkpoint reported yet." in page
    overview = page.split('id="training-overview"', 1)[1].split(">", 1)[0]
    assert ("hidden" in overview) is (job_type is None)


def test_training_metrics_and_checkpoint_names_are_escaped(templates, common):
    attack = '<img src=x onerror="alert(1)">'
    run = {
        "id": "train", "config": {"job_type": "dmasif_train"},
        "training": {
            "latest_metrics": {attack: 1}, "phase": attack,
            "latest_evaluation": {"metrics": {attack: 2}},
            "checkpoints": [{"name": attack, "step": 3}], "complete_reason": attack,
        },
    }
    page = templates.get_template("detail.html").render(**common, run=run, jobs=[], events=[])
    assert attack not in page
    assert "&lt;img" in page


@pytest.mark.parametrize("state,label", [
    ("REJECTED", "Not started · rejected"),
    ("PREPARATION_FAILED", "Not started · preparation failed"),
])
def test_unsubmitted_training_failure_shows_reason_instead_of_waiting(templates, common, state, label):
    reason = 'Server dataset configuration is missing <private>'
    run = {"id": "blocked", "state": state, "reason": reason,
           "config": {"job_type": "dmasif_train", "batch_size": 8}, "training": None}
    history = templates.get_template("history.html").render(
        **common, runs=[run], activity=[], stats={}, filters={},
    )
    assert label in history
    assert "Waiting for training" not in history
    assert "Server dataset configuration is missing &lt;private&gt;" in history
    detail = templates.get_template("detail.html").render(**common, run=run, jobs=[], events=[])
    assert 'id="training-stopped-title">Training did not start</h2>' in detail
    assert 'id="training-stopped-context">No cluster job was submitted.</p>' in detail
    assert 'id="training-stopped-reason" >Server dataset configuration is missing &lt;private&gt;</p>' in detail
    assert "hidden" not in detail.split('id="training-stopped"', 1)[1].split(">", 1)[0]
    for panel in ("training-overview", "evaluation-panel", "checkpoints-panel", "training-events-panel"):
        assert "hidden" in detail.split(f'id="{panel}"', 1)[1].split(">", 1)[0]


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "TIMED_OUT", "PARTIAL"])
def test_terminal_training_keeps_recorded_steps_and_uses_past_tense(templates, common, state):
    run = {"id": "stopped", "state": state, "submitted_at": "2026-11-10T17:00:00Z",
           "config": {"job_type": "dmasif_train"},
           "training": {"event_count": 3, "step": 2, "phase": "training", "latest_metrics": {"loss": 0.5}}}
    page = templates.get_template("detail.html").render(**common, run=run, jobs=[], events=[])
    assert "hidden" in page.split('id="training-stopped"', 1)[1].split(">", 1)[0]
    assert "hidden" not in page.split('id="training-events-panel"', 1)[1].split(">", 1)[0]
    assert "Last reported: Training" in page
    assert "No held-out evaluation was recorded." in page
    assert "No checkpoint was recorded." in page
    assert "loss" in page and "0.5" in page


def test_submitted_failure_without_metrics_does_not_claim_no_job(templates, common):
    run = {"id": "failed", "state": "FAILED", "submitted_at": "2026-11-10T17:00:00Z",
           "config": {"job_type": "dmasif_train"}, "training": {}}
    page = templates.get_template("detail.html").render(**common, run=run, jobs=[{"job_id": "123"}], events=[])
    assert 'id="training-stopped-title">No training metrics recorded</h2>' in page
    assert 'id="training-stopped-context">This run has ended without recorded training progress.</p>' in page
