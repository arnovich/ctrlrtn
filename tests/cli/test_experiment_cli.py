"""Slice 6a: the `experiment` CLI — start / list / stop over a real store."""

from __future__ import annotations

import pytest

from ctrlrtn.cli.commands import main
from ctrlrtn.cli.render import (
    render_experiment_started,
    render_experiments,
)
from ctrlrtn.policy.experiment import Experiment


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the CLI's store at a throwaway SQLite file."""
    monkeypatch.setenv("CTRLRTN_DB", str(tmp_path / "cli.db"))
    config = tmp_path / "config.yaml"
    config.write_text(
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434\n"
        "    api: openai\n"
        "    free: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CTRLRTN_CONFIG", str(config))


# --- renderers ------------------------------------------------------------


def test_render_experiments_empty_guides_the_user():
    out = render_experiments([])
    assert "No experiments yet" in out
    assert "experiment start" in out


def test_render_experiments_table_shows_the_facts():
    exp = Experiment(
        "tag:editor",
        "claude-haiku-4-5",
        30,
        experiment_id="exp:abc",
        candidate_provider="ollama",
    )
    out = render_experiments([exp])
    assert "running" in out
    assert "tag:editor" in out and "claude-haiku-4-5" in out
    assert "exp:abc" in out and "30%" in out
    assert "ollama" in out


def test_render_started_mentions_id_usecase_candidate_split():
    exp = Experiment(
        "tag:editor", "claude-haiku-4-5", 30, experiment_id="exp:abc"
    )
    out = render_experiment_started(exp)
    assert "exp:abc" in out and "tag:editor" in out
    assert "claude-haiku-4-5" in out and "30%" in out


# --- commands -------------------------------------------------------------


def test_start_list_stop_roundtrip(db, capsys):
    main(
        [
            "experiment",
            "start",
            "tag:editor",
            "claude-haiku-4-5",
            "--id",
            "exp:t1",
            "--provider",
            "ollama",
        ]
    )
    started = capsys.readouterr().out
    assert "Started experiment exp:t1" in started
    assert "ollama" in started

    main(["experiment", "list"])
    listed = capsys.readouterr().out
    assert "exp:t1" in listed and "running" in listed
    assert "ollama" in listed

    main(["experiment", "stop", "exp:t1"])
    assert "Stopped experiment exp:t1" in capsys.readouterr().out

    main(["experiment", "list"])
    assert "stopped" in capsys.readouterr().out


def test_duplicate_running_use_case_is_rejected(db, capsys):
    main(
        [
            "experiment",
            "start",
            "tag:editor",
            "claude-haiku-4-5",
            "--id",
            "exp:a",
        ]
    )
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "experiment",
                "start",
                "tag:editor",
                "claude-3-5-haiku",
                "--id",
                "exp:b",
            ]
        )
    assert exit_info.value.code == 2
    assert "already exists" in capsys.readouterr().err


def test_stopping_frees_the_use_case_for_a_new_experiment(db, capsys):
    main(
        [
            "experiment",
            "start",
            "tag:editor",
            "claude-haiku-4-5",
            "--id",
            "exp:a",
        ]
    )
    main(["experiment", "stop", "exp:a"])
    capsys.readouterr()
    # a second running experiment for the same use-case is fine once the first
    # is stopped (the guard is on *running* experiments only)
    main(
        [
            "experiment",
            "start",
            "tag:editor",
            "claude-3-5-haiku",
            "--id",
            "exp:b",
        ]
    )
    assert "Started experiment exp:b" in capsys.readouterr().out


def test_bad_split_is_rejected_before_touching_the_store(db, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["experiment", "start", "tag:x", "m", "--split", "0"])
    assert exit_info.value.code == 2
    assert "split_pct" in capsys.readouterr().err


def test_unknown_candidate_provider_is_rejected(db, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "experiment",
                "start",
                "tag:x",
                "model",
                "--provider",
                "missing",
            ]
        )
    assert exit_info.value.code == 2
    assert "unknown provider" in capsys.readouterr().err


def test_stop_unknown_is_rejected(db, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["experiment", "stop", "exp:nope"])
    assert exit_info.value.code == 2
    assert "No running experiment" in capsys.readouterr().err
