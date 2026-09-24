"""Small value objects returned by recorder stores."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

UNKEYED = "(unkeyed)"
UNTASKED = "(untasked)"
UNSESSIONED = "(unsessioned)"


@dataclass
class UseCaseRanking:
    """One row of the use-cases-ranked-by-spend view."""

    use_case: str
    calls: int
    input_tokens: int
    output_tokens: int
    avg_latency_ms: float
    cost_usd: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ModelRanking:
    """Spend grouped by the model that actually served a request."""

    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    avg_latency_ms: float
    cost_usd: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class TaskSummary:
    """Cost and outcome summary for one app-defined task."""

    task_id: str
    calls: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    use_cases: int
    errors: int = 0
    success: bool | None = None
    score: float | None = None


@dataclass
class SessionSummary:
    """Spend and usage summary for one operator-defined session."""

    session_id: str
    calls: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    use_cases: int
    errors: int = 0
    unknown_cost_calls: int = 0


@dataclass
class Outcome:
    """An app-reported task result; the latest report for a task wins."""

    task_id: str
    success: bool | None = None
    score: float | None = None
    ts: float = field(default_factory=time.time)


def sort_by_spend(rows: list[UseCaseRanking]) -> list[UseCaseRanking]:
    """Order use-cases by spend, then volume, then stable name."""
    rows.sort(key=lambda row: (-row.cost_usd, -row.total_tokens, row.use_case))
    return rows
