"""Focused inspection of recorded call metadata without loading payloads."""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from ctrlrtn.cli.tui.investigation_data import label


def recorded(value: object) -> str:
    """Preserve missing facts and render captured labels literally."""
    return (
        "Not recorded"
        if value is None
        else label(Text.from_ansi(str(value)).plain)
    )


class RecordedTraceScreen(ModalScreen[int]):
    """Inspect one call and step through the same frozen selection."""

    CSS = """
    RecordedTraceScreen { background: $background; }
    #trace-title { height: auto; padding: 1 2; text-style: bold; color: $accent; }
    #trace-body { padding: 0 2; }
    #trace-summary { height: auto; padding: 1 2; border-left: thick $primary; }
    #trace-metadata { height: auto; margin: 1 0; }
    #trace-metadata Static { width: 1fr; height: auto; padding: 1 2; }
    #trace-response { border-left: solid $surface; }
    #trace-reference { height: auto; padding: 1 2; color: $text-muted; }
    #trace-note { height: auto; padding: 1 2; color: $text-muted; }
    """
    BINDINGS = [
        Binding("left", "previous", "Previous trace", priority=True),
        Binding("right", "next", "Next trace", priority=True),
        Binding("escape", "close", "Back to calls", priority=True),
        Binding("q", "app.quit", "Quit", priority=True),
    ]

    def __init__(self, title: str, rows: list[dict], index: int) -> None:
        super().__init__()
        self.context_title = title
        self.rows = rows
        self.index = index

    def compose(self) -> ComposeResult:
        yield Static("", id="trace-title", markup=False)
        with VerticalScroll(id="trace-body"):
            yield Static("", id="trace-summary", markup=False)
            with Horizontal(id="trace-metadata"):
                yield Static("", id="trace-request", markup=False)
                yield Static("", id="trace-response", markup=False)
            yield Static("", id="trace-reference", markup=False)
            yield Static(
                "Recorded metadata · request and response bodies are unavailable in this view.\n"
                "HTTP success, tool success and task completion are separate facts.",
                id="trace-note",
                markup=False,
            )
        yield Footer()

    def on_mount(self) -> None:
        self._render_trace()

    def _render_trace(self) -> None:
        row = self.rows[self.index]
        cost = row.get("cost_usd")
        price = "Unknown cost" if cost is None else f"${cost:.6f}"
        latency = row.get("latency_ms")
        duration = (
            "Duration not recorded"
            if latency is None
            else f"{latency / 1000:.3f}s"
        )
        timestamp = row.get("ts")
        when = (
            None
            if timestamp is None
            else time.strftime("%d %b %Y %H:%M:%S UTC", time.gmtime(timestamp))
        )
        self.query_one("#trace-title", Static).update(
            f"{self.context_title}\nTrace {self.index + 1} of {len(self.rows)} · {recorded(row.get('call_label', row.get('model')))}"
        )
        outcome = row.get("tool_outcome")
        error = "Tool error · " if row.get("tool_error") else ""
        self.query_one("#trace-summary", Static).update(
            f"{price}   ·   {duration}   ·   HTTP {recorded(row.get('status_code'))}\n"
            + error
            + (recorded(outcome) if outcome else "Tool outcome not recorded")
        )
        self.query_one("#trace-request", Static).update(
            "REQUEST METADATA\n\n"
            f"Model: {recorded(row.get('model'))}\n"
            f"Role: {recorded(row.get('use_case_key'))}\n"
            + (
                f"Task: {recorded(row['task'])}\n"
                if "task" in row
                else f"Task: {recorded(row.get('task_id'))}\n"
            )
            + f"Input tokens: {recorded(row.get('input_tokens'))}\n"
            f"Timestamp: {recorded(when)}"
        )
        self.query_one("#trace-response", Static).update(
            "RESPONSE METADATA\n\n"
            f"Output tokens: {recorded(row.get('output_tokens'))}\n"
            f"Stop reason: {recorded(row.get('stop_reason'))}\n"
            f"Router terminal reason: {recorded(row.get('terminal_reason'))}\n"
            f"Requested tools: {recorded(', '.join(row['tools']) if row.get('tools') else None)}\n"
            f"Tool outcome: {recorded(outcome)}"
        )
        reference = row.get("evidence_reference")
        self.query_one("#trace-reference", Static).update(
            f"Evidence: {recorded(reference)}\nDatabase trace ID: not included in this evidence export"
            if reference
            else f"Trace ID: {recorded(row.get('id'))} · recorded database snapshot"
        )
        self.query_one("#trace-body", VerticalScroll).scroll_home(animate=False)
        self.refresh_bindings()

    def action_previous(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._render_trace()

    def action_next(self) -> None:
        if self.index + 1 < len(self.rows):
            self.index += 1
            self._render_trace()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action == "previous":
            return self.index > 0
        if action == "next":
            return self.index + 1 < len(self.rows)
        return True

    def action_close(self) -> None:
        self.dismiss(self.index)
