"""Shared control-plane planning and application services.

Operator interfaces own input and presentation.  This module owns validation,
hazard notices, and the exact state transition they confirm.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.route import Route
from ctrlrtn.telemetry import pricing

ProviderExists = Callable[[str], bool]


class ControlRepository(Protocol):
    """The store access the control-plane services need: experiment and
    model lookups, recorded requests (advisory input to hazard notices, never
    control authority), and the one write, atomic adoption."""

    def experiment(self, experiment_id: str) -> Experiment | None: ...

    def running_experiments(self) -> dict[str, Experiment]: ...

    def use_case_models(self) -> dict[str, str | None]: ...

    def requests_for_use_case(
        self, use_case: str, limit: int, **scope: object
    ) -> list[dict]: ...

    def adopt_experiment(self, experiment_id: str, route: Route) -> bool: ...


@dataclass(frozen=True)
class ControlNotice:
    """A hazard notice attached to a plan: a stable ``code`` and text for the
    operator. Advisory; it never blocks the change."""

    code: str
    message: str


@dataclass(frozen=True)
class RoutePlan:
    """A validated persistent-route change awaiting operator confirmation:
    the exact ``Route`` to install, the running experiment (if any) that will
    keep it dormant until stopped, and the notices to show first."""

    route: Route
    dormant_experiment_id: str | None
    notices: tuple[ControlNotice, ...]


@dataclass(frozen=True)
class AdoptionPlan:
    """A validated experiment adoption awaiting confirmation: the experiment
    to stop, the exact route its candidate becomes, and the notices to show
    first. ``apply_adoption`` performs the two as one transition."""

    experiment: Experiment
    route: Route
    notices: tuple[ControlNotice, ...]


def provider_error(
    provider: str | None, provider_exists: ProviderExists
) -> str | None:
    if provider is None or provider_exists(provider):
        return None
    return f"unknown provider {provider!r}; configure it under providers:"


def _validate_provider(
    provider: str | None, provider_exists: ProviderExists
) -> None:
    error = provider_error(provider, provider_exists)
    if error:
        raise ValueError(error)


def route_hazards(
    store: ControlRepository, use_case: str, model: str
) -> tuple[ControlNotice, ...]:
    """Return best-effort safety context for a proposed model route."""

    if use_case not in store.use_case_models():
        return (
            ControlNotice(
                "unseen_use_case",
                f"no recorded traffic for {use_case!r} — check the key with "
                "`ctrlrtn usecases` (the route applies if traffic appears)",
            ),
        )
    cap = pricing.max_output_tokens(model)
    if cap is None:
        return ()
    try:
        recorded_max = 0
        for row in store.requests_for_use_case(use_case, 20):
            payload = json.loads(row["request_body"])
            requested = payload.get("max_tokens")
            if isinstance(requested, int) and not isinstance(requested, bool):
                recorded_max = max(recorded_max, requested)
    except Exception:
        # Historical data is advisory and must not acquire control authority.
        return ()
    if recorded_max <= cap:
        return ()
    return (
        ControlNotice(
            "output_cap",
            f"recorded calls request max_tokens up to {recorded_max}, above "
            f"{model}'s {cap} output cap — the gateway will clamp to the cap "
            "(possible truncation); watch outcomes after switching",
        ),
    )


def prepare_route_change(
    store: ControlRepository,
    *,
    use_case_key: str,
    model: str,
    previous_model: str | None = None,
    note: str | None = None,
    provider: str | None = None,
    provider_exists: ProviderExists,
) -> RoutePlan:
    _validate_provider(provider, provider_exists)
    previous = previous_model or store.use_case_models().get(use_case_key)
    route = Route(
        use_case_key=use_case_key,
        model=model,
        previous_model=previous,
        note=note,
        provider=provider,
    )
    running = store.running_experiments().get(use_case_key)
    return RoutePlan(
        route=route,
        dormant_experiment_id=(
            running.experiment_id if running is not None else None
        ),
        notices=route_hazards(store, use_case_key, model),
    )


def prepare_adoption(
    store: ControlRepository,
    experiment_id: str,
    *,
    provider_exists: ProviderExists,
) -> AdoptionPlan:
    experiment = store.experiment(experiment_id)
    if experiment is None:
        raise ValueError(f"No experiment with id {experiment_id!r}.")
    _validate_provider(experiment.candidate_provider, provider_exists)
    previous = store.use_case_models().get(experiment.use_case_key)
    route = Route(
        use_case_key=experiment.use_case_key,
        model=experiment.candidate_model,
        previous_model=previous,
        note=f"adopted from {experiment.experiment_id}",
        provider=experiment.candidate_provider,
    )
    return AdoptionPlan(
        experiment=experiment,
        route=route,
        notices=route_hazards(
            store, experiment.use_case_key, experiment.candidate_model
        ),
    )


def apply_adoption(store: ControlRepository, plan: AdoptionPlan) -> bool:
    """Atomically stop the selected experiment and install its exact route."""

    return store.adopt_experiment(plan.experiment.experiment_id, plan.route)
