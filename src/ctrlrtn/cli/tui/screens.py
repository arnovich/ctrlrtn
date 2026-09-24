"""Reusable modal screens for the Textual console."""

from __future__ import annotations

from rich.control import strip_control_codes
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static

from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.workflow.discovery_projection import (
    DiscoveredFamilyProjection,
    render_discovered_family_dag,
)
from ctrlrtn.workflow.graph import render_workflow_structure
from ctrlrtn.workflow.step_detail import render_workflow_step_detail


class WindowPickerScreen(ModalScreen[str | None]):
    """Pick one trailing window, by label, from a fixed list."""

    CSS = """
    WindowPickerScreen { align: center middle; }
    #window-dialog { width: 64; height: auto; border: thick $primary;
                     background: $surface; padding: 1 2; }
    #window-choices { height: auto; margin-top: 1; }
    #window-choices Button { width: auto; min-width: 6; margin-right: 1; }
    #window-hint { color: $text-muted; }
    """
    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("left", "previous", "Previous", show=False),
        Binding("up", "previous", "Previous", show=False),
        Binding("right", "next", "Next", show=False),
        Binding("down", "next", "Next", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        prefix: str,
        labels: tuple[str, ...],
        current: str,
        showing: str,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.prefix = prefix
        self.labels = labels
        self.current = current
        self.showing = showing

    def compose(self) -> ComposeResult:
        with Vertical(id="window-dialog"):
            yield Label(self.title_text, classes="heading")
            yield Static(self.showing, markup=False)
            with Horizontal(id="window-choices"):
                for label in self.labels:
                    yield Button(
                        label,
                        id=f"{self.prefix}-{label}",
                        variant=(
                            "primary" if label == self.current else "default"
                        ),
                    )
            yield Static(
                "← → / tab to move · enter to pick · esc to close",
                id="window-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one(f"#{self.prefix}-{self.current}", Button).focus()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_next(self) -> None:
        self.focus_next()

    def action_previous(self) -> None:
        self.focus_previous()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.id).removeprefix(f"{self.prefix}-"))


class WorkflowStepScreen(ModalScreen[None]):
    """Filter graph nodes and inspect connected metadata and comparisons."""

    CSS = """
    WorkflowStepScreen { align: center middle; }
    #step-dialog { width: 95%; height: 95%; border: thick $primary;
                   background: $surface; padding: 1 2; }
    #step-filter { height: 3; }
    #step-structure { height: 9; }
    #step-runs { height: 14; }
    #step-detail { height: 1fr; }
    """
    BINDINGS = [
        ("escape", "close", "Close"),
        ("ctrl+f", "focus_filter", "Filter nodes"),
    ]

    def __init__(self, store: SqliteTraceStore, graph) -> None:
        super().__init__()
        self.store = store
        self.graph = graph
        self._metadata = {
            node.step_run_id: self._node_metadata(node.step_run_id)
            for node in graph.nodes
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="step-dialog"):
            yield Label(
                f"Step runs · {self.graph.task_id} · "
                f"{self.graph.workflow}@{self.graph.workflow_version}",
                classes="heading",
            )
            yield Input(
                placeholder=(
                    "Filter by step, state, model, provider, or arm (Ctrl+F)"
                ),
                id="step-filter",
            )
            yield Static(
                render_workflow_structure(self.graph),
                id="step-structure",
                markup=False,
            )
            yield DataTable(id="step-runs", cursor_type="row")
            with VerticalScroll():
                yield Static(
                    "Select a step run.", id="step-detail", markup=False
                )

    def on_mount(self) -> None:
        table = self.query_one("#step-runs", DataTable)
        table.add_columns(
            "run",
            "step",
            "try",
            "state",
            "model",
            "provider",
            "arm",
            "calls",
            "cost",
        )
        self._fill("")
        if self.graph.nodes:
            self._show(self.graph.nodes[0].step_run_id)

    def _fill(self, query: str) -> None:
        table = self.query_one("#step-runs", DataTable)
        table.clear()
        query = query.casefold().strip()
        for node in self.graph.nodes:
            model, provider, arm = self._metadata[node.step_run_id]
            haystack = " ".join(
                (node.step, node.status, model, provider, arm)
            ).casefold()
            if query and query not in haystack:
                continue
            table.add_row(
                node.step_run_id[:24],
                node.step[:24],
                str(node.attempt),
                node.status,
                model[:20],
                provider[:16],
                arm[:12],
                str(node.calls),
                f"${node.cost_usd:.4f}",
                key=node.step_run_id,
            )

    def _node_metadata(self, step_run_id: str) -> tuple[str, str, str]:
        detail = self.store.workflow_step_detail(
            self.graph.task_id, step_run_id
        )
        traces = detail["traces"] if detail else []

        def joined(field: str) -> str:
            values = sorted({trace[field] for trace in traces if trace[field]})
            return ",".join(values) or "-"

        return joined("model"), joined("provider"), joined("arm")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "step-filter":
            self._fill(event.value)

    def action_focus_filter(self) -> None:
        self.query_one("#step-filter", Input).focus()

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        self._show(str(event.row_key.value))

    def _show(self, step_run_id: str) -> None:
        detail = self.store.workflow_step_detail(
            self.graph.task_id, step_run_id
        )
        text = (
            render_workflow_step_detail(detail)
            if detail
            else "Step run disappeared."
        )
        self.query_one("#step-detail", Static).update(strip_control_codes(text))

    def action_close(self) -> None:
        self.dismiss(None)


class DiscoveredWorkflowDagScreen(ModalScreen[None]):
    """Full-screen terminal graph for a passively inferred workflow family."""

    CSS = """
    DiscoveredWorkflowDagScreen { align: center middle; }
    #discovered-dag-dialog { width: 95%; height: 95%; border: thick $primary;
                             background: $surface; padding: 1 2; }
    #discovered-dag-scroll { height: 1fr; }
    #discovered-dag { height: auto; }
    """
    BINDINGS = [("escape", "close", "Close"), ("q", "close", "Close")]

    def __init__(self, projection: DiscoveredFamilyProjection) -> None:
        super().__init__()
        self.projection = projection

    def compose(self) -> ComposeResult:
        with Vertical(id="discovered-dag-dialog"):
            yield Label("Discovered workflow · weighted DAG", classes="heading")
            with VerticalScroll(id="discovered-dag-scroll"):
                yield Static(
                    render_discovered_family_dag(self.projection),
                    id="discovered-dag",
                    markup=False,
                )

    def action_close(self) -> None:
        self.dismiss(None)
