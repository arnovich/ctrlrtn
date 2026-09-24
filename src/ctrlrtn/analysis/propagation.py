"""Header-propagation gate (docs/evaluation.md).

Whole-task analysis — task-level live A/B, and clustering for paired
replay — depends on the app stamping the *same* ``x-ctrlrtn-task``
on every sub-agent HTTP call of one edition. If it doesn't, a "task" silently
misses calls and the experiment tests the fail-safe, not the candidate.

This proves it from recorded traffic alone (no keys), over a **recent window**
(so a verdict reflects what is happening now, not a latched fact from old
history): are calls tagged, and does a single task_id actually link the multiple
sub-agent use-cases of an edition. Pure logic here; the windowed/scoped store
query and the rendering live with their kin.

A `use_case_key` is the gate's proxy for "sub-agent" — two agents sharing a
fingerprint, or one emitting two, would mis-count; an unkeyed (`None`) call is
the *absence* of a use-case, never a second one, so it is excluded from spans.
This certifies that propagation *works*, NOT that every sub-agent of every
edition is captured — that would need an operator-declared expected set,
which the gate does not have.
"""

from __future__ import annotations

from dataclasses import dataclass

# Below this tagged fraction, propagation is treated as absent rather than
# partial — a handful of stray tags shouldn't read as "working".
MIN_TAGGED_FRACTION = 0.5
# Require several linked editions in the window, not one fluke, before calling
# cross-agent propagation demonstrated.
MIN_MULTI_AGENT_TASKS = 3


@dataclass
class PropagationReport:
    """Propagation signals over one recent window of completion calls: how
    many carried a task id, how many tagged tasks actually link several keyed
    use-cases, and how one focus use-case fares if asked. ``propagated`` is
    the verdict; the counts are the evidence behind it."""

    window_calls: int  # cap on most-recent completion calls examined
    total_calls: int  # completion calls actually in the window
    untasked_calls: int  # of those, calls with no x-ctrlrtn-task
    n_tasks: int  # distinct tagged tasks
    n_spannable_tasks: int  # tagged tasks with >=2 calls (could link agents)
    multi_agent_tasks: int  # tagged tasks linking >=2 *keyed* use-cases
    focus_use_case: str | None  # the use-case asked about, if any
    focus_task_count: int  # tagged tasks containing the focus use-case
    focus_calls_per_task: (
        float  # mean focus calls in those tasks (when present)
    )
    focus_co_located_tasks: int  # of those, how many also hold a sibling agent

    @property
    def tasked_calls(self) -> int:
        return self.total_calls - self.untasked_calls

    @property
    def tasked_fraction(self) -> float:
        return self.tasked_calls / self.total_calls if self.total_calls else 0.0

    @property
    def propagated(self) -> bool:
        """Cross-agent propagation is demonstrated only if recent calls are
        tagged *and* enough recent tasks link multiple sub-agents."""
        return (
            self.tasked_fraction >= MIN_TAGGED_FRACTION
            and self.multi_agent_tasks >= MIN_MULTI_AGENT_TASKS
        )


def build_propagation_report(
    rows: list[dict],
    *,
    focus_use_case: str | None = None,
    window_calls: int = 0,
) -> PropagationReport:
    """Aggregate ``{task_id, use_case_key, calls}`` rows (NULLs as None) into the
    propagation signals. An untasked call (task_id None) is counted but never
    grouped into a task; a None use_case_key (unkeyed) is NOT a sub-agent, so it
    never contributes to a cross-agent span."""
    total_calls = sum(r["calls"] for r in rows)
    untasked_calls = sum(r["calls"] for r in rows if r["task_id"] is None)

    by_task: dict[object, dict[object, int]] = {}
    for r in rows:
        if r["task_id"] is None:
            continue
        buckets = by_task.setdefault(r["task_id"], {})
        buckets[r["use_case_key"]] = (
            buckets.get(r["use_case_key"], 0) + r["calls"]
        )

    n_spannable_tasks = 0
    multi_agent_tasks = 0
    for buckets in by_task.values():
        if sum(buckets.values()) >= 2:
            n_spannable_tasks += 1
        if _keyed_count(buckets) >= 2:
            multi_agent_tasks += 1

    focus_task_count = 0
    focus_calls = 0
    focus_co_located = 0
    if focus_use_case is not None:
        for buckets in by_task.values():
            if focus_use_case not in buckets:
                continue
            focus_task_count += 1
            focus_calls += buckets[focus_use_case]
            if _keyed_count(buckets) >= 2:
                focus_co_located += 1

    return PropagationReport(
        window_calls=window_calls,
        total_calls=total_calls,
        untasked_calls=untasked_calls,
        n_tasks=len(by_task),
        n_spannable_tasks=n_spannable_tasks,
        multi_agent_tasks=multi_agent_tasks,
        focus_use_case=focus_use_case,
        focus_task_count=focus_task_count,
        focus_calls_per_task=(
            focus_calls / focus_task_count if focus_task_count else 0.0
        ),
        focus_co_located_tasks=focus_co_located,
    )


def _keyed_count(buckets: dict) -> int:
    """Distinct real use-cases in a task — an unkeyed (None) bucket is not one."""
    return sum(1 for k in buckets if k is not None)
