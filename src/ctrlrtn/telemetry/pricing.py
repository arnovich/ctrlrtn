"""Per-model price table and cache-aware cost computation.

Cost is the third optimization parameter (with tokens and latency). The table
is config-driven: default prices ship in ``prices.toml`` beside this module (USD
per million tokens, approximate list prices — edit that file to change them),
and ``CTRLRTN_PRICES`` may point at a TOML file that overlays the defaults,
merging **per field** so fixing one price keeps a model's other fields (e.g.
its cache rates). Cost reads cache tokens so cached input is billed at its
discounted rate; ignoring them overstates both baseline spend and savings.

Models are matched by prefix, so version-suffixed ids
(``claude-sonnet-4-5-20250929``) resolve to their family price.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from ctrlrtn.telemetry.usage import Usage

_DEFAULT_PRICES_FILE = "prices.toml"
_PRICES_ENV = "CTRLRTN_PRICES"


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000,000 tokens, plus the model's max output tokens."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    max_output: int | None = None  # synchronous max output tokens, if known


def _price_field(entry: dict, field: str, default: float | None) -> float:
    raw = entry.get(field, default)
    if raw is None:
        raise ValueError(f"missing {field!r}")
    value = float(raw)  # TOML gives int/float; a bad type raises here
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field}={raw!r} must be a non-negative number")
    return value


def _model_price(name: str, entry: dict) -> ModelPrice:
    try:
        raw_cap = entry.get("max_output")
        if raw_cap is not None and (
            not isinstance(raw_cap, int) or raw_cap < 1
        ):
            raise ValueError(f"max_output={raw_cap!r} must be a positive int")
        return ModelPrice(
            input=_price_field(entry, "input", None),
            output=_price_field(entry, "output", None),
            cache_read=_price_field(entry, "cache_read", 0.0),
            cache_write=_price_field(entry, "cache_write", 0.0),
            max_output=raw_cap,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid price entry for {name!r}: {exc}") from exc


def _overlay(base: dict, extra: dict) -> None:
    """Overlay ``extra`` onto ``base``: price entries merge **per field** (so a
    partial override keeps a model's untouched fields), ladder entries replace
    per key (a ladder value is a whole list)."""
    for model, fields in extra.get("prices", {}).items():
        base.setdefault("prices", {}).setdefault(model, {}).update(fields)
    if "ladder" in extra:
        base.setdefault("ladder", {}).update(extra["ladder"])


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, ModelPrice], dict[str, tuple[str, ...]]]:
    """The (prices, ladder) tables: packaged defaults, overlaid by the file at
    ``CTRLRTN_PRICES`` when set. Cached; call ``reload`` after changing the
    environment (tests do)."""
    text = (
        resources.files("ctrlrtn.telemetry")
        .joinpath(_DEFAULT_PRICES_FILE)
        .read_text(encoding="utf-8")
    )
    data = tomllib.loads(text)
    override = os.environ.get(_PRICES_ENV)
    if override:
        _overlay(data, _read_override(override))
    prices = {
        name: _model_price(name, entry)
        for name, entry in data.get("prices", {}).items()
    }
    ladder = {
        name: tuple(models) for name, models in data.get("ladder", {}).items()
    }
    return prices, ladder


def _read_override(path: str) -> dict:
    """Parse the ``CTRLRTN_PRICES`` override file, attributing any failure
    to the env var + path so a broken override is diagnosable (not a bare
    FileNotFoundError/TOMLDecodeError from deep in a price lookup)."""
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"{_PRICES_ENV}={path!r}: file not found") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{_PRICES_ENV}={path!r}: {exc}") from exc


def load() -> None:
    """Eagerly build and validate the price tables. Call at server startup so a
    broken override fails the boot loudly instead of silently dropping cost
    enrichment per trace once traffic arrives."""
    _tables()


def reload() -> None:
    """Drop the cached tables so the next lookup re-reads the files."""
    _tables.cache_clear()


def _family(model: str | None) -> str | None:
    """The price-table key a model id belongs to (longest matching prefix, so
    ``gpt-4o-mini`` beats ``gpt-4o``). The prefix must end on a delimiter, so
    ``gpt-4ofoo`` does not match ``gpt-4o``."""
    if not model:
        return None
    prices = _tables()[0]
    if model in prices:
        return model
    matches = [
        key
        for key in prices
        if model.startswith(key) and model[len(key)] in "-_"
    ]
    return max(matches, key=len) if matches else None


def price_for(model: str | None) -> ModelPrice | None:
    family = _family(model)
    return _tables()[0][family] if family is not None else None


def max_output_tokens(model: str | None) -> int | None:
    """The model's synchronous max output tokens, or None when unknown."""
    price = price_for(model)
    return price.max_output if price is not None else None


def known_models() -> list[str]:
    """Every model the router can price, sorted. The console offers these as
    completions when you name a candidate or baseline — a model missing here
    still works, it just records unpriced calls."""
    return sorted(_tables()[0])


def cheaper_candidates(model: str | None) -> list[str]:
    """Cheaper same-family models that could be tested as substitutes."""
    family = _family(model)
    return list(_tables()[1].get(family, ())) if family is not None else []


def cost_usd(
    model: str | None, usage: Usage | None, *, free: bool = False
) -> float | None:
    """Cache-aware cost in USD, or None if the model is unpriced.

    ``free`` is an explicit provider policy, so it wins even when the model or
    usage is unknown. Token counts remain independently recorded when present.
    """
    if free:
        return 0.0
    price = price_for(model)
    if price is None or usage is None:
        return None
    return (
        (usage.input_tokens or 0) * price.input
        + (usage.output_tokens or 0) * price.output
        + (usage.cache_read_tokens or 0) * price.cache_read
        + (usage.cache_write_tokens or 0) * price.cache_write
    ) / 1_000_000
