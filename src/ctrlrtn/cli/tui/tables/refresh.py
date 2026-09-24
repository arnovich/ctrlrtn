"""Console snapshot refresh, table filling, paging, and detail rendering."""

from __future__ import annotations

import time

from rich.control import strip_control_codes
from rich.text import Text
from textual.widgets import (
    DataTable,
    Label,
    Sparkline,
    Static,
)

from ctrlrtn.analysis.report import (
    _fmt_cost,
    _fmt_outcome,
)
from ctrlrtn.cli.tui.formatting import (
    graph_axis,
    graph_label,
    page_heading,
)
from ctrlrtn.cli.tui.models import (
    _PAGE_SIZE,
    _PAGED_TABLES,
    _PANE_MAX_ROWS,
    _SCOPED_TABLE_HEADINGS,
    _SIDEBAR_PANES,
    _SIDEBAR_TABLES,
)
from ctrlrtn.cli.tui.state import (
    call_detail,
    experiment_detail,
    job_detail,
    load_state,
    model_detail,
    routing_status,
    shadow_detail,
    shadow_status,
    task_detail,
    usecase_detail,
)
from ctrlrtn.workflow.catalog import (
    build_workflow_catalog,
    render_workflow_catalog,
)
from ctrlrtn.workflow.discovery import (
    WorkflowDiscoveryReport,
    render_workflow_discovery,
)
from ctrlrtn.workflow.discovery_job import (
    WorkflowDiscoveryJobError,
    projections_from_workflow_discovery_artifact,
    report_from_workflow_discovery_artifact,
)
from ctrlrtn.workflow.discovery_projection import (
    render_discovered_family_projection,
)
from ctrlrtn.workflow.graph import (
    build_workflow_flow,
    render_workflow_flow,
    render_workflow_timeline,
)
from ctrlrtn.workflow.recommend import (
    build_workflow_recommendations,
    render_workflow_recommendations,
)

_EMPTY = "No data yet. Start the gateway or queue an experiment job."


