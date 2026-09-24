"""Experiment analysis, calibration, replay, and campaign CLI commands."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, NoReturn

from ctrlrtn.eval.calibration import (
    ALIGNED,
    INSUFFICIENT,
    MISALIGNED,
    LabeledPairing,
)
from ctrlrtn.eval.judge import Pairing
from ctrlrtn.eval.tripwire import (
    GROSS_REGRESSION,
    INCONCLUSIVE,
    NO_DATA,
    NO_GROSS_REGRESSION,
    NOT_EXERCISED,
    UNDERPOWERED,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
StoreFactory = Callable[..., SqliteTraceStore]
LiveFunctionFactory = Callable[..., Callable[..., Any]]

_TRIPWIRE_EXIT = {
    NO_GROSS_REGRESSION: 0,
    GROSS_REGRESSION: 1,
    INCONCLUSIVE: 3,
    UNDERPOWERED: 3,
    NOT_EXERCISED: 4,
    NO_DATA: 4,
}
_CALIBRATION_EXIT = {ALIGNED: 0, MISALIGNED: 1, INSUFFICIENT: 3}


def _exit_failure(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _load_deblind_key(key_path: str, fail: Fail = _exit_failure) -> dict:
    """Load the sidecar ``{id: candidate_is_a}`` de-blind map."""
    try:
        with open(key_path, "r", encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except OSError:
        fail(
            f"cannot read de-blind key {key_path} (written beside the labels "
            "file by `calibration-set`); it is required to un-blind the scores."
        )
    keys: dict = {}
    for lineno, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise TypeError("line is not a JSON object")
            candidate_is_a = row["candidate_is_a"]
            if not isinstance(candidate_is_a, bool):
                raise TypeError("candidate_is_a must be true/false")
            keys[row["id"]] = candidate_is_a
        except (KeyError, TypeError, ValueError) as exc:
            fail(f"de-blind key {key_path} line {lineno}: {exc}")
    return keys


def _load_labels(path: str, fail: Fail = _exit_failure) -> list[LabeledPairing]:
    """Load human scores and safely de-blind their paired model outputs."""
    keys = _load_deblind_key(path + ".key", fail)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except OSError as exc:
        fail(f"cannot read labels file {path}: {exc}")
    labeled: list[LabeledPairing] = []
    for lineno, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise TypeError("line is not a JSON object")
            row_id = row["id"]
            output_a, output_b = row["output_a"], row["output_b"]
            score_a, score_b = float(row["score_a"]), float(row["score_b"])
            for score in (score_a, score_b):
                if not 0.0 <= score <= 10.0:
                    raise ValueError(f"score {score} out of 0-10")
            if row_id not in keys:
                raise KeyError(f"id {row_id!r} not in the de-blind key")
            candidate_is_a = keys[row_id]
            task = row.get("task", "")
        except (KeyError, TypeError, ValueError) as exc:
            fail(f"labels file {path} line {lineno}: {exc}")
        candidate_output, baseline_output = (
            (output_a, output_b) if candidate_is_a else (output_b, output_a)
        )
        candidate_score, baseline_score = (
            (score_a, score_b) if candidate_is_a else (score_b, score_a)
        )
        labeled.append(
            LabeledPairing(
                pairing=Pairing(
                    task=task,
                    baseline_output=baseline_output,
                    candidate_output=candidate_output,
                ),
                human_baseline=baseline_score,
                human_candidate=candidate_score,
            )
        )
    return labeled
