"""Pure terminal rendering for CLI results."""

from __future__ import annotations

import time

from ctrlrtn.analysis.propagation import (
    MIN_MULTI_AGENT_TASKS,
    MIN_TAGGED_FRACTION,
    PropagationReport,
)
from ctrlrtn.analysis.recommend import Recommendation
from ctrlrtn.analysis.report import _fmt, _fmt_cost, _fmt_outcome
from ctrlrtn.eval.calibration import ALIGNED, INSUFFICIENT, MISALIGNED
from ctrlrtn.eval.replay import ReplayReport
from ctrlrtn.policy.budget import BudgetPolicy
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.fallback import ApprovedFallback
from ctrlrtn.recorder.models import (
    UNSESSIONED,
    UNTASKED,
    SessionSummary,
    TaskSummary,
    UseCaseRanking,
)


def _safe(text: str) -> str:
    """Drop control/non-printable chars before rendering to the terminal.
    Task ids come from a client-set header, so they're untrusted input."""
    return "".join(c for c in text if c.isprintable())


def render_rankings(rows: list[UseCaseRanking]) -> str:
    if not rows:
        return "No traffic recorded yet."
    header = (
        f"{'use-case':<24} {'calls':>7} {'in_tok':>10} "
        f"{'out_tok':>10} {'avg_ms':>8} {'cost':>10}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.use_case[:24]:<24} {row.calls:>7} "
            f"{row.input_tokens:>10} {row.output_tokens:>10} "
            f"{row.avg_latency_ms:>8.0f} {_fmt_cost(row.cost_usd):>10}"
        )
    return "\n".join(lines)


def render_calls(rows: list[dict]) -> str:
    if not rows:
        return "No calls recorded yet."
    header = (
        f"{'id':>5}  {'use-case':<24} {'provider':<12} {'model':<22} "
        f"{'st':>3} {'ms':>6} {'in':>7} {'out':>7} {'cost':>9}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r['id']:>5}  {(r['use_case_key'] or '(unkeyed)')[:24]:<24} "
            f"{(r.get('provider') or '-')[:12]:<12} "
            f"{(r['model'] or '-')[:22]:<22} {r['status_code']:>3} "
            f"{r['latency_ms']:>6.0f} {_fmt(r['input_tokens']):>7} "
            f"{_fmt(r['output_tokens']):>7} {_fmt_cost(r['cost_usd']):>9}"
        )
    return "\n".join(lines)


def render_tasks(rows: list[TaskSummary], limit: int = 50) -> str:
    if not rows:
        return "No tasks recorded yet."
    if len(rows) == 1 and rows[0].task_id == UNTASKED:
        return (
            f"All {rows[0].calls} calls are untagged — set the "
            "`x-ctrlrtn-task` header (one value per app/agent run) to group "
            "calls into tasks. See docs/instrument-a-workflow.md."
        )
    header = (
        f"{'task':<28} {'calls':>6} {'use-cases':>10} {'errors':>7} "
        f"{'in_tok':>10} {'out_tok':>10} {'cost':>10} {'outcome':<10}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{_safe(r.task_id)[:28]:<28} {r.calls:>6} {r.use_cases:>10} "
            f"{r.errors:>7} {r.input_tokens:>10} {r.output_tokens:>10} "
            f"{_fmt_cost(r.cost_usd):>10} {_fmt_outcome(r.success, r.score):<10}"
        )
    if len(rows) == limit:
        lines.append(
            f"Showing top {limit} tasks by cost; pass --limit to see more."
        )
    return "\n".join(lines)


