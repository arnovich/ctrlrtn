"""Git-backed desired routing state: schema, provenance, and activation."""

from __future__ import annotations

import sqlite3
import subprocess

import pytest

from ctrlrtn.cli import commands as cli
from ctrlrtn.control_config import (
    ControlConfig,
    ControlConfigError,
    ControlRevision,
    config_diff,
    load_control_config,
    verify_git_revision,
)
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.route import Route, WorkflowRoute
from ctrlrtn.recorder.store import SqliteTraceStore


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _document() -> str:
    return """\
version: 1
routes:
  tag:editor:
    model: local-editor
    previous_model: incumbent
    provider: ollama
    note: replay passed
experiments:
  tag:journalist:
    id: exp:journalist-v1
    candidate_model: local-writer
    provider: ollama
    split_pct: 25
    max_calls_per_task: 30
    max_cost_usd_per_task: 2.5
workflow_routes:
  article-pipeline:
    git:abc123:
      model: local-generalist
      provider: ollama
      steps:
        draft:
          model: local-writer
          note: replay passed
workflows:
  article-pipeline:
    git:abc123:
      steps:
        research:
          allows: {fan_out: true, retry: true}
        draft:
          predecessors: [research]
          allows: {retry: true}
"""


def test_load_control_config_validates_and_builds_domain_objects(tmp_path):
    config = load_control_config(_write(tmp_path / "routing.yaml", _document()))
    assert config.routes == (
        Route(
            "tag:editor",
            "local-editor",
            previous_model="incumbent",
            note="replay passed",
            provider="ollama",
            ts=0.0,
        ),
    )
    assert config.experiments[0].experiment_id == "exp:journalist-v1"
    assert config.experiments[0].split_pct == 25
    assert config.experiments[0].max_calls_per_task == 30
    assert [route.key for route in config.workflow_routes] == [
        ("article-pipeline", "git:abc123", None),
        ("article-pipeline", "git:abc123", "draft"),
    ]
    assert config.workflows[0].steps[1].predecessors == ("research",)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("version: 2\n", "version must be 1"),
        ("version: 1\nroutes: []\n", "routes must be a mapping"),
        (
            "version: 1\nexperiments:\n  tag:x:\n    candidate_model: m\n",
            "requires a stable non-empty id",
        ),
        (
            "version: 1\nroutes:\n  tag:x:\n    model: m\n    secret: x\n",
            "unknown route 'tag:x' key",
        ),
        (
            "version: 1\nworkflow_routes:\n  pipeline:\n    v1:\n      model: m\n",
            "has no declared workflow",
        ),
        (
            "version: 1\nworkflows:\n  pipeline:\n    v1:\n      steps:\n"
            "        draft:\n          predecessors: [missing]\n",
            "invalid predecessors",
        ),
    ],
)
def test_invalid_control_documents_fail_cleanly(tmp_path, text, message):
    path = _write(tmp_path / "routing.yaml", text)
    with pytest.raises(ControlConfigError, match=message):
        load_control_config(path)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_git_revision_requires_a_clean_tracked_document(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    path = repo / "routing.yaml"
    _write(path, _document())
    _git(repo, "add", "routing.yaml")
    _git(repo, "commit", "-qm", "routing")
    revision = verify_git_revision(str(repo), str(path))
    assert len(revision.revision) == 40
    assert revision.source_path == "routing.yaml"
    assert len(revision.document_sha256) == 64

    path.write_text(_document() + "# dirty\n", encoding="utf-8")
    with pytest.raises(ControlConfigError, match="uncommitted changes"):
        verify_git_revision(str(repo), str(path))


def test_activation_reconciles_atomically_and_records_provenance(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    store.set_route(Route("tag:old", "old-model", ts=2.0))
    store.create_experiment(
        Experiment("tag:old", "old-candidate", 50, experiment_id="exp:old")
    )
    config = ControlConfig(
        routes=(Route("tag:new", "new-model", previous_model="base", ts=0.0),),
        experiments=(
            Experiment(
                "tag:new",
                "new-candidate",
                20,
                experiment_id="exp:new",
                created_epoch=0.0,
            ),
        ),
        workflow_routes=(
            WorkflowRoute("pipeline", "v1", "step-model", step="draft"),
        ),
    )
    revision = ControlRevision("a" * 40, "routing.yaml", "b" * 64, 10.0)
    try:
        store.activate_control_config(config, revision)
        assert [route.use_case_key for route in store.routes()] == ["tag:new"]
        assert store.routes()[0].ts == 10.0
        assert set(store.running_experiments()) == {"tag:new"}
        assert not store.experiment("exp:old").is_running
        assert store.experiment("exp:new").created_epoch == 10.0
        assert store.control_revision() == revision
        assert store.workflow_routes()[0].key == ("pipeline", "v1", "draft")
        assert store.workflow_routes()[0].ts == 10.0
        assert store.workflow_definitions() == list(config.workflows)

        # Re-activation is idempotent: unchanged objects retain their timestamps.
        newer = ControlRevision("c" * 40, "routing.yaml", "d" * 64, 20.0)
        store.activate_control_config(config, newer)
        assert store.routes()[0].ts == 10.0
        assert store.experiment("exp:new").created_epoch == 10.0
        assert store.control_revision() == newer
        assert store.workflow_routes()[0].ts == 10.0
    finally:
        store.close()


def test_failed_activation_retains_last_known_good_state(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    store.set_route(Route("tag:kept", "kept", ts=1.0))
    store.create_experiment(
        Experiment("tag:x", "first", 50, experiment_id="exp:used")
    )
    store.stop_experiment("exp:used")
    bad = ControlConfig(
        routes=(Route("tag:new", "new", ts=0.0),),
        experiments=(
            Experiment("tag:y", "second", 50, experiment_id="exp:used"),
        ),
    )
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.activate_control_config(
                bad, ControlRevision("a" * 40, "routing.yaml", "b" * 64)
            )
        assert [
            (route.use_case_key, route.model) for route in store.routes()
        ] == [("tag:kept", "kept")]
        assert store.control_revision() is None
    finally:
        store.close()


def test_diff_compares_desired_with_active_state():
    current = ControlConfig(routes=(Route("tag:x", "old", ts=1.0),))
    desired = ControlConfig(routes=(Route("tag:x", "new", ts=0.0),))
    diff = config_diff(current, desired)
    assert "--- active" in diff and "+++ desired" in diff
    assert "-    model: old" in diff and "+    model: new" in diff


def test_cli_validate_diff_activate_and_status(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    path = repo / "routing.yaml"
    _write(
        path,
        "version: 1\nroutes:\n  tag:x:\n    model: candidate\n",
    )
    _git(repo, "add", "routing.yaml")
    _git(repo, "commit", "-qm", "routing")
    db = str(tmp_path / "router.db")
    monkeypatch.setenv("CTRLRTN_DB", db)

    cli.main(["routing-config", "validate", str(path)])
    assert "Valid routing config" in capsys.readouterr().out
    cli.main(["routing-config", "diff", str(path)])
    assert "+  tag:x:" in capsys.readouterr().out
    cli.main(["routing-config", "activate", str(path), "--repo", str(repo)])
    assert "Activated routing.yaml" in capsys.readouterr().out
    cli.main(["routing-config", "status"])
    status = capsys.readouterr().out
    assert "revision:" in status and "active:    1 route(s)" in status
