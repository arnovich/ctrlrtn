"""Window and pagination models shared by the Textual console."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

_ALL_WINDOW = "all"
_GRAPH_POINTS = 180


@dataclass(frozen=True)
class GraphWindow:
    """A selectable graph span and its fixed bucket grid."""

    label: str
    bucket_seconds: int
    buckets: int

    @property
    def is_all(self) -> bool:
        return self.bucket_seconds == 0

    @property
    def seconds(self) -> int | None:
        return None if self.is_all else self.bucket_seconds * self.buckets

    def resolve(self, earliest: float | None, now: float) -> "GraphWindow":
        if not self.is_all:
            return self
        span = now - earliest if earliest is not None else 0.0
        span = max(span, float(_DEFAULT_GRAPH_WINDOW.seconds or 1800))
        # The current bucket may be almost empty. Reserve its slot so the
        # remaining complete buckets still cover the earliest recorded call.
        complete_buckets = (
            _GRAPH_POINTS - 1 if earliest is not None else _GRAPH_POINTS
        )
        return GraphWindow(
            self.label,
            max(1, ceil(span / complete_buckets)),
            _GRAPH_POINTS,
        )

    def span_text(self) -> str:
        return "all time" if self.is_all else f"last {self.label}"


_GRAPH_WINDOWS = (
    GraphWindow("10m", 5, 120),
    GraphWindow("30m", 10, 180),
    GraphWindow("1h", 20, 180),
    GraphWindow("6h", 120, 180),
    GraphWindow("12h", 240, 180),
    GraphWindow("1d", 480, 180),
    GraphWindow("7d", 3600, 168),
    GraphWindow(_ALL_WINDOW, 0, 0),
)
_DEFAULT_GRAPH_WINDOW = _GRAPH_WINDOWS[1]


@dataclass(frozen=True)
class TableWindow:
    """A selectable trailing span for traffic aggregate tables."""

    label: str
    seconds: int | None

    def since(self, now: float) -> float | None:
        return None if self.seconds is None else now - self.seconds

    def span_text(self) -> str:
        return "all time" if self.seconds is None else f"last {self.label}"


_TABLE_WINDOWS = tuple(
    TableWindow(window.label, window.seconds) for window in _GRAPH_WINDOWS
)
_DEFAULT_TABLE_WINDOW = _TABLE_WINDOWS[-1]

_SCOPED_TABLE_HEADINGS = {
    "label-usecases": "Use-cases (by spend)",
    "label-models": "Models (served)",
    "label-tasks": "Tasks (by cost)",
}
_SIDEBAR_PANES = {
    "jobs": "jobs",
    "experiments": "experiments",
    "shadows": "shadows",
    "usecases": "use-cases",
    "models": "models",
    "workflows": "workflows",
    "discovered": "discovered",
    "tasks": "tasks",
    "calls": "calls",
}
_SIDEBAR_TABLES = tuple(_SIDEBAR_PANES)
_PANE_MAX_ROWS = 8
_PAGED_TABLES = {
    "jobs": "Jobs",
    "workflows": "Workflows (recent)",
    "calls": "Calls (live)",
}
_PAGE_SIZE = 50
