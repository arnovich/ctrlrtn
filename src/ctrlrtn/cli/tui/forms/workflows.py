"""Keyboard-friendly modal forms used by the Textual console."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import (
    Button,
    Input,
    Label,
    Static,
)

from ctrlrtn.config import load_settings
from ctrlrtn.control.service import (
    provider_error,
)
from ctrlrtn.eval.live import DEFAULT_JUDGE_MODEL
from ctrlrtn.policy.experiment import (
    DEFAULT_MAX_CALLS_PER_TASK,
    DEFAULT_MAX_COST_USD_PER_TASK,
)

from .base import KeyboardForm, form_hint


class WorkflowIdentifyScreen(KeyboardForm, ModalScreen[dict | None]):
    """Name one reviewed family and choose an immutable proposal path."""

    CSS = """
    WorkflowIdentifyScreen { align: center middle; }
    #identify-dialog { width: 78; height: auto; border: thick $primary;
                       background: $surface; padding: 1 2; }
    #identify-dialog Input { margin-bottom: 1; }
    #identify-buttons { height: auto; align-horizontal: right; }
    #identify-buttons Button { margin-left: 1; }
    """

    def __init__(self, family_id: str) -> None:
        super().__init__()
        self.family_id = family_id

    def compose(self) -> ComposeResult:
        with Vertical(id="identify-dialog"):
            yield Label(
                "Create inert workflow identification", classes="heading"
            )
            yield Static(f"family: {self.family_id}", markup=False)
            yield Label("Stable workflow name")
            yield Input(placeholder="newsroom", id="identify-workflow")
            yield Label("Workflow version")
            yield Input(placeholder="git:revision", id="identify-version")
            yield Label("New proposal JSON path (must not exist)")
            yield Input(
                f"workflow-identification-{self.family_id.split(':')[-1]}.json",
                id="identify-output",
            )
            with Horizontal(id="identify-buttons"):
                yield Button(
                    "Create proposal", variant="primary", id="identify-create"
                )
            yield form_hint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        values = {
            "workflow": self.query_one(
                "#identify-workflow", Input
            ).value.strip(),
            "workflow_version": self.query_one(
                "#identify-version", Input
            ).value.strip(),
            "output": self.query_one("#identify-output", Input).value.strip(),
        }
        if not all(values.values()):
            self.notify(
                "Workflow, version, and output are required.", severity="error"
            )
            return
        self.dismiss(values)


class WorkflowProposalVerifyScreen(KeyboardForm, ModalScreen[str | None]):
    CSS = """
    WorkflowProposalVerifyScreen { align: center middle; }
    #proposal-verify-dialog { width: 78; height: auto; border: thick $primary;
                              background: $surface; padding: 1 2; }
    #proposal-verify-buttons { height: auto; align-horizontal: right; }
    #proposal-verify-buttons Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="proposal-verify-dialog"):
            yield Label(
                "Verify workflow identification proposal", classes="heading"
            )
            yield Input(id="proposal-verify-path")
            with Horizontal(id="proposal-verify-buttons"):
                yield Button(
                    "Verify", variant="primary", id="proposal-verify-run"
                )
            yield form_hint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        path = self.query_one("#proposal-verify-path", Input).value.strip()
        if not path:
            self.notify("Proposal path is required.", severity="error")
            return
        self.dismiss(path)


class WorkflowDiscoveryScopeScreen(KeyboardForm, ModalScreen[dict | None]):
    """Queue a durable discovery over one explicit task-cohort scope."""

    CSS = """
    WorkflowDiscoveryScopeScreen { align: center middle; }
    #discovery-scope-dialog { width: 78; height: auto; border: thick $primary;
                              background: $surface; padding: 1 2; }
    #discovery-scope-dialog Input { margin-bottom: 1; }
    #discovery-scope-buttons { height: auto; align-horizontal: right; }
    #discovery-scope-buttons Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="discovery-scope-dialog"):
            yield Label("Scoped passive workflow discovery", classes="heading")
            yield Static(
                "All values are optional. Times are Unix timestamps; "
                "arm requires experiment.",
                markup=False,
            )
            for label, field in (
                ("Since", "since"),
                ("Until", "until"),
                ("Provider", "provider"),
                ("Served model", "model"),
                ("Experiment ID", "experiment"),
                ("Arm (baseline/candidate)", "arm"),
            ):
                yield Label(label)
                yield Input(id=f"discovery-scope-{field}")
            with Horizontal(id="discovery-scope-buttons"):
                yield Button(
                    "Queue", variant="primary", id="discovery-scope-queue"
                )
            yield form_hint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        values = {
            field: self.query_one(
                f"#discovery-scope-{field}", Input
            ).value.strip()
            for field in (
                "since",
                "until",
                "provider",
                "model",
                "experiment",
                "arm",
            )
        }
        try:
            for field in ("since", "until"):
                values[field] = float(values[field]) if values[field] else None
        except ValueError:
            self.notify(
                "Since and until must be Unix timestamps.", severity="error"
            )
            return
        self.dismiss(values)
