"""Console snapshot refresh, table filling, paging, and detail rendering."""

from __future__ import annotations

from rich.control import strip_control_codes
from textual.widgets import (
    DataTable,
    Static,
)

from ctrlrtn.cli.tui.models import (
    _SIDEBAR_TABLES,
)
from ctrlrtn.cli.tui.state import (
    call_detail,
    experiment_detail,
    job_detail,
    model_detail,
    shadow_detail,
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


class TableDetailMixin:
    """Track active selections and render the focused table detail."""

    def _active_table(self) -> str:
        if self._maximized:
            return self._maximized  # the lone visible table owns the detail
        for table_id in _SIDEBAR_TABLES:
            if (
                self.query_one(f"#{table_id}", DataTable).has_focus
                and self._selected[table_id] is not None
            ):
                self._last_table = table_id
                return table_id
        # No table has focus — the detail pane itself does, say, while it is
        # being scrolled. Keep describing the row that put it there rather than
        # jumping to whichever list happens to come first.
        if self._selected.get(self._last_table) is not None:
            return self._last_table
        return next(
            (key for key, selected in self._selected.items() if selected),
            "jobs",
        )

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        # Only user moves arrive here — refill-time events are prevent()ed.
        table_id = event.data_table.id
        if table_id not in self._selected:
            return
        if event.row_key is None:
            # Moving the cursor in an EMPTY table posts a highlight with no row
            # at all (cursor_row=-1). Nothing is selected then, and the feed
            # keeps whatever follow state it had — with no rows there is no
            # "top row" to be on or off.
            self._selected[table_id] = None
            self._render_detail()
            return
        self._selected[table_id] = event.row_key.value
        if table_id == "calls":
            # On the top (newest) row of the newest page: follow the feed;
            # anywhere else: pause so the inspected call holds still while
            # new ones arrive. By position, not key, so a stale key can't
            # mis-toggle it.
            self._follow_calls = (
                event.cursor_row == 0 and self._page["calls"] == 0
            )
        self._render_detail()

    def on_descendant_focus(self) -> None:
        self._render_detail()  # focus moved to another table -> retarget detail

    def _render_detail(self) -> None:
        try:
            text = self._detail_text()
        except Exception as exc:
            text = f"could not render detail: {exc}"
        self._show(text)

    def _show(self, text: str) -> None:
        # Bodies are untrusted bytes; drop control codes so a recorded ANSI
        # escape can't garble the terminal.
        text = strip_control_codes(text)
        self._last_detail = text
        self.query_one("#detail", Static).update(text)

    def _detail_text(self) -> str:
        table_id = self._active_table()
        key = self._selected[table_id]
        if key is None:
            return _EMPTY
        if table_id == "jobs":
            job = next((job for job in self._jobs if job.job_id == key), None)
            return (
                job_detail(
                    job, self._store.workflow_discovery_invalidation(job.job_id)
                )
                if job
                else f"job {key} is no longer listed."
            )
        if table_id == "experiments":
            exp = next(
                (e for e in self._experiments if e.experiment_id == key), None
            )
            if exp is None:
                return f"experiment {key} is no longer listed."
            return experiment_detail(self._store, exp)
        if table_id == "shadows":
            shadow = next(
                (s for s in self._shadows if s.shadow_id == key), None
            )
            if shadow is None:
                return f"shadow {key} is no longer listed."
            return shadow_detail(self._store, shadow)
        if table_id == "usecases":
            return usecase_detail(
                self._store, self._rankings, self._experiments, key
            )
        if table_id == "models":
            return model_detail(self._models, key)
        if table_id == "workflows":
            graph = self._store.workflow_graph(key)
            if graph is None:
                return f"workflow task {key} is no longer listed."
            recommendations = build_workflow_recommendations(
                self._store.workflow_step_metrics(
                    graph.workflow, graph.workflow_version
                ),
                [
                    definition
                    for definition in self._store.workflow_definitions()
                    if definition.workflow == graph.workflow
                    and definition.workflow_version == graph.workflow_version
                ],
                self._store.workflow_graphs(
                    graph.workflow, graph.workflow_version, 1000
                ),
            )
            aggregate = build_workflow_flow(
                self._store.workflow_graphs(
                    graph.workflow, graph.workflow_version, 1000
                )
            )
            aggregate_text = (
                render_workflow_flow(aggregate)
                if aggregate is not None
                else "No compatible aggregate flow."
            )
            catalog_text = render_workflow_catalog(
                build_workflow_catalog(self._store, graph.workflow)
            )
            return (
                render_workflow_timeline(graph)
                + "\n\n"
                + aggregate_text
                + "\n\n"
                + catalog_text
                + "\n\n"
                + render_workflow_recommendations(recommendations)
            )
        if table_id == "discovered":
            family = next(
                (item for item in self._discovered if item.family_id == key),
                None,
            )
            if family is None:
                return "discovered workflow family is no longer listed"
            summary = render_workflow_discovery(
                WorkflowDiscoveryReport(
                    total_tasks=family.support,
                    eligible_tasks=family.support,
                    families=(family,),
                    unclustered_tasks=0,
                    ambiguous_tool_links=0,
                )
            )
            job = next(
                (
                    item
                    for item in self._jobs
                    if item.job_id == self._discovery_job_id
                    and item.result is not None
                ),
                None,
            )
            if job is None:
                return summary
            try:
                projection = next(
                    (
                        item
                        for item in projections_from_workflow_discovery_artifact(
                            job.result
                        )
                        if item.family_id == family.family_id
                    ),
                    None,
                )
            except WorkflowDiscoveryJobError:
                return summary
            return (
                summary
                if projection is None
                else summary
                + "\n\n"
                + render_discovered_family_projection(projection)
            )
        if table_id == "calls":
            return call_detail(self._store, key)
        return task_detail(self._tasks, key)
