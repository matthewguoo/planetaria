"""is_overnight_et: the Blue Ocean overnight window (Sun 20:00 -> Fri 04:00 ET)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.market_data import is_overnight_et

ET = ZoneInfo("America/New_York")


def et(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


# 2026-07-27 is a Monday.
def test_weeknight_windows():
    assert is_overnight_et(et(2026, 7, 27, 21))      # Mon 21:00
    assert is_overnight_et(et(2026, 7, 28, 3, 30))   # Tue 03:30
    assert not is_overnight_et(et(2026, 7, 28, 4))   # Tue 04:00 -> premarket
    assert not is_overnight_et(et(2026, 7, 28, 12))  # Tue midday
    assert not is_overnight_et(et(2026, 7, 28, 19, 59))  # Tue AH
    assert is_overnight_et(et(2026, 7, 28, 20))      # Tue 20:00


def test_weekend_boundaries():
    assert is_overnight_et(et(2026, 7, 31, 3, 59))   # Fri 03:59 (Thu session tail)
    assert not is_overnight_et(et(2026, 7, 31, 20, 30))  # Fri night -> closed
    assert not is_overnight_et(et(2026, 8, 1, 22))   # Sat -> closed
    assert not is_overnight_et(et(2026, 8, 2, 12))   # Sun midday -> closed
    assert is_overnight_et(et(2026, 8, 2, 20, 5))    # Sun 20:05 -> session opens
