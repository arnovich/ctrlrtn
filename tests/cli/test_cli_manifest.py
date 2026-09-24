"""Compatibility manifest for every public CLI command path."""

from __future__ import annotations

import argparse

import pytest

from ctrlrtn.cli import commands

PUBLIC_COMMANDS = [
    "serve",
    "console",
    "worker",
    "jobs list",
    "jobs cancel",
    "jobs export",
    "scrub-credentials",
    "prune",
    "dataset create",
    "dataset verify",
    "usecases",
    "spend",
    "budget",
    "calls",
    "tasks",
    "sessions",
    "show",
    "reenrich",
    "recommendations",
    "propagation",
    "workflow infer",
    "workflow inferred",
    "workflow discover",
    "workflow discovery-compare",
    "workflow discovery-project",
    "workflow identify",
    "workflow identify-verify",
    "workflow erase-task",
    "workflow diagnostics",
    "workflow steps",
    "workflow diagram",
    "workflow flow",
    "workflow catalog",
    "workflow compare",
    "workflow step-detail",
    "workflow recommendations",
    "experiment start",
    "experiment list",
    "experiment stop",
    "experiment status",
    "shadow start",
    "shadow list",
    "shadow stop",
    "route set",
    "route list",
    "route clear",
    "route adopt",
    "routing-config validate",
    "routing-config status",
    "routing-config diff",
    "routing-config activate",
    "fallback approve",
    "fallback list",
    "fallback clear",
    "replay-eval",
    "campaign-report",
    "calibration-set",
    "calibrate",
]


def _public_parser(monkeypatch, capsys) -> argparse.ArgumentParser:
    captured: list[argparse.ArgumentParser] = []
    real_build_parser = commands.build_parser

    def capture(*args, **kwargs):
        parser = real_build_parser(*args, **kwargs)
        captured.append(parser)
        return parser

    monkeypatch.setattr(commands, "build_parser", capture)
    with pytest.raises(SystemExit, match="0"):
        commands.main(["--help"])
    capsys.readouterr()
    return captured[0]


def _leaves(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> list[str]:
    action = next(
        (
            candidate
            for candidate in parser._actions
            if isinstance(candidate, argparse._SubParsersAction)
        ),
        None,
    )
    if action is None:
        return [" ".join(prefix)]
    paths: list[str] = []
    seen: set[int] = set()
    for name, child in action.choices.items():
        if id(child) in seen:  # argparse aliases point at the same parser
            continue
        seen.add(id(child))
        paths.extend(_leaves(child, (*prefix, name)))
    return paths


@pytest.mark.contract
def test_public_command_manifest_is_stable(monkeypatch, capsys):
    assert _leaves(_public_parser(monkeypatch, capsys)) == PUBLIC_COMMANDS


@pytest.mark.contract
def test_every_public_command_has_working_help(monkeypatch, capsys):
    parser = _public_parser(monkeypatch, capsys)
    for command in PUBLIC_COMMANDS:
        with pytest.raises(SystemExit, match="0"):
            parser.parse_args([*command.split(), "--help"])
        output = capsys.readouterr().out
        assert output.startswith("usage: ctrlrtn ")
