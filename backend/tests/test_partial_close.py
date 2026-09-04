"""Manual partial close: N of M leave under their own order, the plan keeps
its stop for the remainder, the resting TP re-sizes, realized P/L
accumulates instead of being overwritten, and the ladder takes a pending
partial down before closing the rest."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.models.trade import TradePlan
from app.services.exit_enforcer import ExitEnforcer
from app.services.plan_fsm import PlanEvent
from app.services.risk import RiskService
from app.services.trade_service import TradeService


class FakeOrder:
    def __init__(self, oid, req, status="accepted", filled_qty=0, price=None):
        self.id = oid
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = price
        self.filled_at = datetime.now(timezone.utc)
        self.symbol = getattr(req, "symbol", None)
        self.client_order_id = getattr(req, "client_order_id", "")
        self.qty = getattr(req, "qty", None)
        self.limit_price = getattr(req, "limit_price", None)


class FakeAlpaca:
    """Fills market closes instantly at `price`; limit closes rest."""

    def __init__(self, price=1.50):
        self.configured = True
        self.price = price
        self.orders: dict[str, FakeOrder] = {}
        self.cancelled: list[str] = []
        self.submitted: list = []
        self._n = 0
        self.settings = SimpleNamespace(trading_mode="paper", alpaca_paper=True, alpaca_account_name="t")
        self.trading = SimpleNamespace(
            submit_order=self._submit, get_order_by_id=self._get, cancel_order_by_id=self._cancel,
            get_order_by_client_id=self._by_client, get_all_positions=lambda: [],
            get_account=lambda: SimpleNamespace(equity="10000", cash="10000", buying_power="10000",
                                                daytrade_count=0, status="ACTIVE"),
        )

    def _submit(self, req):
        self._n += 1
        self.submitted.append(req)
        market = getattr(req, "limit_price", None) is None
        qty = int(float(getattr(req, "qty", 1) or 1))
        o = FakeOrder(f"o{self._n}", req, "filled" if market else "accepted",
                      qty if market else 0, self.price if market else None)
        self.orders[o.id] = o
        return o

    def _get(self, oid):
        return self.orders[oid]

    def _cancel(self, oid):
        self.cancelled.append(oid)
        o = self.orders[oid]
        if o.status != "filled":
            o.status = "canceled"

    def _by_client(self, cid):
        for o in self.orders.values():
            if o.client_order_id == cid:
                return o
        raise KeyError(cid)

    def fill(self, oid, qty, price):
        o = self.orders[oid]
        o.status, o.filled_qty, o.filled_avg_price = "filled", qty, price

    async def call(self, fn, /, *args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("retries", None)
        return fn(*args, **kwargs)


class QuietMarket:
    stream_age_s = None

    def __init__(self):
        self.broadcast = SimpleNamespace(publish=lambda *a, **k: None, subscribe=lambda *a, **k: None)

    def latest_quote(self, symbol):
        return {"bid": 1.49, "ask": 1.51, "mid": 1.50, "ts": 0, "src": "test"}

    async def fetch_latest_stock_quote(self, symbol):
        return self.latest_quote(symbol)

    def spot(self, symbol):
        return 100.0


@pytest_asyncio.fixture
async def rig(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    alpaca = FakeAlpaca()
    market = QuietMarket()
    risk = RiskService(db)
    trade = TradeService(db, alpaca, market, risk)
    enforcer = ExitEnforcer(db, market, trade)
    enforcer.partial_wait_s = 2.0
    enforcer.arm = _noop_arm  # no monitor tasks in this rig
    enforcer.disarm = lambda plan_id: None
    yield SimpleNamespace(db=db, alpaca=alpaca, trade=trade, enforcer=enforcer, risk=risk)
    await db.close()


async def _noop_arm(plan_id):
    return None


async def _held_plan(db, qty=5, tp=2.0, sl=0.5, tp_order_id=None) -> TradePlan:
    plan = TradePlan(
        underlying="SPY", strategy="long_call",
        legs=[{"symbol": "SPY260918C00650000", "right": "C", "strike": 650.0,
               "expiry": "2026-09-18", "side": 1, "ratio": 1, "entry": 1.0, "iv": 0.2}],
        qty=qty, entry_limit=1.0, tp_premium=tp, sl_premium=sl,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(days=5),
        status="filled", filled_qty=qty, fill_premium=1.0, entry_order_id="e1",
        tp_order_id=tp_order_id,
    )
    async with db.session() as s:
        s.add(plan)
        await s.commit()
        await s.refresh(plan)
    return plan


@pytest.mark.asyncio
async def test_market_partial_reduces_the_plan_and_keeps_the_stop(rig):
    plan = await _held_plan(rig.db, qty=5)
    out = await rig.enforcer.manual_close(plan.id, qty=2)
    assert out["mode"] == "partial" and out["status"] == "filled"
    assert out["closed_qty"] == 2 and out["remaining_qty"] == 3
    fresh = await rig.trade.get_plan(plan.id)
    assert fresh.status == "filled" and fresh.filled_qty == 3 and fresh.qty == 3
    assert fresh.sl_premium == 0.5 and fresh.partial_exit is None
    waves = fresh.exit_fills
    assert len(waves) == 1 and waves[0]["kind"] == "partial" and waves[0]["qty"] == 2
    assert waves[0]["realized"] == pytest.approx((1.5 - 1.0) * 100 * 2)
    assert fresh.partial_realized == pytest.approx(100.0)
    # the closing order carried the partial quantity and its own key
    req = rig.alpaca.submitted[-1]
    assert int(req.qty) == 2 and req.client_order_id.startswith(f"{plan.id}-xp")
    # a later final close adds the partial's P/L instead of losing it
    assert fresh.close_pnl(2.0) == pytest.approx((2.0 - 1.0) * 100 * 3 + 100.0)
    events = await rig.trade.fsm.events_for(plan.id)
    assert {e["event"] for e in events} >= {"exit_partial_submitted", "exit_partial"}


@pytest.mark.asyncio
async def test_limit_partial_rests_and_the_stream_finishes_it(rig):
    plan = await _held_plan(rig.db, qty=4)
    out = await rig.enforcer.manual_close(plan.id, qty=1, order_type="limit", limit_price=1.80)
    assert out["status"] == "resting"
    fresh = await rig.trade.get_plan(plan.id)
    assert fresh.partial_exit["order_id"] == out["order_id"] and fresh.filled_qty == 4
    # the same plan refuses a second partial while one rests
    with pytest.raises(ValueError, match="already pending"):
        await rig.enforcer.manual_close(plan.id, qty=1)
    # the broker fills it; the trade-update path resolves the order by partial_exit
    rig.alpaca.fill(out["order_id"], 1, 1.80)
    update = SimpleNamespace(event="fill", order=rig.alpaca.orders[out["order_id"]])
    await rig.trade.on_trade_update(update)
    fresh = await rig.trade.get_plan(plan.id)
    assert fresh.filled_qty == 3 and fresh.partial_exit is None
    assert fresh.exit_fills[-1]["premium"] == pytest.approx(1.80)


@pytest.mark.asyncio
async def test_dead_partial_order_clears_without_changing_qty(rig):
    plan = await _held_plan(rig.db, qty=4)
    out = await rig.enforcer.manual_close(plan.id, qty=1, order_type="limit", limit_price=1.80)
    rig.alpaca.orders[out["order_id"]].status = "canceled"
    await rig.trade.on_trade_update(SimpleNamespace(event="canceled", order=rig.alpaca.orders[out["order_id"]]))
    fresh = await rig.trade.get_plan(plan.id)
    assert fresh.partial_exit is None and fresh.filled_qty == 4


@pytest.mark.asyncio
async def test_partial_takes_the_resting_tp_down_and_resizes_it(rig):
    plan = await _held_plan(rig.db, qty=5)
    await rig.trade.submit_resting_tp(plan)
    plan = await rig.trade.get_plan(plan.id)
    tp_id = plan.tp_order_id
    assert tp_id and int(rig.alpaca.orders[tp_id].qty) == 5
    await rig.enforcer.manual_close(plan.id, qty=2)
    assert tp_id in rig.alpaca.cancelled
    fresh = await rig.trade.get_plan(plan.id)
    assert fresh.tp_order_id is None and fresh.filled_qty == 3
    await rig.trade.submit_resting_tp(fresh)
    fresh = await rig.trade.get_plan(plan.id)
    assert int(rig.alpaca.orders[fresh.tp_order_id].qty) == 3


@pytest.mark.asyncio
async def test_ladder_cancels_a_pending_partial_first(rig):
    plan = await _held_plan(rig.db, qty=4)
    out = await rig.enforcer.manual_close(plan.id, qty=1, order_type="limit", limit_price=1.80)
    pending = out["order_id"]
    rig.enforcer.escalation = [(None, 0.0)]
    rig.enforcer._session_open = _always_open
    rig.enforcer._held_close_legs = _held_all(plan)
    await rig.enforcer._execute_exit(plan.id, "manual")
    assert rig.alpaca.cancelled[0] == pending
    fresh = await rig.trade.get_plan(plan.id)
    assert fresh.partial_exit is None
    assert fresh.status in ("exiting", "closed")


async def _always_open(plan):
    return True


def _held_all(plan):
    async def held(p):
        return p.legs, p.effective_qty
    return held


@pytest.mark.asyncio
async def test_qty_bounds_and_states(rig):
    plan = await _held_plan(rig.db, qty=3)
    # qty >= held is a whole close -> the ladder path (market): returns ladder
    rig.enforcer._execute_exit = _record(rig)
    out = await rig.enforcer.manual_close(plan.id, qty=3)
    assert out == {"mode": "whole", "status": "ladder"}
    # a planned row with no order cancels cleanly; a closed plan is refused
    planned = TradePlan(underlying="SPY", strategy="x", legs=plan.legs, qty=1, entry_limit=1.0,
                        tp_premium=2.0, sl_premium=0.5, time_stop_utc=datetime.now(timezone.utc) + timedelta(days=1),
                        status="planned")
    async with rig.db.session() as s:
        s.add(planned)
        await s.commit()
        await s.refresh(planned)
    assert (await rig.enforcer.manual_close(planned.id))["status"] == "cancelled"
    assert (await rig.trade.get_plan(planned.id)).status == "cancelled"
    await rig.trade.fsm.apply(plan.id, PlanEvent.FORCE_CLOSED, realized_pnl=0.0)
    with pytest.raises(ValueError, match="nothing to close"):
        await rig.enforcer.manual_close(plan.id)


def _record(rig):
    async def run(plan_id, reason):
        rig.ladder = (plan_id, reason)
    return run


@pytest.mark.asyncio
async def test_whole_limit_close_is_the_resting_tp(rig):
    plan = await _held_plan(rig.db, qty=2, tp=2.0, sl=0.5)
    out = await rig.enforcer.manual_close(plan.id, order_type="limit", limit_price=1.75)
    assert out["mode"] == "whole" and out["status"] == "resting_tp" and out["tp_premium"] == 1.75
    fresh = await rig.trade.get_plan(plan.id)
    assert fresh.tp_premium == 1.75 and fresh.sl_premium == 0.5 and fresh.status == "filled"
    with pytest.raises(ValueError):
        await rig.enforcer.manual_close(plan.id, order_type="limit", limit_price=0.4)  # below the stop


@pytest.mark.asyncio
async def test_reconcile_resolves_a_partial_after_restart(rig):
    plan = await _held_plan(rig.db, qty=4)
    out = await rig.enforcer.manual_close(plan.id, qty=2, order_type="limit", limit_price=1.80)
    rig.alpaca.fill(out["order_id"], 2, 1.80)
    # simulate a crash before the order id was persisted
    await rig.trade.fsm.update_fields(plan.id, partial_exit={**(await rig.trade.get_plan(plan.id)).partial_exit, "order_id": None})
    resolved = await rig.enforcer._reconcile_partial(await rig.trade.get_plan(plan.id))
    assert resolved.filled_qty == 2 and resolved.partial_exit is None


@pytest.mark.asyncio
async def test_todays_realized_counts_partial_waves(rig):
    plan = await _held_plan(rig.db, qty=5)
    await rig.enforcer.manual_close(plan.id, qty=2)
    assert await rig.risk.todays_realized_pnl() == pytest.approx(100.0)
