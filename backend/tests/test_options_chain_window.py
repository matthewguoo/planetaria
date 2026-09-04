"""The chain's expiry window is counted in weekdays: a Friday's 3-DTE chain
must reach into next week, and a long weekend must not empty it."""

from datetime import date

from app.services.options_chain import weekdays_ahead


def test_weekdays_ahead_crosses_weekends():
    fri = date(2026, 9, 4)                       # Friday before Labor Day
    assert weekdays_ahead(fri, 0) == fri
    assert weekdays_ahead(fri, 1) == date(2026, 9, 7)   # Monday (holiday: not skipped)
    assert weekdays_ahead(fri, 3) == date(2026, 9, 9)   # Wednesday: Tue + Wed expiries in
    assert weekdays_ahead(date(2026, 9, 1), 3) == date(2026, 9, 4)  # Tue -> Fri, same as calendar
    assert weekdays_ahead(date(2026, 9, 5), 1) == date(2026, 9, 7)  # a Saturday start
    assert weekdays_ahead(fri, -2) == fri
