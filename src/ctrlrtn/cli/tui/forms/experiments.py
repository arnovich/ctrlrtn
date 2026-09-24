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

from .base import (
    KeyboardForm,
    _provider_exists,
    field_matches,
    form_hint,
)


class OfflineExperimentScreen(KeyboardForm, ModalScreen[dict | None]):
    """Collect an offline replay definition; no database write occurs here."""

    CSS = """
    OfflineExperimentScreen { align: center middle; }
    #offline-dialog { width: 72; height: auto; max-height: 90%;
                      overflow-y: auto; border: thick $primary;
                      background: $surface; padding: 1 2; }
    /* Three fields to a row: Input defaults to width 100%, so without this
       the first one takes the row and the other two sit off-screen. */
    #offline-dialog Horizontal { height: auto; }
    #offline-dialog Horizontal Input { width: 1fr; margin-right: 1; }
    #offline-dialog Input { margin-bottom: 1; }
    #offline-buttons { height: auto; align-horizontal: right; }
    #offline-buttons Button { margin-left: 1; }
    """

    def __init__(
        self,
        use_case: str = "",
        *,
        use_cases: tuple[str, ...] = (),
        models: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.use_case = use_case
        self.use_cases = use_cases
        self.models = models

    def compose(self) -> ComposeResult:
        with Vertical(id="offline-dialog"):
            yield Label("Queue offline replay experiment", classes="heading")
            yield Label("Use-case")
            yield Input(
                self.use_case,
                id="offline-use-case",
                suggester=SuggestFromList(self.use_cases, case_sensitive=False),
            )
            yield Static(
                "", id="offline-use-case-matches", classes="formmatches"
            )
            yield Label("Candidate model")
            yield Input(
                placeholder="candidate model",
                id="offline-candidate",
                suggester=SuggestFromList(self.models, case_sensitive=False),
            )
            yield Static(
                "", id="offline-candidate-matches", classes="formmatches"
            )
            yield Label("Baseline model (blank = infer)")
            yield Input(
                id="offline-baseline",
                suggester=SuggestFromList(self.models, case_sensitive=False),
            )
            yield Static(
                "", id="offline-baseline-matches", classes="formmatches"
            )
            yield Label("Sample limit · margin · judge replicates")
            with Horizontal():
                yield Input("50", type="integer", id="offline-limit")
                yield Input("1.0", type="number", id="offline-margin")
                yield Input("2", type="integer", id="offline-replicates")
            yield Label("Judge model")
            yield Input(DEFAULT_JUDGE_MODEL, id="offline-judge")
            yield Label("Max output tokens (optional)")
            yield Input(type="integer", id="offline-max-tokens")
            yield Label("Optional exact scope: workflow · version · step")
            with Horizontal():
                yield Input(id="offline-workflow")
                yield Input(id="offline-workflow-version")
                yield Input(id="offline-step")
            with Horizontal(id="offline-buttons"):
                yield Button(
                    "Review replay", variant="primary", id="offline-review"
                )
            yield form_hint()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Keep each type-ahead field's match line current as it is typed."""
        options = {
            "offline-use-case": self.use_cases,
            "offline-candidate": self.models,
            "offline-baseline": self.models,
        }.get(event.input.id or "")
        if options is None:
            return
        matches = self.query_one(f"#{event.input.id}-matches", Static)
        text = field_matches(event.value, options)
        matches.update(text)
        matches.set_class(bool(text), "shown")  # no blank row without a hint

    def on_button_pressed(self, event: Button.Pressed) -> None:
        value = lambda selector: self.query_one(selector, Input).value.strip()
        try:
            result = {
                "use_case": value("#offline-use-case"),
                "candidate_model": value("#offline-candidate"),
                "baseline_model": value("#offline-baseline") or None,
                "limit": int(value("#offline-limit")),
                "margin": float(value("#offline-margin")),
                "replicates": int(value("#offline-replicates")),
                "judge_model": value("#offline-judge"),
                "max_tokens": (
                    int(value("#offline-max-tokens"))
                    if value("#offline-max-tokens")
                    else None
                ),
                "workflow": value("#offline-workflow") or None,
                "workflow_version": value("#offline-workflow-version") or None,
                "step": value("#offline-step") or None,
            }
        except ValueError:
            self.notify(
                "Limit, margin, replicates and max tokens must be numbers.",
                severity="error",
            )
            return
        self.dismiss(result)


class LiveExperimentScreen(KeyboardForm, ModalScreen[dict | None]):
    """Collect a live traffic-split definition; writing waits for review."""

    CSS = """
    LiveExperimentScreen { align: center middle; }
    #live-dialog { width: 72; height: auto; max-height: 90%;
                   overflow-y: auto; border: thick $primary;
                   background: $surface; padding: 1 2; }
    #live-dialog Input { margin-bottom: 1; }
    #live-dialog Horizontal { height: auto; }
    #live-dialog Horizontal Input { width: 1fr; margin-right: 1; }
    #live-buttons { height: auto; align-horizontal: right; }
    #live-buttons Button { margin-left: 1; }
    """

    def __init__(self, use_case: str = "") -> None:
        super().__init__()
        self.use_case = use_case

    def compose(self) -> ComposeResult:
        with Vertical(id="live-dialog"):
            yield Label(
                "Start live traffic-split experiment", classes="heading"
            )
            yield Label("Use-case")
            yield Input(self.use_case, id="live-use-case")
            yield Label("Candidate model")
            yield Input(placeholder="candidate model", id="live-candidate")
            yield Label("Named provider (optional)")
            yield Input(id="live-provider")
            yield Label("Candidate split % · max calls/task · max cost/task")
            with Horizontal():
                yield Input("50", type="integer", id="live-split")
                yield Input(
                    str(DEFAULT_MAX_CALLS_PER_TASK),
                    type="integer",
                    id="live-max-calls",
                )
                yield Input(
                    str(DEFAULT_MAX_COST_USD_PER_TASK),
                    type="number",
                    id="live-max-cost",
                )
            yield Label("Optional scope: workflow · version · optional step")
            with Horizontal():
                yield Input(id="live-workflow")
                yield Input(id="live-workflow-version")
                yield Input(id="live-step")
            with Horizontal(id="live-buttons"):
                yield Button(
                    "Review split", variant="primary", id="live-review"
                )
            yield form_hint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        value = lambda selector: self.query_one(selector, Input).value.strip()
        try:
            result = {
                "use_case_key": value("#live-use-case"),
                "candidate_model": value("#live-candidate"),
                "candidate_provider": value("#live-provider") or None,
                "split_pct": int(value("#live-split")),
                "max_calls_per_task": int(value("#live-max-calls")),
                "max_cost_usd_per_task": float(value("#live-max-cost")),
                "workflow": value("#live-workflow") or None,
                "workflow_version": value("#live-workflow-version") or None,
                "step": value("#live-step") or None,
            }
        except ValueError:
            self.notify(
                "Split, max calls and max cost must be numbers.",
                severity="error",
            )
            return
        self.dismiss(result)


class ShadowExperimentScreen(KeyboardForm, ModalScreen[dict | None]):
    CSS = """
    ShadowExperimentScreen { align: center middle; }
    #shadow-dialog { width: 72; height: auto; max-height: 90%;
                   overflow-y: auto; border: thick $primary;
                     background: $surface; padding: 1 2; }
    #shadow-dialog Input { margin-bottom: 1; }
    #shadow-dialog Horizontal { height: auto; }
    #shadow-dialog Horizontal Input { width: 1fr; margin-right: 1; }
    #shadow-buttons { height: auto; align-horizontal: right; }
    #shadow-buttons Button { margin-left: 1; }
    """

    def __init__(self, use_case: str = "") -> None:
        super().__init__()
        self.use_case = use_case

    def compose(self) -> ComposeResult:
        with Vertical(id="shadow-dialog"):
            yield Label("Start online shadow experiment", classes="heading")
            yield Label("Use-case")
            yield Input(self.use_case, id="shadow-use-case")
            yield Label("Candidate model")
            yield Input(id="shadow-candidate")
            yield Label("Named provider (optional) · sample %")
            with Horizontal():
                yield Input(id="shadow-provider")
                yield Input("10", type="integer", id="shadow-sample")
            yield Label("Optional exact scope: workflow · version · step")
            with Horizontal():
                yield Input(id="shadow-workflow")
                yield Input(id="shadow-workflow-version")
                yield Input(id="shadow-step")
            with Horizontal(id="shadow-buttons"):
                yield Button(
                    "Review mirror", variant="primary", id="shadow-review"
                )
            yield form_hint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        value = lambda selector: self.query_one(selector, Input).value.strip()
        try:
            sample = int(value("#shadow-sample"))
        except ValueError:
            self.notify("Sample must be an integer.", severity="error")
            return
        self.dismiss(
            {
                "use_case_key": value("#shadow-use-case"),
                "candidate_model": value("#shadow-candidate"),
                "candidate_provider": value("#shadow-provider") or None,
                "sample_pct": sample,
                "workflow": value("#shadow-workflow") or None,
                "workflow_version": value("#shadow-workflow-version") or None,
                "step": value("#shadow-step") or None,
            }
        )


class RouteScreen(KeyboardForm, ModalScreen[dict | None]):
    """Collect a persistent route definition; writing waits for review."""

    CSS = """
    RouteScreen { align: center middle; }
    #route-dialog { width: 72; height: auto; border: thick $primary;
                    background: $surface; padding: 1 2; }
    #route-dialog Input { margin-bottom: 1; }
    #route-buttons { height: auto; align-horizontal: right; }
    #route-buttons Button { margin-left: 1; }
    """

    def __init__(self, use_case: str = "", model: str = "") -> None:
        super().__init__()
        self.use_case = use_case
        self.model = model

    def compose(self) -> ComposeResult:
        with Vertical(id="route-dialog"):
            yield Label("Set persistent route", classes="heading")
            yield Label("Use-case")
            yield Input(self.use_case, id="route-use-case")
            yield Label("Model")
            yield Input(
                self.model, placeholder="model to serve", id="route-model"
            )
            yield Label("Named provider (optional)")
            yield Input(id="route-provider")
            yield Label("Previous model (blank = infer from traffic)")
            yield Input(id="route-previous")
            yield Label("Reason / note (optional)")
            yield Input(id="route-note")
            with Horizontal(id="route-buttons"):
                yield Button(
                    "Review route", variant="primary", id="route-review"
                )
            yield form_hint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        value = lambda selector: self.query_one(selector, Input).value.strip()
        self.dismiss(
            {
                "use_case_key": value("#route-use-case"),
                "model": value("#route-model"),
                "provider": value("#route-provider") or None,
                "previous_model": value("#route-previous") or None,
                "note": value("#route-note") or None,
            }
        )
