"""Argument registration for experiments and mutable routing controls."""

from __future__ import annotations

from ctrlrtn.eval.tripwire import (
    DEFAULT_GROSS_MARGIN,
    DEFAULT_MIN_TASKS_PER_ARM,
)
from ctrlrtn.policy.experiment import (
    DEFAULT_MAX_CALLS_PER_TASK,
)


def register(sub, controls, evaluation) -> None:
    experiment = sub.add_parser(
        "experiment",
        help="manage live A/B experiments (start / list / stop)",
    )
    exp_sub = experiment.add_subparsers(
        dest="experiment_command", required=True
    )
    exp_start = exp_sub.add_parser(
        "start", help="start a live A/B experiment for a use-case"
    )
    exp_start.add_argument(
        "use_case", help="use-case key to test (fp:... or tag:...)"
    )
    exp_start.add_argument(
        "candidate", help="candidate (cheaper) model to serve on the test arm"
    )
    exp_start.add_argument(
        "--provider",
        default=None,
        help="named provider for the candidate (default: baseline provider)",
    )
    exp_start.add_argument(
        "--split",
        type=int,
        default=50,
        help="candidate's share of this use-case's tasks, 1..99; default 50",
    )
    exp_start.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_MAX_CALLS_PER_TASK,
        dest="max_calls",
        help="per-task candidate call ceiling before a counted-failure "
        f"cut-off; default {DEFAULT_MAX_CALLS_PER_TASK}. Set it BELOW your "
        "client's own max-turns.",
    )
    exp_start.add_argument(
        "--id", default=None, help="experiment id (default: auto exp:<uuid>)"
    )
    exp_start.add_argument("--workflow", default=None)
    exp_start.add_argument("--workflow-version", default=None)
    exp_start.add_argument("--step", default=None)
    exp_start.set_defaults(func=controls._experiment_start)

    exp_list = exp_sub.add_parser("list", help="list experiments, newest first")
    exp_list.add_argument("--limit", type=int, default=50)
    exp_list.set_defaults(func=controls._experiment_list)
    exp_stop = exp_sub.add_parser(
        "stop", help="stop a running experiment (revert to baseline)"
    )
    exp_stop.add_argument("experiment_id", help="the experiment id to stop")
    exp_stop.set_defaults(func=controls._experiment_stop)
    exp_status = exp_sub.add_parser(
        "status",
        help="tripwire verdict for an experiment (gross-regression check "
        "with Manski bounds; exits 0 safe / 1 regression / 3 can't-conclude / "
        "4 no data)",
    )
    exp_status.add_argument("experiment_id", help="the experiment id")
    exp_status.add_argument(
        "--idle-minutes",
        type=float,
        default=45.0,
        dest="idle_minutes",
        help="a task with no new calls for this long is closed; default 45. "
        "Raise it above your longest inter-call gap so a long task isn't "
        "split or counted unreported early.",
    )
    exp_status.add_argument(
        "--gross-margin",
        type=float,
        default=DEFAULT_GROSS_MARGIN,
        dest="gross_margin",
        help="absolute failure-rate gap that counts as a gross regression, "
        f"0..1; default {DEFAULT_GROSS_MARGIN:g}. Absolute, so at a low base "
        "rate it still permits a large relative rise.",
    )
    exp_status.add_argument(
        "--min-tasks",
        type=int,
        default=DEFAULT_MIN_TASKS_PER_ARM,
        dest="min_tasks",
        help="min tasks with a REPORTED outcome per arm before concluding; "
        f"default (and floor) {DEFAULT_MIN_TASKS_PER_ARM}",
    )
    exp_status.add_argument(
        "--fail-below",
        type=float,
        default=None,
        dest="fail_below",
        help="treat a reported score below this as a failure (else only the "
        "success flag and ceiling terminals classify a task)",
    )
    exp_status.set_defaults(func=evaluation._experiment_status)

    shadow = sub.add_parser(
        "shadow", help="manage online side-by-side shadow experiments"
    )
    shadow_sub = shadow.add_subparsers(dest="shadow_command", required=True)
    shadow_start = shadow_sub.add_parser(
        "start", help="mirror live inputs to a candidate off the response path"
    )
    shadow_start.add_argument("use_case")
    shadow_start.add_argument("candidate")
    shadow_start.add_argument("--provider", default=None)
    shadow_start.add_argument(
        "--sample", type=int, default=10, help="percent mirrored, 1..100"
    )
    shadow_start.add_argument("--id", default=None)
    shadow_start.add_argument("--workflow", default=None)
    shadow_start.add_argument("--workflow-version", default=None)
    shadow_start.add_argument("--step", default=None)
    shadow_start.set_defaults(func=controls._shadow_start)
    shadow_list = shadow_sub.add_parser(
        "list", help="show shadows and attrition"
    )
    shadow_list.add_argument("--limit", type=int, default=50)
    shadow_list.set_defaults(func=controls._shadow_list)
    shadow_stop = shadow_sub.add_parser("stop", help="stop mirroring")
    shadow_stop.add_argument("shadow_id")
    shadow_stop.set_defaults(func=controls._shadow_stop)

    route = sub.add_parser(
        "route",
        help="persistent per-use-case model overrides — the switch after a "
        "verdict (set / list / clear / adopt)",
    )
    route_sub = route.add_subparsers(dest="route_cmd", required=True)
    route_set = route_sub.add_parser(
        "set",
        help="serve <model> for every call of <use-case> from now on "
        "(re-setting a route restarts its savings window)",
    )
    route_set.add_argument(
        "use_case", help="the use-case key (tag:... / fp:...)"
    )
    route_set.add_argument("model", help="the model to serve")
    route_set.add_argument(
        "--provider",
        default=None,
        help="named provider to serve the routed model on",
    )
    route_set.add_argument(
        "--previous",
        default=None,
        help="the model this replaces (else inferred from recorded traffic; "
        "used as the savings baseline)",
    )
    route_set.add_argument(
        "--note", default=None, help="why (e.g. 'replay-eval NON_INFERIOR')"
    )
    route_set.set_defaults(func=controls._route_set)
    route_list = route_sub.add_parser(
        "list",
        help="all routes with post-switch calls, spend and realized savings",
    )
    route_list.set_defaults(func=controls._route_list)
    route_clear = route_sub.add_parser(
        "clear", help="remove the override; the use-case passes through again"
    )
    route_clear.add_argument("use_case", help="the use-case key")
    route_clear.set_defaults(func=controls._route_clear)
    route_adopt = route_sub.add_parser(
        "adopt",
        help="act on a verdict: stop the experiment (if running) and route "
        "its use-case to the candidate it tested",
    )
    route_adopt.add_argument("experiment_id", help="the experiment id")
    route_adopt.set_defaults(func=controls._route_adopt)

    routing_config = sub.add_parser(
        "routing-config",
        help="validate and atomically activate Git-backed routes/experiments",
    )
    routing_sub = routing_config.add_subparsers(
        dest="routing_config_command", required=True
    )
    routing_validate = routing_sub.add_parser(
        "validate", help="validate a desired-state routing YAML file"
    )
    routing_validate.add_argument("path", nargs="?", default="routing.yaml")
    routing_validate.set_defaults(func=controls._routing_config_validate)
    routing_status = routing_sub.add_parser(
        "status", help="show the active revision and control-plane counts"
    )
    routing_status.set_defaults(func=controls._routing_config_status)
    routing_diff = routing_sub.add_parser(
        "diff", help="compare desired YAML with active SQLite state"
    )
    routing_diff.add_argument("path", nargs="?", default="routing.yaml")
    routing_diff.set_defaults(func=controls._routing_config_diff)
    routing_activate = routing_sub.add_parser(
        "activate",
        help="activate a clean, tracked routing YAML revision atomically",
    )
    routing_activate.add_argument("path", nargs="?", default="routing.yaml")
    routing_activate.add_argument(
        "--repo", default=".", help="Git repository containing the file"
    )
    routing_activate.set_defaults(func=controls._routing_config_activate)


def register_fallback(sub, controls) -> None:
    fallback = sub.add_parser(
        "fallback",
        help="manage evidence-approved budget fallbacks (approve / list / clear)",
    )
    fallback_sub = fallback.add_subparsers(dest="fallback_cmd", required=True)
    fallback_approve = fallback_sub.add_parser(
        "approve",
        help="approve the NON_INFERIOR candidate in a replay-eval JSON artifact",
    )
    fallback_approve.add_argument("evidence", help="replay-eval --json file")
    fallback_approve.add_argument(
        "--provider",
        default=None,
        help="named provider to serve the fallback model on",
    )
    fallback_approve.set_defaults(func=controls._fallback_approve)
    fallback_list = fallback_sub.add_parser(
        "list", help="list approved budget fallbacks and their evidence"
    )
    fallback_list.set_defaults(func=controls._fallback_list)
    fallback_clear = fallback_sub.add_parser(
        "clear", help="remove an approved fallback"
    )
    fallback_clear.add_argument("use_case", help="the use-case key")
    fallback_clear.set_defaults(func=controls._fallback_clear)
