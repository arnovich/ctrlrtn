"""Zero-eval recommendations from recorded traffic.

These need no candidate inference and no live calls -- they read the recorded
rankings plus the price table and surface where to look first:

  - **caching_waste**: caching is writing to cache but never reading back -- a
    net loss (the prefix isn't reused within the TTL), so stop caching it;
  - **focus**: which use-case dominates spend (where optimizing pays off most);
  - **enable_caching**: large repeated input with no cache activity recorded --
    a safe win that needs no model change and no eval;
  - **downgrade_candidate**: a use-case on a model that has a cheaper same-family
    sibling, with the *ceiling* saving if it proves non-inferior.

Everything here is a hint for a human, not an automatic change. The downgrade
saving is a ceiling (it assumes the cheaper model uses the same tokens, which it
often will not) and is explicitly gated on a non-inferiority eval. Dollar figures
are modelled from an approximate price table, not billed amounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import inf

from ctrlrtn.recorder.models import UseCaseRanking
from ctrlrtn.telemetry.pricing import (
    cheaper_candidates,
    cost_usd,
    price_for,
)
from ctrlrtn.telemetry.usage import Usage

_FOCUS_SHARE = 0.30  # flag the top use-case when it is >= this share of cost
_FOCUS_MIN_COST = 0.01  # ...but not when total spend is trivial
_CACHE_MIN_AVG_INPUT = 1000  # avg input tok/call worth caching
_CACHE_MIN_CALLS = 3  # the input must actually be repeated
_WASTE_MIN_WRITE_TOKENS = 1000  # ignore trivially small cache writes
_CANDIDATE_MIN_COST = 0.01  # ignore trivially cheap use-cases
_MIN_SAVINGS_USD = 0.005  # don't surface a downgrade that saves ~nothing

# Surface order: an active money leak (caching_waste) first, then descriptive
# focus, then the safe/free win (caching), then the risky model change. A
# downgrade must never outrank caching.
_ORDER = {
    "caching_waste": 0,
    "focus": 1,
    "enable_caching": 2,
    "downgrade_candidate": 3,
}


@dataclass
class Recommendation:
    kind: str  # focus | enable_caching | downgrade_candidate
    use_case: str
    summary: str
    detail: str
    est_savings_usd: float | None = None


def build_recommendations(
    rankings: list[UseCaseRanking],
    models: dict[str, str | None],
) -> list[Recommendation]:
    recs: list[Recommendation] = []
    total_cost = sum(r.cost_usd for r in rankings)

    recs.extend(_focus(rankings, total_cost))
    for ranking in rankings:
        model = models.get(ranking.use_case)
        recs.extend(_caching_waste(ranking))
        recs.extend(_caching(ranking, model))
        recs.extend(_downgrade(ranking, model))

    recs.sort(
        key=lambda r: (
            _ORDER.get(r.kind, 9),
            -(r.est_savings_usd or 0.0),
            r.use_case,
        )
    )
    return recs


def _focus(
    rankings: list[UseCaseRanking], total_cost: float
) -> list[Recommendation]:
    if total_cost <= 0:
        return []
    top = max(rankings, key=lambda r: r.cost_usd)
    if top.cost_usd < _FOCUS_MIN_COST:
        return []
    share = top.cost_usd / total_cost
    if share < _FOCUS_SHARE:
        return []
    return [
        Recommendation(
            kind="focus",
            use_case=top.use_case,
            summary=f"Focus here first: {_pct(share)} of all cost.",
            detail=(
                f"${top.cost_usd:.4f} over {top.calls} calls "
                f"(of ${total_cost:.4f} total)."
            ),
        )
    ]


def _caching_waste(ranking: UseCaseRanking) -> list[Recommendation]:
    """Caching is writing to cache but never reading it back — a net loss (the
    cache write costs ~25% extra and is never amortized). This is the cost
    guardrail that makes "caching is a safe win" verifiable, not just asserted:
    a use-case here should have caching turned off (or its calls clustered
    within the cache TTL so they actually re-read)."""
    write = ranking.cache_write_tokens or 0
    if write < _WASTE_MIN_WRITE_TOKENS or (ranking.cache_read_tokens or 0) > 0:
        return []
    return [
        Recommendation(
            kind="caching_waste",
            use_case=ranking.use_case,
            summary=(
                "Caching writes but never reads — a net loss; stop caching "
                "this use-case."
            ),
            detail=(
                f"{ranking.calls} calls wrote ~{write:,} tokens to cache and "
                f"read 0 back — the prefix isn't reused within the cache TTL, "
                f"so each write costs ~25% extra for nothing. Disable cache "
                f"injection here, or widen the window so calls cluster in TTL."
            ),
        )
    ]


def _caching(
    ranking: UseCaseRanking, model: str | None
) -> list[Recommendation]:
    # Only recommend enabling caching when there is no cache activity at all;
    # if writes are happening, caching is already on (see _caching_waste).
    if (
        ranking.calls < _CACHE_MIN_CALLS
        or (ranking.cache_read_tokens or 0) > 0
        or (ranking.cache_write_tokens or 0) > 0
    ):
        return []
    avg_input = (ranking.input_tokens or 0) / ranking.calls
    if avg_input < _CACHE_MIN_AVG_INPUT:
        return []
    detail = (
        f"{ranking.calls} calls, ~{avg_input:.0f} input tok/call, no cache "
        f"activity recorded — prompt caching is unused."
    )
    price = price_for(model)
    if price is not None:
        input_cost = (ranking.input_tokens or 0) * price.input / 1_000_000
        detail += (
            f" Input cost so far ${input_cost:.4f}; caching the repeated prefix "
            f"(incl. the growing context for multi-turn agents) bills repeats "
            f"at ~10%."
        )
    return [
        Recommendation(
            kind="enable_caching",
            use_case=ranking.use_case,
            summary="Enable prompt caching (safe — no model change, no eval).",
            detail=detail,
        )
    ]


def _downgrade(
    ranking: UseCaseRanking, model: str | None
) -> list[Recommendation]:
    candidates = cheaper_candidates(model)
    if not candidates or ranking.cost_usd < _CANDIDATE_MIN_COST:
        return []
    usage = _usage_of(ranking)
    base = cost_usd(
        model, usage
    )  # like-for-like: current model on these tokens
    if base is None:
        return []
    candidate = min(candidates, key=lambda c: cost_usd(c, usage) or inf)
    candidate_cost = cost_usd(candidate, usage)
    if candidate_cost is None:
        return []
    savings = base - candidate_cost
    if savings < _MIN_SAVINGS_USD:
        return []
    return [
        Recommendation(
            kind="downgrade_candidate",
            use_case=ranking.use_case,
            summary=(
                f"Test {model} -> {candidate} "
                f"(up to ${savings:.4f} if non-inferior)."
            ),
            detail=(
                f"~${base:.4f} vs ~${candidate_cost:.4f} at this token volume — "
                f"a ceiling, not a forecast (a cheaper model may use more "
                f"tokens, and for tool-loop agents may take more turns). Eval "
                f"end-to-end task success, not output similarity, before "
                f"switching."
            ),
            est_savings_usd=savings,
        )
    ]


def _usage_of(ranking: UseCaseRanking) -> Usage:
    return Usage(
        input_tokens=ranking.input_tokens,
        output_tokens=ranking.output_tokens,
        cache_read_tokens=ranking.cache_read_tokens,
        cache_write_tokens=ranking.cache_write_tokens,
    )


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"