def render_sessions(
    rows: list[SessionSummary],
    limit: int = 50,
    *,
    session_limit_usd: float | None = None,
) -> str:
    if not rows:
        return "No sessions recorded yet."
    if len(rows) == 1 and rows[0].session_id == UNSESSIONED:
        return (
            f"All {rows[0].calls} calls are untagged — set the "
            "`x-ctrlrtn-session` header to attribute spend to an "
            "operator-defined session."
        )
    remaining_header = (
        f" {'remaining':>10}" if session_limit_usd is not None else ""
    )
    header = (
        f"{'session':<28} {'calls':>6} {'use-cases':>10} {'errors':>7} "
        f"{'unknown':>7} {'in_tok':>10} {'out_tok':>10} {'known cost':>10}"
        f"{remaining_header}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        remaining = ""
        if session_limit_usd is not None:
            value = (
                "N/A"
                if row.unknown_cost_calls
                else _fmt_cost(max(session_limit_usd - row.cost_usd, 0.0))
            )
            remaining = f" {value:>10}"
        lines.append(
            f"{_safe(row.session_id)[:28]:<28} {row.calls:>6} "
            f"{row.use_cases:>10} {row.errors:>7} "
            f"{row.unknown_cost_calls:>7} {row.input_tokens:>10} "
            f"{row.output_tokens:>10} {_fmt_cost(row.cost_usd):>10}"
            f"{remaining}"
        )
    if len(rows) == limit:
        lines.append(
            f"Showing top {limit} sessions by cost; pass --limit to see more."
        )
    return "\n".join(lines)


def render_budget_status(
    policy: BudgetPolicy,
    *,
    kill_switch: bool,
    daily_total: float,
    daily_by_use_case: dict[str, float],
    unknown_priced_calls: int,
    blocked: dict[str, int],
    fallback_calls: int,
    compact: bool = False,
) -> str:
    """Render current policy beside today's persisted accounting."""

    def ceiling(spent: float, limit: float) -> str:
        remaining = max(limit - spent, 0.0)
        return (
            f"{_fmt_cost(spent)} / {_fmt_cost(limit)} "
            f"({_fmt_cost(remaining)} remaining)"
        )

    reasons = (
        "budget",
        "unpriced_model",
        "session_required",
        "unknown_session_cost",
        "unreservable_request",
        "fallback_unavailable",
    )
    if compact:
        global_daily = (
            "not configured"
            if policy.global_daily_usd is None
            else ceiling(daily_total, policy.global_daily_usd)
        )
        session = (
            "not configured"
            if policy.session_limit_usd is None
            else _fmt_cost(policy.session_limit_usd)
        )
        blocked_count = sum(blocked.get(reason, 0) for reason in reasons)
        return (
            f"Kill switch: {'ON' if kill_switch else 'off'} · "
            f"Global daily: {global_daily}\n"
            f"Use-case limits: {len(policy.use_case_daily_usd)} · "
            f"Session limit: {session} · "
            f"Reservations: {'on' if policy.reserve_in_flight else 'off'}\n"
            f"Today: {unknown_priced_calls} unknown-priced · "
            f"{blocked_count} blocked · {fallback_calls} fallbacks · b details"
        )

    lines = [f"Kill switch: {'ON' if kill_switch else 'off'}"]
    if policy.global_daily_usd is None:
        lines.append("Global daily: not configured")
    else:
        lines.append(
            "Global daily: " + ceiling(daily_total, policy.global_daily_usd)
        )

    if policy.use_case_daily_usd:
        lines.append("Use-case daily:")
        for use_case, limit in sorted(policy.use_case_daily_usd.items()):
            line = f"  {_safe(use_case)}: " + ceiling(
                daily_by_use_case.get(use_case, 0.0), limit
            )
            fallback_at = policy.use_case_fallback_usd.get(use_case)
            if fallback_at is not None:
                line += f"; fallback at {_fmt_cost(fallback_at)}"
            lines.append(line)
    else:
        lines.append("Use-case daily: not configured")

    if policy.session_limit_usd is None:
        lines.append("Session limit: not configured")
    else:
        lines.append(
            f"Session limit: {_fmt_cost(policy.session_limit_usd)} "
            "per x-ctrlrtn-session"
        )

    lines.append(
        "In-flight reservations: "
        + ("on" if policy.reserve_in_flight else "off")
    )

    lines.append(f"Unknown-priced calls today: {unknown_priced_calls}")
    lines.append(f"Evidence-approved fallbacks today: {fallback_calls}")
    lines.append(
        "Blocked today: "
        + ", ".join(f"{reason}={blocked.get(reason, 0)}" for reason in reasons)
    )
    lines.append("Remaining amounts use known recorded spend only.")
    return "\n".join(lines)


def render_fallbacks(fallbacks: list[ApprovedFallback]) -> str:
    if not fallbacks:
        return "No approved budget fallbacks."
    lines = [
        f"{'use-case':<24} {'fallback':<24} {'baseline':<24} {'provider':<12} evidence"
    ]
    for fallback in fallbacks:
        lines.append(
            f"{_safe(fallback.use_case_key)[:24]:<24} "
            f"{_safe(fallback.model)[:24]:<24} "
            f"{_safe(fallback.baseline_model)[:24]:<24} "
            f"{_safe(fallback.provider or '(same)')[:12]:<12} "
            f"{_fmt_ts(fallback.evidence_created)}"
        )
    return "\n".join(lines)


def _fmt_ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def render_experiments(exps: list[Experiment]) -> str:
    if not exps:
        return (
            "No experiments yet. Start one with:\n"
            "  ctrlrtn experiment start <use-case> <candidate-model>"
        )
    header = (
        f"{'status':<8} {'use-case':<22} {'candidate':<22} {'provider':<12} "
        f"{'split':>5} "
        f"{'id':<22} {'created':<16}"
    )
    lines = [header, "-" * len(header)]
    for e in exps:
        lines.append(
            f"{e.status:<8} {_safe(e.scope.label)[:22]:<22} "
            f"{_safe(e.candidate_model)[:22]:<22} "
            f"{_safe(e.candidate_provider or '-')[:12]:<12} "
            f"{e.split_pct:>4}% "
            f"{e.experiment_id:<22} {_fmt_ts(e.created_epoch):<16}"
        )
    return "\n".join(lines)


def render_experiment_started(exp: Experiment) -> str:
    return (
        f"Started experiment {exp.experiment_id}\n"
        f"  use-case:   {exp.use_case_key}\n"
        f"  scope:      {exp.scope.label}\n"
        f"  candidate:  {exp.candidate_model}  "
        f"on {exp.candidate_provider or 'the baseline provider'}  "
        f"(serving {exp.split_pct}% of this use-case's tasks)\n"
        f"  ceilings:   {exp.max_calls_per_task} calls/task "
        f"(enforced), ${exp.max_cost_usd_per_task:g}/task (recorded, not "
        "enforced)\n"
        "The gateway picks it up within ~10s. Stop it with:\n"
        f"  ctrlrtn experiment stop {exp.experiment_id}"
    )


def render_routes(rows: list[dict]) -> str:
    """``rows``: dicts of {route, usage, saved} built by _route_list."""
    if not rows:
        return (
            "No routes set. Adopt an experiment's candidate with "
            "`ctrlrtn route adopt <experiment-id>` or set one directly "
            "with `route set <use-case> <model>`."
        )
    header = (
        f"{'use-case':<26} {'model':<22} {'provider':<12} {'was':<22} "
        f"{'since':<16} {'calls':>6} {'spent':>9} {'saved':>9}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        route, usage, saved = row["route"], row["usage"], row["saved"]
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(route.ts))
        lines.append(
            f"{route.use_case_key[:26]:<26} {route.model[:22]:<22} "
            f"{(route.provider or '-')[:12]:<12} "
            f"{(route.previous_model or '-')[:22]:<22} "
            f"{since:<16} {usage['calls']:>6} "
            f"{_fmt_cost(usage['cost_usd']):>9} "
            f"{_fmt_cost(saved) if saved is not None else '-':>9}"
        )
        if route.note:
            lines.append(f"{'':<26} {route.note}")
    lines.append("")
    lines.append(
        "calls/spent/saved count only the calls the route actually swapped; "
        "saved = that traffic priced at the old model minus actual spend "
        "(same token mix; verbosity differences between models are not "
        "modeled)."
    )
    return "\n".join(lines)


def render_recommendations(recs: list[Recommendation]) -> str:
    if not recs:
        return "No recommendations yet — record more traffic first."
    lines: list[str] = []
    for i, rec in enumerate(recs, 1):
        lines.append(f"{i}. [{rec.kind}] {rec.use_case}")
        lines.append(f"   {rec.summary}")
        lines.append(f"   {rec.detail}")
        lines.append("")
    lines.append(
        "Costs are estimates from approximate list prices (prices.toml); "
        "verify against your provider bill."
    )
    return "\n".join(lines).rstrip()


def render_propagation(report: PropagationReport) -> str:
    lines = [
        f"header propagation (x-ctrlrtn-task) — last {report.window_calls} "
        "completion calls:",
        f"  calls={report.total_calls}  tagged={report.tasked_calls} "
        f"({report.tasked_fraction:.0%})  untagged={report.untasked_calls}",
        f"  tagged tasks={report.n_tasks} ({report.multi_agent_tasks} of "
        f"{report.n_spannable_tasks} multi-call tasks link >=2 sub-agents)",
    ]
    if report.total_calls == 0:
        lines.append(
            "  verdict: NO DATA — start the gateway (`serve`), send traffic "
            "with the x-ctrlrtn-task header set, then re-check (see "
            "docs/instrument-a-workflow.md)."
        )
        return "\n".join(lines)
    if report.tasked_fraction < MIN_TAGGED_FRACTION:
        lines.append(
            "  verdict: NOT PROPAGATING — most calls carry no x-ctrlrtn-task; the "
            "app must set the same value on every sub-agent call "
            "(docs/instrument-a-workflow.md)."
        )
    elif report.multi_agent_tasks < MIN_MULTI_AGENT_TASKS:
        lines.append(
            "  verdict: TAGGED, NO CROSS-AGENT LINK — calls are tagged, but too "
            "few tasks link multiple sub-agents to confirm whole-task grouping. "
            "-> run more real multi-agent tasks; if it persists, the sub-agents "
            "aren't inheriting the parent's x-ctrlrtn-task."
        )
    else:
        lines.append(
            "  verdict: PROPAGATING — a shared x-ctrlrtn-task links multiple "
            "sub-agents across recent tasks (confirms propagation works, not "
            "that every sub-agent of every edition is captured)."
        )
    if report.focus_use_case:
        lines.append(
            f"  focus {report.focus_use_case}: tagged in "
            f"{report.focus_task_count} task(s), "
            f"{report.focus_calls_per_task:.1f} calls/task when present, "
            f"{report.focus_co_located_tasks} linked with a sibling agent"
        )
        if report.focus_task_count == 0:
            lines.append(
                "    -> in NO tagged task; its replay is still valid (each "
                "input an independent unit), but if one run makes several of "
                "its calls, tag them so they cluster."
            )
        elif report.focus_co_located_tasks == 0:
            lines.append(
                "    -> tagged, but never shares a task with another sub-agent, "
                "so cross-agent grouping is unconfirmed for it."
            )
    return "\n".join(lines)


def render_replay(report: ReplayReport) -> str:
    r = report.result
    if r.non_inferior:
        verdict = "NON-INFERIOR"
    elif r.underpowered:
        verdict = "UNDERPOWERED (cannot conclude)"
    else:
        verdict = "NOT non-inferior"
    lines = [
        f"replay eval: {report.baseline_model} -> {report.candidate_model}",
        f"  pairings={report.n_pairings}  units={r.n_units}  "
        f"failed={report.n_failed}  blank={report.n_blank}",
    ]
    if report.n_pairings:
        bound = (
            "-inf"
            if r.lower_bound == float("-inf")
            else f"{r.lower_bound:+.3f}"
        )
        lines.append(
            f"  mean score diff (cand - base) = {report.mean_diff:+.3f}  "
            f"(0-10 judge scale; margin {r.margin}, "
            f"{int(r.confidence * 100)}% lower bound {bound})"
        )
    else:
        lines.append("  no usable pairings — nothing to compare")
    lines.append(f"  verdict: {verdict}")
    if report.failures:
        # Without this, a bad key or unsupported field reads as "UNDERPOWERED".
        lines.append(f"  {report.n_failed} sample(s) failed, e.g.:")
        lines.extend(f"    - {reason}" for reason in report.failures)
    if r.non_inferior:
        lines.append(
            "  -> non-inferior on this replay batch, but this trusts the judge "
            "blindly; before switching, check it agrees with humans: "
            "`ctrlrtn calibration-set` then `ctrlrtn calibrate`."
        )
    elif r.underpowered:
        lines.append(
            "  -> too few independent units (or too much of the batch was "
            "unusable); replay more inputs, or fix the failures above."
        )
    else:
        lines.append(
            "  -> candidate is worse on this batch; keep the baseline (or "
            "raise --margin if a larger quality drop is acceptable)."
        )
    return "\n".join(lines)


_CALIBRATION_GLOSS = {
    ALIGNED: (
        "Aligned: on this sample the judge tracks the human labels — it beats a "
        "coin flip, does not compress real quality gaps, and does not over-rate "
        "the candidate at parity. ADVISORY ONLY: replay-eval does not read this, "
        "so you still decide the downgrade by hand (and re-check if the judge "
        "model or the use-case's prompts change — a calibration is point-in-"
        "time)."
    ),
    MISALIGNED: (
        "Misaligned: the judge disagrees with humans, compresses real quality "
        "gaps, or favours the candidate at parity — trusting its verdict risks a "
        "downgrade a human would reject. Cheapest fix: re-run `calibrate "
        "--judge-model <other>` on the SAME file (it re-judges the stored "
        "outputs, no new labels needed) until one aligns."
    ),
    INSUFFICIENT: (
        "Insufficient: too few pairs where the human preferred one side "
        "(near-ties carry no signal). Label more with `calibration-set` — a "
        "near-parity candidate needs more pairs to clear the directional floor."
    ),
}


def render_calibration(report) -> str:
    lines = [
        f"VERDICT: {report.verdict}  (advisory — replay-eval does not read it)",
        f"  ({report.n} scored pairs, {report.n_directional} with a human "
        f"preference; needs >= {report.min_directional} directional)",
        "",
        f"  agreement    {report.agreement_rate:.0%}  "
        f"(Wilson lower bound {report.agreement_lb:.2f}; must clear "
        f"{report.min_agreement_lb:.2f} to beat a coin flip)",
        f"  slope        {report.slope:+.2f}  "
        f"(judge diff per unit human diff; < {report.min_slope:.1f} compresses "
        "real gaps)",
        f"  bias@parity  {report.bias:+.2f}  "
        f"(judge's diff when humans tie; > {report.bias_limit:.2f} over-rates "
        "the candidate)",
        f"  correlation  {report.correlation:+.2f}  "
        "(Spearman; diagnostic only)",
        f"  candidate win-rate   judge {report.candidate_win_rate_judge:.0%}"
        f"  vs human {report.candidate_win_rate_human:.0%}",
    ]
    if report.warnings:
        lines.append("")
        lines.append("warnings:")
        lines += [f"  ! {w}" for w in report.warnings]
    lines.append("")
    lines.append(_CALIBRATION_GLOSS[report.verdict])
    return "\n".join(lines)
