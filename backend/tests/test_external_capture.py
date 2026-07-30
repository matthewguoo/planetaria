"""External-exit capture: when the broker liquidates a position out from
under the engine (auto_liquidate, expiry desk, manual close in their UI),
the trade record must carry the REAL chunked closing fills — premium, P/L,
and exit time — not blanks.

The fixture order shapes mirror the actual 2026-07-29 NVDA incident verified
against Alpaca paper: a 22-set MLEG auto_liquidate order (symbol=None parent,
per-leg children) plus two single-leg SIMPLE orders for the last 9 sets, at
very different prices. Also verified that day: GET /orders with a `symbols`
filter DROPS MLEG parents entirely — capture must scan unfiltered.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.models.trade import TradePlan
from app.services.exit_enforcer import ExitEnforcer
from app.services.plan_fsm import PlanStateMachine
from app.services.risk import RiskService
from app.services.trade_service import TradeService

LONG_SYM = "NVDA260729C00190000"
SHORT_SYM = "NVDA260729C00197500"
T0 = datetime(2026, 7, 29, 16, 5, tzinfo=timezone.utc)


class _Broadcast:
    def publish(self, topic, msg):
        pass


class _Market:
    def __init__(self):
        self.broadcast = _Broadcast()
        self.quotes = {}

    def latest_quote(self, symbol):
        return self.quotes.get(symbol)


def _order(**kw):
    defaults = dict(
        id="", client_order_id="", symbol=None, legs=None, filled_qty=0,
        filled_avg_price=0, position_intent=None, filled_at=None, submitted_at=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _broker_history(plan_id: str) -> list:
    """Closed-order history as Alpaca returns it WITHOUT a symbols filter."""
    entry = _order(  # our own entry MLEG — must be pruned including children
        id="entry-1", client_order_id=f"{plan_id}-e",
        filled_qty=31, filled_avg_price=1.83,
        submitted_at=T0, filled_at=T0,
        legs=[
            _order(id="entry-1a", client_order_id="broker-gen-1", symbol=LONG_SYM,
                   filled_qty=31, filled_avg_price=1.89, position_intent="buy_to_open",
                   filled_at=T0),
            _order(id="entry-1b", client_order_id="broker-gen-2", symbol=SHORT_SYM,
                   filled_qty=31, filled_avg_price=0.06, position_intent="sell_to_open",
                   filled_at=T0),
        ],
    )
    liq = _order(  # Alpaca auto_liquidate: MLEG parent, symbol=None
        id="liq-1", client_order_id="broker-liq-1",
        filled_qty=22, filled_avg_price=-3.59,
        submitted_at=T0 + timedelta(hours=3, minutes=25),
        filled_at=T0 + timedelta(hours=3, minutes=25),
        legs=[
            _order(id="liq-1a", client_order_id="broker-liq-1a", symbol=LONG_SYM,
                   filled_qty=22, filled_avg_price=3.60, position_intent="sell_to_close",
                   filled_at=T0 + timedelta(hours=3, minutes=25)),
            _order(id="liq-1b", client_order_id="broker-liq-1b", symbol=SHORT_SYM,
                   filled_qty=22, filled_avg_price=0.01, position_intent="buy_to_close",
                   filled_at=T0 + timedelta(hours=3, minutes=25)),
        ],
    )
    sweep_short = _order(  # expiry desk: single-leg SIMPLE closes
        id="sweep-1", client_order_id="broker-sweep-1", symbol=SHORT_SYM,
        filled_qty=9, filled_avg_price=0.01, position_intent="buy_to_close",
        submitted_at=T0 + timedelta(hours=3, minutes=53),
        filled_at=T0 + timedelta(hours=3, minutes=53),
    )
    sweep_long = _order(
        id="sweep-2", client_order_id="broker-sweep-2", symbol=LONG_SYM,
        filled_qty=9, filled_avg_price=0.75, position_intent="sell_to_close",
        submitted_at=T0 + timedelta(hours=3, minutes=53, seconds=9),
        filled_at=T0 + timedelta(hours=3, minutes=53, seconds=9),
    )
    return [sweep_long, sweep_short, liq, entry]


@pytest_asyncio.fixture
async def env(tmp_path):
    ns = SimpleNamespace()
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/test.db")

    ns.requests = []

    async def fake_call(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "get_orders":
            ns.requests.append(args[0])
            return _broker_history(ns.plan_id)
        raise AssertionError(f"unexpected broker call {fn}")

    def get_orders(*a, **k):  # named target for fake_call dispatch
        raise AssertionError("must go through alpaca.call")

    alpaca = SimpleNamespace(
        configured=True,
        call=fake_call,
        trading=SimpleNamespace(get_orders=get_orders),
    )
    market = _Market()
    risk = RiskService(db)
    trade = TradeService(db, alpaca, market, risk)
    enforcer = ExitEnforcer(db, market, trade)

    async with db.session() as session:
        plan = TradePlan(
            underlying="NVDA",
            strategy="call_debit_spread",
            legs=[
                {"symbol": LONG_SYM, "right": "C", "strike": 190.0,
                 "expiry": "2026-07-29", "side": 1, "ratio": 1, "entry": 1.95, "iv": 0.34},
                {"symbol": SHORT_SYM, "right": "C", "strike": 197.5,
                 "expiry": "2026-07-29", "side": -1, "ratio": 1, "entry": 0.085, "iv": 0.41},
            ],
            qty=31,
            entry_limit=1.86,
            tp_premium=3.11,
            sl_premium=1.47,
            time_stop_utc=T0 + timedelta(hours=3),
            status="filled",
            filled_qty=31,
            entry_order_id="entry-1",
            fill_premium=1.83,
            created_at=T0,
        )
        session.add(plan)
        await session.commit()
        ns.plan_id = plan.id

    ns.db, ns.trade, ns.enforcer = db, trade, enforcer
    yield ns
    await db.close()


@pytest.mark.asyncio
async def test_capture_weights_chunked_external_fills(env):
    plan = await env.trade.get_plan(env.plan_id)
    premium, sets_closed, exited_at, events, detail = (
        await env.enforcer._capture_external_exit(plan)
    )
    # long leg avg = (22*3.60 + 9*0.75)/31 = 2.772581; short avg = 0.01
    assert premium == pytest.approx(2.7626, abs=1e-4)
    assert sets_closed == 31
    assert exited_at == T0 + timedelta(hours=3, minutes=53, seconds=9)
    assert "recovered" in detail
    # the scan must NOT pass a symbols filter (it drops MLEG parents)
    assert all(getattr(r, "symbols", None) in (None, []) for r in env.requests)
    # two closing WAVES: 22 sets @ 3.59 net, then 9 sets @ 0.74 net
    assert events is not None and len(events) == 2
    assert events[0]["qty"] == 22
    assert events[0]["premium"] == pytest.approx(3.59, abs=1e-4)
    assert events[1]["qty"] == 9
    assert events[1]["premium"] == pytest.approx(0.74, abs=1e-4)
    assert events[0]["ts"] < events[1]["ts"]


@pytest.mark.asyncio
async def test_force_close_records_real_pnl_and_exit_time(env):
    plan = await env.trade.get_plan(env.plan_id)
    await env.enforcer._force_close_with_capture(plan, "position gone at broker")
    closed = await env.trade.get_plan(env.plan_id)
    assert closed.status == "closed"
    assert closed.exit_premium == pytest.approx(2.7626, abs=1e-4)
    # (2.7626 - 1.83) * 100 * 31
    assert closed.realized_pnl == pytest.approx(2891.06, abs=0.5)
    assert closed.exit_reason == "external"
    assert closed.exited_at is not None
    assert "recovered" in (closed.notes or "")
    assert closed.exit_fills is not None and len(closed.exit_fills) == 2


@pytest.mark.asyncio
async def test_repair_backfills_blank_closed_plan(env):
    # Simulate the legacy failure: plan force-closed with NO exit data.
    fsm = PlanStateMachine(env.db, _Broadcast())
    await fsm.update_fields(
        env.plan_id, status="closed",
        notes="position vanished at broker during exit",
    )
    plan = await env.trade.get_plan(env.plan_id)
    assert plan.exit_premium is None

    await env.enforcer._repair_blank_exits()

    repaired = await env.trade.get_plan(env.plan_id)
    assert repaired.exit_premium == pytest.approx(2.7626, abs=1e-4)
    assert repaired.realized_pnl == pytest.approx(2891.06, abs=0.5)
    assert repaired.exited_at is not None
    assert repaired.exit_reason == "external"
    assert repaired.exit_fills is not None and len(repaired.exit_fills) == 2
    assert "backfilled" in repaired.notes
    # original context preserved
    assert "position vanished" in repaired.notes


@pytest.mark.asyncio
async def test_repair_recaptures_external_close_missing_waves(env):
    """A plan repaired by the pre-wave build (premium set, no exit_fills)
    gets re-captured so the chart can draw one marker per closing wave."""
    fsm = PlanStateMachine(env.db, _Broadcast())
    await fsm.update_fields(
        env.plan_id, status="closed", exit_premium=2.7626,
        realized_pnl=2891.06, exit_reason="external",
        notes="exit backfilled from broker history: closing fills recovered from broker history",
    )
    await env.enforcer._repair_blank_exits()
    repaired = await env.trade.get_plan(env.plan_id)
    assert repaired.exit_fills is not None and len(repaired.exit_fills) == 2
    # notes not duplicated on re-capture
    assert repaired.notes.count("backfilled") == 1
