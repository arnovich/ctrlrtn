"""The cost-saving campaign report: one table + one chart from a recorded run.

The campaign playbook (``docs/campaign.md``) records real multi-agent traffic
per role, runs ``replay-eval`` per use-case for a paired quality verdict, and
optionally live A/B experiments for in-vivo confirmation. This module folds all
three into one artifact:

* per role: recorded spend, the same token mix **repriced at the candidate's
  rates** (an honest what-if — identical workload, cheaper model), the
  replay-eval verdict, and — where a live experiment ran — real cost/task per
  arm plus the tripwire verdict;
* a markdown table and a dependency-free SVG bar chart for the README.

Verdicts come from ``replay-eval --json`` files so the chart never claims more
than the eval concluded: a bar only reads as a saving when the paired judge
found the candidate non-inferior.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ctrlrtn.eval.replay import ReplayReport
from ctrlrtn.eval.tripwire import run_tripwire
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.recorder.repositories import ReportingRepository
from ctrlrtn.telemetry import pricing
from ctrlrtn.telemetry.usage import Usage

# replay-eval --json verdict strings (mirrors render_replay's three outcomes).
NON_INFERIOR = "NON_INFERIOR"
UNDERPOWERED = "UNDERPOWERED"
NOT_NON_INFERIOR = "NOT_NON_INFERIOR"


def replay_report_json(
    report: ReplayReport,
    use_case: str,
    *,
    scope: dict | None = None,
    dataset: dict | None = None,
) -> dict:
    """The machine-readable form of one replay-eval run (``--json``). Infinite
    bounds serialize as None — JSON has no -inf."""
    r = report.result
    if r.non_inferior:
        verdict = NON_INFERIOR
    elif r.underpowered:
        verdict = UNDERPOWERED
    else:
        verdict = NOT_NON_INFERIOR
    result = {
        "use_case": use_case,
        "baseline_model": report.baseline_model,
        "candidate_model": report.candidate_model,
        "verdict": verdict,
        "mean_diff": r.mean_diff,
        "lower_bound": (
            None if r.lower_bound == float("-inf") else r.lower_bound
        ),
        "margin": r.margin,
        "confidence": r.confidence,
        "n_pairings": report.n_pairings,
        "n_units": r.n_units,
        "n_failed": report.n_failed,
        "n_blank": report.n_blank,
        "created": time.time(),
    }
    if scope:
        result["scope"] = scope
        result["claim"] = "workflow_step"
    if dataset:
        result["dataset"] = dict(dataset)
    return result


@dataclass
class RoleRow:
    """One use-case (role) in the campaign report."""

    use_case: str
    calls: int
    cost_usd: float  # recorded spend on this role
    baseline_model: str | None
    candidate_model: str | None
    repriced_usd: float | None  # same token mix at the candidate's prices
    replay: dict | None  # the replay-eval --json blob, if provided
    experiment_id: str | None = None
    tripwire_verdict: str | None = None
    baseline_cost_per_task: float | None = None
    candidate_cost_per_task: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def replay_verdict(self) -> str | None:
        return self.replay["verdict"] if self.replay else None

    @property
    def savings_pct(self) -> float | None:
        """Repriced saving on the recorded workload (positive = cheaper)."""
        if self.repriced_usd is None or self.cost_usd <= 0:
            return None
        return 1.0 - self.repriced_usd / self.cost_usd


def _reprice(row, candidate: str | None) -> float | None:
    """The recorded token mix priced at the candidate's rates (cache-aware).
    None when the candidate is unknown/unpriced — never silently $0."""
    if candidate is None:
        return None
    return pricing.cost_usd(
        candidate,
        Usage(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cache_read_tokens=row.cache_read_tokens,
            cache_write_tokens=row.cache_write_tokens,
        ),
    )


def build_campaign_report(
    store: ReportingRepository,
    replay_reports: list[dict] | None = None,
    *,
    now: float | None = None,
) -> list[RoleRow]:
    """Fold recorded spend, replay verdicts and live experiments into one row
    per role. Included: every ``tag:`` use-case, plus any use-case that has a
    replay verdict or an experiment (fp: keys included that way)."""
    # A role/use-case campaign is a whole-scope report. Step evidence must not
    # silently bless the role or be combined with whole-task outcomes.
    replays = {
        r["use_case"]: r
        for r in (replay_reports or [])
        if r.get("scope") is None
    }
    experiments: dict[str, Experiment] = {}
    for experiment in store.experiments(limit=500):
        if experiment.scope.is_workflow_scoped:
            continue
        # experiments() is newest-first; keep the newest per use-case.
        experiments.setdefault(experiment.use_case_key, experiment)
    baselines = store.use_case_models()
    now = time.time() if now is None else now

    rows = []
    # baseline_only: a running experiment's candidate-arm calls must not
    # dilute "what this role costs today" (or the repricing's token mix).
    for ranking in store.rankings(baseline_only=True):
        use_case = ranking.use_case
        replay = replays.get(use_case)
        exp = experiments.get(use_case)
        if not (use_case.startswith("tag:") or replay or exp):
            continue
        candidate = None
        if replay:
            candidate = replay["candidate_model"]
        elif exp is not None:
            candidate = exp.candidate_model
        row = RoleRow(
            use_case=use_case,
            calls=ranking.calls,
            cost_usd=ranking.cost_usd,
            baseline_model=baselines.get(use_case),
            candidate_model=candidate,
            repriced_usd=_reprice(ranking, candidate),
            replay=replay,
        )
        if candidate and row.repriced_usd is None:
            row.warnings.append(f"no price table entry for {candidate}")
        if row.calls and row.cost_usd == 0:
            row.warnings.append(
                "recorded cost is $0 despite traffic — unpriced baseline "
                "model? The bar understates the true spend."
            )
        if exp is not None:
            tripwire = run_tripwire(
                store.experiment_task_rows(exp.experiment_id), now=now
            )
            row.experiment_id = exp.experiment_id
            row.tripwire_verdict = tripwire.verdict
            row.baseline_cost_per_task = tripwire.baseline.cost_per_task
            row.candidate_cost_per_task = tripwire.candidate.cost_per_task
        rows.append(row)
    seen = {r.use_case for r in rows}
    for use_case, replay in replays.items():
        if use_case in seen:
            continue
        rows.append(
            RoleRow(
                use_case=use_case,
                calls=0,
                cost_usd=0.0,
                baseline_model=replay.get("baseline_model"),
                candidate_model=replay.get("candidate_model"),
                repriced_usd=None,
                replay=replay,
                warnings=[
                    "replay verdict has NO recorded traffic in this database "
                    "— use-case typo, or report run against the wrong db_path?"
                ],
            )
        )
    return rows


_VERDICT_MARK = {
    NON_INFERIOR: "✓ non-inferior",
    UNDERPOWERED: "? underpowered",
    NOT_NON_INFERIOR: "✗ worse",
    None: "— not evaluated",
}


def _md(text: str) -> str:
    """Neutralize table-breaking characters in client-controlled strings — a
    tag of "edi|tor" must not add a column to the published table."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'")


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.0%}"


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def render_campaign_markdown(rows: list[RoleRow]) -> str:
    """The README table: what each role cost, what the same workload would
    cost on the candidate, and whether the paired eval blesses the swap."""
    if not rows:
        return "No campaign data recorded yet."
    lines = [
        "| role | calls | recorded cost | at candidate prices | saving "
        "| replay verdict | live A/B |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for r in rows:
        saving = _pct(r.savings_pct)
        live = "—"
        if r.experiment_id:
            live = (
                f"{r.tripwire_verdict} "
                f"({_usd(r.baseline_cost_per_task)} vs "
                f"{_usd(r.candidate_cost_per_task)}/task)"
            )
        verdict = _VERDICT_MARK.get(r.replay_verdict, r.replay_verdict or "")
        lines.append(
            f"| {_md(r.use_case)} | {r.calls} | {_usd(r.cost_usd)} "
            f"| {_usd(r.repriced_usd)} | {saving} "
            f"| {_md(verdict)} "
            f"| {live} |"
        )
    total = sum(r.cost_usd for r in rows)
    blessed_savings = [
        r.cost_usd - r.repriced_usd
        for r in rows
        if r.replay_verdict == NON_INFERIOR and r.repriced_usd is not None
    ]
    if blessed_savings and total > 0:
        # Only count savings the eval actually blessed; the rest stays as-is.
        saved = sum(blessed_savings)
        lines.append("")
        lines.append(
            f"Switching only the ✓ roles would save **${saved:.2f} of "
            f"${total:.2f} ({saved / total:.0%})** on this recorded workload "
            "(repriced at the candidate's rates; live per-arm $/task is the "
            "ground truth)."
        )
    for r in rows:
        for w in r.warnings:
            lines.append(f"\n> ⚠ {_md(r.use_case)}: {w}")
    return "\n".join(lines)


# --- the chart ---------------------------------------------------------------

_BAR_H = 16
_ROW_H = 64
_CHART_W = 720
_LEFT = 200  # label gutter
# Room for the value text after the longest possible bar ("$123.45 ✓
# non-inferior" ~ 22 monospace chars); model names live in the subtitle.
_RIGHT = 170
_BASELINE_COLOR = "#5b7fa6"
_BG_COLOR = "#ffffff"
_TEXT_COLOR = "#1f2328"
_CANDIDATE_COLOR = "#3fa66f"
_MUTED_COLOR = "#9aa4ad"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_campaign_svg(rows: list[RoleRow]) -> str:
    """A dependency-free grouped bar chart: per role, recorded cost vs the same
    workload at candidate prices. The candidate bar is green only when the
    paired eval said non-inferior — an unevaluated saving renders muted, so the
    chart can't oversell."""
    rows = [r for r in rows if r.cost_usd > 0]
    if not rows:
        return "<svg xmlns='http://www.w3.org/2000/svg'/>"
    scale_max = max(max(r.cost_usd, r.repriced_usd or 0.0) for r in rows) or 1.0
    plot_w = _CHART_W - _LEFT - _RIGHT
    height = _ROW_H * len(rows) + 76
    baselines = sorted({r.baseline_model for r in rows if r.baseline_model})
    candidates = sorted({r.candidate_model for r in rows if r.candidate_model})
    subtitle = (
        f"{', '.join(baselines) or '?'} (blue) vs "
        f"{', '.join(candidates) or '?'} (green = non-inferior, grey = not)"
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_CHART_W}" '
        f'height="{height}" font-family="ui-monospace, monospace" '
        f'font-size="12" fill="{_TEXT_COLOR}">',
        # Solid background + explicit text fill: GitHub renders README SVGs
        # over the page background, so a transparent chart with default-black
        # text is invisible in dark mode.
        f'<rect width="100%" height="100%" fill="{_BG_COLOR}"/>',
        '<text x="8" y="20" font-size="14" font-weight="bold">'
        "Recorded workload, repriced at the candidate — green only where "
        "the paired eval passed</text>",
        f'<text x="8" y="38" font-size="11" fill="{_MUTED_COLOR}">'
        f"{_esc(subtitle)}</text>",
    ]
    y = 60
    for r in rows:
        blessed = r.replay_verdict == NON_INFERIOR
        base_w = max(1.0, plot_w * r.cost_usd / scale_max)
        parts.append(f'<text x="8" y="{y + 12}">{_esc(r.use_case[:24])}</text>')
        parts.append(
            f'<rect x="{_LEFT}" y="{y}" width="{base_w:.1f}" '
            f'height="{_BAR_H}" fill="{_BASELINE_COLOR}"/>'
        )
        # Compact value labels only — model names live in the subtitle, so the
        # text after the longest bar still fits inside the canvas.
        parts.append(
            f'<text x="{_LEFT + base_w + 6:.1f}" y="{y + 12}">'
            f"{_usd(r.cost_usd)}</text>"
        )
        if r.repriced_usd is not None:
            cand_w = max(1.0, plot_w * r.repriced_usd / scale_max)
            color = _CANDIDATE_COLOR if blessed else _MUTED_COLOR
            mark = _VERDICT_MARK.get(r.replay_verdict, r.replay_verdict or "")
            parts.append(
                f'<rect x="{_LEFT}" y="{y + _BAR_H + 4}" '
                f'width="{cand_w:.1f}" height="{_BAR_H}" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{_LEFT + cand_w + 6:.1f}" y="{y + _BAR_H + 16}">'
                f"{_usd(r.repriced_usd)} {_esc(mark)}</text>"
            )
        y += _ROW_H
    parts.append("</svg>")
    return "\n".join(parts)
