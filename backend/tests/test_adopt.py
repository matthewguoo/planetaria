"""Adoption flow: untracked broker option positions become ONE managed
multi-leg TradePlan per underlying, with TP/SL brackets on the signed
premium axis and the exit enforcer armed."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.risk import RiskService
from app.services.trade_service import TradeService


class _Broadcast:
    def __init__(self):
        self.published = []

    def publish(self, topic, msg):
        self.published.append((topic, msg))


class _Enforcer:
    def __init__(self):
        self.armed = []

    async def arm(self, plan_id):
        self.armed.append(plan_id)


@pytest_asyncio.fixture
async def service(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    alpaca = SimpleNamespace(configured=True)
    market = SimpleNamespace(broadcast=_Broadcast())
    risk = RiskService(db)
    svc = TradeService(db, alpaca, market, risk)
    svc.enforcer = _Enforcer()
    yield svc
    await db.close()


def _pos(symbol, qty, avg, underlying, expiry, right, strike):
    return {
        "symbol": symbol,
        "qty": qty,
        "side": 1 if qty > 0 else -1,
        "asset_class": "option",
        "avg_entry_price": avg,
        "current_price": None,
        "market_value": None,
        "unrealized_pl": None,
        "occ": {"underlying": underlying, "expiry": expiry, "right": right, "strike": strike},
    }


@pytest.mark.asyncio
async def test_adopt_groups_legs_into_one_plan(service, monkeypatch):
    positions = [
        _pos("SPY260731C00450000", 2, 3.0, "SPY", "2026-07-31", "C", 450.0),
        _pos("SPY260731C00455000", -2, 1.5, "SPY", "2026-07-31", "C", 455.0),
    ]

    async def fake_broker_positions(max_age_s=5.0):
        return positions

    monkeypatch.setattr(service, "broker_positions", fake_broker_positions)
    time_stop = datetime.now(timezone.utc) + timedelta(hours=2)

    adopted = await service.adopt_positions(
        [p["symbol"] for p in positions], tp_pct=1.0, sl_pct=0.5, time_stop_utc=time_stop
    )

    assert len(adopted) == 1  # ONE trade object for the whole spread
    plan = adopted[0]
    assert plan["underlying"] == "SPY"
    assert plan["strategy"] == "adopted"
    assert plan["qty"] == 2  # gcd of |2| and |-2|
    assert len(plan["legs"]) == 2
    assert [leg["ratio"] for leg in plan["legs"]] == [1, 1]
    # entry = +3.0 - 1.5 = 1.5 debit/share
    assert plan["entry_limit"] == pytest.approx(1.5)
    assert plan["fill_premium"] == pytest.approx(1.5)
    assert plan["tp_premium"] == pytest.approx(3.0)
    assert plan["sl_premium"] == pytest.approx(0.75)
    assert plan["status"] == "filled"
    assert service.enforcer.armed == [plan["id"]]
    # And it's no longer untracked.
    assert await service.untracked_positions() == []


@pytest.mark.asyncio
async def test_adopt_credit_group_brackets_correctly(service, monkeypatch):
    positions = [
        _pos("QQQ260731P00380000", -1, 2.0, "QQQ", "2026-07-31", "P", 380.0),
        _pos("QQQ260731P00375000", 1, 0.8, "QQQ", "2026-07-31", "P", 375.0),
    ]

    async def fake_broker_positions(max_age_s=5.0):
        return positions

    monkeypatch.setattr(service, "broker_positions", fake_broker_positions)
    time_stop = datetime.now(timezone.utc) + timedelta(hours=2)

    adopted = await service.adopt_positions(
        [p["symbol"] for p in positions], tp_pct=0.5, sl_pct=0.5, time_stop_utc=time_stop
    )
    plan = adopted[0]
    # entry = -2.0 + 0.8 = -1.2 credit; SL < entry < TP on the signed axis.
    assert plan["entry_limit"] == pytest.approx(-1.2)
    assert plan["sl_premium"] < plan["entry_limit"] < plan["tp_premium"]
    assert plan["tp_premium"] == pytest.approx(-0.6)
    assert plan["sl_premium"] == pytest.approx(-1.8)


@pytest.mark.asyncio
async def test_adopt_rejects_unknown_symbols(service, monkeypatch):
    async def fake_broker_positions(max_age_s=5.0):
        return []

    monkeypatch.setattr(service, "broker_positions", fake_broker_positions)
    with pytest.raises(ValueError):
        await service.adopt_positions(
            ["SPY260731C00450000"], 1.0, 0.5, datetime.now(timezone.utc)
        )
