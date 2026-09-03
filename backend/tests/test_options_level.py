"""ACCOUNT CAPABILITY: `options_level` bounds the option shapes the risk gate
accepts — level 2 is one long leg, below 2 is nothing, 3 is the broker's
own limit. The UI hides what the level cannot place; this is the server's
half of that promise."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.risk import RiskService

ET = ZoneInfo("America/New_York")

LONG_CALL = [{"symbol": "SPY260918C00600000", "right": "C", "strike": 600, "side": 1,
              "ratio": 1, "entry": 2.0, "half_spread": 0.02}]
DEBIT_SPREAD = LONG_CALL + [{"symbol": "SPY260918C00605000", "right": "C", "strike": 605,
                             "side": -1, "ratio": 1, "entry": 1.0, "half_spread": 0.02}]


@pytest_asyncio.fixture
async def risk(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/lvl.db")
    yield RiskService(db)
    await db.close()


def _base(**overrides):
    stop = (datetime.now(ET) + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    base = dict(
        account_equity=11_000.0,
        entry_cost_dollars=200.0,
        max_loss_dollars=100.0,
        time_stop_utc=stop,
        expiry_date_et="2026-09-18",
        legs=LONG_CALL,
        underlying="SPY",
        stream_age_s=1.0,
        daytrade_count=0,
        asset_class="option",
        strategy_id=None,
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_default_level_3_accepts_spreads(risk):
    assert (await risk.get_settings())["options_level"] == 3
    v = await risk.validate_new_trade(**_base(legs=DEBIT_SPREAD))
    assert not any("options level" in s for s in v)


@pytest.mark.asyncio
async def test_level_2_is_long_single_leg_only(risk):
    await risk.update_settings({"options_level": 2})
    assert not any("options level" in s for s in await risk.validate_new_trade(**_base()))
    v = await risk.validate_new_trade(**_base(legs=DEBIT_SPREAD))
    assert any("options level 2" in s for s in v)
    short_put = [dict(LONG_CALL[0], right="P", side=-1)]
    v = await risk.validate_new_trade(**_base(legs=short_put))
    assert any("options level 2" in s for s in v)


@pytest.mark.asyncio
async def test_level_0_refuses_all_option_entries_but_not_shares(risk):
    await risk.update_settings({"options_level": 0})
    v = await risk.validate_new_trade(**_base())
    assert any("options level 0" in s for s in v)
    shares = [{"symbol": "TQQQ", "side": 1, "ratio": 1, "entry": 80.0, "half_spread": 0.01}]
    v = await risk.validate_new_trade(
        **_base(legs=shares, underlying="TQQQ", asset_class="equity", expiry_date_et="")
    )
    assert not any("options level" in s for s in v)


@pytest.mark.asyncio
async def test_level_is_validated(risk):
    with pytest.raises(ValueError):
        await risk.update_settings({"options_level": 4})
    with pytest.raises(ValueError):
        await risk.update_settings({"options_level": -1})
