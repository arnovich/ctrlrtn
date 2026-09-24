"""Human-readable renderers shared beyond the CLI.

The tripwire verdict is rendered both by ``ctrlrtn experiment status`` and
by the read-only console monitor, so it lives here in a neutral module rather
than in ``cli/commands.py`` — that keeps the console from importing the CLI
(with its uvicorn/argparse weight) just to format a report.
"""

from __future__ import annotations

import json
import time

from ctrlrtn.eval.tripwire import (
    GROSS_REGRESSION,
    INCONCLUSIVE,
    NO_DATA,
    NO_GROSS_REGRESSION,
    NOT_EXERCISED,
    UNDERPOWERED,
    TripwireReport,
)
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.telemetry.usage import decode_body

# Header values to mask when rendering a trace (the user's own key flows
# through the gateway).
_SECRET_HEADERS = {"authorization", "x-api-key", "api-key"}


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def _fmt_cost(value) -> str:
    return "-" if value is None else f"${value:.4f}"


def _fmt_outcome(success, score) -> str:
    """Compact app-reported outcome: "ok 0.90", "fail", "0.90", or "-"."""
    parts = []
    if success is True:
        parts.append("ok")
    elif success is False:
        parts.append("fail")
    if score is not None:
        parts.append(f"{score:.2f}")
    return " ".join(parts) if parts else "-"


def _fmt_pct_band(low: float, high: float) -> str:
    return f"{low:.0%}-{high:.0%}"


_TRIPWIRE_GLOSS = {
    NO_GROSS_REGRESSION: (
        "No gross regression: the candidate is within the margin even under the "
        "worst case for the sampling interval and the imputation of unreported "
        "tasks. This is a coarse tripwire, not a certification, and the interval "
        "is fixed-n (repeated peeking is not fully corrected) — for a precise "
        "margin run `replay-eval`."
    ),
    GROSS_REGRESSION: (
        "Gross regression: the candidate is materially worse even in its best "
        "case. Stop the experiment (`experiment stop`) and investigate (check "
        "the ceiling-share warning first — a too-low --max-calls can fake this)."
    ),
    INCONCLUSIVE: (
        "Inconclusive: the failure-rate bounds straddle the margin — too few, "
        "too noisy, or too many unreported tasks. Report outcomes (POST "
        "/ctrlrtn/outcome) or gather more closed tasks."
    ),
    UNDERPOWERED: (
        "Underpowered: not enough tasks with a reported outcome per arm yet. "
        "Keep it running, and make sure the app POSTs /ctrlrtn/outcome."
    ),
    NOT_EXERCISED: (
        "Not exercised: the candidate arm isn't actually being swapped (no real "
        "swap, or no candidate tasks). Check the split and that x-ctrlrtn-task "
        "propagates to this use-case — you are NOT testing the candidate."
    ),
    NO_DATA: (
        "No data: no tasks recorded for this experiment. If traffic is flowing, "
        "x-ctrlrtn-task isn't propagating to this use-case or the use-case isn't "
        "being hit — run `ctrlrtn propagation`. This is not a wait state."
    ),
}


