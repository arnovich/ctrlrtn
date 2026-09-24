"""Stateful properties for in-flight budget reservation accounting."""

from __future__ import annotations

import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ctrlrtn.policy.budget import BudgetGate, BudgetPolicy, SpendSnapshot
from ctrlrtn.recorder.trace import Trace

_BODY = b'{"model":"claude-haiku-4-5","messages":[],"max_tokens":64}'


def _settlement(reservation_id: int, cost: float, now: float) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=_BODY,
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
        model="claude-haiku-4-5",
        cost_usd=cost,
        session_id="session-property",
        budget_reservation_id=reservation_id,
        ts=now,
    )


@settings(max_examples=100, deadline=None)
@given(
    actions=st.lists(
        st.tuples(
            st.booleans(),
            st.floats(
                min_value=0.0,
                max_value=0.02,
                allow_nan=False,
                allow_infinity=False,
            ),
        ),
        min_size=1,
        max_size=60,
    )
)
def test_reservations_always_equal_unsettled_admissions(actions):
    snapshot = SpendSnapshot()
    gate = BudgetGate(
        BudgetPolicy(
            global_daily_usd=100.0,
            session_limit_usd=100.0,
            reserve_in_flight=True,
        ),
        snapshot,
    )
    pending: dict[int, float] = {}
    settled = 0.0
    now = time.time()

    for admit, observed_cost in actions:
        if admit or not pending:
            decision = gate.check(
                {"x-ctrlrtn-session": "session-property"},
                _BODY,
                _BODY,
                provider_free=False,
            )
            assert decision.allowed
            assert decision.reservation_id is not None
            assert decision.reservation_id not in pending
            pending[decision.reservation_id] = decision.reserved_usd
        else:
            reservation_id = next(iter(pending))
            pending.pop(reservation_id)
            gate.observe(
                _settlement(reservation_id, observed_cost, now), now=now
            )
            settled += observed_cost

        assert gate.reserved_usd == pytest.approx(sum(pending.values()))
        assert gate.reserved_usd >= 0
        assert snapshot.total == pytest.approx(settled)
        assert snapshot.session_total("session-property") == pytest.approx(
            settled
        )