class TableRefreshMixin:
    def _reload(self) -> None:
        # A read that fails (writer briefly holds a lock, DB mid-checkpoint,
        # an id that vanished) must not kill the monitor — show it, keep going.
        try:
            state = load_state(
                self._store,
                budget_policy=self._budget_policy,
                kill_switch=self._kill_switch,
                graph_window=self._graph_window,
                table_window=self._table_window,
                pages=self._page,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
            # Everything on screen is now older than it looks: say so in the
            # header as well as the detail pane, and leave the last good
            # timestamp standing as the age of what is still displayed.
            self._healthy = False
            self._update_header()
            self._show(f"could not read {self._db_path or 'database'}: {exc}")
            return
        self._last_update = time.time()
        self._healthy = True
        self._update_header()
        self._jobs = state["jobs"]
        self._has_more = state["has_more"]
        completed_discovery = state["discovery_job"]
        if (
            completed_discovery is not None
            and completed_discovery.result is not None
            and completed_discovery.job_id != self._discovery_job_id
        ):
            try:
                report = report_from_workflow_discovery_artifact(
                    completed_discovery.result
                )
            except WorkflowDiscoveryJobError:
                # The job detail renders the integrity error. Keep the last
                # valid discovery table instead of replacing it with bad data.
                pass
            else:
                self._discovery_job_id = completed_discovery.job_id
                self._discovery_report = report
                self._discovered = list(report.families)
        self._experiments = state["experiments"]
        self._shadows = state["shadows"]
        self._rankings = state["rankings"]
        self._workflows = state["workflows"]
        self._tasks = state["tasks"]
        self._calls = state["calls"]
        self._models = state["models"]
        self._budget_status = state["budget_status"]
        self._budget_summary = state["budget_summary"]
        self._update_budget_display()
        self.query_one("#routing-status", Static).update(
            routing_status(
                state["control_revision"],
                self._routing_config_path,
                self._routing_repo,
            )
        )
        self.query_one("#shadow-status", Static).update(
            shadow_status(state["shadows"], state["shadow_stats"])
        )
        self._update_graphs(state["series"])
        self._update_table_headings()
        # Suppress the RowHighlighted events add_row/move_cursor would post
        # (prevent() is post-time, so it works even though handlers only run
        # later on the message pump): _fill owns the selection during a refill,
        # and the detail renders exactly once per reload. A boolean guard reset
        # after the fills would NOT work — the events drain after it resets.
        with self.prevent(DataTable.RowHighlighted):
            self._fill_jobs()
            self._fill_experiments()
            self._fill_shadows()
            self._fill_usecases()
            self._fill_models()
            self._fill_workflows()
            self._fill_discovered()
            self._fill_tasks()
            self._fill_calls()
        # After the fills: the headings name the row range each page holds,
        # and panes that came back empty give their space up.
        self._update_paged_headings()
        self._collapse_empty_panes()
        self._render_detail()

    def action_page_forward(self) -> None:
        self._turn_page(1)

    def action_page_back(self) -> None:
        self._turn_page(-1)

    def _turn_page(self, step: int) -> None:
        """Page the focused list. Only the unbounded lists page; the rest are
        bounded by their own nature or by a window."""
        table = self._active_table()
        if table not in _PAGED_TABLES:
            self._notice(
                "Focus jobs, workflows or calls to page — "
                f"{_PAGE_SIZE} rows a page."
            )
            return
        page = self._page[table] + step
        if page < 0 or (step > 0 and not self._has_more[table]):
            return  # already at an end; silence beats a nagging notice
        self._page[table] = page
        # Paging off the newest page is an explicit "let me read this" — the
        # feed must stop scrolling under the cursor until we return to page 1.
        if table == "calls":
            self._follow_calls = page == 0
        self._reload()

    def _collapse_empty_panes(self) -> None:
        """Hide the lists that have nothing in them, naming them on one line
        instead. Ten panes each hold a heading, a column-header row and an
        equal share of the sidebar (`DataTable { height: 1fr }`), so on a small
        terminal the empty ones crowd out the two that have data. An empty pane
        also has nothing to act on — every action that needs one needs a
        selected row — so nothing becomes unreachable by hiding it. The FOCUSED
        pane always stays visible, however empty, so the cursor never lands on
        something invisible."""
        if self._maximized is not None:
            return  # maximize owns visibility; it shows exactly one table
        collapsed = []
        for table_id, name in _SIDEBAR_PANES.items():
            table = self.query_one(f"#{table_id}", DataTable)
            keep = table.row_count > 0 or table.has_focus
            table.display = keep
            self.query_one(f"#label-{table_id}", Label).display = keep
            if not keep:
                collapsed.append(name)
                continue
            # Share the sidebar in proportion to how much each list has to
            # show, instead of ten equal slices. Fractions rather than fixed
            # rows because they always sum to the space available: content
            # heights overflow the moment several lists are busy, and the feed
            # — last in the column — is what falls off the bottom. Short lists
            # are additionally capped at their own content so they never hold
            # blank rows; the feed is uncapped and soaks up the remainder.
            table.styles.height = (
                f"{min(table.row_count + 1, _PANE_MAX_ROWS)}fr"
            )
            # +2, not +1: the column header takes one row and a horizontal
            # scrollbar (wide cells, narrow sidebar) takes another, and a cap
            # tight enough to hide the highlighted row would be worse than a
            # blank line.
            table.styles.max_height = (
                None if table_id == "calls" else table.row_count + 2
            )
        note = self.query_one("#empty-note", Static)
        note.update(f"empty · {' · '.join(collapsed)}" if collapsed else "")
        note.display = bool(collapsed)

    def _update_paged_headings(self) -> None:
        for table, title in _PAGED_TABLES.items():
            rows = self.query_one(f"#{table}", DataTable).row_count
            self.query_one(f"#label-{table}", Label).update(
                page_heading(
                    title, self._page[table], rows, self._has_more[table]
                )
            )

    def _update_table_headings(self) -> None:
        """Name the span each traffic table covers, so an empty use-case list
        reads as "nothing in the last 10m" and not "nothing ever"."""
        span = self._table_window.span_text()
        for widget_id, title in _SCOPED_TABLE_HEADINGS.items():
            self.query_one(f"#{widget_id}", Label).update(f"{title} · {span}")

    def _update_graphs(self, series: list[dict]) -> None:
        """Feed the time buckets to the four sparklines and put the window
        summaries in their labels."""
        calls = [float(b["calls"]) for b in series]
        costs = [b["cost_usd"] for b in series]
        latency = [b["latency_ms"] for b in series]
        tokens = [float(b["tokens"]) for b in series]
        self.query_one("#calls-graph", Sparkline).data = calls
        self.query_one("#cost-graph", Sparkline).data = costs
        self.query_one("#latency-graph", Sparkline).data = latency
        self.query_one("#tokens-graph", Sparkline).data = tokens
        window = self._graph_window.span_text()
        n_calls = sum(calls)
        # Weight each bucket's average by its call count for the window avg.
        avg_ms = (
            sum(ms * n for ms, n in zip(latency, calls)) / n_calls
            if n_calls
            else 0.0
        )
        # The bucket width is the gap between two buckets — no need to thread
        # the resolved grid down here, and it is right even for "all".
        bucket = (
            int(series[1]["ts"] - series[0]["ts"]) if len(series) > 1 else 1
        )
        self.query_one("#calls-graph-label", Label).update(
            graph_label(
                "calls",
                window,
                f"{int(n_calls)} total",
                str(int(max(calls, default=0))),
                bucket,
            )
        )
        self.query_one("#cost-graph-label", Label).update(
            graph_label(
                "cost",
                window,
                f"{_fmt_cost(sum(costs))} total",
                _fmt_cost(max(costs, default=0.0)),
                bucket,
            )
        )
        self.query_one("#latency-graph-label", Label).update(
            graph_label(
                "latency",
                window,
                f"avg {avg_ms:.0f}ms",
                f"{max(latency, default=0.0):.0f}ms",
                bucket,
            )
        )
        self.query_one("#tokens-graph-label", Label).update(
            graph_label(
                "tokens",
                window,
                f"{int(sum(tokens))} total",
                str(int(max(tokens, default=0))),
                bucket,
            )
        )
        self._series = series
        # After the repaint, not during it: on the first reload the pane has
        # no width yet, and an axis is laid out in characters.
        self.call_after_refresh(self._update_graph_axis)

    def _update_graph_axis(self) -> None:
        """Redraw the shared time axis at its current width. content_size, not
        size: the padding that lines the axis up with the bars above it is not
        room to draw in, and measuring the outer box clipped "now" to "n"."""
        axis = self.query_one("#graph-axis", Static)
        axis.update(graph_axis(self._series, axis.content_size.width))

    def on_resize(self, event: object) -> None:
        # The axis is laid out in characters, so it has to be rebuilt when the
        # terminal changes width rather than wait for the next refresh tick.
        if self._series:
            self._update_graph_axis()
