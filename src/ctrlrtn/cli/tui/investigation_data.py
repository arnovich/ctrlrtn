"""Read models for linking traffic metrics to tasks and replay evidence."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.control import strip_control_codes


def label(value: object) -> str:
    """Keep captured identifiers inert and legible in a terminal."""
    return strip_control_codes(str(value)).replace("\n", " ").replace("\r", " ")


def role_name(value: str | None) -> str:
    """Present a readable role while retaining its exact key in details."""
    return label(value or "Unattributed").removeprefix("tag:").replace("_", " ")


def completion(row: dict) -> str:
    """Do not promote missing or conflicting evidence into success."""
    low, high = row["minimum_success"], row["maximum_success"]
    if low is not None and high is not None and low != high:
        return "Conflicting reports"
    if high == 1:
        return "Completed"
    if low == 0:
        return "Failed"
    return "Not reported"


def summarize(rows: list[dict]) -> dict:
    """Count tasks once and preserve missing cost and outcome coverage."""
    tasks = {row["task_id"]: row for row in rows if row["task_id"] is not None}
    states = [completion(row) for row in tasks.values()]
    return dict(
        calls=len(rows),
        known_cost=sum(row["cost_usd"] or 0 for row in rows),
        unknown_cost=sum(row["cost_usd"] is None for row in rows),
        errors=sum(not 200 <= row["status_code"] < 300 for row in rows),
        tasks=len(tasks),
        untasked=sum(row["task_id"] is None for row in rows),
        completed=states.count("Completed"),
        failed=states.count("Failed"),
        missing=states.count("Not reported"),
        conflicting=states.count("Conflicting reports"),
        duplicate_reports=sum(
            row["outcome_reports"] > 1 for row in tasks.values()
        ),
    )


@dataclass(frozen=True)
class ReplayEvidence:
    """Explicit attached evidence, never a fabricated live experiment row."""

    title: str
    role: str
    baseline: str
    candidate: str
    verdict: str
    decision: str
    limitation: str
    next_step: str
    pairs: tuple[dict, ...]
    costs: dict[str, dict]
    units: int
    edition_labels: dict[str, str] = field(default_factory=dict)
    sample_unit: str = "turns"
    cost_label: str = "Replay response costs"
    score_scope: str = (
        "Scores are 0–10 for a single turn, not complete-workflow outcomes."
    )
    view_label: str = "Original replay"
    boundary: str = (
        "One response per model per turn; requested tools were not executed."
    )

    def trace_rows(self, index: int) -> list[dict]:
        """Project attached per-call evidence without inventing database IDs."""
        pair = self.pairs[index]
        rows = []
        for arm, name in (
            ("baseline", self.baseline),
            ("candidate", self.candidate),
        ):
            call = next(
                (call for call in pair["calls"] if call["label"] == arm), None
            )
            if call is None:
                continue
            records = call.get("records", [call])
            steps = call.get("steps")
            for number, record in enumerate(records, 1):
                step = steps[number - 1] if steps else {}
                seconds = record.get(
                    "elapsed_seconds", record.get("latency_seconds")
                )
                rows.append(
                    dict(
                        id=None,
                        task_id=None,
                        edition=pair["result"]["edition"],
                        ts=None,
                        use_case_key=self.role,
                        model=record.get("model", record.get("served_model")),
                        status_code=record.get("status_code"),
                        cost_usd=record.get("cost_usd"),
                        latency_ms=None if seconds is None else seconds * 1000,
                        input_tokens=record.get("input_tokens"),
                        output_tokens=record.get("output_tokens"),
                        terminal_reason=None,
                        stop_reason=record.get("stop_reason"),
                        call_label=f"{name} {number}",
                        tools=record.get(
                            "tools", record.get("emitted_tools", [])
                        ),
                        tool_outcome=step.get(
                            "outcome", "Not executed · replay stopped"
                        ),
                        tool_error=step.get("is_error"),
                        evidence_reference=record.get(
                            "evidence_reference",
                            f"reconciliation.json / {pair.get('pair', index)} / {arm}",
                        ),
                    )
                )
        return rows

    def timeline(self, index: int, arm: str) -> str:
        """Show each measured call and distinguish requests from tool execution."""
        pair = self.pairs[index]
        name = self.baseline if arm == "baseline" else self.candidate
        score = pair["result"].get(arm)
        lines = [
            f"{name} · {score if score is not None else 'Not scored'}/10",
            "",
        ]
        call = next(
            (call for call in pair["calls"] if call["label"] == arm), None
        )
        if call is None:
            return "\n".join(lines + ["No call evidence"])
        steps = call.get("steps")
        if steps is None:
            steps = [
                dict(
                    tool=", ".join(call.get("emitted_tools", []))
                    or "No tool requested",
                    cost_usd=call.get("cost_usd"),
                    outcome="Not executed · replay stopped",
                    is_error=False,
                )
            ]
        recovered = False
        for number, step in enumerate(steps, 1):
            cost = step.get("cost_usd")
            price = "Unknown cost" if cost is None else f"${cost:.6f}"
            if number > 1:
                lines.append("   ↓")
            lines.append(f"{number}  {label(step['tool'])}")
            outcome = label(step["outcome"])
            if recovered and outcome == "Review completed":
                outcome = "Recovered · review completed"
            lines.extend([f"   {price}", f"   {outcome}"])
            recovered |= bool(step.get("is_error"))
        total = call.get("cost_usd")
        price = "Unknown" if total is None else f"${total:.6f}"
        count = len(steps)
        lines.extend(
            ["", f"Total {price} · {count} call{'s' if count != 1 else ''}"]
        )
        if "tool_errors" in call:
            errors = call["tool_errors"]
            lines.append(f"{errors} tool error{'s' if errors != 1 else ''}")
        return "\n".join(lines)


@dataclass(frozen=True)
class InvestigationContext:
    """Optional campaign scope supplied by a caller with known provenance."""

    title: str = "Traffic investigation"
    scope: str = "Recorded traffic"
    cost_label: str = "Recorded cost"
    roles: tuple[str, ...] | None = None
    since: float | None = None
    cutoff: float | None = None
    comparison: ReplayEvidence | None = None
    comparisons: tuple[ReplayEvidence, ...] = ()
    initial_comparison: int = 0
    task_labels: dict[str, str] = field(default_factory=dict)


def group_rows(rows: list[dict], field: str) -> list[tuple[object, list[dict]]]:
    """Group evidence by exact identifiers and order by known spend."""
    groups: dict[object, list[dict]] = {}
    for row in rows:
        groups.setdefault(row[field], []).append(row)
    return sorted(
        groups.items(),
        key=lambda item: -sum(row["cost_usd"] or 0 for row in item[1]),
    )
