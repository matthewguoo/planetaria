"""freshest_spot: the frozen-quote guard (quotes vs printing tape)."""

from app.services.market_data import freshest_spot


def quote(mid: float, ts_ms: int) -> dict:
    return {"mid": mid, "bid": mid - 0.1, "ask": mid + 0.1, "ts": ts_ms}


def bar(close: float, start_ms: int) -> dict:
    return {"t": start_ms, "o": close, "h": close, "l": close, "c": close, "v": 1}


NOW = 1_785_400_000_000


def test_fresh_quote_wins():
    assert freshest_spot(quote(741.0, NOW), bar(740.5, NOW - 120_000)) == 741.0


def test_frozen_quote_loses_to_printing_tape():
    # Quote from yesterday's close, bars printing now -> bar close wins.
    stale = quote(741.04, NOW - 16 * 3600 * 1000)
    assert freshest_spot(stale, bar(743.56, NOW - 60_000)) == 743.56


def test_quote_within_grace_of_bar_wins():
    # Quote 30s older than bar close: still inside the 60s grace.
    q = quote(741.2, NOW - 30_000)
    assert freshest_spot(q, bar(741.5, NOW - 60_000)) == 741.2


def test_no_bars_falls_back_to_quote():
    assert freshest_spot(quote(741.0, NOW - 3600_000), None) == 741.0


def test_no_quote_falls_back_to_bar():
    assert freshest_spot(None, bar(742.0, NOW)) == 742.0


def test_zero_mid_quote_ignored():
    q = {"mid": 0.0, "bid": 0, "ask": 0, "ts": NOW}
    assert freshest_spot(q, bar(742.0, NOW)) == 742.0


def test_nothing_returns_zero():
    assert freshest_spot(None, None) == 0.0
