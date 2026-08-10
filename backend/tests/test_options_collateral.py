"""Fleet options-collateral guard: a strategy allocation must price what the
broker HOLDS, not what the premium costs. An uncovered short put is
cash-secured at ~strike x 100 (verified against the paper API 2026-07-29:
~$70,087 held for one 700P), so premium-based accounting lets a $10k
allocation commit the whole account. options_required_capital is the gate."""

from app.services.strategy_runner import options_required_capital


def leg(right, strike, side, ratio=1):
    return {"symbol": f"X{right}{int(strike)}", "right": right,
            "strike": float(strike), "expiry": "2026-12-18",
            "side": side, "ratio": ratio, "entry": 1.0}


def test_cash_secured_put_holds_the_strike():
    required, fatal = options_required_capital(
        [leg("P", 700, -1)], qty=1, entry_limit=-5.0)
    assert fatal is None
    assert required == 700 * 100


def test_uncovered_short_call_is_fatal():
    required, fatal = options_required_capital(
        [leg("C", 560, -1)], qty=1, entry_limit=-2.0)
    assert fatal is not None
    assert "uncovered short call" in fatal


def test_iron_fly_holds_both_widths():
    legs = [leg("C", 560, -1), leg("P", 560, -1),
            leg("C", 566, 1), leg("P", 554, 1)]
    required, fatal = options_required_capital(legs, qty=3, entry_limit=-3.0)
    assert fatal is None
    assert required == (6 + 6) * 100 * 3


def test_credit_spread_holds_the_width():
    legs = [leg("C", 100, -1), leg("C", 105, 1)]
    required, fatal = options_required_capital(legs, qty=2, entry_limit=-1.2)
    assert fatal is None
    assert required == 5 * 100 * 2


def test_debit_spread_is_conservative_width_plus_debit():
    # True max loss is the 2.0 debit; the guard deliberately counts width
    # + debit. Overcounting is acceptable, undercounting never is.
    legs = [leg("C", 100, 1), leg("C", 105, -1)]
    required, fatal = options_required_capital(legs, qty=1, entry_limit=2.0)
    assert fatal is None
    assert required == 5 * 100 + 2.0 * 100


def test_ratio_short_puts_cover_one_leave_one_naked():
    legs = [leg("P", 100, -1, ratio=2), leg("P", 95, 1)]
    required, fatal = options_required_capital(legs, qty=1, entry_limit=-1.0)
    assert fatal is None
    assert required == 5 * 100 + 100 * 100      # one width + one CSP


def test_long_only_debit_structure_holds_its_debit():
    legs = [leg("C", 100, 1), leg("P", 100, 1)]     # long straddle
    required, fatal = options_required_capital(legs, qty=2, entry_limit=4.2)
    assert fatal is None
    assert required == 4.2 * 100 * 2


def test_missing_right_is_fatal():
    bad = [{"symbol": "SPY", "side": 1, "ratio": 1}]
    _, fatal = options_required_capital(bad, qty=1, entry_limit=1.0)
    assert fatal is not None


def test_nearest_long_pairs_first():
    # Two shorts at 100/110; longs at 95 and 111. Nearest-first pairing:
    # 110 pairs with 111 (width 1), 100 pairs with 95 (width 5).
    legs = [leg("C", 100, -1), leg("C", 110, -1),
            leg("C", 95, 1), leg("C", 111, 1)]
    required, fatal = options_required_capital(legs, qty=1, entry_limit=-0.5)
    assert fatal is None
    assert required == (1 + 5) * 100
