"""Console snapshot refresh, table filling, paging, and detail rendering."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import (
    DataTable,
)

from ctrlrtn.analysis.report import (
    _fmt_cost,
    _fmt_outcome,
)

_EMPTY = "No data yet. Start the gateway or queue an experiment job."


class TableFillMixin:
    """Populate console tables while preserving selection and feed state."""

    def _fill(self, table_id: str, rows: list[tuple[tuple, str]]) -> None:
        """Repopulate a table, keeping the tracked selection if its row still
        exists else falling back to the top row, so cursor and detail agree.
        The calls feed instead follows the top (newest) row while
        ``_follow_calls`` is on — and resumes following when a paused-on row
        scrolls out of the feed window. Off page 1 it never follows: an older
        page is being read, not tailed."""
        table = self.query_one(f"#{table_id}", DataTable)
        table.clear()
        for cells, key in rows:
            # rich Text cells render literally; a raw str would go through
            # DataTable's Text.from_markup, where a client-controlled value
            # (task id, tag, model) containing "[/..." raises MarkupError.
            table.add_row(*(Text(c) for c in cells), key=key)
        keys = [key for _, key in rows]
        if keys:
            tracked = self._selected[table_id]
            if (
                table_id == "calls"
                and self._page["calls"] == 0
                and (self._follow_calls or tracked not in keys)
            ):
                self._follow_calls = True
                chosen = keys[0]
            else:
                chosen = tracked if tracked in keys else keys[0]
            table.move_cursor(row=keys.index(chosen))
            self._selected[table_id] = chosen
        else:
            self._selected[table_id] = None

    def _fill_experiments(self) -> None:
        self._fill(
            "experiments",
            [
                (
                    (
                        e.experiment_id[:16],
                        e.scope.label[:16],
                        e.candidate_model[:22],
                        f"{e.split_pct}%",
                        "running" if e.is_running else "stopped",
                    ),
                    e.experiment_id,
                )
                for e in self._experiments
            ],
        )

    def _fill_shadows(self) -> None:
        self._fill(
            "shadows",
            [
                (
                    (
                        shadow.shadow_id[:16],
                        shadow.scope.label[:16],
                        shadow.candidate_model[:22],
                        f"{shadow.sample_pct}%",
                        "running" if shadow.is_running else "stopped",
                    ),
                    shadow.shadow_id,
                )
                for shadow in self._shadows
            ],
        )

    def _fill_jobs(self) -> None:
        self._fill(
            "jobs",
            [
                (
                    (
                        job.job_id[:16],
                        job.kind[:18],
                        (
                            f"{job.progress_current}/{job.progress_total}"
                            if job.progress_total is not None
                            else str(job.progress_current)
                        ),
                        job.status,
                    ),
                    job.job_id,
                )
                for job in self._jobs
            ],
        )

    def _fill_usecases(self) -> None:
        self._fill(
            "usecases",
            [
                (
                    (r.use_case[:22], str(r.calls), f"${r.cost_usd:.2f}"),
                    r.use_case,
                )
                for r in self._rankings
            ],
        )

    def _fill_tasks(self) -> None:
        self._fill(
            "tasks",
            [
                (
                    (
                        t.task_id[:16],
                        str(t.calls),
                        f"${t.cost_usd:.2f}",
                        _fmt_outcome(t.success, t.score),
                    ),
                    t.task_id,
                )
                for t in self._tasks
            ],
        )

    def _fill_workflows(self) -> None:
        self._fill(
            "workflows",
            [
                (
                    (
                        row["task_id"][:16],
                        f"{row['workflow']}@{row['workflow_version']}"[:24],
                        str(row["step_runs"]),
                        str(row["calls"]),
                        f"${row['cost_usd']:.2f}",
                    ),
                    row["task_id"],
                )
                for row in self._workflows
            ],
        )

    def _fill_discovered(self) -> None:
        self._fill(
            "discovered",
            [
                (
                    (
                        family.family_id.split(":")[-1][:16],
                        str(family.support),
                        str(family.variants),
                        f"{family.cohesion:.0%}",
                        f"{family.linked_task_rate:.0%}",
                    ),
                    family.family_id,
                )
                for family in self._discovered
            ],
        )

    def _fill_models(self) -> None:
        self._fill(
            "models",
            [
                (
                    (m.model[:24], str(m.calls), f"${m.cost_usd:.2f}"),
                    m.model,
                )
                for m in self._models
            ],
        )

    def _fill_calls(self) -> None:
        self._fill(
            "calls",
            [
                (
                    (
                        str(c["id"]),
                        (c["use_case_key"] or "(unkeyed)")[:14],
                        (c["model"] or "-")[:14],
                        str(c["status_code"]),
                        _fmt_cost(c["cost_usd"]),
                    ),
                    str(c["id"]),
                )
                for c in self._calls
            ],
        )

    # --- selection -> detail ------------------------------------------------
    # The detail pane follows whichever table is FOCUSED; refill-time highlight
    # events never reach the handler (prevented at post time in _reload).
