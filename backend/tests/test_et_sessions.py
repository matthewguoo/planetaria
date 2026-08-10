"""One session table, three views of it. equity_session is the verified
24/5 truth; et_session and is_overnight_et delegate to it. These tests pin
the delegation and the two week-boundary bugs it fixed: Friday 20:00+ used
to read "overnight" on the timer-event label while the market was closed,
and Sunday 20:00+ read "weekend" while Blue Ocean was open."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.market_clock import ET, equity_session
from app.services.market_data import is_overnight_et
from app.services.signals.feeds import et_session

UTC = ZoneInfo("UTC")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """ET instant in the week of Mon 2026-08-03 .. Sun 2026-08-09."""
    return datetime(2026, 8, 3 + day, hour, minute, tzinfo=ET)


def test_week_boundaries():
    # Sunday evening: Blue Ocean opens at 20:00 — that is a session, not
    # the weekend.
    assert equity_session(at(6, 19, 59)) is None
    assert et_session(at(6, 19, 59)) == "weekend"
    assert equity_session(at(6, 20, 0)) == "overnight"
    assert et_session(at(6, 20, 0)) == "overnight"
    assert is_overnight_et(at(6, 20, 0).astimezone(UTC))

    # Friday evening: the 24/5 week ends at 20:00 — closed, not overnight.
    assert equity_session(at(4, 19, 59)) == "postmarket"
    assert equity_session(at(4, 20, 0)) is None
    assert et_session(at(4, 20, 0)) == "weekend"
    assert not is_overnight_et(at(4, 20, 0).astimezone(UTC))

    # Saturday: nothing anywhere.
    assert equity_session(at(5, 12, 0)) is None
    assert et_session(at(5, 12, 0)) == "weekend"
    assert not is_overnight_et(at(5, 12, 0).astimezone(UTC))


def test_weekday_ladder():
    assert equity_session(at(1, 3, 0)) == "overnight"
    assert equity_session(at(1, 5, 0)) == "premarket"
    assert equity_session(at(1, 10, 0)) == "rth"
    assert equity_session(at(1, 17, 0)) == "postmarket"
    assert equity_session(at(1, 21, 0)) == "overnight"


def test_three_views_agree_every_minute_of_the_week():
    """The delegation makes disagreement impossible by construction; this
    sweep keeps it impossible if someone re-inlines one of them."""
    t = at(0, 0, 0)
    for _ in range(7 * 24 * 60 // 5):
        s = equity_session(t)
        assert et_session(t) == (s or "weekend")
        assert is_overnight_et(t.astimezone(UTC)) == (s == "overnight")
        t += timedelta(minutes=5)
