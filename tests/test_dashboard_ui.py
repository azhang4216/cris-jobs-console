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