def render_tripwire(report: TripwireReport, exp: Experiment) -> str:
    lines = [
        f"experiment {exp.experiment_id}  [{exp.status}]",
        f"  use-case:  {exp.use_case_key}",
        f"  scope:     {exp.scope.label}",
        f"  candidate: {exp.candidate_model}  (split {exp.split_pct}%)",
        "",
        f"VERDICT: {report.verdict}",
        f"  (gross margin = {report.gross_margin:.0%} failure-rate gap; "
        f"min {report.min_tasks_per_arm} reported tasks/arm; "
        f"{report.confidence:.0%} interval)",
        # Progress toward the power gate: it counts REPORTED (ok+fail) tasks,
        # not the 'tasks' (total) column — so make that count explicit per arm.
        f"  power:  baseline {report.baseline.reported}/"
        f"{report.min_tasks_per_arm}  candidate "
        f"{report.candidate.reported}/{report.min_tasks_per_arm}  "
        "reported tasks/arm",
        "",
    ]
    if exp.scope.is_step_scoped:
        lines.extend(
            [
                "EVIDENCE SCOPE: stable step only; not a whole-workflow verdict.",
                "",
            ]
        )
    elif exp.scope.is_workflow_scoped:
        lines.extend(
            [
                "EVIDENCE SCOPE: complete task outcomes for exact workflow version.",
                "Assignment is task-stable across every matching workflow step.",
                "",
            ]
        )
    header = (
        f"{'arm':<10} {'tasks':>6} {'ok':>5} {'fail':>5} {'(ceil)':>7} "
        f"{'unrep':>6} {'open':>5} {'calls':>6} {'$/task':>9}"
    )
    lines += [header, "-" * len(header)]
    for arm in (report.baseline, report.candidate):
        lines.append(
            f"{arm.arm:<10} {arm.total:>6} {arm.success:>5} {arm.failure:>5} "
            f"{arm.ceiling:>7} {arm.unreported:>6} {arm.open:>5} "
            f"{arm.median_calls:>6.1f} {_fmt_cost(arm.cost_per_task):>9}"
        )
    if exp.scope.is_workflow_scoped and not exp.scope.is_step_scoped:
        lines.extend(["", "observed exact-version paths:"])
        for arm in (report.baseline, report.candidate):
            if not arm.paths:
                lines.append(f"  {arm.arm:<10} (no lifecycle path evidence)")
                continue
            for digest, count, path in arm.paths:
                lines.append(
                    f"  {arm.arm:<10} {count:>4}  {digest}  "
                    f"{' -> '.join(path)}"
                )
    lines.append("")
    lines.append(
        "candidate-minus-baseline failure rate ∈ "
        f"[{report.diff_low:+.0%}, {report.diff_high:+.0%}]  "
        f"(gross if low > {report.gross_margin:.0%}; "
        f"safe if high ≤ {report.gross_margin:.0%})"
    )
    savings = report.savings_pct
    if savings is not None:
        lines.append(
            f"candidate cost/task {_fmt_cost(report.candidate.cost_per_task)} "
            f"vs baseline {_fmt_cost(report.baseline.cost_per_task)} "
            f"— {savings:+.0%} (positive = saving)"
        )
    if report.contaminated:
        lines.append(
            "excluded (contaminated: mixed arm, experiment, or workflow "
            "identity): "
            f"{report.contaminated} task(s)"
        )
    if report.warnings:
        lines.append("")
        lines.append("warnings:")
        lines += [f"  ! {w}" for w in report.warnings]
    lines.append("")
    lines.append(_TRIPWIRE_GLOSS[report.verdict])
    if report.verdict == NO_GROSS_REGRESSION:
        # The verdict's actionable next step, right where the verdict is read.
        lines.append("")
        lines.append(
            f"recommendation: adopt — `ctrlrtn route adopt "
            f"{exp.experiment_id}` stops this experiment and serves "
            f"{exp.candidate_model} for ALL {exp.use_case_key} traffic "
            "(watch the realized saving in `route list`)."
        )
    return "\n".join(lines)


def _body(raw: bytes | None) -> str:
    if not raw:
        return "(empty)"
    try:
        return json.dumps(json.loads(raw), indent=2)
    except Exception:
        return raw.decode("utf-8", "replace")


def _mask(key: str, value: str) -> str:
    if key.lower() in _SECRET_HEADERS and len(value) > 10:
        return f"{value[:6]}…{value[-2:]}"
    return value


def render_trace(t: dict) -> str:
    """One call in full detail (headers masked, bodies pretty-printed) — the
    `show` command and the console's call detail render the same way."""
    query = f"?{t['query']}" if t["query"] else ""
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t["ts"]))
    lines = [
        f"trace #{t['id']}  {t['method']} {t['path']}{query}",
        f"  time     = {when}",
        f"  use_case = {_fmt(t['use_case_key'])}",
        f"  provider = {_fmt(t.get('provider'))}",
        f"  task     = {_fmt(t['task_id'])}   "
        f"session = {_fmt(t.get('session_id'))}",
        f"  workflow = {_fmt(t.get('workflow'))}@"
        f"{_fmt(t.get('workflow_version'))}   step = {_fmt(t.get('step'))}",
        f"  step_run = {_fmt(t.get('step_run_id'))}   "
        f"attempt = {_fmt(t.get('step_attempt'))}   "
        f"parent = {_fmt(t.get('parent_step_run_id'))}",
        f"  dependencies = "
        f"{', '.join(t.get('dependency_step_run_ids') or ()) or '-'}",
        f"  served_model = {_fmt(t['served_model'])}",
        f"  model    = {_fmt(t['model'])}   status = {t['status_code']}   "
        f"latency = {t['latency_ms']:.0f}ms",
        f"  tokens in/out = {_fmt(t['input_tokens'])}/"
        f"{_fmt(t['output_tokens'])}   "
        f"cache r/w = {_fmt(t['cache_read_tokens'])}/"
        f"{_fmt(t['cache_write_tokens'])}   "
        f"cost = {_fmt_cost(t['cost_usd'])}",
        "── request headers ──",
        *[f"  {k}: {_mask(k, v)}" for k, v in t["request_headers"].items()],
        "── request body ──",
        _body(t["request_body"]),
        "── response headers ──",
        *[f"  {k}: {v}" for k, v in t["response_headers"].items()],
        "── response body ──",
        _body(
            decode_body(
                t["response_body"],
                t["response_headers"].get("content-encoding"),
            )
        ),
    ]
    if t.get("workflow_identity_error"):
        lines.insert(
            8, f"  workflow identity INVALID: {t['workflow_identity_error']}"
        )
    if t.get("route_rule_scope"):
        lines.insert(
            9,
            f"  route    = {t['route_rule_scope']}:{t.get('route_rule_key')}   "
            f"revision = {_fmt(t.get('control_revision'))}",
        )
    return "\n".join(lines)
