"""The spread optimizer: pricing math, the risk-settings presets, the cash
account PDT exemption, entry stamping in place_trade, the entry chase in
the enforcer's monitor, and the half-spread exit ladder."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

import app.services.trade_service as ts
from app.db.session import Database
from app.models.trade import TradePlan
from app.services.exit_enforcer import ExitEnforcer
from app.services.plan_fsm import PlanEvent
from app.services.risk import DEFAULT_RISK, PRESET_KEYS, RISK_PRESETS, RiskService, preset_matches
from app.services.spread_optimizer import (
    EntryWork,
    entry_limit,
    exit_ladder,
    exit_limit,
    work_spread_enabled,
)
from app.services.trade_service import TradeService

from tests.test_chaos import FakeAlpaca, FakeMarket, FlakyBroker, SYM, deliver_fill, make_plan, wait_status
from tests.test_swing_exits import CaptureAlpaca, TapeMarket, sl_only_payload

# ---------------------------------------------------------------- pricing


def test_entry_rounds_up_and_exit_rounds_down_to_the_tick():
    # mid 2.00, half-spread 0.10
    assert entry_limit(2.0, 0.10, 0.0) == 2.0
    assert entry_limit(2.0, 0.10, 0.25) == 2.03   # 2.025 -> up
    assert entry_limit(2.0, 0.10, 1.0) == 2.10    # the ask
    assert entry_limit(2.0, 0.10, -0.5) == 1.95   # inside our side
    assert exit_limit(2.0, 0.10, 0.25) == 1.97    # 1.975 -> down
    assert exit_limit(2.0, 0.10, 1.0) == 1.90     # the bid
    # Credit structures (negative axis) work the same direction.
    assert entry_limit(-1.0, 0.10, 0.5) == -0.95   # smaller credit = marketable
    assert exit_limit(-1.0, 0.10, 0.5) == -1.05    # pay more to close
    # Never exactly zero.
    assert entry_limit(-0.004, 0.0, 0.0) == 0.01
    assert exit_limit(0.004, 0.0, 0.0) == -0.01


def test_exit_ladder_shape_scales_with_settings():
    ladder = exit_ladder(step_s=2.0, exit_max=1.0)
    assert ladder == [(0.25, 2.0), (0.5, 2.0), (1.0, 3.0), (None, 0.0)]
    tight = exit_ladder(step_s=4.0, exit_max=0.5)
    assert tight[2] == (0.5, 6.0) and tight[-1] == (None, 0.0)


def test_entry_work_walks_to_max_and_stops():
    cfg = {"spread_opt_entry_start": -0.5, "spread_opt_entry_step": 0.5,
           "spread_opt_entry_max": 1.0, "spread_opt_step_s": 2.0}
    w = EntryWork.from_settings(cfg, staged=2.0)
    assert (w.frac, w.rung, w.exhausted) == (-0.5, 0, False)
    w = w.next()
    assert (w.frac, w.rung) == (0.0, 1)
    w = w.next().next()
    assert (w.frac, w.rung, w.exhausted) == (1.0, 3, True)
    assert EntryWork.from_json(w.to_json()) == w
    # start above max clamps to max (already exhausted).
    assert EntryWork.from_settings({**cfg, "spread_opt_entry_start": 1.5}, 2.0).exhausted


def test_work_spread_enabled_prefers_the_plan_stamp():
    assert work_spread_enabled({"work_spread": True}, {"spread_optimizer": False})
    assert not work_spread_enabled({"work_spread": False}, {"spread_optimizer": True})
    assert work_spread_enabled(None, {"spread_optimizer": True})
    assert not work_spread_enabled({}, {})


# --------------------------------------------------------------- settings


@pytest_asyncio.fixture
async def risk(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/risk.db")
    yield RiskService(db)
    await db.close()


def test_every_preset_key_is_a_real_setting_and_every_preset_is_complete():
    for key in PRESET_KEYS:
        assert key in DEFAULT_RISK
    for name, preset in RISK_PRESETS.items():
        assert set(preset["values"]) == set(PRESET_KEYS), name
        for capability in ("options_level", "equity_long_only", "equity_short_overnight",
                           "manual_equity_require_stop"):
            assert capability not in preset["values"]


@pytest.mark.asyncio
async def test_presets_apply_within_bounds_and_light_the_active_chip(risk):
    assert preset_matches("default", await risk.get_settings())
    settings = await risk.apply_preset("scalp")
    assert settings["spread_optimizer"] is True
    assert settings["entry_ttl_min"] == 1
    assert settings["spread_opt_step_s"] == 2.0
    assert settings["default_tp_pct"] == 0.30 and settings["default_sl_pct"] == 0.20
    assert preset_matches("scalp", settings) and not preset_matches("default", settings)
    # A hand edit un-lights it; capability keys are untouched by the preset.
    settings = await risk.update_settings({"max_positions": 5})
    assert not preset_matches("scalp", settings)
    assert settings["options_level"] == DEFAULT_RISK["options_level"]
    settings = await risk.apply_preset("swing")
    assert settings["spread_opt_entry_start"] == -0.5
    with pytest.raises(ValueError):
        await risk.apply_preset("yolo")
    with pytest.raises(ValueError):
        await risk.update_settings({"spread_opt_entry_start": -2.0})
    with pytest.raises(ValueError):
        await risk.update_settings({"spread_opt_step_s": 0.1})


@pytest.mark.asyncio
async def test_pdt_guard_is_a_margin_rule(risk):
    from tests.test_risk_guards import _base

    margin = await risk.validate_new_trade(**_base(account_equity=10_000, daytrade_count=3))
    assert any("PDT" in v for v in margin)
    cash = await risk.validate_new_trade(
        **_base(account_equity=10_000, daytrade_count=3), margin_account=False
    )
    assert not any("PDT" in v for v in cash)


# ------------------------------------------------------------ place_trade


class OptionTapeMarket(TapeMarket):
    """Two-sided option book for one leg + the refresh hook place_trade calls."""

    async def refresh_option_quotes(self, symbols, max_age_s=30.0):
        return None


def option_payload(**overrides) -> dict:
    payload = {
        "underlying": "SPY", "strategy": "long_call", "asset_class": "option",
        "legs": [{"symbol": SYM, "right": "C", "strike": 450.0, "expiry": "2030-12-20",
                  "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.2, "half_spread": 0.05}],
        "qty": 1, "entry_limit": 2.0, "tp_premium": 3.0, "sl_premium": 1.0,
        "time_stop_utc": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    payload.update(overrides)
    return payload


async def _rig(tmp_path, market):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/opt.db")
    alpaca = CaptureAlpaca()
    risk = RiskService(db)
    trade = TradeService(db=db, alpaca=alpaca, market=market, risk=risk)
    return trade, alpaca, risk, db


async def _retire(trade, plan: dict) -> None:
    """Close the just-placed plan so the next identical order is not a
    duplicate-guard refusal (the guard is about OPEN twins)."""
    await trade.fsm.apply(plan["id"], PlanEvent.ENTRY_CANCELLED)


@pytest.mark.asyncio
async def test_place_trade_stamps_and_reprices_rung_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "equity_session", lambda: "rth")
    market = OptionTapeMarket(price=2.0, half_spread=0.10)
    market.stream_age_s = 1.0
    trade, alpaca, risk, db = await _rig(tmp_path, market)
    try:
        # OFF (default): the staged mid rests, nothing stamped but the choice.
        plan = await trade.place_trade(option_payload())
        assert plan["entry_limit"] == 2.0
        assert plan["pricing"] == {"work_spread": False}
        assert alpaca.submitted[-1].limit_price == 2.0
        await _retire(trade, plan)

        # Global ON with a start inside the book: rung 0 is 2.00 - 0.5*0.10.
        await risk.update_settings({"spread_optimizer": True, "spread_opt_entry_start": -0.5,
                                    "spread_opt_entry_step": 0.5})
        plan = await trade.place_trade(option_payload())
        assert plan["entry_limit"] == 1.95
        assert alpaca.submitted[-1].limit_price == 1.95
        assert plan["pricing"]["work_spread"] is True
        assert plan["pricing"]["entry"]["staged"] == 2.0
        assert plan["pricing"]["entry"]["frac"] == -0.5
        assert plan["pricing"]["book"] == {"mid": 2.0, "half_spread": 0.1}
        await _retire(trade, plan)

        # Per-order OFF beats the global ON.
        plan = await trade.place_trade(option_payload(work_spread=False))
        assert plan["entry_limit"] == 2.0 and plan["pricing"] == {"work_spread": False}
        await _retire(trade, plan)

        # A worked rung that would land on/through the TP falls back to the
        # staged limit: start at the ask (2.10) with a 2.05 target.
        await risk.update_settings({"spread_opt_entry_start": 1.0})
        plan = await trade.place_trade(option_payload(tp_premium=2.05))
        assert plan["entry_limit"] == 2.0 and plan["pricing"]["work_spread"] is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_place_trade_works_equity_entries_too(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
    market = TapeMarket(price=100.0, half_spread=0.05)
    trade, alpaca, risk, db = await _rig(tmp_path, market)
    try:
        await risk.update_settings({"spread_optimizer": True, "spread_opt_entry_start": 1.0})
        # Equity SL-only: staged 100, half-spread 0.05 -> rung 0 at the ask.
        plan = await trade.place_trade(sl_only_payload())
        assert plan["entry_limit"] == 100.05
        assert alpaca.submitted[-1].limit_price == 100.05
    finally:
        await db.close()


# ---------------------------------------------------------- the chase


@pytest_asyncio.fixture
async def stack(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/chase.db")
    broker = FlakyBroker()
    alpaca = FakeAlpaca(broker)
    market = FakeMarket()
    risk = RiskService(db)
    trade = TradeService(db, alpaca, market, risk)
    trade.REWORK_CANCEL_POLL_S = 0.02
    enforcer = ExitEnforcer(db, market, trade)
    enforcer.escalation = [(0.02, 0.05), (0.06, 0.05), (None, 0.0)]
    enforcer.verify_poll_s = 0.04
    enforcer.verify_attempts = 150
    enforcer.rearm_delay_s = 0.05
    enforcer.reconcile_interval_s = 0.15
    enforcer.resting_tp = False
    await risk.update_settings({"sl_confirm_s": 0.0, "spread_optimizer": True,
                                "spread_opt_step_s": 0.5})
    yield SimpleNamespace(db=db, broker=broker, alpaca=alpaca, market=market,
                          trade=trade, enforcer=enforcer, risk=risk)
    await enforcer.shutdown()
    await db.close()


def book(market: FakeMarket, mid: float, hs: float) -> None:
    market.quotes[SYM] = {"bid": mid - hs, "ask": mid + hs, "mid": mid}


async def submitted_plan(stack, *, staged=2.0, start=0.0, step=0.5, maximum=1.0, step_s=0.3):
    plan = await make_plan(stack.db, status="submitted", entry=staged, tp=3.0, sl=1.0)
    work = EntryWork(staged=staged, start=start, step=step, max=maximum, step_s=step_s,
                     frac=start, rung=0)
    order = stack.broker.submit_order(
        SimpleNamespace(client_order_id=f"{plan.id}-e", limit_price=staged, qty=1))
    await stack.trade.fsm.update_fields(
        plan.id, entry_order_id=order.id,
        pricing={"work_spread": True, "entry": work.to_json()},
    )
    return await stack.trade.get_plan(plan.id), order


@pytest.mark.asyncio
async def test_chase_walks_the_entry_toward_the_ask_then_fills(stack):
    book(stack.market, 2.0, 0.10)
    plan, first = await submitted_plan(stack)
    await stack.enforcer.arm(plan.id)

    # Rung 1 after one step: 2.00 + 0.5 * 0.10 = 2.05, first order cancelled.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        plan = await stack.trade.get_plan(plan.id)
        if plan.pricing["entry"]["rung"] >= 1 and plan.entry_order_id != first.id:
            break
        await asyncio.sleep(0.02)
    assert plan.status == "submitted"
    assert plan.pricing["entry"]["rung"] == 1 and plan.entry_limit == 2.05
    assert stack.broker.orders[first.id].status == "canceled"
    second = stack.broker.orders[plan.entry_order_id]
    assert second.client_order_id == f"{plan.id}-e1" and second.limit_price == 2.05
    assert "reworking" not in plan.pricing

    # The broker's late cancel event for the FIRST order is not the entry dying.
    await stack.trade.on_trade_update(
        SimpleNamespace(order=stack.broker.orders[first.id], event="canceled"))
    assert (await stack.trade.get_plan(plan.id)).status == "submitted"

    # Rung 2 reaches the ask (max) and the chase stops there.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        plan = await stack.trade.get_plan(plan.id)
        if plan.pricing["entry"]["rung"] >= 2:
            break
        await asyncio.sleep(0.02)
    assert plan.entry_limit == 2.10 and EntryWork.from_json(plan.pricing["entry"]).exhausted
    await asyncio.sleep(0.8)
    assert (await stack.trade.get_plan(plan.id)).pricing["entry"]["rung"] == 2  # no rung 3

    # The resting touch order fills: the plan is filled at the worked price.
    live = stack.broker.orders[plan.entry_order_id]
    stack.broker.fill(live.id, 2.10)
    await deliver_fill(stack.trade, live)
    assert await wait_status(stack.trade, plan.id, ("filled",)) == "filled"
    assert (await stack.trade.get_plan(plan.id)).fill_premium == 2.10
    stack.enforcer.disarm(plan.id)
    await asyncio.sleep(0.05)  # let the cancelled monitor unwind before teardown


@pytest.mark.asyncio
async def test_chase_abandons_when_the_market_runs_away(stack):
    book(stack.market, 2.0, 0.10)
    plan, first = await submitted_plan(stack)
    # The book gaps far past the staged price's tolerance before the first step.
    book(stack.market, 2.60, 0.10)
    await stack.enforcer.arm(plan.id)
    status = await wait_status(stack.trade, plan.id, ("cancelled",), deadline=3.0)
    assert status == "cancelled"
    plan = await stack.trade.get_plan(plan.id)
    assert "chase abandoned" in (plan.notes or "")
    assert stack.broker.orders[first.id].status == "canceled"
    assert len(stack.broker.live_orders_for(plan.id)) == 0


@pytest.mark.asyncio
async def test_chase_own_cancel_is_ignored_but_a_partial_cancel_is_not(stack):
    book(stack.market, 2.0, 0.10)
    plan, first = await submitted_plan(stack)
    pricing = {**plan.pricing, "reworking": first.id}
    await stack.trade.fsm.update_fields(plan.id, pricing=pricing)
    stack.broker.orders[first.id].status = "canceled"
    await stack.trade.on_trade_update(SimpleNamespace(order=stack.broker.orders[first.id],
                                                      event="canceled"))
    assert (await stack.trade.get_plan(plan.id)).status == "submitted"
    # ...unless something filled first: then the cancel-with-fills is the
    # broker telling us what we hold, and the partial is managed.
    stack.broker.orders[first.id].filled_qty = 1
    stack.broker.orders[first.id].filled_avg_price = 2.0
    await stack.trade.on_trade_update(SimpleNamespace(order=stack.broker.orders[first.id],
                                                      event="partial_fill"))
    assert (await stack.trade.get_plan(plan.id)).status == "partially_filled"
    await stack.trade.on_trade_update(SimpleNamespace(order=stack.broker.orders[first.id],
                                                      event="canceled"))
    assert (await stack.trade.get_plan(plan.id)).status == "filled"


# ---------------------------------------------------------- exit ladder


@pytest.mark.asyncio
async def test_spread_exit_ladder_prices_off_the_book_then_market(stack):
    book(stack.market, 2.0, 0.10)
    plan = await make_plan(stack.db, status="filled", broker=stack.broker)
    stack.enforcer.spread_ladder_override = [(0.5, 0.05), (1.0, 0.05), (None, 0.0)]
    task = asyncio.create_task(stack.enforcer._execute_exit(plan.id, "manual"))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        exits = [o for o in stack.broker.orders.values()
                 if o.client_order_id.startswith(f"{plan.id}-x")]
        if len(exits) >= 3:
            break
        await asyncio.sleep(0.02)
    exits.sort(key=lambda o: o.client_order_id)
    assert [o.limit_price for o in exits] == [1.95, 1.90, None]
    last = exits[-1]
    stack.broker.fill(last.id, 1.88)
    await deliver_fill(stack.trade, last)
    await asyncio.wait_for(task, 5.0)
    assert (await stack.trade.get_plan(plan.id)).status == "closed"


@pytest.mark.asyncio
async def test_plan_stamped_off_keeps_the_legacy_ladder(stack):
    book(stack.market, 2.0, 0.10)
    plan = await make_plan(stack.db, status="filled", broker=stack.broker)
    await stack.trade.fsm.update_fields(plan.id, pricing={"work_spread": False})
    task = asyncio.create_task(stack.enforcer._execute_exit(plan.id, "manual"))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        exits = [o for o in stack.broker.orders.values()
                 if o.client_order_id.startswith(f"{plan.id}-x")]
        if len(exits) >= 3:
            break
        await asyncio.sleep(0.02)
    exits.sort(key=lambda o: o.client_order_id)
    assert [o.limit_price for o in exits] == [1.96, 1.88, None]  # mid-2%, mid-6%, market
    last = exits[-1]
    stack.broker.fill(last.id, 1.88)
    await deliver_fill(stack.trade, last)
    await asyncio.wait_for(task, 5.0)


def test_plan_json_carries_pricing():
    plan = TradePlan(underlying="SPY", strategy="long_call", legs=[], qty=1, entry_limit=1.0,
                     time_stop_utc=datetime.now(timezone.utc), pricing={"work_spread": True})
    assert plan.to_dict()["pricing"] == {"work_spread": True}
