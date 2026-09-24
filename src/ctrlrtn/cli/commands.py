"""CLI composition root and stable compatibility exports."""

from __future__ import annotations

import json
import sys
from typing import NoReturn

from ctrlrtn.cli.control import ControlCommands
from ctrlrtn.cli.dataset import DatasetCommands
from ctrlrtn.cli.evaluation import EvaluationCommands
from ctrlrtn.cli.operations import OperationsCommands
from ctrlrtn.cli.parser import build_parser
from ctrlrtn.cli.reporting import ReportingCommands
from ctrlrtn.cli.runtime import (
    RuntimeCommands,
    build_app,
    configure_logging,
)
from ctrlrtn.cli.workflow import WorkflowCommands
from ctrlrtn.config import ConfigError, load_settings
from ctrlrtn.control_config import ControlConfigError
from ctrlrtn.eval.dataset_manifest import DatasetManifestError
from ctrlrtn.eval.live import anthropic_judge_fn, anthropic_replay_fn
from ctrlrtn.jobs.replay import run_replay_job
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.workflow.discovery_job import run_workflow_discovery_job


def _db_path() -> str:
    """Resolve the recorder path through the gateway's layered settings."""
    return load_settings().db_path


def _fail(message: str) -> NoReturn:
    """Report an operator error without conflating it with a verdict exit."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _read_json_artifact(path: str, label: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        _fail(f"could not read {label}: {exc}")


def _write_json_artifact(path: str, document: dict, label: str) -> None:
    try:
        with open(path, "x", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, indent=2)
            handle.write("\n")
    except FileExistsError:
        _fail(f"{label} already exists: {path}")
    except OSError as exc:
        _fail(f"could not write {label}: {exc}")


def _configure_logging(level: str) -> int:
    """Compatibility shim for integrations importing the former root helper."""
    return configure_logging(level, _fail)


def _console(args) -> None:
    """Compatibility shim for direct callers of the former root handler."""
    RuntimeCommands(
        load_settings, _fail, build_app, _configure_logging
    )._console(args)


def main(argv: list[str] | None = None) -> None:
    """Compose command families, parse arguments, and normalize domain errors."""
    controls = ControlCommands(_db_path, _fail)
    evaluation = EvaluationCommands(
        _db_path,
        _fail,
        SqliteTraceStore,
        anthropic_replay_fn,
        anthropic_judge_fn,
    )
    reporting = ReportingCommands(_db_path, load_settings)
    runtime = RuntimeCommands(
        load_settings, _fail, build_app, _configure_logging
    )
    operations = OperationsCommands(
        _db_path,
        _fail,
        _read_json_artifact,
        SqliteTraceStore,
        run_replay_job,
        run_workflow_discovery_job,
    )
    datasets = DatasetCommands(_db_path, _fail, _write_json_artifact)
    workflows = WorkflowCommands(_db_path, _fail, _read_json_artifact)
    parser = build_parser(
        controls,
        workflows,
        datasets,
        reporting,
        evaluation,
        runtime,
        operations,
    )
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (
        ConfigError,
        ControlConfigError,
        DatasetManifestError,
    ) as exc:
        _fail(str(exc))


if __name__ == "__main__":
    main()
