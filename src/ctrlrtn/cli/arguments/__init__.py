"""Composable argument-parser assembly by CLI command family."""

from __future__ import annotations

import argparse

from ctrlrtn import __version__
from ctrlrtn.cli.arguments import control
from ctrlrtn.cli.arguments import dataset as dataset_arguments
from ctrlrtn.cli.arguments import evaluation as evaluation_arguments
from ctrlrtn.cli.arguments import reporting as reporting_arguments
from ctrlrtn.cli.arguments import runtime as runtime_arguments
from ctrlrtn.cli.arguments import workflow as workflow_arguments


def build_parser(
    controls,
    workflows,
    datasets,
    reporting,
    evaluation,
    runtime,
    operations,
) -> argparse.ArgumentParser:
    """Build the public CLI without coupling registrars to the composition root."""
    parser = argparse.ArgumentParser(prog="ctrlrtn")
    parser.add_argument(
        "--version", action="version", version=f"ctrlrtn {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Preserve the established top-level help order while ownership remains
    # organized by command family.
    runtime_arguments.register_processes(sub, runtime, operations)
    runtime_arguments.register_maintenance(sub, operations)
    dataset_arguments.register_dataset(sub, datasets)
    reporting_arguments.register(sub, reporting)
    evaluation_arguments.register_analysis(sub, evaluation)
    workflow_arguments.register(sub, workflows)
    control.register(sub, controls, evaluation)
    control.register_fallback(sub, controls)
    evaluation_arguments.register_evaluations(sub, evaluation)
    return parser
