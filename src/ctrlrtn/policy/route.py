"""Persistent per-use-case model overrides — the "switch" after a verdict.

An experiment answers "is the cheaper model good enough for this use-case?";
a **route** acts on the answer: every call for the use-case is served the
routed model from then on (100% of traffic, no arms). Set and inspected via
``ctrlrtn route ...``; the gateway applies it on the same snapshot-driven
hot path as experiments, with the precedence:

    running experiment  >  route  >  pass-through

An experiment on a use-case keeps a route dormant — a test in flight owns the
traffic split, and the route takes over when the experiment stops.

The route stores the model it replaced and when, so the switch is monitorable:
post-switch traffic priced at the OLD model minus what it actually cost is the
realized saving ("was sonnet, now haiku, saved $Z").
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ctrlrtn.telemetry import pricing
from ctrlrtn.telemetry.usage import Usage


@dataclass(frozen=True)
class Route:
    """One persistent override: serve ``model`` for ``use_case_key``."""

    use_case_key: str
    model: str
    previous_model: str | None = None  # what it replaced (savings baseline)
    note: str | None = None  # e.g. "adopted from exp:..."
    ts: float = field(default_factory=time.time)
    provider: str | None = None

    def __post_init__(self) -> None:
        if not self.use_case_key:
            raise ValueError("use_case_key must be non-empty")
        if not self.model:
            raise ValueError("model must be non-empty")
        if self.provider is not None and not self.provider:
            raise ValueError("provider must be non-empty when set")


@dataclass(frozen=True)
class WorkflowRoute:
    """An exact-version workflow rule, optionally narrowed to one stable step."""

    workflow: str
    workflow_version: str
    model: str
    step: str | None = None
    provider: str | None = None
    note: str | None = None
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        for name in ("workflow", "workflow_version", "model"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        if self.step is not None and not self.step:
            raise ValueError("step must be non-empty when set")
        if self.provider is not None and not self.provider:
            raise ValueError("provider must be non-empty when set")

    @property
    def key(self) -> tuple[str, str, str | None]:
        return self.workflow, self.workflow_version, self.step


@dataclass(frozen=True)
class RouteDecision:
    """What the serving path decided when a persistent route fired.

    ``experiment_id`` and ``arm`` stay absent because routed traffic is ordinary
    traffic, not an experiment arm. The fields implement the gateway's explicit
    ``ServingDecision`` protocol.
    """

    use_case_key: str
    served_model: str
    original_model: str
    provider: str | None = None
    rule_scope: str = "use_case"
    rule_key: str | None = None
    control_revision: str | None = None
    experiment_id: None = None
    arm: None = None
    is_candidate: bool = False


def route_savings(usage: dict, previous_model: str | None) -> float | None:
    """Realized saving on post-switch traffic: the same token mix priced at the
    model the route replaced, minus what it actually cost. None when the
    previous model is unknown/unpriced (never a made-up $0). ``usage`` is the
    store's post-switch aggregate (tokens + cost_usd)."""
    if previous_model is None:
        return None
    would_have_cost = pricing.cost_usd(
        previous_model,
        Usage(
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_tokens"],
            cache_write_tokens=usage["cache_write_tokens"],
        ),
    )
    if would_have_cost is None:
        return None
    return would_have_cost - usage["cost_usd"]
