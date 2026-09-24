"""Experiment, shadow, routing, and Git-config TUI actions."""

from __future__ import annotations

import sqlite3

from ctrlrtn.cli.tui.forms import (
    ConfirmScreen,
    GitConfigScreen,
    LiveExperimentScreen,
    RouteScreen,
    ShadowExperimentScreen,
    _provider_exists,
    _unknown_provider,
)
from ctrlrtn.control.service import (
    AdoptionPlan,
    apply_adoption,
    prepare_adoption,
    prepare_route_change,
)
from ctrlrtn.control_config import (
    ControlConfig,
    ControlConfigError,
    ControlRevision,
    config_diff,
    live_config,
    load_control_config,
    verify_git_revision,
)
from ctrlrtn.policy.experiment import (
    Experiment,
)
from ctrlrtn.policy.route import Route
from ctrlrtn.policy.shadow import ShadowExperiment


class RouteControlActions:
    """Create and clear explicit serving routes."""

    def action_set_route(self) -> None:
        use_case = self._selected_use_case()
        route = next(
            (r for r in self._store.routes() if r.use_case_key == use_case),
            None,
        )
        self.push_screen(
            RouteScreen(use_case, route.model if route else ""),
            self._review_route,
        )

    def _review_route(self, values: dict | None) -> None:
        if values is None:
            return
        try:
            plan = prepare_route_change(
                self._store,
                use_case_key=values["use_case_key"],
                model=values["model"],
                previous_model=values["previous_model"],
                note=values["note"],
                provider=values["provider"],
                provider_exists=_provider_exists,
            )
        except ValueError as exc:
            self._notice(f"invalid route: {exc}", error=True)
            return
        route = plan.route
        dormant = (
            f"\nWARNING: {plan.dormant_experiment_id} is running; this route remains "
            "dormant until it stops."
            if plan.dormant_experiment_id
            else ""
        )
        warnings = "".join(
            f"\nWARNING: {notice.message}." for notice in plan.notices
        )
        message = (
            "Set persistent route\n\n"
            f"use-case: {route.use_case_key}\n"
            f"route: {route.previous_model or '(unknown)'} -> {route.model}\n"
            f"provider: {route.provider or 'original request provider'}\n"
            f"note: {route.note or '-'}{dormant}{warnings}\n\n"
            "The gateway applies this within ~10s."
        )
        self.push_screen(
            ConfirmScreen(message, "Set route"),
            lambda confirmed: self._set_route(route, confirmed),
        )

    def _set_route(self, route: Route, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            store = self._control_store()
            try:
                store.set_route(route)
            finally:
                store.close()
        except (ValueError, sqlite3.Error) as exc:
            self._notice(f"could not set route: {exc}", error=True)
            return
        self._notice(
            f"Routing {route.use_case_key} -> {route.model}; "
            "gateway updates within ~10s."
        )
        self._reload()

    def action_clear_route(self) -> None:
        use_case = self._selected_use_case()
        route = next(
            (r for r in self._store.routes() if r.use_case_key == use_case),
            None,
        )
        if route is None:
            self._notice(
                "Focus a use-case with a persistent route to clear it."
            )
            return
        self.push_screen(
            ConfirmScreen(
                f"Clear route for {use_case}?\n\n"
                f"Remove -> {route.model}. Traffic returns to pass-through unless "
                "a live experiment owns it.",
                "Clear route",
            ),
            lambda confirmed: self._clear_route(use_case, confirmed),
        )

    def _clear_route(self, use_case: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            store = self._control_store()
            try:
                cleared = store.clear_route(use_case)
            finally:
                store.close()
        except sqlite3.Error as exc:
            self._notice(f"could not clear route: {exc}", error=True)
            return
        self._notice(
            f"Cleared route for {use_case}; gateway updates within ~10s."
            if cleared
            else f"Route for {use_case} no longer exists."
        )
        self._reload()
