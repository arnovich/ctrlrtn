"""Experiment analysis, calibration, replay, and campaign CLI commands."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections.abc import Callable
from typing import Any, NoReturn

import httpx

from ctrlrtn.cli.render import (
    render_calibration,
)
from ctrlrtn.eval.calibration import (
    ALIGNED,
    INSUFFICIENT,
    MISALIGNED,
    calibrate,
)
from ctrlrtn.eval.live import JUDGE_TIMEOUT, REPLAY_TIMEOUT
from ctrlrtn.eval.replay import (
    extract_input_summary,
)
from ctrlrtn.eval.tripwire import (
    GROSS_REGRESSION,
    INCONCLUSIVE,
    NO_DATA,
    NO_GROSS_REGRESSION,
    NOT_EXERCISED,
    UNDERPOWERED,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

from .loading import _load_labels

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


class CalibrationCommands:
    """Build blinded calibration sets and evaluate human-labelled pairs."""

    def _calibrate(self, args: argparse.Namespace) -> None:
        if args.replicates < 2 or args.replicates % 2 != 0:
            self._fail("--replicates must be an even number >= 2.")
        if args.margin is not None and args.margin <= 0:
            self._fail("--margin must be > 0.")
        labeled = _load_labels(args.labels_file, self._fail)
        if not labeled:
            self._fail(f"No labelled pairings in {args.labels_file}.")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self._fail("Set ANTHROPIC_API_KEY to run the judge.")
        print(
            f"Judging {len(labeled)} labelled pairs "
            f"({len(labeled) * args.replicates} calls with {args.judge_model})."
        )
        with httpx.Client(timeout=JUDGE_TIMEOUT) as judge_http:
            report = calibrate(
                self._judge_function_factory(
                    api_key, client=judge_http, model=args.judge_model
                ),
                labeled,
                replicates=args.replicates,
                margin=args.margin,
            )
        print(render_calibration(report))
        raise SystemExit(_CALIBRATION_EXIT[report.verdict])

    def _calibration_set(self, args: argparse.Namespace) -> None:
        if args.n < 1:
            self._fail("--n must be >= 1.")
        if args.max_tokens is not None and args.max_tokens < 1:
            self._fail("--max-tokens must be >= 1.")
        store = self._store_factory(self._database_path())
        try:
            rows = store.requests_for_use_case(args.use_case, args.n)
            if not rows:
                self._fail(
                    "No replayable (successful) recorded calls for use-case "
                    f"{args.use_case} — failed calls are not replay inputs. "
                    "Run `ctrlrtn usecases` to list keys."
                )
            baseline = args.baseline or store.use_case_models().get(
                args.use_case
            )
            if not baseline:
                self._fail(
                    "Could not infer the baseline model; pass --baseline."
                )
            if baseline == args.candidate:
                self._fail(
                    f"Baseline and candidate are both {baseline}; nothing to do."
                )
        finally:
            store.close()
        count = len(rows)
        key_path = args.out + ".key"
        print(
            f"Plan: replay {count} recorded inputs on 2 arms "
            f"({count * 2} calls billed to your key), write blinded pairs to "
            f"{args.out} and the de-blind key to {key_path}."
        )
        if not args.yes:
            print("Dry run. Re-run with --yes to spend.")
            return
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self._fail("Set ANTHROPIC_API_KEY to replay.")
        rng = random.Random(args.seed)
        written = failed = blank = 0
        with (
            httpx.Client(timeout=REPLAY_TIMEOUT) as replay_http,
            open(args.out, "w", encoding="utf-8") as output_file,
            open(key_path, "w", encoding="utf-8") as key_file,
        ):
            replay = self._replay_function_factory(
                api_key, client=replay_http, max_tokens=args.max_tokens
            )
            for row in rows:
                body = row["request_body"]
                try:
                    baseline_output = replay(body, baseline)
                    candidate_output = replay(body, args.candidate)
                except Exception as exc:  # noqa: BLE001 - isolate one input
                    failed += 1
                    print(
                        f"  ! replay failed on one input: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if not (baseline_output.strip() or candidate_output.strip()):
                    blank += 1
                    continue
                candidate_is_a = rng.random() < 0.5
                output_a, output_b = (
                    (candidate_output, baseline_output)
                    if candidate_is_a
                    else (baseline_output, candidate_output)
                )
                output_file.write(
                    json.dumps(
                        {
                            "id": written,
                            "task": extract_input_summary(body),
                            "output_a": output_a,
                            "output_b": output_b,
                        }
                    )
                    + "\n"
                )
                key_file.write(
                    json.dumps(
                        {"id": written, "candidate_is_a": candidate_is_a}
                    )
                    + "\n"
                )
                written += 1
        print(
            f"Wrote {written} blinded pairs to {args.out} (de-blind key "
            f"{key_path}); {failed} failed, {blank} blank skipped."
        )
        if not written:
            self._fail(
                "No usable pairs were written (every input failed or was blank)."
            )
        print(
            "Score each response A and B from 0-10: add `score_a`/`score_b` "
            f"to every line in {args.out} (do NOT open or edit {key_path}), "
            f"then run `ctrlrtn calibrate {args.out}`."
        )
