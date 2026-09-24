"""Pure command-panel, graph, and pagination formatting for the TUI."""

from __future__ import annotations

import time

from textual.binding import Binding

from ctrlrtn.cli.tui.models import _PAGE_SIZE

_COMMAND_GROUPS: tuple[
    tuple[str, tuple[tuple[str, str, str, bool], ...]], ...
] = (
    (
        "Monitor",
        (
            ("f2", "investigate", "Investigate", True),
            ("q", "quit", "Quit", True),
            ("r", "refresh", "Refresh now", False),
            ("m", "toggle_maximize", "Maximize pane", True),
            ("enter", "expand_detail", "Expand detail", False),
            ("escape", "restore_layout", "Back to normal", False),
            ("ctrl+d", "scroll_detail(1)", "Detail page down", False),
            ("ctrl+u", "scroll_detail(-1)", "Detail page up", False),
            ("question_mark", "toggle_commands", "Commands", True),
        ),
    ),
    (
        "View",
        (
            ("w", "graph_window", "Graph window", True),
            ("t", "table_window", "Table window", True),
            ("b", "toggle_budget", "Budget details", False),
            ("left_square_bracket", "page_back", "Prev page", False),
            ("right_square_bracket", "page_forward", "Next page", False),
        ),
    ),
    (
        "Experiments",
        (
            ("o", "new_offline", "New offline eval", False),
            ("e", "new_live", "New live A/B", False),
            ("h", "new_shadow", "New shadow", False),
            ("s", "stop_experiment", "Stop A/B", False),
            ("z", "stop_shadow", "Stop shadow", False),
            ("a", "adopt_experiment", "Adopt candidate", False),
        ),
    ),
    (
        "Routing",
        (
            ("p", "set_route", "Set route", False),
            ("c", "clear_route", "Clear route", False),
            ("g", "git_config", "Git config", False),
            ("x", "cancel_job", "Cancel job", False),
        ),
    ),
    (
        "Workflows",
        (
            ("d", "workflow_steps", "Step drill-down", False),
            ("l", "discovered_workflow_dag", "Open discovered DAG", False),
            ("u", "discover_workflows", "Discover workflows", False),
            ("f", "discover_workflows_scoped", "Scoped discovery", False),
            ("i", "identify_workflow", "Identify workflow", False),
            ("y", "verify_workflow_proposal", "Verify identification", False),
            ("j", "compare_workflow_discoveries", "Compare discoveries", False),
        ),
    ),
)
_KEY_LABELS = {
    "question_mark": "?",
    "escape": "esc",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
}
_KEY_ALIASES = {"question_mark": ("f1",)}


def console_bindings() -> list[Binding]:
    bindings = []
    for _, commands in _COMMAND_GROUPS:
        for key, action, description, in_footer in commands:
            bindings.append(Binding(key, action, description, show=in_footer))
            aliases = list(_KEY_ALIASES.get(key, ()))
            if len(key) == 1 and key.isalpha():
                aliases.append(key.upper())
            for alias in aliases:
                bindings.append(Binding(alias, action, description, show=False))
    return bindings


def command_panel_text() -> str:
    lines = []
    for group, commands in _COMMAND_GROUPS:
        lines.append(group)
        for key, _, description, _ in commands:
            lines.append(f"  {_KEY_LABELS.get(key, key):>6}  {description}")
        lines.append("")
    lines.append("Letter keys ignore shift.")
    return "\n".join(lines)


def graph_axis(series: list[dict], width: int) -> str:
    if len(series) < 2 or width < 24:
        return ""
    start, last = series[0]["ts"], series[-1]["ts"]
    middle = series[len(series) // 2]["ts"]
    stamp = "%H:%M" if last - start <= 86_400 else "%d %b"
    left = time.strftime(stamp, time.localtime(start))
    mid = time.strftime(stamp, time.localtime(middle))
    right = "now"
    gap_left = (width - len(mid)) // 2 - len(left)
    gap_right = width - len(left) - gap_left - len(mid) - len(right)
    if gap_left < 1 or gap_right < 1:
        return f"{left}{'─' * (width - len(left) - len(right))}{right}"
    return f"{left}{'─' * gap_left}{mid}{'─' * gap_right}{right}"


def graph_label(
    kind: str, window: str, total: str, peak: str, bucket_seconds: int
) -> str:
    return (
        f"{kind} · {window} · {total} · peak {peak}/{_duration(bucket_seconds)}"
    )


def _duration(seconds: int) -> str:
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def page_heading(title: str, page: int, rows: int, has_more: bool) -> str:
    if page == 0 and not has_more:
        return title
    first = page * _PAGE_SIZE + 1
    back = "‹ " if page else ""
    forward = " ›" if has_more else ""
    if not rows:
        return f"{title} · {back}empty page {page + 1}"
    return f"{title} · {back}{first}–{first + rows - 1}{forward}"
