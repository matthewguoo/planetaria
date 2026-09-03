"""Extended hours are not an outage. The free tier's stream is dark before
08:00 and after 17:00 ET while the venues keep printing; the poller refreshes
quotes there, and the UI's position marks fall back to the broker's own
position prices when the quote cache has nothing usable."""

from app.services.market_data import EXT_STALE_S, ext_hours_poll_due
from app.services.trade_service import mark_with_fallback

LEG = [{"symbol": "AAPX", "side": 1, "ratio": 1}]
SPREAD = [
    {"symbol": "SPY260918C00600000", "side": 1, "ratio": 1},
    {"symbol": "SPY260918C00605000", "side": -1, "ratio": 1},
]


def test_poller_runs_only_in_the_two_extended_sessions_and_only_when_stale():
    assert ext_hours_poll_due("premarket", None)
    assert ext_hours_poll_due("postmarket", EXT_STALE_S + 1)
    assert not ext_hours_poll_due("premarket", 5.0)  # fresh enough
    assert not ext_hours_poll_due("rth", 999.0)  # the stream owns RTH
    assert not ext_hours_poll_due("overnight", 999.0)  # Blue Ocean poller owns it
    assert not ext_hours_poll_due(None, 999.0)  # weekend: nothing prints


def test_live_quote_wins_and_is_tagged_quote():
    mid, src = mark_with_fallback(LEG, {"AAPX": {"bid": 20.0, "ask": 20.2, "mid": 20.1}}, {"AAPX": 19.0})
    assert (mid, src) == (20.1, "quote")


def test_broker_price_fills_a_dark_quote_cache():
    mid, src = mark_with_fallback(LEG, {"AAPX": None}, {"AAPX": 19.5})
    assert (mid, src) == (19.5, "broker")
    # multi-leg: every leg must resolve, mixing live and broker legs is fine
    quotes = {"SPY260918C00600000": {"bid": 5.0, "ask": 5.2, "mid": 5.1}, "SPY260918C00605000": None}
    mid, src = mark_with_fallback(SPREAD, quotes, {"SPY260918C00605000": 2.0})
    assert src == "broker"
    assert mid == 5.1 - 2.0


def test_nothing_to_mark_stays_honest():
    assert mark_with_fallback(LEG, {"AAPX": None}, {}) == (None, None)
    assert mark_with_fallback(LEG, {"AAPX": None}, {"AAPX": 0.0}) == (None, None)
