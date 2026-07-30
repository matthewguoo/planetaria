"""Unit tests for the pure/parsing pieces of the trade service — order price
signing, OCC parsing, and fill-value normalization (the invariants the exit
enforcer's correctness rests on)."""

from types import SimpleNamespace

import pytest

from app.services.trade_service import (
    parse_occ_symbol,
    position_mid_from_quotes,
    round_tick,
    staged_price_ok,
)


def test_staged_price_ok_fast_tape_guard():
    # Inside 2x half-spread: fine even when off-mid.
    ok, drift, tol = staged_price_ok(2.50, 2.62, 0.08)
    assert ok and drift == pytest.approx(0.12) and tol == pytest.approx(0.2096)
    # Beyond both spread slack and the 8% floor: rejected.
    ok, drift, _ = staged_price_ok(2.50, 3.10, 0.05)
    assert not ok and drift == pytest.approx(0.60)
    # Tight book, small drift within 8% floor: fine.
    assert staged_price_ok(2.50, 2.55, 0.01)[0]
    # Credit structures compare on the signed axis.
    assert staged_price_ok(-2.96, -2.90, 0.05)[0]
    ok, _, _ = staged_price_ok(-2.96, -2.20, 0.03)
    assert not ok
    # Near-zero mids use the 0.05 floor so tolerance never collapses.
    assert staged_price_ok(0.05, 0.06, 0.005)[0]


def test_round_tick_preserves_sign():
    assert round_tick(1.234) == 1.23
    assert round_tick(-1.234) == -1.23
    assert round_tick(0.004) == 0.01     # never zero
    assert round_tick(-0.004) == -0.01
    assert round_tick(2.0) == 2.0
    assert round_tick(-0.35) == -0.35


def test_parse_occ_symbol():
    occ = parse_occ_symbol("SPY260729C00450000")
    assert occ == {"underlying": "SPY", "expiry": "2026-07-29", "right": "C", "strike": 450.0}
    occ = parse_occ_symbol("AAPL251219P00190500")
    assert occ == {"underlying": "AAPL", "expiry": "2025-12-19", "right": "P", "strike": 190.5}
    assert parse_occ_symbol("SPY") is None
    assert parse_occ_symbol("") is None
    assert parse_occ_symbol("SPY260729X00450000") is None  # bad right


def test_position_mid_from_quotes_signs():
    legs = [
        {"symbol": "A", "side": 1, "ratio": 1},
        {"symbol": "B", "side": -1, "ratio": 1},
    ]
    quotes = {
        "A": {"bid": 1.9, "ask": 2.1, "mid": 2.0},
        "B": {"bid": 0.7, "ask": 0.9, "mid": 0.8},
    }
    assert position_mid_from_quotes(legs, quotes) == pytest.approx(1.2)
    # Missing quote -> None (never a silent partial sum).
    assert position_mid_from_quotes(legs, {"A": quotes["A"]}) is None
    # Credit structure: net short -> negative value.
    credit_legs = [{"symbol": "A", "side": -1, "ratio": 1}]
    assert position_mid_from_quotes(credit_legs, quotes) == pytest.approx(-2.0)


class _StubTradeService:
    """Borrow TradeService._fill_value without constructing the real thing."""

    from app.services.trade_service import TradeService as _T

    _fill_value = _T._fill_value


def _plan(legs, entry_limit):
    return SimpleNamespace(legs=legs, entry_limit=entry_limit)


def test_fill_value_single_leg():
    svc = _StubTradeService()
    long_plan = _plan([{"side": 1}], 2.0)
    short_plan = _plan([{"side": -1}], -2.0)
    # Long entry/exit: unsigned premium stays positive.
    assert svc._fill_value(long_plan, 2.05, is_entry=True) == pytest.approx(2.05)
    assert svc._fill_value(long_plan, 3.10, is_entry=False) == pytest.approx(3.10)
    # Short: broker premium is unsigned; position value is negative.
    assert svc._fill_value(short_plan, 2.05, is_entry=True) == pytest.approx(-2.05)
    assert svc._fill_value(short_plan, 1.10, is_entry=False) == pytest.approx(-1.10)


def test_fill_value_mleg():
    svc = _StubTradeService()
    debit_plan = _plan([{"side": 1}, {"side": -1}], 1.2)
    credit_plan = _plan([{"side": -1}, {"side": 1}], -1.2)
    # Debit entry fills at a debit.
    assert svc._fill_value(debit_plan, 1.15, is_entry=True) == pytest.approx(1.15)
    # Credit entry: sign forced to the entry's credit orientation either way.
    assert svc._fill_value(credit_plan, 1.15, is_entry=True) == pytest.approx(-1.15)
    assert svc._fill_value(credit_plan, -1.15, is_entry=True) == pytest.approx(-1.15)
    # Exits are submitted with reversed legs: order price sign flips back
    # into position-value terms.
    assert svc._fill_value(debit_plan, -1.9, is_entry=False) == pytest.approx(1.9)
    assert svc._fill_value(credit_plan, 0.4, is_entry=False) == pytest.approx(-0.4)
    assert svc._fill_value(debit_plan, None, is_entry=False) is None
