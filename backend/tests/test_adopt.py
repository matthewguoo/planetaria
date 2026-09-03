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


class _FakeAlpaca:
    """Fake AlpacaService exercising the real broker_positions() code path:
    call() actually invokes the SDK fn, unlike tests that monkeypatch
    broker_positions wholesale."""

    def __init__(self, positions):
        self.configured = True
        self.get_all_calls = 0

        def get_all_positions():
            self.get_all_calls += 1
            return positions

        self.trading = SimpleNamespace(get_all_positions=get_all_positions)

    async def call(self, fn, /, *args, **kwargs):
        return fn(*args)


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
async def test_broker_positions_normalizes_and_caches(service):
    # Regression: the cache-store line referenced a nonexistent `_time`
    # alias, so every real broker_positions() call raised NameError (and
    # every caller swallowed it). Run the REAL method end to end.
    raw = [
        SimpleNamespace(
            symbol="SPY260731C00450000",
            qty="2",
            asset_class="us_option",
            avg_entry_price="3.0",
            current_price="3.5",
            market_value="700",
            unrealized_pl="100",
        ),
        SimpleNamespace(
            symbol="AAPL",
            qty="-10",
            asset_class="us_equity",
            avg_entry_price="200",
            current_price=None,
            market_value=None,
            unrealized_pl=None,
        ),
    ]
    service.alpaca = _FakeAlpaca(raw)

    out = await service.broker_positions()

    assert [p["symbol"] for p in out] == ["SPY260731C00450000", "AAPL"]
    opt, stk = out
    assert opt["asset_class"] == "option"
    assert opt["occ"] == {
        "underlying": "SPY", "expiry": "2026-07-31", "right": "C", "strike": 450.0
    }
    assert opt["qty"] == 2.0 and opt["side"] == 1
    assert stk["asset_class"] == "stock" and stk["occ"] is None
    assert stk["qty"] == -10.0 and stk["side"] == -1

    # Second call within max_age_s must come from the cache.
    assert await service.broker_positions() == out
    assert service.alpaca.get_all_calls == 1


def _stock(symbol, qty, avg):
    return {
        "symbol": symbol,
        "qty": qty,
        "side": 1 if qty > 0 else -1,
        "asset_class": "stock",
        "avg_entry_price": avg,
        "current_price": None,
        "market_value": None,
        "unrealized_pl": None,
        "occ": None,
    }


@pytest.mark.asyncio
async def test_adopt_stock_positions_as_single_leg_equity_plans(service, monkeypatch):
    """The live Roth's leveraged ETFs: whole shares adopt, fractional residue
    stays untracked, stops are % of the share basis (multiplier 1)."""
    positions = [
        _stock("AAPX", 100, 38.60),
        _stock("AVGG", 12.48, 24.73),   # fractional: adopts floor = 12
        _stock("DUST", 0.4, 10.0),      # under one share: skipped
        _pos("SPY260731C00450000", 2, 3.0, "SPY", "2026-07-31", "C", 450.0),
    ]

    async def fake_broker_positions(max_age_s=5.0):
        return positions

    monkeypatch.setattr(service, "broker_positions", fake_broker_positions)
    service.alpaca = SimpleNamespace(
        configured=True, settings=SimpleNamespace(alpaca_account_name="live_roth"))
    time_stop = datetime.now(timezone.utc) + timedelta(days=30)

    adopted = await service.adopt_positions(
        [p["symbol"] for p in positions], tp_pct=0.20, sl_pct=0.10, time_stop_utc=time_stop
    )

    by_sym = {p["underlying"]: p for p in adopted}
    assert set(by_sym) == {"AAPX", "AVGG", "SPY"}  # DUST skipped, SPY still options
    aapx = by_sym["AAPX"]
    assert aapx["asset_class"] == "equity"
    assert aapx["qty"] == 100 and aapx["filled_qty"] == 100
    assert aapx["legs"] == [{"symbol": "AAPX", "side": 1, "ratio": 1, "entry": 38.60}]
    assert aapx["entry_limit"] == pytest.approx(38.60)
    assert aapx["tp_premium"] == pytest.approx(38.60 * 1.20)
    assert aapx["sl_premium"] == pytest.approx(38.60 * 0.90)
    assert aapx["status"] == "filled"
    assert aapx["account"] == "live_roth"
    assert by_sym["AVGG"]["qty"] == 12
    assert by_sym["SPY"]["asset_class"] == "option"
    assert by_sym["SPY"]["account"] == "live_roth"  # options branch stamps too now
    assert set(service.enforcer.armed) == {p["id"] for p in adopted}
    # The equity legs are covered; the fractional residue of AVGG is not a
    # separate broker position, so nothing stays untracked but DUST.
    assert [p["symbol"] for p in await service.untracked_positions()] == ["DUST"]


@pytest.mark.asyncio
async def test_adopt_shares_refuses_option_defaults(service, monkeypatch):
    """A bare adopt call carries the OPTION defaults (50% stop, today's
    cutoff): on shares that is a same-day forced sale. Refused unless the
    caller set sl_pct and time_stop_utc explicitly."""
    positions = [_stock("AAPX", 100, 38.60)]

    async def fake_broker_positions(max_age_s=5.0):
        return positions

    monkeypatch.setattr(service, "broker_positions", fake_broker_positions)
    with pytest.raises(ValueError, match="explicit sl_pct and time_stop_utc"):
        await service.adopt_positions(
            ["AAPX"], 1.0, 0.5, datetime.now(timezone.utc), explicit_exits=False
        )
    assert service.enforcer.armed == []
    # Options with the defaults are still fine (that is what the defaults are for).
    positions[:] = [_pos("SPY260731C00450000", 2, 3.0, "SPY", "2026-07-31", "C", 450.0)]
    adopted = await service.adopt_positions(
        ["SPY260731C00450000"], 1.0, 0.5, datetime.now(timezone.utc), explicit_exits=False
    )
    assert len(adopted) == 1


@pytest.mark.asyncio
async def test_adopt_rejects_unknown_symbols(service, monkeypatch):
    async def fake_broker_positions(max_age_s=5.0):
        return []

    monkeypatch.setattr(service, "broker_positions", fake_broker_positions)
    with pytest.raises(ValueError):
        await service.adopt_positions(
            ["SPY260731C00450000"], 1.0, 0.5, datetime.now(timezone.utc)
        )
