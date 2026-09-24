"""Config-driven prices: packaged TOML defaults + CTRLRTN_PRICES overlay."""

from __future__ import annotations

import pytest

from ctrlrtn.telemetry import pricing
from ctrlrtn.telemetry.pricing import cheaper_candidates, price_for


@pytest.fixture(autouse=True)
def _fresh_prices():
    # Clear the cached tables around each test so an override can't leak.
    pricing.reload()
    yield
    pricing.reload()


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "prices.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_defaults_load_from_packaged_toml():
    price = price_for("claude-sonnet-4-5")
    assert price is not None
    assert price.input == 3.0
    assert price.cache_read == 0.30
    assert cheaper_candidates("claude-sonnet-4-5") == ["claude-haiku-4-5"]


def test_override_merges_fields_of_one_model(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        "[prices.claude-opus-4]\ninput = 5.0\noutput = 25.0\n",
    )
    monkeypatch.setenv("CTRLRTN_PRICES", path)
    pricing.reload()
    price = price_for("claude-opus-4")
    assert (price.input, price.output) == (5.0, 25.0)
    assert price.cache_read == 1.50  # untouched field kept, not zeroed
    assert price_for("claude-sonnet-4-5").input == 3.0  # other models untouched


def test_override_can_add_a_new_model(tmp_path, monkeypatch):
    # The canonical fix for the stale Opus price: add a version-specific row.
    path = _write(
        tmp_path,
        "[prices.claude-opus-4-8]\ninput = 6.0\noutput = 30.0\n",
    )
    monkeypatch.setenv("CTRLRTN_PRICES", path)
    pricing.reload()
    assert price_for("claude-opus-4-8").input == 6.0  # exact match
    # longest-prefix wins, so a dated id beats the stale claude-opus-4 row
    assert price_for("claude-opus-4-8-20260101").input == 6.0


def test_override_can_extend_the_ladder(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        '[ladder]\nclaude-opus-4 = ["claude-haiku-4-5"]\n',
    )
    monkeypatch.setenv("CTRLRTN_PRICES", path)
    pricing.reload()
    assert cheaper_candidates("claude-opus-4") == ["claude-haiku-4-5"]


def test_malformed_entry_raises_clearly(tmp_path, monkeypatch):
    path = _write(tmp_path, "[prices.broken]\noutput = 1.0\n")  # no input
    monkeypatch.setenv("CTRLRTN_PRICES", path)
    pricing.reload()
    with pytest.raises(ValueError, match="broken"):
        price_for("broken")


def test_missing_override_file_names_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("CTRLRTN_PRICES", str(tmp_path / "nope.toml"))
    pricing.reload()
    with pytest.raises(ValueError, match="CTRLRTN_PRICES.*not found"):
        pricing.load()


def test_malformed_toml_override_names_the_env_var(tmp_path, monkeypatch):
    path = _write(tmp_path, "this is not = = valid toml\n")
    monkeypatch.setenv("CTRLRTN_PRICES", path)
    pricing.reload()
    with pytest.raises(ValueError, match="CTRLRTN_PRICES"):
        pricing.load()


@pytest.mark.parametrize("bad", ["nan", "inf", "-5.0"])
def test_non_finite_or_negative_price_is_rejected(tmp_path, monkeypatch, bad):
    path = _write(tmp_path, f"[prices.claude-opus-4]\ninput = {bad}\n")
    monkeypatch.setenv("CTRLRTN_PRICES", path)
    pricing.reload()
    with pytest.raises(ValueError, match="claude-opus-4"):
        pricing.load()
