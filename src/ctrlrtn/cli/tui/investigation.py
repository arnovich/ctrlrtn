"""Read-only, question-led traffic investigation with connected evidence."""

from __future__ import annotations

import sqlite3
import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from ctrlrtn.cli.tui.investigation_data import (
    InvestigationContext,
    ReplayEvidence,
    completion,
    group_rows,
    label,
    role_name,
    summarize,
)
from ctrlrtn.cli.tui.trace import RecordedTraceScreen
from ctrlrtn.recorder.store import SqliteTraceStore

_LIMIT = 10_000


def money(value: float | None) -> str:
    """Keep unpriced calls distinct from free calls."""
    return "Unknown" if value is None else f"${value:.6f}"


def cells(*values: object) -> tuple[Text, ...]:
    """Render captured labels literally, including Rich-looking markup."""
    return tuple(Text(label(value)) for value in values)


class InvestigationCallsScreen(ModalScreen[None]):
    """Recorded calls linked to a role, task or comparison selection."""

    CSS = """
    InvestigationCallsScreen { background: $background; }
    #calls-context { height: auto; padding: 1 2; color: $accent; }
    #calls-note { height: auto; padding: 0 2 1 2; }
    #investigation-calls { height: 2fr; margin: 0 2; }
    #call-facts-scroll { height: 1fr; padding: 1 2; border-top: solid $primary; }
    """
    BINDINGS = [
        Binding("i", "inspect_trace", "Inspect trace", priority=True),
        Binding("escape", "close", "Back", priority=True),
        Binding("q", "app.quit", "Quit", priority=True),
    ]

    def __init__(self, title: str, rows: list[dict]) -> None:
        super().__init__()
        self.context_title = title
        self.rows = rows

    def compose(self) -> ComposeResult:
        yield Static(self.context_title, id="calls-context", markup=False)
        yield Static(
            f"{len(self.rows)} recorded calls. Select a row, then Enter or i to inspect its trace.",
            id="calls-note",
            markup=False,
        )
        yield DataTable(id="investigation-calls", cursor_type="row")
        with VerticalScroll(id="call-facts-scroll"):
            yield Static("", id="call-facts", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        if self.rows and "evidence_reference" in self.rows[0]:
            table.add_columns(
                "Call",
                "Requested tool",
                "Tool outcome",
                "HTTP",
                "Cost",
                "Duration",
            )
            for index, row in enumerate(self.rows):
                duration = row["latency_ms"]
                table.add_row(
                    *cells(
                        row["call_label"],
                        ", ".join(row["tools"]),
                        row["tool_outcome"],
                        row["status_code"],
                        money(row["cost_usd"]),
                        (
                            "Unknown"
                            if duration is None
                            else f"{duration / 1000:.3f}s"
                        ),
                    ),
                    key=str(index),
                )
            table.focus()
            return
        table.add_columns(
            "Time (UTC)", "Role", "Model", "HTTP", "Cost", "Latency"
        )
        for index, row in enumerate(self.rows):
            table.add_row(
                *cells(
                    time.strftime("%d %b %H:%M:%S", time.gmtime(row["ts"])),
                    role_name(row["use_case_key"]),
                    row["model"] or "Unknown",
                    row["status_code"],
                    money(row["cost_usd"]),
                    f"{row['latency_ms'] / 1000:.2f}s",
                ),
                key=str(index),
            )
        table.focus()

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key.value is None:
            return
        row = self.rows[int(event.row_key.value)]
        if "evidence_reference" in row:
            self.query_one("#call-facts", Static).update(
                f"{label(row['call_label'])} · {label(row['model'])}\n"
                f"{label(row['tool_outcome'])} · {money(row['cost_usd'])}\n"
                f"Evidence: {label(row['evidence_reference'])}\n\n"
                "Enter or i opens this individual trace."
            )
            return
        self.query_one("#call-facts", Static).update(
            f"Call {row['id']}   Task: {label(row['task_id'] or 'Not supplied')}\n"
            f"Role: {label(row['use_case_key'])}\n"
            f"Model: {label(row['model'] or 'Unknown')}\n"
            f"HTTP {row['status_code']}   {money(row['cost_usd'])}   "
            f"{row['latency_ms'] / 1000:.3f}s\n"
            f"Input / output tokens: {row['input_tokens']} / {row['output_tokens']}\n"
            f"Router terminal reason: {label(row['terminal_reason'] or 'None')}\n"
            "HTTP status describes this call; task completion is reported separately."
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_inspect_trace()

    def action_inspect_trace(self) -> None:
        index = self.query_one(DataTable).cursor_row
        if 0 <= index < len(self.rows):
            self.app.push_screen(
                RecordedTraceScreen(self.context_title, self.rows, index),
                self._trace_closed,
            )

    def _trace_closed(self, index: int) -> None:
        self.query_one(DataTable).move_cursor(row=index)

    def action_close(self) -> None:
        self.dismiss(None)


class InvestigationScreen(ModalScreen[None]):
    """A scoped overview with full-width tables and selected-pair evidence."""

    CSS = """
    InvestigationScreen { background: $background; }
    #investigation-title { height: 2; padding: 0 2; text-style: bold; color: $accent; }
    #investigation-scope { height: auto; padding: 0 2; color: $text-muted; }
    #investigation-tabs { height: 1fr; }
    InvestigationScreen TabPane { padding: 1 2; }
    .investigation-heading { height: auto; text-style: bold; margin-bottom: 1; }
    #overview-metrics { height: auto; margin-bottom: 1; }
    #overview-cost { width: 1fr; height: auto; }
    #overview-outcomes { width: 2fr; height: auto; }
    #overview-decision { height: auto; padding: 1 2; border-left: thick $warning; }
    #overview-next { height: auto; margin-top: 1; }
    .investigation-actions { height: auto; margin: 1 0; }
    .investigation-actions Button { width: auto; margin-right: 2; }
    #investigation-roles, #investigation-tasks { height: 1fr; min-height: 7; }
    #role-facts, #task-facts { height: auto; margin-top: 1; }
    .investigation-note { height: auto; color: $text-muted; margin: 1 0; }
    #comparison-decision { height: auto; color: $warning; text-style: bold; }
    #comparison-costs { height: auto; margin: 1 0; }
    #comparison-body { height: 1fr; }
    #comparison-pairs { width: 35; height: 1fr; }
    #pair-detail-scroll { width: 1fr; height: 1fr; padding: 0 1; border-left: solid $primary; }
    #comparison-switch { height: 3; }
    #comparison-switch Button { width: auto; margin-right: 1; }
    #comparison-switch Static { width: 1fr; padding: 1; color: $text-muted; }
    #comparison-boundary { height: auto; color: $text-muted; margin-bottom: 1; }
    #timeline-heading { height: auto; text-style: bold; margin-bottom: 1; }
    #comparison-timelines { height: auto; }
    #comparison-timelines Static { width: 1fr; height: auto; padding-right: 1; }
    #timeline-candidate { border-left: solid $surface; padding-left: 1; }
    #pair-detail { height: auto; margin-top: 1; color: $text-muted; }
    #comparison-empty { height: auto; padding: 1 0; }
    """
    BINDINGS = [
        Binding("1", "section('overview-tab')", "Overview", priority=True),
        Binding("2", "section('roles-tab')", "Roles", priority=True),
        Binding("3", "section('tasks-tab')", "Tasks", priority=True),
        Binding("4", "section('comparison-tab')", "Comparison", priority=True),
        Binding("c", "cycle_comparison", "Toggle comparison", priority=True),
        Binding("i", "inspect_comparison", "Inspect calls", priority=True),
        Binding("r", "refresh", "Refresh", priority=True),
        Binding("escape", "close", "Monitor", priority=True),
        Binding("q", "app.quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        store: SqliteTraceStore,
        context: InvestigationContext | None = None,
    ) -> None:
        super().__init__()
        self.store = store
        self.context = context or InvestigationContext()
        self.comparisons = self.context.comparisons or (
            (self.context.comparison,) if self.context.comparison else ()
        )
        self._comparison_mode = self.context.initial_comparison
        self._comparison_row = 0
        self.rows: list[dict] = []
        self.roles: list[tuple[object, list[dict]]] = []
        self.tasks: list[tuple[object, list[dict]]] = []
        self.selected_role = 0
        self.selected_task = 0

    def compose(self) -> ComposeResult:
        yield Static(self.context.title, id="investigation-title", markup=False)
        yield Static("", id="investigation-scope", markup=False)
        with TabbedContent(id="investigation-tabs"):
            with TabPane("1 Overview", id="overview-tab"):
                with VerticalScroll(id="overview-content"):
                    yield Label(
                        "What ran, what it cost, and what we learned",
                        classes="investigation-heading",
                    )
                    with Horizontal(id="overview-metrics"):
                        yield Static("", id="overview-cost", markup=False)
                        yield Static("", id="overview-outcomes", markup=False)
                    yield Static("", id="overview-decision", markup=False)
                    yield Static("", id="overview-next", markup=False)
            with TabPane("2 Roles", id="roles-tab"):
                yield Label(
                    "Where does the money go?", classes="investigation-heading"
                )
                yield DataTable(id="investigation-roles", cursor_type="row")
                yield Static("", id="role-facts", markup=False)
                with Horizontal(classes="investigation-actions"):
                    yield Button(
                        "Inspect selected role's calls", id="role-calls"
                    )
                    yield Button(
                        "Compare this role", id="role-comparison", disabled=True
                    )
            with TabPane("3 Tasks", id="tasks-tab"):
                yield Label(
                    "Did the complete task finish?",
                    classes="investigation-heading",
                )
                yield DataTable(id="investigation-tasks", cursor_type="row")
                yield Static("", id="task-facts", markup=False)
                with Horizontal(classes="investigation-actions"):
                    yield Button(
                        "Inspect selected task's calls", id="task-calls"
                    )
                yield Static(
                    "Completion is an application report. It does not establish factual quality.",
                    classes="investigation-note",
                    markup=False,
                )
            with TabPane("4 Comparison", id="comparison-tab"):
                if self.context.comparison is None:
                    yield Static(
                        "No replay comparison is attached to this investigation.\n\n"
                        "Return to the monitor to inspect existing experiment jobs or prepare a replay.\n"
                        "Recorded spend alone is not evidence that a cheaper model will work.",
                        id="comparison-empty",
                        markup=False,
                    )
                else:
                    if len(self.comparisons) > 1:
                        with Horizontal(id="comparison-switch"):
                            for index, comparison in enumerate(
                                self.comparisons
                            ):
                                yield Button(
                                    comparison.view_label,
                                    id=f"comparison-mode-{index}",
                                )
                            yield Static(
                                "c Toggle · Enter Inspect calls", markup=False
                            )
                    yield Static("", id="comparison-decision", markup=False)
                    yield Static("", id="comparison-boundary", markup=False)
                    yield Static("", id="comparison-costs", markup=False)
                    with Horizontal(id="comparison-body"):
                        yield DataTable(
                            id="comparison-pairs", cursor_type="row"
                        )
                        with VerticalScroll(id="pair-detail-scroll"):
                            yield Static(
                                "", id="timeline-heading", markup=False
                            )
                            with Horizontal(id="comparison-timelines"):
                                yield Static(
                                    "", id="timeline-baseline", markup=False
                                )
                                yield Static(
                                    "", id="timeline-candidate", markup=False
                                )
                            yield Static(
                                "Select a turn.", id="pair-detail", markup=False
                            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#investigation-roles", DataTable).add_columns(
            "Role", "Calls", "Known cost", "Share", "Avg latency", "Unpriced"
        )
        self.query_one("#investigation-tasks", DataTable).add_columns(
            "Task",
            "Completion",
            "Calls",
            "Known cost",
            "HTTP errors",
            "Reports",
        )
        comparison = self.active_comparison
        if comparison is not None:
            table = self.query_one("#comparison-pairs", DataTable)
            table.add_columns(
                "Stage",
                "Edition",
                "Δ score",
            )
            self._render_comparison()
        self.action_refresh()

    @property
    def active_comparison(self) -> ReplayEvidence | None:
        """The selected evidence boundary; overview findings stay independent."""
        return (
            self.comparisons[self._comparison_mode]
            if self.comparisons
            else None
        )

    def _render_comparison(self) -> None:
        comparison = self.active_comparison
        if comparison is None:
            return
        table = self.query_one("#comparison-pairs", DataTable)
        with self.prevent(DataTable.RowHighlighted):
            table.clear()
            for index, pair in enumerate(comparison.pairs):
                result = pair["result"]
                table.add_row(
                    *cells(
                        result["phase"].capitalize(),
                        comparison.edition_labels.get(
                            result["edition"], result["edition"]
                        ),
                        (
                            f"{result['diff']:+.2f}"
                            if "diff" in result
                            else "Not scored"
                        ),
                    ),
                    key=f"{self._comparison_mode}:{index}",
                )
            table.move_cursor(row=self._comparison_row)
        self.query_one("#comparison-decision", Static).update(
            f"{comparison.title} | {comparison.verdict} · {comparison.units} editions"
        )
        boundary = comparison.boundary
        if len(self.comparisons) > 1:
            boundary += " These are separate runs and scoring boundaries; compare models within each test."
        self.query_one("#comparison-boundary", Static).update(boundary)
        self.query_one("#comparison-costs", Static).update(
            f"{comparison.cost_label}: {comparison.baseline} {money(comparison.costs['baseline']['cost_usd'])} · "
            f"{comparison.candidate} {money(comparison.costs['candidate']['cost_usd'])} · "
            f"Judging {money(comparison.costs['judge']['cost_usd'])} (separate)"
        )
        for index in range(len(self.comparisons)):
            button = self.query(f"#comparison-mode-{index}")
            if button:
                button.first(Button).variant = (
                    "primary" if index == self._comparison_mode else "default"
                )
        self._comparison_detail()

    def _comparison_detail(self) -> None:
        comparison = self.active_comparison
        if comparison is None:
            return
        if not comparison.pairs:
            self.query_one("#timeline-heading", Static).update(
                "No paired calls in this comparison."
            )
            for identifier in (
                "timeline-baseline",
                "timeline-candidate",
                "pair-detail",
            ):
                self.query_one("#" + identifier, Static).update("")
            return
        result = comparison.pairs[self._comparison_row]["result"]
        edition = comparison.edition_labels.get(
            result["edition"], result["edition"]
        )
        self.query_one("#timeline-heading", Static).update(
            f"{label(edition)} · {label(result['phase']).capitalize()} · {comparison.view_label}"
        )
        for arm in ("baseline", "candidate"):
            self.query_one("#timeline-" + arm, Static).update(
                comparison.timeline(self._comparison_row, arm)
            )
        self.query_one("#pair-detail", Static).update(
            comparison.limitation
            + "\n\n"
            + comparison.score_scope
            + "\n\nNext: "
            + comparison.next_step
        )
        self.query_one("#pair-detail-scroll", VerticalScroll).scroll_home(
            animate=False
        )

    def _switch_comparison(self, index: int) -> None:
        """Carry the edition and stage across boundaries when available."""
        current = self.active_comparison
        selected = (
            current.pairs[self._comparison_row]["result"]
            if current and current.pairs
            else {}
        )
        self._comparison_mode = index
        pairs = self.comparisons[index].pairs
        self._comparison_row = next(
            (
                i
                for i, pair in enumerate(pairs)
                if pair["result"]["edition"] == selected.get("edition")
                and pair["result"]["phase"] == selected.get("phase")
            ),
            next(
                (
                    i
                    for i, pair in enumerate(pairs)
                    if pair["result"]["edition"] == selected.get("edition")
                ),
                0,
            ),
        )
        self._render_comparison()

    def action_cycle_comparison(self) -> None:
        self._switch_comparison(
            (self._comparison_mode + 1) % len(self.comparisons)
        )

    def action_inspect_comparison(self) -> None:
        comparison = self.active_comparison
        if comparison and comparison.pairs:
            result = comparison.pairs[self._comparison_row]["result"]
            edition = comparison.edition_labels.get(
                result["edition"], result["edition"]
            )
            rows = comparison.trace_rows(self._comparison_row)
            if rows:
                self.app.push_screen(
                    InvestigationCallsScreen(
                        f"{comparison.view_label} / {label(edition)} / {label(result['phase'])}",
                        rows,
                    )
                )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action == "inspect_comparison":
            return bool(
                self.active_comparison
                and self.active_comparison.pairs
                and self.query_one(TabbedContent).active == "comparison-tab"
            )
        if action == "cycle_comparison":
            return (
                len(self.comparisons) > 1
                and self.query_one(TabbedContent).active == "comparison-tab"
            )
        return True

    def action_refresh(self) -> None:
        cutoff = (
            time.time() if self.context.cutoff is None else self.context.cutoff
        )
        try:
            rows = self.store.investigation_calls(
                roles=self.context.roles,
                since=self.context.since,
                cutoff=cutoff,
                limit=_LIMIT + 1,
            )
        except sqlite3.Error:
            self.query_one("#investigation-scope", Static).update(
                "Could not refresh this investigation. Displayed data may be stale; press r to retry."
            )
            return
        partial = len(rows) > _LIMIT
        self.rows = rows[-_LIMIT:]
        stamp = time.strftime("%d %b %Y %H:%M UTC", time.gmtime(cutoff))
        scope = f"{self.context.scope} · observed through {stamp}"
        if partial:
            scope += f" · Latest {_LIMIT:,} calls only; totals are partial"
        self.query_one("#investigation-scope", Static).update(scope)
        self._fill_metrics()

    def _fill_metrics(self) -> None:
        role_key = self.roles[self.selected_role][0] if self.roles else None
        task_key = self.tasks[self.selected_task][0] if self.tasks else None
        facts = summarize(self.rows)
        self.roles = group_rows(self.rows, "use_case_key")
        self.tasks = group_rows(self.rows, "task_id")
        self.selected_role = next(
            (i for i, (key, _) in enumerate(self.roles) if key == role_key), 0
        )
        self.selected_task = next(
            (i for i, (key, _) in enumerate(self.tasks) if key == task_key), 0
        )
        self.query_one("#overview-cost", Static).update(
            f"{self.context.cost_label}\n{money(facts['known_cost'])}\n"
            f"{facts['calls']} calls · {len(self.roles)} roles\n{facts['unknown_cost']} calls with unknown cost"
        )
        self.query_one("#overview-outcomes", Static).update(
            f"Task completion\n{facts['completed']} completed · {facts['failed']} failed · {facts['missing']} not reported\n"
            f"{facts['tasks']} identified tasks · {facts['untasked']} calls without task identity\n"
            f"{facts['conflicting']} conflicting outcomes · {facts['duplicate_reports']} tasks with multiple reports"
        )
        comparison = self.context.comparison
        self.query_one("#overview-decision", Static).update(
            f"{comparison.decision}\n{comparison.verdict}: {len(comparison.pairs)} {comparison.sample_unit} from {comparison.units} independent editions.\n{comparison.limitation}"
            if comparison
            else "Start with a costly role, then inspect its tasks and calls.\n"
            "No model comparison is attached. Spend and completion alone do not establish quality or savings."
        )
        self.query_one("#overview-next", Static).update(
            "Next step\n" + comparison.next_step
            if comparison
            else "Select the Roles tab to see the evidence behind each role."
        )
        table = self.query_one("#investigation-roles", DataTable)
        with self.prevent(DataTable.RowHighlighted):
            table.clear()
            for index, (role, rows) in enumerate(self.roles):
                item = summarize(rows)
                share = (
                    item["known_cost"] / facts["known_cost"]
                    if facts["known_cost"]
                    else 0
                )
                table.add_row(
                    *cells(
                        role_name(role),
                        len(rows),
                        money(item["known_cost"]),
                        f"{share:.1%}",
                        f"{sum(row['latency_ms'] for row in rows) / len(rows) / 1000:.2f}s",
                        item["unknown_cost"],
                    ),
                    key=str(index),
                )
            table.move_cursor(row=self.selected_role)
        table = self.query_one("#investigation-tasks", DataTable)
        with self.prevent(DataTable.RowHighlighted):
            table.clear()
            for index, (task, rows) in enumerate(self.tasks):
                item = summarize(rows)
                table.add_row(
                    *cells(
                        self.context.task_labels.get(
                            task, task or "No task identity"
                        ),
                        completion(rows[0]),
                        len(rows),
                        money(item["known_cost"]),
                        item["errors"],
                        rows[0]["outcome_reports"],
                    ),
                    key=str(index),
                )
            table.move_cursor(row=self.selected_task)
        self._role_detail()
        self._task_detail()

    def _role_detail(self) -> None:
        if not self.roles:
            self.query_one("#role-comparison", Button).disabled = True
            self.query_one("#role-facts", Static).update(
                "No calls in this scope. Broaden the monitor's time window or record traffic."
            )
            return
        role, rows = self.roles[self.selected_role]
        comparison = self.context.comparison
        self.query_one("#role-comparison", Button).disabled = not (
            comparison and comparison.role == role
        )
        self.query_one("#role-facts", Static).update(
            f"Selected role: {label(role)}\n"
            f"Models: {', '.join(sorted({label(row['model'] or 'Unknown') for row in rows}))}\n"
            "Share uses known cost in this scope. Enter opens this role's recorded calls."
        )

    def _task_detail(self) -> None:
        if not self.tasks:
            self.query_one("#task-facts", Static).update(
                "No task evidence in this scope."
            )
            return
        task, rows = self.tasks[self.selected_task]
        facts = summarize(rows)
        self.query_one("#task-facts", Static).update(
            f"Selected task: {label(self.context.task_labels.get(task, task or 'No task identity'))} "
            f"({label(task or 'No task identity')})\n"
            f"{completion(rows[0])} · {facts['unknown_cost']} unpriced calls. "
            "Enter opens the chronological call sequence."
        )

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key.value is None:
            return
        if event.data_table.id == "comparison-pairs":
            mode, row = map(int, event.row_key.value.split(":"))
            comparison = self.active_comparison
            if (
                mode == self._comparison_mode
                and comparison
                and row < len(comparison.pairs)
            ):
                self._comparison_row = row
                self._comparison_detail()
            return
        index = int(event.row_key.value)
        if event.data_table.id == "investigation-roles" and index < len(
            self.roles
        ):
            self.selected_role = index
            self._role_detail()
        elif event.data_table.id == "investigation-tasks" and index < len(
            self.tasks
        ):
            self.selected_task = index
            self._task_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "comparison-pairs":
            self.action_inspect_comparison()
        elif event.data_table.id == "investigation-roles":
            self._open_calls("role")
        elif event.data_table.id == "investigation-tasks":
            self._open_calls("task")

    def _open_calls(self, kind: str) -> None:
        groups = self.roles if kind == "role" else self.tasks
        if groups:
            key, rows = groups[
                self.selected_role if kind == "role" else self.selected_task
            ]
            display_key = (
                self.context.task_labels.get(key, key)
                if kind == "task"
                else role_name(key)
            )
            self.app.push_screen(
                InvestigationCallsScreen(
                    f"{self.context.title} / {kind}: {label(display_key or 'Unattributed')}",
                    rows,
                )
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("comparison-mode-"):
            self._switch_comparison(int(event.button.id.rsplit("-", 1)[1]))
        elif event.button.id == "role-comparison":
            self.action_section("comparison-tab")
        elif event.button.id in ("role-calls", "task-calls"):
            self._open_calls(
                "role" if event.button.id == "role-calls" else "task"
            )

    def action_section(self, section: str) -> None:
        # Release the old pane before hiding it. Otherwise its focused table
        # can scroll itself back into view and reactivate the previous tab.
        self.set_focus(None)
        self.query_one(TabbedContent).active = section

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if self.query_one(TabbedContent).active != event.pane.id:
            return
        target = {
            "overview-tab": "overview-content",
            "roles-tab": "investigation-roles",
            "tasks-tab": "investigation-tasks",
            "comparison-tab": "comparison-pairs",
        }.get(event.pane.id)
        if target and self.query("#" + target):
            self.query_one("#" + target).focus()

    def action_close(self) -> None:
        self.dismiss(None)
