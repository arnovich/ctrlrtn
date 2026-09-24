"""Argument registration for workflow analysis."""

from __future__ import annotations


def register(sub, commands) -> None:
    workflow = sub.add_parser(
        "workflow", help="offline workflow inference and diagnostics"
    )
    workflow_sub = workflow.add_subparsers(
        dest="workflow_command", required=True
    )
    workflow_infer = workflow_sub.add_parser(
        "infer",
        help="rebuild evidence-only tool-result links and measure them against explicit edges",
    )
    workflow_infer.set_defaults(func=commands._workflow_infer)
    workflow_list = workflow_sub.add_parser(
        "inferred", help="list persisted inferred edges"
    )
    workflow_list.set_defaults(func=commands._workflow_inferred)
    workflow_discover = workflow_sub.add_parser(
        "discover",
        help="cluster recurring analysis-only workflow families from legacy task traffic",
    )
    workflow_discover.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="maximum recent explicit tasks plus unscoped calls to inspect",
    )
    workflow_discover.add_argument(
        "--min-support",
        type=int,
        default=2,
        help="minimum tasks required to report a family",
    )
    workflow_discover.add_argument(
        "--similarity",
        type=float,
        default=0.75,
        help="structural similarity threshold in (0, 1]",
    )
    workflow_discover.add_argument(
        "--background",
        action="store_true",
        help="freeze inputs and queue durable discovery for a worker",
    )
    workflow_discover.add_argument("--since", type=float, default=None)
    workflow_discover.add_argument("--until", type=float, default=None)
    workflow_discover.add_argument("--provider", default=None)
    workflow_discover.add_argument("--model", default=None)
    workflow_discover.add_argument("--experiment", default=None)
    workflow_discover.add_argument(
        "--arm", choices=("baseline", "candidate"), default=None
    )
    workflow_discover.set_defaults(func=commands._workflow_discover)
    workflow_compare_snapshots = workflow_sub.add_parser(
        "discovery-compare",
        help="compare two completed durable workflow discovery jobs",
    )
    workflow_compare_snapshots.add_argument("previous_job")
    workflow_compare_snapshots.add_argument("current_job")
    workflow_compare_snapshots.add_argument(
        "--similarity", type=float, default=0.6
    )
    workflow_compare_snapshots.add_argument(
        "--allow-unrelated",
        action="store_true",
        help="explicitly compare scopes that differ across multiple dimensions",
    )
    workflow_compare_snapshots.set_defaults(
        func=commands._workflow_discovery_compare
    )
    workflow_project = workflow_sub.add_parser(
        "discovery-project",
        help="inspect or export a passive discovered-family projection",
    )
    workflow_project.add_argument("job_id")
    workflow_project.add_argument("family_id")
    workflow_project.add_argument(
        "--format", choices=("text", "mermaid", "json"), default="text"
    )
    workflow_project.add_argument("--output", default=None)
    workflow_project.set_defaults(func=commands._workflow_discovery_project)
    workflow_identify = workflow_sub.add_parser(
        "identify",
        help="write an inert named workflow proposal from a discovered family",
    )
    workflow_identify.add_argument("family_id")
    workflow_identify.add_argument("workflow")
    workflow_identify.add_argument("workflow_version")
    workflow_identify.add_argument("--output", required=True)
    workflow_identify.add_argument("--limit", type=int, default=1000)
    workflow_identify.add_argument("--min-support", type=int, default=2)
    workflow_identify.add_argument("--similarity", type=float, default=0.75)
    workflow_identify.set_defaults(func=commands._workflow_identify)
    workflow_verify = workflow_sub.add_parser(
        "identify-verify",
        help="verify an inert workflow identification proposal",
    )
    workflow_verify.add_argument("path")
    workflow_verify.set_defaults(func=commands._workflow_identify_verify)
    workflow_erase = workflow_sub.add_parser(
        "erase-task",
        help="plan or erase one workflow task and derived database records",
    )
    workflow_erase.add_argument("task_id")
    workflow_erase.add_argument(
        "--apply",
        action="store_true",
        help="perform the deletion (default is a read-only dry run)",
    )
    workflow_erase.set_defaults(func=commands._workflow_erase_task)
    workflow_diagnostics = workflow_sub.add_parser(
        "diagnostics", help="show workflow identity and lifecycle consistency"
    )
    workflow_diagnostics.set_defaults(func=commands._workflow_diagnostics)
    workflow_steps = workflow_sub.add_parser(
        "steps",
        help="show explicit step-level cost, latency, state, and outcomes",
    )
    workflow_steps.add_argument("--workflow", default=None)
    workflow_steps.add_argument("--version", default=None)
    workflow_steps.set_defaults(func=commands._workflow_steps)
    workflow_diagram = workflow_sub.add_parser(
        "diagram", help="export one task graph as a Mermaid flowchart"
    )
    workflow_diagram.add_argument("task_id")
    workflow_diagram.add_argument(
        "--output",
        default=None,
        help="write Mermaid to this path (default stdout)",
    )
    workflow_diagram.set_defaults(func=commands._workflow_diagram)
    workflow_flow = workflow_sub.add_parser(
        "flow", help="aggregate exact-version task paths and export a Sankey"
    )
    workflow_flow.add_argument("workflow")
    workflow_flow.add_argument("version")
    workflow_flow.add_argument("--limit", type=int, default=1000)
    workflow_flow.add_argument(
        "--output", default=None, help="write explicit-edge Mermaid Sankey"
    )
    workflow_flow.set_defaults(func=commands._workflow_flow)
    workflow_catalog = workflow_sub.add_parser(
        "catalog", help="summarize exact workflow versions and active controls"
    )
    workflow_catalog.add_argument("--workflow", default=None)
    workflow_catalog.set_defaults(func=commands._workflow_catalog)
    workflow_compare = workflow_sub.add_parser(
        "compare", help="descriptively compare two versions of one workflow"
    )
    workflow_compare.add_argument("workflow")
    workflow_compare.add_argument("left_version")
    workflow_compare.add_argument("right_version")
    workflow_compare.set_defaults(func=commands._workflow_compare)
    workflow_step_detail = workflow_sub.add_parser(
        "step-detail", help="show connected metadata for one exact step run"
    )
    workflow_step_detail.add_argument("task_id")
    workflow_step_detail.add_argument("step_run_id")
    workflow_step_detail.set_defaults(func=commands._workflow_step_detail)
    workflow_recommendations = workflow_sub.add_parser(
        "recommendations",
        help="show evidence-labelled, read-only optimization opportunities",
    )
    workflow_recommendations.add_argument("--workflow", default=None)
    workflow_recommendations.add_argument("--version", default=None)
    workflow_recommendations.set_defaults(
        func=commands._workflow_recommendations
    )
