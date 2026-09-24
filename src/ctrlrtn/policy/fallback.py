"""Evidence-approved model fallbacks used under budget pressure."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

_APPROVED_VERDICT = "NON_INFERIOR"


@dataclass(frozen=True)
class ApprovedFallback:
    """A durable permission to downgrade one use-case to one model."""

    use_case_key: str
    model: str
    baseline_model: str
    evidence_created: float
    provider: str | None = None
    approved_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        for name in ("use_case_key", "model", "baseline_model"):
            if not isinstance(getattr(self, name), str) or not getattr(
                self, name
            ):
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("evidence_created", "approved_at"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be finite")
        if self.provider is not None and not self.provider:
            raise ValueError("provider must be non-empty when set")


@dataclass(frozen=True)
class FallbackDecision:
    """A serving decision produced by an approved budget fallback."""

    use_case_key: str
    served_model: str
    original_model: str
    provider: str | None = None
    experiment_id: None = None
    arm: None = None
    is_candidate: bool = False
    is_budget_fallback: bool = True


def _required_str(replay: Mapping[str, object], key: str) -> str:
    value = replay.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"fallback evidence {key} must be a non-empty string")
    return value


def approved_fallback_from_replay(
    replay: Mapping[str, object], *, provider: str | None = None
) -> ApprovedFallback:
    """Validate a replay JSON artifact and turn its safe verdict into policy."""
    if replay.get("verdict") != _APPROVED_VERDICT:
        raise ValueError("fallback evidence verdict must be NON_INFERIOR")
    if replay.get("scope") is not None:
        raise ValueError(
            "step-scoped replay evidence cannot approve a use-case fallback"
        )
    use_case = _required_str(replay, "use_case")
    candidate_model = _required_str(replay, "candidate_model")
    baseline_model = _required_str(replay, "baseline_model")
    created = replay.get("created")
    if (
        isinstance(created, bool)
        or not isinstance(created, (int, float))
        or not math.isfinite(created)
    ):
        raise ValueError("fallback evidence created must be finite")
    return ApprovedFallback(
        use_case_key=use_case,
        model=candidate_model,
        baseline_model=baseline_model,
        evidence_created=float(created),
        provider=provider,
    )
