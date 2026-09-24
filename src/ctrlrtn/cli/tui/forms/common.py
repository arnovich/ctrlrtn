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


class ConfirmScreen(KeyboardForm, ModalScreen[bool]):
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-dialog { width: 72; height: auto; border: thick $warning;
                      background: $surface; padding: 1 2; }
    #confirm-buttons { height: auto; align-horizontal: right; }
    #confirm-buttons Button { margin-left: 1; }
    """

    def __init__(self, message: str, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.message = message
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self.message, markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Back", id="confirm-no")
                yield Button(
                    self.confirm_label, variant="warning", id="confirm-yes"
                )

    def action_close(self) -> None:
        self.dismiss(False)  # escape on a confirmation means "no"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class GitConfigScreen(KeyboardForm, ModalScreen[bool]):
    """Scrollable active-versus-desired preview before local activation."""

    CSS = """
    GitConfigScreen { align: center middle; }
    #git-dialog { width: 100; height: 90%; border: thick $warning;
                  background: $surface; padding: 1 2; }
    #git-diff-body { height: 1fr; }
    #git-buttons { height: auto; align-horizontal: right; }
    #git-buttons Button { margin-left: 1; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="git-dialog"):
            yield Label(
                "Activate committed routing configuration", classes="heading"
            )
            with VerticalScroll(id="git-diff-body"):
                yield Static(self.message, markup=False)
            with Horizontal(id="git-buttons"):
                yield Button("Back", id="git-no")
                yield Button(
                    "Activate revision", variant="warning", id="git-yes"
                )

    def action_close(self) -> None:
        self.dismiss(False)  # escape leaves the revision unactivated

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "git-yes")
