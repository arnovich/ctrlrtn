"""Argument registration for analysis, replay, and calibration."""

from __future__ import annotations

from ctrlrtn.eval.live import DEFAULT_JUDGE_MODEL


def register_analysis(sub, commands) -> None:
    recommendations = sub.add_parser(
        "recommendations",
        help="suggest where to optimize (spend, caching, cheaper models)",
    )
    recommendations.set_defaults(func=commands._recommendations)

    propagation = sub.add_parser(
        "propagation",
        help="check x-ctrlrtn-task propagation across a task's sub-agent calls "
        "(gate for task-level analysis; exits non-zero unless propagating)",
    )
    propagation.add_argument(
        "--use-case",
        default=None,
        dest="use_case",
        help="focus on one use-case key, e.g. the editor (run `usecases` to "
        "list keys)",
    )
    propagation.add_argument(
        "--window",
        type=int,
        default=2000,
        help="examine the most recent N completion calls; default 2000",
    )
    propagation.set_defaults(func=commands._propagation)


def register_evaluations(sub, commands) -> None:
    replay_eval = sub.add_parser(
        "replay-eval",
        help="shadow-replay a candidate model for a use-case and run a "
        "non-inferiority test (needs ANTHROPIC_API_KEY)",
    )
    replay_eval.add_argument(
        "use_case", help="the use-case key (fp:... or tag)"
    )
    replay_eval.add_argument("candidate", help="the cheaper candidate model")
    replay_eval.add_argument(
        "--baseline", default=None, help="incumbent model (else inferred)"
    )
    replay_eval.add_argument("--workflow", default=None)
    replay_eval.add_argument("--workflow-version", default=None)
    replay_eval.add_argument("--step", default=None)
    replay_eval.add_argument(
        "--margin",
        type=float,
        default=1.0,
        help="non-inferiority margin in judge points (0-10 scale); default 1.0",
    )
    replay_eval.add_argument(
        "--limit",
        type=int,
        default=None,
        help="how many recent recorded inputs to replay; default 50",
    )
    replay_eval.add_argument(
        "--dataset-manifest",
        default=None,
        help="evaluate only the verified evaluation split in this lineage manifest",
    )
    replay_eval.add_argument(
        "--replicates",
        type=int,
        default=2,
        help="judge calls per pairing (even, >=2) to average out position bias; "
        "default 2",
    )
    replay_eval.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        dest="max_tokens",
        help="clamp the candidate's max_tokens to its ceiling (a smaller "
        "candidate can 400 on the baseline's recorded cap)",
    )
    replay_eval.add_argument(
        "--judge-model", default=DEFAULT_JUDGE_MODEL, dest="judge_model"
    )
    replay_eval.add_argument(
        "--yes",
        action="store_true",
        help="actually spend; without it, prints the call plan and stops",
    )
    replay_eval.add_argument(
        "--background",
        action="store_true",
        help="queue the paid eval for `ctrlrtn worker` and return",
    )
    replay_eval.add_argument(
        "--json",
        default=None,
        dest="json_out",
        help="also write the verdict as machine-readable JSON to this path "
        "(the input to `campaign-report`)",
    )
    replay_eval.set_defaults(func=commands._replay_eval)

    campaign_report = sub.add_parser(
        "campaign-report",
        help="fold recorded spend, replay verdicts and live experiments into "
        "a README-ready table + SVG chart",
    )
    campaign_report.add_argument(
        "--replay-json",
        nargs="*",
        default=[],
        dest="replay_json",
        help="JSON files written by `replay-eval --json`, one per use-case",
    )
    campaign_report.add_argument(
        "--only",
        nargs="*",
        default=[],
        help="limit the report to these use-cases (e.g. exclude legacy fp: "
        "keys); totals are then scoped to the listed roles",
    )
    campaign_report.add_argument(
        "--md", default=None, help="write the markdown table here (else stdout)"
    )
    campaign_report.add_argument(
        "--svg", default=None, help="write the SVG bar chart here"
    )
    campaign_report.set_defaults(func=commands._campaign_report)

    calibration_set = sub.add_parser(
        "calibration-set",
        help="replay a use-case's inputs on both arms and write blinded pairs "
        "for a human to score (needs ANTHROPIC_API_KEY)",
    )
    calibration_set.add_argument("use_case", help="the use-case key")
    calibration_set.add_argument("candidate", help="the candidate model")
    calibration_set.add_argument(
        "--out", required=True, help="output JSONL path for the blinded pairs"
    )
    calibration_set.add_argument(
        "--n",
        type=int,
        default=40,
        help="inputs to replay; default 40 (near-ties don't count toward the "
        "directional floor, so allow headroom)",
    )
    calibration_set.add_argument(
        "--baseline", default=None, help="incumbent model (else inferred)"
    )
    calibration_set.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        dest="max_tokens",
        help="clamp the candidate's max_tokens to its ceiling",
    )
    calibration_set.add_argument(
        "--seed",
        type=int,
        default=0,
        help="A/B position randomization seed; default 0",
    )
    calibration_set.add_argument(
        "--yes",
        action="store_true",
        help="actually spend; without it, prints the plan and stops",
    )
    calibration_set.set_defaults(func=commands._calibration_set)

    calibrate = sub.add_parser(
        "calibrate",
        help="check a judge against human-labelled pairs before trusting its "
        "replay-eval verdict (needs ANTHROPIC_API_KEY)",
    )
    calibrate.add_argument(
        "labels_file",
        help="a JSONL from `calibration-set` with human score_a/score_b added",
    )
    calibrate.add_argument(
        "--margin",
        type=float,
        default=None,
        help="NI margin in judge points; sets the bias ceiling "
        "(default a 1.0-point ceiling)",
    )
    calibrate.add_argument(
        "--replicates",
        type=int,
        default=2,
        help="judge calls per pairing (even, >=2); default 2",
    )
    calibrate.add_argument(
        "--judge-model", default=DEFAULT_JUDGE_MODEL, dest="judge_model"
    )
    calibrate.set_defaults(func=commands._calibrate)
