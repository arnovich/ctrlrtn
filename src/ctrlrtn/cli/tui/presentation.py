"""Detail-pane and maximize-layout interaction for the TUI."""

from __future__ import annotations

from textual.widgets import (
    DataTable,
    Label,
    Static,
)

from ctrlrtn.cli.tui.models import (
    _SIDEBAR_TABLES,
)
from ctrlrtn.cli.tui.tables import ConsoleTables


class ConsolePresentation(ConsoleTables):
    def action_toggle_budget(self) -> None:
        """Expand live budget diagnostics without changing the policy."""
        self._budget_expanded = not self._budget_expanded
        if self._detail_expanded:
            self._set_detail_expanded(False)
        self._update_budget_display()

    def _update_budget_display(self) -> None:
        self.query_one("#budget-status", Static).update(
            self._budget_status + "\nb compact view"
            if self._budget_expanded
            else self._budget_summary
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens it: the detail takes the whole right-hand
        column. Unlike `m` this leaves the sidebar alone — you are reading one
        record, not browsing a list."""
        self.action_expand_detail()

    def action_expand_detail(self) -> None:
        self._set_detail_expanded(True)

    def action_scroll_detail(self, direction: int) -> None:
        """Page the detail from wherever focus is. While a list has focus the
        page keys belong to IT — DataTable binds pageup/pagedown to move its
        own cursor — so without these, reading a long record meant expanding
        it first (enter) or tabbing to the scroller."""
        pane = self.query_one("#detail-pane")
        if direction > 0:
            pane.scroll_page_down()
        else:
            pane.scroll_page_up()

    def action_restore_layout(self) -> None:
        """Escape backs out of whichever layout you are in, innermost first."""
        if self._detail_expanded:
            self._set_detail_expanded(False)
        elif self._maximized:
            self._set_maximized(None)

    def _set_detail_expanded(self, expanded: bool) -> None:
        """Give the detail the whole right-hand column by hiding what sits
        above it — the three status lines and the graph stack — and focus it,
        so the arrows, page keys and home/end scroll the record itself."""
        self._detail_expanded = expanded
        for widget_id in (
            "#budget-status",
            "#routing-status",
            "#shadow-status",
        ):
            self.query_one(widget_id).display = not expanded
        # The graphs also yield to a maximized TABLE, so restoring here must
        # not bring them back over the top of that mode.
        self.query_one("#graphs").display = (
            not expanded and self._maximized is None
        )
        pane = self.query_one("#detail-pane")
        pane.set_class(expanded, "expanded")
        pane.border_title = "esc to go back" if expanded else ""
        if expanded:
            pane.focus()
        else:
            # Back to the list the record came from.
            self.query_one(f"#{self._active_table()}", DataTable).focus()

    def action_toggle_maximize(self) -> None:
        """Toggle the focused table between the four-pane layout and a
        maximized view (`m`): the table alone fills the left half, the detail
        pane keeps the right half and keeps tracking the selection. NOT
        Textual's Screen.maximize — that shows only the one widget, losing the
        detail. All widgets stay mounted (just hidden), so refresh, follow mode
        and selection-driven detail keep working unchanged."""
        self._set_maximized(None if self._maximized else self._active_table())

    def _set_maximized(self, table_id: str | None) -> None:
        self._maximized = table_id
        for tid in _SIDEBAR_TABLES:
            visible = table_id is None or tid == table_id
            self.query_one(f"#label-{tid}", Label).display = visible
            self.query_one(f"#{tid}", DataTable).display = visible
        # The collapsed-pane line belongs to the four-pane layout only.
        self.query_one("#empty-note", Static).display = False
        # Give the lone table half the screen; back to the slim sidebar else.
        self.query_one("#sidebar").styles.width = "50%" if table_id else 46
        if table_id:
            # The maximized table is the whole point of the mode: uncap it.
            table = self.query_one(f"#{table_id}", DataTable)
            table.styles.max_height = None
            table.styles.height = "1fr"
        # Maximized = inspection mode: the graphs yield their rows to the
        # detail pane and come back with the four-pane layout — unless the
        # detail is expanded, which hides them for its own reasons.
        self.query_one("#graphs").display = (
            table_id is None and not self._detail_expanded
        )
        if table_id:
            self.query_one(f"#{table_id}", DataTable).focus()
        else:
            self._collapse_empty_panes()  # restore the compacted sidebar
