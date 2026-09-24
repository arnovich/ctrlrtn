"""Live TUI application and narrowly scoped experiment control plane.

Browse jobs, experiments, use-cases, tasks and a
live feed of the calls flowing through the gateway, drill into an experiment's
tripwire verdict — all auto-refreshing. Launch with ``ctrlrtn console``.

The long-lived monitor connection remains READ-ONLY. Explicit operator actions
open a short-lived writer, commit one control-plane change, and close it.
Textual is an optional dependency (the ``tui`` extra); the CLI imports this
module lazily so a core install stays light.
"""

from __future__ import annotations

import sqlite3
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Sparkline,
    Static,
)

from ctrlrtn.cli.tui.actions import ConsoleActions
from ctrlrtn.cli.tui.formatting import (
    command_panel_text,
)
from ctrlrtn.cli.tui.formatting import console_bindings as _console_bindings
from ctrlrtn.cli.tui.forms import (
    ConsoleUnavailable,
)
from ctrlrtn.cli.tui.models import (
    _DEFAULT_GRAPH_WINDOW,
    _DEFAULT_TABLE_WINDOW,
    _GRAPH_WINDOWS,
    _PAGED_TABLES,
    _TABLE_WINDOWS,
    GraphWindow,
    TableWindow,
)
from ctrlrtn.cli.tui.presentation import ConsolePresentation
from ctrlrtn.cli.tui.screens import (
    WindowPickerScreen,
)
from ctrlrtn.jobs import Job
from ctrlrtn.policy.budget import BudgetPolicy
from ctrlrtn.policy.experiment import (
    Experiment,
)
from ctrlrtn.policy.shadow import ShadowExperiment
from ctrlrtn.recorder.models import (
    ModelRanking,
    TaskSummary,
    UseCaseRanking,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.workflow.discovery import (
    DiscoveredWorkflowFamily,
    WorkflowDiscoveryReport,
)

_REFRESH_DEFAULT = 3.0
_EMPTY = "No data yet. Start the gateway or queue an experiment job."


class ConsoleApp(ConsoleActions, ConsolePresentation, App):
    """The monitor. Arrow-navigate the tables; Tab moves between them; the
    detail pane tracks the FOCUSED table's highlighted row and refreshes on a
    timer. All DB reads go through one read-only connection."""

    CSS = """
    #sidebar { width: 46; border-right: solid $primary; }
    .heading { text-style: bold; color: $accent; padding: 0 1; }
    DataTable { height: 1fr; }
    #empty-note { color: $text-muted; padding: 0 1; height: auto; }
    #graphs { height: auto; border-bottom: solid $primary; padding-bottom: 1; }
    #budget-status { height: auto; border-bottom: solid $primary; padding: 0 1; }
    #routing-status { height: auto; border-bottom: solid $primary; padding: 0 1; }
    #shadow-status { height: auto; border-bottom: solid $primary; padding: 0 1; }
    #experiment-actions { height: auto; color: $accent; padding: 0 1; }
    .graphlabel { color: $text-muted; padding: 0 1; }
    #graph-axis { color: $text-muted; padding: 0 1; height: 1; }
    Sparkline { height: 2; margin: 0 1; }
    #calls-graph > .sparkline--max-color { color: $success; }
    #calls-graph > .sparkline--min-color { color: $success-darken-3; }
    #cost-graph > .sparkline--max-color { color: $warning; }
    #cost-graph > .sparkline--min-color { color: $warning-darken-3; }
    #latency-graph > .sparkline--max-color { color: $error; }
    #latency-graph > .sparkline--min-color { color: $error-darken-3; }
    #tokens-graph > .sparkline--max-color { color: $accent; }
    #tokens-graph > .sparkline--min-color { color: $accent-darken-3; }
    #detail { padding: 1 2; }
    #detail-pane.expanded { border: round $accent; }
    .formhint { color: $text-muted; margin-top: 1; }
    .formmatches { color: $text-muted; display: none; }
    .formmatches.shown { display: block; }
    HeaderIcon { color: $success; }
    Header.stale HeaderIcon { color: $error; }
    #commands { dock: right; width: 34; padding: 1 2; display: none;
                background: $surface; border-left: solid $primary; }
    #commands.open { display: block; }
    """
    BINDINGS = _console_bindings()
    TITLE = "ctrlrtn console"

    def __init__(
        self,
        store: SqliteTraceStore,
        *,
        db_path: str = "",
        refresh_seconds: float = _REFRESH_DEFAULT,
        budget_policy: BudgetPolicy | None = None,
        kill_switch: bool = False,
        routing_config_path: str = "routing.yaml",
        routing_repo: str = ".",
        graph_window: GraphWindow = _DEFAULT_GRAPH_WINDOW,
        table_window: TableWindow = _DEFAULT_TABLE_WINDOW,
    ) -> None:
        super().__init__()
        self._store = store
        self._db_path = db_path
        self._refresh_seconds = refresh_seconds
        self._graph_window = graph_window
        self._table_window = table_window
        self._budget_policy = budget_policy or BudgetPolicy()
        self._kill_switch = kill_switch
        self._routing_config_path = routing_config_path
        self._routing_repo = routing_repo
        self._jobs: list[Job] = []
        self._experiments: list[Experiment] = []
        self._shadows: list[ShadowExperiment] = []
        self._rankings: list[UseCaseRanking] = []
        self._workflows: list[dict] = []
        self._discovered: list[DiscoveredWorkflowFamily] = []
        self._discovery_report: WorkflowDiscoveryReport | None = None
        self._discovery_job_id: str | None = None
        self._tasks: list[TaskSummary] = []
        self._calls: list[dict] = []
        self._models: list[ModelRanking] = []
        # Highlighted row key per table; the detail follows the FOCUSED table.
        self._selected: dict[str, str | None] = {
            "jobs": None,
            "experiments": None,
            "shadows": None,
            "usecases": None,
            "models": None,
            "workflows": None,
            "discovered": None,
            "tasks": None,
            "calls": None,
        }
        # 0-based page per unbounded list; a page survives refresh ticks, so
        # reading page 3 of the feed isn't yanked back to the tail every 3s.
        self._page = {table: 0 for table in _PAGED_TABLES}
        self._has_more = {table: False for table in _PAGED_TABLES}
        # The live call feed follows the newest row (tail -f). Moving the
        # cursor off the top row pauses following so a call can be inspected
        # while new ones arrive; returning to the top — or the inspected row
        # scrolling out of the feed window — resumes it.
        self._follow_calls = True
        # The table currently maximized (None = normal four-pane layout).
        self._maximized: str | None = None
        self._last_detail = _EMPTY  # mirror of the detail pane (for tests)
        self._last_notice = ""
        self._series: list[dict] = []  # last graph series (for resize redraws)
        self._last_update: float | None = None  # last SUCCESSFUL read
        self._healthy = True
        # The detail pane alone in the right-hand column (enter), and the table
        # whose row it is describing while focus sits elsewhere.
        self._detail_expanded = False
        self._budget_expanded = False
        self._budget_status = ""
        self._budget_summary = ""
        self._last_table = "jobs"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("Jobs", classes="heading", id="label-jobs")
                yield DataTable(id="jobs", cursor_type="row")
                yield Label(
                    "Experiments", classes="heading", id="label-experiments"
                )
                yield DataTable(id="experiments", cursor_type="row")
                yield Label("Shadows", classes="heading", id="label-shadows")
                yield DataTable(id="shadows", cursor_type="row")
                yield Label(
                    "Use-cases (by spend)",
                    classes="heading",
                    id="label-usecases",
                )
                yield DataTable(id="usecases", cursor_type="row")
                yield Label(
                    "Models (served)", classes="heading", id="label-models"
                )
                yield DataTable(id="models", cursor_type="row")
                yield Label(
                    "Workflows (recent)",
                    classes="heading",
                    id="label-workflows",
                )
                yield DataTable(id="workflows", cursor_type="row")
                yield Label(
                    "Discovered workflows",
                    classes="heading",
                    id="label-discovered",
                )
                yield DataTable(id="discovered", cursor_type="row")
                yield Label(
                    "Tasks (by cost)", classes="heading", id="label-tasks"
                )
                yield DataTable(id="tasks", cursor_type="row")
                yield Label("Calls (live)", classes="heading", id="label-calls")
                yield DataTable(id="calls", cursor_type="row")
                # One line standing in for every pane collapsed as empty.
                yield Static("", id="empty-note", markup=False)
            with Vertical():
                yield Static("", id="budget-status", markup=False)
                yield Static("", id="routing-status", markup=False)
                yield Static("", id="shadow-status", markup=False)
                yield Static(
                    "F2 Investigate · o Offline test · e Live A/B · h Shadow · a Adopt · ? Commands",
                    id="experiment-actions",
                    markup=False,
                )
                # Stacked full-width graphs: each one gets the whole pane's
                # columns, so twice the time resolution of a two-column split.
                with Vertical(id="graphs"):
                    yield Label(
                        "", classes="graphlabel", id="calls-graph-label"
                    )
                    yield Sparkline([], id="calls-graph")
                    yield Label("", classes="graphlabel", id="cost-graph-label")
                    yield Sparkline([], id="cost-graph")
                    yield Label(
                        "", classes="graphlabel", id="latency-graph-label"
                    )
                    yield Sparkline([], id="latency-graph")
                    yield Label(
                        "", classes="graphlabel", id="tokens-graph-label"
                    )
                    yield Sparkline([], id="tokens-graph")
                    # One axis for the stack: the four graphs share an x range.
                    yield Static("", id="graph-axis", markup=False)
                with VerticalScroll(id="detail-pane"):
                    # markup=False: the pane shows recorded bodies — arbitrary text
                    # where "[...]" must render literally, not parse as markup.
                    yield Static(_EMPTY, id="detail", markup=False)
            # Docked right, closed until `?`: the footer shows a legible few,
            # this shows the lot.
            with VerticalScroll(id="commands"):
                yield Label("Commands", classes="heading")
                yield Static(
                    command_panel_text(), id="commands-body", markup=False
                )
        yield Footer()

    def on_mount(self) -> None:
        self._update_header()
        self.query_one("#jobs", DataTable).add_columns(
            "id", "kind", "progress", "state"
        )
        self.query_one("#experiments", DataTable).add_columns(
            "id", "use-case", "candidate", "split", "state"
        )
        self.query_one("#shadows", DataTable).add_columns(
            "id", "use-case", "candidate", "sample", "state"
        )
        self.query_one("#usecases", DataTable).add_columns(
            "use-case", "calls", "cost"
        )
        self.query_one("#models", DataTable).add_columns(
            "model", "calls", "cost"
        )
        self.query_one("#workflows", DataTable).add_columns(
            "task", "workflow", "steps", "calls", "cost"
        )
        self.query_one("#discovered", DataTable).add_columns(
            "family", "tasks", "variants", "cohesion", "links"
        )
        self.query_one("#tasks", DataTable).add_columns(
            "task", "calls", "cost", "outcome"
        )
        self.query_one("#calls", DataTable).add_columns(
            "id", "use-case", "model", "st", "cost"
        )
        self._reload()
        self.set_interval(self._refresh_seconds, self._reload)

    # --- data ---------------------------------------------------------------

    def action_refresh(self) -> None:
        self._reload()

    def action_investigate(self) -> None:
        """Open a read-only investigation over the current table window."""
        from ctrlrtn.cli.tui.investigation import InvestigationScreen
        from ctrlrtn.cli.tui.investigation_data import InvestigationContext

        self.push_screen(
            InvestigationScreen(
                self._store,
                InvestigationContext(
                    scope=f"Recorded traffic · {self._table_window.span_text()}",
                    since=self._table_window.since(time.time()),
                ),
            )
        )

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        """Keep monitor control shortcuts inactive inside investigations."""
        from ctrlrtn.cli.tui.investigation import (
            InvestigationCallsScreen,
            InvestigationScreen,
        )

        if isinstance(
            self.screen, (InvestigationScreen, InvestigationCallsScreen)
        ):
            return action in {
                "quit",
                "help_quit",
                "copy_text",
                "focus_next",
                "focus_previous",
            }
        return True

    def _update_header(self) -> None:
        """Make the header say whether what you are looking at is live.

        The monitor refreshes on a timer, so a frozen screen and a healthy one
        look identical — the header used to advertise "refresh 3s" whether or
        not a refresh had worked since. Now it carries the clock time of the
        last SUCCESSFUL read (which ticks along by itself, so a stopped clock
        is the tell), and the header's icon — a decorative circle that only
        ever opened the command palette — becomes the status light: filled and
        green while reads are landing, red once one has failed and the screen
        is stale."""
        stamp = (
            time.strftime("%H:%M:%S", time.localtime(self._last_update))
            if self._last_update
            else "never"
        )
        state = f"updated {stamp}" if self._healthy else f"STALE since {stamp}"
        self.sub_title = (
            f"{self._db_path}  ·  {state}  ·  "
            f"refresh {self._refresh_seconds:g}s"
        )
        self.query_one(Header).set_class(not self._healthy, "stale")
        # HeaderIcon is Textual-private; reach it by type name rather than
        # import its module. `icon` is a reactive, so this redraws itself.
        icon = self.query_one("HeaderIcon")
        icon.icon = "●"  # type: ignore[attr-defined]
        icon.tooltip = (
            f"reading {self._db_path or 'the database'} · {state}"
            "\nclick for the command palette"
        )

    def action_toggle_commands(self) -> None:
        """Open or close the commands panel — the footer only has room for a
        few, and twenty truncated entries help nobody."""
        self.query_one("#commands").toggle_class("open")

    def action_graph_window(self) -> None:
        self.push_screen(
            WindowPickerScreen(
                title="Traffic graph window",
                prefix="graph-window",
                labels=tuple(w.label for w in _GRAPH_WINDOWS),
                current=self._graph_window.label,
                showing=f"Graphs show {self._graph_window.span_text()}.",
            ),
            self._set_graph_window,
        )

    def _set_graph_window(self, label: str | None) -> None:
        """Adopt a picked window and redraw at once, rather than leaving the
        old series up until the next refresh tick."""
        if label is None or label == self._graph_window.label:
            return
        self._graph_window = next(w for w in _GRAPH_WINDOWS if w.label == label)
        self._notice(
            f"Traffic graphs now show {self._graph_window.span_text()}."
        )
        self._reload()

    def action_table_window(self) -> None:
        self.push_screen(
            WindowPickerScreen(
                title="Use-case / model / task table window",
                prefix="table-window",
                labels=tuple(w.label for w in _TABLE_WINDOWS),
                current=self._table_window.label,
                showing=f"Tables cover {self._table_window.span_text()}.",
            ),
            self._set_table_window,
        )

    def _set_table_window(self, label: str | None) -> None:
        if label is None or label == self._table_window.label:
            return
        self._table_window = next(w for w in _TABLE_WINDOWS if w.label == label)
        self._notice(
            "Use-case, model and task tables now cover "
            f"{self._table_window.span_text()}."
        )
        self._reload()

    def _notice(self, message: str, *, error: bool = False) -> None:
        self._last_notice = message
        self.notify(message, severity="error" if error else "information")


def run_console(
    db_path: str,
    refresh_seconds: float = _REFRESH_DEFAULT,
    *,
    budget_policy: BudgetPolicy | None = None,
    kill_switch: bool = False,
    routing_config_path: str = "routing.yaml",
    routing_repo: str = ".",
) -> None:
    try:
        store = SqliteTraceStore(db_path, read_only=True)
    except sqlite3.OperationalError as exc:
        raise ConsoleUnavailable(
            f"no readable database at {db_path} — start the gateway "
            f"(`ctrlrtn serve`) first, or check db_path ({exc})."
        ) from None
    try:
        ConsoleApp(
            store,
            db_path=db_path,
            refresh_seconds=refresh_seconds,
            budget_policy=budget_policy,
            kill_switch=kill_switch,
            routing_config_path=routing_config_path,
            routing_repo=routing_repo,
        ).run()
    finally:
        store.close()
