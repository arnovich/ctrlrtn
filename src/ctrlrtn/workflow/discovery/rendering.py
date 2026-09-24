"""Operator-facing rendering for workflow-family reports."""

from __future__ import annotations

from ctrlrtn.workflow.discovery.models import WorkflowDiscoveryReport

_MAX_RENDER_ASSIGNMENTS = 200


def render_workflow_discovery(report: WorkflowDiscoveryReport) -> str:
    lines = [
        f"workflow family discovery ({report.algorithm})",
        f"tasks: {report.total_tasks} observed · {report.eligible_tasks} with 2+ calls · "
        f"{report.unclustered_tasks} unclustered",
        f"ambiguous exact tool links skipped: {report.ambiguous_tool_links}",
        f"synthetic correlation: {report.synthetic_traces} trace(s) · "
        f"{report.synthetic_tasks} fragment(s) · "
        f"{report.uncorrelated_traces} uncorrelated · "
        f"{report.ambiguous_correlations} ambiguous",
        "",
    ]
    if not report.families:
        lines.append(
            "No recurring workflow families met the requested thresholds."
        )
    for family in report.families:
        lines.extend(
            [
                f"{family.family_id}  support={family.support} variants={family.variants} "
                f"cohesion={family.cohesion:.1%} linked-tasks={family.linked_task_rate:.1%}",
                f"  representative task digest: {family.representative_task_digest}",
                "  member task digests: "
                + ", ".join(family.member_task_digests),
                "  probable nodes:",
            ]
        )
        lines.extend(
            f"    {node.label}: {node.tasks}/{family.support} tasks, {node.runs} calls"
            for node in family.nodes
        )
        lines.append("  exact tool-result edges:")
        lines.extend(
            (
                f"    {edge.source} ~> {edge.target}: {edge.tasks}/{family.support} "
                f"tasks, {edge.occurrences} occurrence(s)"
            )
            for edge in family.edges
        )
        if not family.edges:
            lines.append("    none")
        lines.append("")
    lines.append("task assignments:")
    lines.extend(
        (
            f"  {item.task_digest}: {item.family_id or 'unclustered'} · "
            f"match={item.match_score:.1%} · {item.identity_source}"
            + (" · AMBIGUOUS" if item.ambiguous else "")
            if item.match_score is not None
            else f"  {item.task_digest}: unclustered · {item.identity_source} · "
            f"{item.reason or 'no family'}"
            + (" · AMBIGUOUS" if item.ambiguous else "")
        )
        for item in report.assignments[:_MAX_RENDER_ASSIGNMENTS]
    )
    if not report.assignments:
        lines.append("  none")
    elif len(report.assignments) > _MAX_RENDER_ASSIGNMENTS:
        lines.append(
            f"  ... {len(report.assignments) - _MAX_RENDER_ASSIGNMENTS} more in artifact"
        )
    lines.append("")
    lines.extend(
        [
            "Discovered families are structural hypotheses, not declared workflows.",
            "Families without exact links reflect recurring role/tool-shape multisets, not causality.",
            "They are analysis-only and cannot authorize routing or execution changes.",
        ]
    )
    return "\n".join(lines)
