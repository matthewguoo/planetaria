"""Chaos / pressure tests: the execution stack (TradeService + ExitEnforcer +
FSM) against a deliberately unreliable broker — latency, transient errors,
hung calls, orders that land after the caller gave up, lost fill events,
double closes, and a concurrent storm. The invariant under test: every
triggered exit COMPLETES, exactly once, and nothing hangs.
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from app.db.session import Database
from app.models.trade import TradePlan
from app.services.alpaca import AlpacaService
from app.services.broadcast import Broadcaster
from app.services.exit_enforcer import ExitEnforcer
from app.services.risk import RiskService
from app.services.trade_service import TradeService

# ------------------------------------------------------------------ fakes


class FlakyBroker:
    """Sync (threadpool-called) fake of the TradingClient surface we use.
    Knobs simulate real market-API misbehavior."""

    def __init__(self):
        self.lock = threading.Lock()
        self.history: list[tuple[float, str, str]] = []  # (mono, action, cid)
        self.orders: dict[str, SimpleNamespace] = {}
        self.by_client_id: dict[str, SimpleNamespace] = {}
        self.closed_plans: set[str] = set()  # plans whose position is gone
        self.plan_symbols: dict[str, set[str]] = {}  # open position truth
        self.position_symbols: set[str] = set()
        # Chaos knobs
        self.latency: float = 0.0
        self.fail_submits: int = 0  # next N submits raise ConnectionError
        self.fail_after_register: int = 0  # register order, THEN raise
        self.hang_next_submit: float = 0.0  # submit sleeps this long (once)
        self.async_cancels: bool = False  # cancel ACCEPTED but order stays live
        self.submit_count = 0

    # -- helpers ---------------------------------------------------------

    def _register(self, request) -> SimpleNamespace:
        cid = getattr(request, "client_order_id", None) or uuid4().hex
        with self.lock:
            if cid in self.by_client_id:
                raise ValueError(f"client_order_id must be unique: {cid}")
            plan_id = cid.split("-x")[0].split("-e")[0] if "-" in cid else ""
            if "-x" in cid and plan_id in self.closed_plans:
                raise ValueError("insufficient position (already closed)")
            order = SimpleNamespace(
                id=uuid4().hex[:12],
                client_order_id=cid,
                status="accepted",
                filled_avg_price=None,
                filled_qty=0,
                limit_price=getattr(request, "limit_price", None),
                qty=getattr(request, "qty", 1),
                submitted_mono=time.monotonic(),
            )
            self.orders[order.id] = order
            self.by_client_id[cid] = order
            self.history.append((time.monotonic(), "register", cid))
        return order

    def fill(self, order_id: str, price: float) -> SimpleNamespace:
        with self.lock:
            order = self.orders[order_id]
            if order.status in ("canceled", "rejected"):
                raise AssertionError("filled a dead order")
            order.status = "filled"
            order.filled_avg_price = price
            order.filled_qty = float(order.qty)
            cid = order.client_order_id
            self.history.append((time.monotonic(), "fill", cid))
            if "-x" in cid:
                plan_id = cid.split("-x")[0]
                self.closed_plans.add(plan_id)
                self.position_symbols -= self.plan_symbols.pop(plan_id, set())
            return order

    def open_position(self, plan) -> None:
        symbols = {leg["symbol"] for leg in plan.legs}
        with self.lock:
            self.plan_symbols[plan.id] = symbols
            self.position_symbols |= symbols

    def filled_exits_for(self, plan_id: str) -> list[SimpleNamespace]:
        with self.lock:
            return [
                o for o in self.orders.values()
                if o.client_order_id.startswith(f"{plan_id}-x") and o.status == "filled"
            ]

    def live_orders_for(self, plan_id: str) -> list[SimpleNamespace]:
        with self.lock:
            return [
                o for o in self.orders.values()
                if o.client_order_id.startswith(plan_id)
                and o.status not in ("filled", "canceled", "rejected", "expired")
            ]

    # -- TradingClient surface ------------------------------------------

    def submit_order(self, request):
        if self.latency:
            time.sleep(self.latency)
        with self.lock:
            self.submit_count += 1
            if self.fail_submits > 0:
                self.fail_submits -= 1
                raise ConnectionError("simulated connection drop on submit")
            hang = self.hang_next_submit
            self.hang_next_submit = 0.0
        if hang:
            time.sleep(hang)  # caller's wait_for gives up; order lands late
        order = self._register(request)
        with self.lock:
            if self.fail_after_register > 0:
                self.fail_after_register -= 1
                raise ConnectionError("simulated drop AFTER broker accepted")
        return order

    def get_order_by_id(self, order_id):
        if self.latency:
            time.sleep(self.latency)
        with self.lock:
            if order_id not in self.orders:
                raise KeyError(f"no order {order_id}")
            return self.orders[order_id]

    def get_order_by_client_id(self, client_id):
        if self.latency:
            time.sleep(self.latency)
        with self.lock:
            if client_id not in self.by_client_id:
                raise KeyError(f"no order for client id {client_id}")
            return self.by_client_id[client_id]

    def cancel_order_by_id(self, order_id):
        if self.latency:
            time.sleep(self.latency)
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                raise KeyError(f"no order {order_id}")
            if order.status == "filled":
                raise ValueError("order is not cancelable (filled)")
            if self.async_cancels:
                # Real broker semantics: the cancel is ACCEPTED (no error)
                # but the order is only pending_cancel — it can still fill.
                self.history.append((time.monotonic(), "cancel-req", order.client_order_id))
                return
            order.status = "canceled"
            self.history.append((time.monotonic(), "cancel", order.client_order_id))

    def get_all_positions(self):
        with self.lock:
            return [
                SimpleNamespace(
                    symbol=s, qty=1.0, asset_class="us_option", avg_entry_price=2.0,
                    current_price=None, market_value=None, unrealized_pl=None,
                )
                for s in sorted(self.position_symbols)
            ]


class FakeAlpaca(AlpacaService):
    """Real AlpacaService.call semantics (threadpool + wait_for + retry)
    over the flaky broker, with a compressed timeout for fast tests."""

    def __init__(self, broker: FlakyBroker, call_timeout: float = 0.5):
        self.configured = True
        self.settings = None
        self.trading = broker
        self.call_timeout = call_timeout

    async def call(self, fn, /, *args, timeout: float | None = None, retries: int = 0, **kwargs):
        return await super().call(
            fn, *args, timeout=timeout or self.call_timeout, retries=retries, **kwargs
        )


class FakeMarket:
    def __init__(self):
        self.broadcast = Broadcaster()
        self.quotes: dict[str, dict] = {}
        self.stream_age_s = 1.0
        self.refresh_calls = 0

    def latest_quote(self, symbol):
        return self.quotes.get(symbol)

    async def subscribe_options(self, symbols):
        pass

    async def unsubscribe_options(self, symbols):
        pass

    async def subscribe_stock(self, symbol):
        pass

    async def unsubscribe_stock(self, symbol):
        pass

    async def refresh_option_quotes(self, symbols, max_age_s: float = 30.0):
        # REST/demo fallback is a no-op here; quotes are set by the tests.
        self.refresh_calls += 1

    def pump(self, symbol: str, mid: float) -> None:
        self.quotes[symbol] = {"bid": mid - 0.01, "ask": mid + 0.01, "mid": mid}
        self.broadcast.publish(f"oquote:{symbol}", {"t": "oquote", "symbol": symbol})


# ---------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def stack(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/chaos.db")
    broker = FlakyBroker()
    alpaca = FakeAlpaca(broker)
    market = FakeMarket()
    risk = RiskService(db)
    trade = TradeService(db, alpaca, market, risk)
    enforcer = ExitEnforcer(db, market, trade)
    # Compress every timing knob so escalation/verification run in ms.
    enforcer.escalation = [(0.02, 0.05), (0.06, 0.05), (None, 0.0)]
    enforcer.verify_poll_s = 0.04
    enforcer.verify_attempts = 150
    enforcer.rearm_delay_s = 0.05
    enforcer.reconcile_interval_s = 0.15
    # Resting TP off by default so the software-trigger scenarios stay pure;
    # dedicated tests flip it on.
    enforcer.resting_tp = False
    # SL dwell off by default (legacy instant-fire semantics for the storm
    # scenarios); the denoising tests configure their own dwell.
    await risk.update_settings({"sl_confirm_s": 0.0})
    yield SimpleNamespace(
        db=db, broker=broker, alpaca=alpaca, market=market, trade=trade, enforcer=enforcer
    )
    await enforcer.shutdown()
    await db.close()


SYM = "SPY260731C00450000"


async def make_plan(db, *, status="filled", symbol=SYM, tp=4.0, sl=1.0,
                    stop_in_h=2.0, entry=2.0, broker=None) -> TradePlan:
    plan = TradePlan(
        underlying="SPY",
        strategy="long_call",
        # Far-future expiry: parking an exit is only legal for legs with a
        # session ahead of them — an expired-leg plan settles instead (see
        # test_expiry_settlement.py), which is not what these suites test.
        legs=[{"symbol": symbol, "right": "C", "strike": 450.0,
               "expiry": "2030-12-20", "side": 1, "ratio": 1, "entry": entry, "iv": 0.2}],
        qty=1,
        entry_limit=entry,
        tp_premium=tp,
        sl_premium=sl,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=stop_in_h),
        status=status,
        fill_premium=entry if status in ("filled", "exiting") else None,
        filled_qty=1 if status in ("filled", "exiting") else None,
    )
    async with db.session() as session:
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
    if broker is not None and status in ("filled", "exiting", "partially_filled"):
        broker.open_position(plan)
    return plan


async def deliver_fill(trade, order) -> None:
    """Emulate a TradingStream fill event."""
    await trade.on_trade_update(SimpleNamespace(order=order, event="fill"))


async def wait_status(trade, plan_id, want=("closed",), deadline=5.0) -> str:
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline:
        status = (await trade.get_plan(plan_id)).status
        if status in want:
            return status
        await asyncio.sleep(0.02)
    return (await trade.get_plan(plan_id)).status


async def auto_filler(broker, trade, deliver_events=True, delay=0.06, price=1.5,
                      skip_substr=None):
    """Fills accepted orders after a delay; optionally delivers the stream
    event (a real broker sometimes doesn't — reconcile must cover).
    skip_substr: never fill orders whose client id contains it (lets a test
    keep a resting TP unfilled while its stop-close race runs)."""
    try:
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            with broker.lock:
                due = [o for o in broker.orders.values()
                       if o.status == "accepted" and now - o.submitted_mono >= delay
                       and not (skip_substr and skip_substr in o.client_order_id)]
            for order in due:
                try:
                    broker.fill(order.id, price)
                except AssertionError:
                    continue
                if deliver_events:
                    await deliver_fill(trade, order)
    except asyncio.CancelledError:
        raise


# ------------------------------------------------------------------ tests


@pytest.mark.asyncio
async def test_tp_exit_completes_despite_submit_failures(stack):
    """Two transient submit failures must not stop a TP exit."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.broker.fail_submits = 2
    filler = asyncio.create_task(auto_filler(stack.broker, stack.trade, price=4.4))
    try:
        await stack.enforcer.arm(plan.id)
        for _ in range(30):
            stack.market.pump(SYM, 4.5)  # >= TP
            await asyncio.sleep(0.02)
        status = await wait_status(stack.trade, plan.id)
        assert status == "closed", f"plan stuck in {status}"
        done = await stack.trade.get_plan(plan.id)
        assert done.exit_reason == "tp"
        assert done.exit_premium is not None and done.realized_pnl is not None
        assert len(stack.broker.filled_exits_for(plan.id)) == 1
        assert stack.broker.live_orders_for(plan.id) == []
    finally:
        filler.cancel()


@pytest.mark.asyncio
async def test_hung_submit_does_not_wedge_and_never_double_closes(stack):
    """A submit that outlives the call timeout but LANDS at the broker (the
    nastiest real-API behavior) must neither freeze the monitor nor produce
    two filled closes for one position."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.broker.hang_next_submit = 1.2  # > FakeAlpaca 0.5s call timeout
    filler = asyncio.create_task(
        auto_filler(stack.broker, stack.trade, deliver_events=False, price=0.9)
    )
    try:
        await stack.enforcer.arm(plan.id)
        for _ in range(40):
            stack.market.pump(SYM, 0.8)  # <= SL
            await asyncio.sleep(0.02)
        status = await wait_status(stack.trade, plan.id, deadline=15.0)
        assert status == "closed", f"plan stuck in {status}"
        # Exactly one close made it through; the ghost was cancelled/adopted.
        await asyncio.sleep(1.5)  # let the hung submit land + sweeps run
        assert len(stack.broker.filled_exits_for(plan.id)) == 1
        assert stack.broker.live_orders_for(plan.id) == []
    finally:
        filler.cancel()


@pytest.mark.asyncio
async def test_lost_fill_event_recovered_by_reconcile(stack):
    """Entry fills at the broker while the TradingStream is down: the
    periodic reconcile must pick it up and arm management."""
    plan = await make_plan(stack.db, status="planned")
    order = stack.broker._register(SimpleNamespace(client_order_id=f"{plan.id}-e", qty=1))
    await stack.trade.fsm.apply(
        plan.id,
        __import__("app.services.plan_fsm", fromlist=["PlanEvent"]).PlanEvent.ENTRY_SUBMITTED,
        entry_order_id=order.id,
    )
    stack.broker.fill(order.id, 2.05)  # fill lands; NO stream event delivered
    await stack.enforcer.reconcile_once()
    refreshed = await stack.trade.get_plan(plan.id)
    assert refreshed.status == "filled"
    assert refreshed.fill_premium == pytest.approx(2.05)
    assert plan.id in stack.enforcer._monitors  # management armed


@pytest.mark.asyncio
async def test_time_stop_fires_with_no_quotes_at_all(stack):
    """Time stop is quote-independent: zero option quotes, exit still runs."""
    plan = await make_plan(stack.db, stop_in_h=-0.01, broker=stack.broker)  # already past
    filler = asyncio.create_task(
        auto_filler(stack.broker, stack.trade, deliver_events=False, price=1.8)
    )
    try:
        await stack.enforcer.arm(plan.id)
        # Generous deadline: under full-suite load the compressed 40ms
        # timers get starved; the invariant is completion, not speed.
        status = await wait_status(stack.trade, plan.id, deadline=15.0)
        assert status == "closed", f"plan stuck in {status}"
        assert (await stack.trade.get_plan(plan.id)).exit_reason == "time_stop"
    finally:
        filler.cancel()


@pytest.mark.asyncio
async def test_duplicate_and_late_events_are_ignored(stack):
    """Replayed fills and late cancels after close must be no-ops."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.market.pump(SYM, 4.5)
    filler = asyncio.create_task(auto_filler(stack.broker, stack.trade, price=4.4))
    try:
        await stack.enforcer.arm(plan.id)
        for _ in range(30):
            stack.market.pump(SYM, 4.5)
            await asyncio.sleep(0.02)
        assert await wait_status(stack.trade, plan.id) == "closed"
    finally:
        filler.cancel()
    done = await stack.trade.get_plan(plan.id)
    exit_order = stack.broker.orders[done.exit_order_id]
    # Replay the fill, then a bogus late cancel.
    await deliver_fill(stack.trade, exit_order)
    await stack.trade.on_trade_update(SimpleNamespace(order=exit_order, event="canceled"))
    after = await stack.trade.get_plan(plan.id)
    assert after.status == "closed"
    assert after.realized_pnl == done.realized_pnl


@pytest.mark.asyncio
async def test_entry_submit_ambiguity_is_idempotent(stack):
    """Broker accepts the entry but the response is lost: recovery must find
    the order by client id; a second submit can never duplicate it."""
    plan = await make_plan(stack.db, status="planned")
    stack.broker.fail_after_register = 1
    order = await stack.trade._submit_entry(plan)
    assert order.client_order_id == f"{plan.id}-e"
    with stack.broker.lock:
        n_orders = len(stack.broker.orders)
    assert n_orders == 1
    # A blind duplicate submit is rejected + recovers the SAME order.
    again = await stack.trade._submit_entry(plan)
    assert again.id == order.id
    with stack.broker.lock:
        assert len(stack.broker.orders) == 1


@pytest.mark.asyncio
async def test_storm_all_plans_close_exactly_once(stack):
    """12 concurrent plans, flaky broker (latency + 20% submit failures),
    ~40% of fill events lost, TP/SL/time-stop mixed. Everything must reach
    closed with exactly one filled close order per plan."""
    stack.broker.latency = 0.01
    plans = []
    for i in range(12):
        kind = i % 3
        sym = f"SPY26073{i % 2}C00{450 + i}000"
        plan = await make_plan(
            stack.db, symbol=sym,
            stop_in_h=(-0.01 if kind == 2 else 2.0),
            broker=stack.broker,
        )
        plans.append((plan, kind, sym))

    lost_counter = {"n": 0}

    async def flaky_filler():
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            with stack.broker.lock:
                if stack.broker.fail_submits == 0 and stack.broker.submit_count % 5 == 4:
                    stack.broker.fail_submits = 1  # ~20% transient failures
                due = [o for o in stack.broker.orders.values()
                       if o.status == "accepted" and now - o.submitted_mono >= 0.05]
            for order in due:
                try:
                    stack.broker.fill(order.id, 1.5)
                except AssertionError:
                    continue
                lost_counter["n"] += 1
                if lost_counter["n"] % 5 in (1, 3, 4):  # ~40% of events lost
                    await deliver_fill(stack.trade, order)

    filler = asyncio.create_task(flaky_filler())
    reconciler = asyncio.create_task(stack.enforcer.reconcile_loop())
    try:
        for plan, _, _ in plans:
            await stack.enforcer.arm(plan.id)

        async def pump_quotes():
            while True:
                for _, kind, sym in plans:
                    if kind == 0:
                        stack.market.pump(sym, 4.5)  # TP
                    elif kind == 1:
                        stack.market.pump(sym, 0.8)  # SL
                await asyncio.sleep(0.02)

        pumper = asyncio.create_task(pump_quotes())
        try:
            results = await asyncio.gather(
                *[wait_status(stack.trade, p.id, deadline=20.0) for p, _, _ in plans]
            )
        finally:
            pumper.cancel()
        if not all(r == "closed" for r in results):
            for (plan, _, _), r in zip(plans, results):
                if r != "closed":
                    tail = [h for h in stack.broker.history if plan.id in h[2]]
                    print(f"\nSTUCK {plan.id} ({r}):")
                    for t, action, cid in tail:
                        print(f"  {t:.3f} {action:9s} {cid[len(plan.id):]}")
        assert all(r == "closed" for r in results), f"stuck: {results}"
        for plan, kind, _ in plans:
            fills = stack.broker.filled_exits_for(plan.id)
            assert len(fills) == 1, f"plan {plan.id} has {len(fills)} filled closes"
            done = await stack.trade.get_plan(plan.id)
            assert done.exit_premium is not None
            expected = {0: "tp", 1: "sl", 2: "time_stop"}[kind]
            assert done.exit_reason == expected
        # No zombie orders left live at the broker.
        for plan, _, _ in plans:
            await stack.enforcer._sweep_ghosts_on_close(plan.id)
            assert stack.broker.live_orders_for(plan.id) == []
    finally:
        filler.cancel()
        reconciler.cancel()


@pytest.mark.asyncio
async def test_tp_fires_from_polled_quotes_without_stream_ticks(stack):
    """THE after-hours / illiquid-leg case: the option stream never ticks
    (no oquote broadcasts at all), but cached/REST-polled quotes exist.
    Monitors must evaluate TP/SL on the poll cadence, not wait forever."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.enforcer.quote_poll_s = 0.05  # compress the poll cadence
    # Quote present in the cache (as REST seeding would leave it) — but
    # never broadcast: the monitor gets zero wake-up ticks.
    stack.market.quotes[SYM] = {"bid": 4.49, "ask": 4.51, "mid": 4.5}  # >= TP 4.0
    filler = asyncio.create_task(auto_filler(stack.broker, stack.trade, price=4.4))
    try:
        await stack.enforcer.arm(plan.id)
        status = await wait_status(stack.trade, plan.id, deadline=10.0)
        assert status == "closed", f"plan stuck in {status} - poll path did not evaluate"
        done = await stack.trade.get_plan(plan.id)
        assert done.exit_reason == "tp"
        assert stack.market.refresh_calls > 0  # the poll fallback actually ran
    finally:
        filler.cancel()


@pytest.mark.asyncio
async def test_monitor_reports_unevaluable_mid(stack):
    """No quote for a leg even after polling: the monitor must surface
    no-mid health instead of silently doing nothing."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.enforcer.quote_poll_s = 0.05
    stack.enforcer.quote_poll_near_s = 0.05
    # NO quotes at all for the leg.
    try:
        await stack.enforcer.arm(plan.id)
        await asyncio.sleep(0.5)
        health = stack.enforcer.monitor_health.get(plan.id, "")
        assert health.startswith("no-mid"), f"health not surfaced: {health!r}"
        assert SYM in health
    finally:
        stack.enforcer.disarm(plan.id)


@pytest.mark.asyncio
async def test_resting_tp_placed_at_broker_and_fill_closes_plan(stack):
    """With resting TP on, the monitor parks a live limit close at the
    broker; its fill (delivered via the stream) closes the plan as TP."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.enforcer.resting_tp = True
    stack.enforcer.quote_poll_s = 0.05
    stack.enforcer.quote_poll_near_s = 0.05
    # Mid safely between SL and TP: no software trigger fires.
    stack.market.quotes[SYM] = {"bid": 1.99, "ask": 2.01, "mid": 2.0}
    try:
        await stack.enforcer.arm(plan.id)
        # The resting TP order should appear at the broker AND on the plan
        # (poll the plan row: the DB write trails the broker accept).
        deadline = time.monotonic() + 5
        refreshed = None
        while time.monotonic() < deadline:
            refreshed = await stack.trade.get_plan(plan.id)
            if refreshed.tp_order_id:
                break
            await asyncio.sleep(0.02)
        assert refreshed is not None and refreshed.tp_order_id, "resting TP never recorded"
        with stack.broker.lock:
            tp_order = stack.broker.orders.get(refreshed.tp_order_id)
        assert tp_order is not None and "-xtp" in tp_order.client_order_id
        assert tp_order.status == "accepted"
        tp_orders = [tp_order]
        # Broker fills the resting TP (price = the TP premium).
        order = stack.broker.fill(tp_orders[0].id, 4.0)
        await deliver_fill(stack.trade, order)
        status = await wait_status(stack.trade, plan.id, deadline=5.0)
        assert status == "closed"
        done = await stack.trade.get_plan(plan.id)
        assert done.exit_reason == "tp"
        assert done.exit_premium == pytest.approx(4.0)
    finally:
        stack.enforcer.disarm(plan.id)


@pytest.mark.asyncio
async def test_sl_trigger_cancels_resting_tp_first(stack):
    """SL firing while a TP rests at the broker must take the TP down before
    submitting the stop close — never two live closes at once."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.enforcer.resting_tp = True
    stack.enforcer.quote_poll_s = 0.05
    stack.enforcer.quote_poll_near_s = 0.05
    stack.market.quotes[SYM] = {"bid": 1.99, "ask": 2.01, "mid": 2.0}
    # The filler must never fill the RESTING TP itself: this test is about
    # the SL trigger winning and taking the TP down. (If the TP fill wins
    # the race instead, absorbing it as a TP close is the CORRECT outcome —
    # covered by test_resting_tp_placed_at_broker_and_fill_closes_plan.)
    filler = asyncio.create_task(
        auto_filler(stack.broker, stack.trade, deliver_events=False, price=0.9,
                    skip_substr="-xtp")
    )
    try:
        await stack.enforcer.arm(plan.id)
        # Wait for the resting TP, then crash the mid through the stop.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            refreshed = await stack.trade.get_plan(plan.id)
            if refreshed.tp_order_id:
                break
            await asyncio.sleep(0.02)
        assert refreshed.tp_order_id, "resting TP never placed"
        tp_order_id = refreshed.tp_order_id
        stack.market.quotes[SYM] = {"bid": 0.79, "ask": 0.81, "mid": 0.8}  # <= SL
        status = await wait_status(stack.trade, plan.id, deadline=10.0)
        assert status == "closed"
        done = await stack.trade.get_plan(plan.id)
        assert done.exit_reason == "sl"
        # The old resting TP must be canceled, and exactly one close filled.
        with stack.broker.lock:
            assert stack.broker.orders[tp_order_id].status == "canceled"
        assert len(stack.broker.filled_exits_for(plan.id)) == 1
    finally:
        filler.cancel()


@pytest.mark.asyncio
async def test_async_cancel_race_absorbs_in_flight_tp_fill(stack):
    """Broker cancels are asynchronous: an ACCEPTED cancel is not a dead
    order. If the resting TP fills while its cancel is pending, the takedown
    must absorb that fill (plan closed as TP) — never report the TP gone and
    let a second close go up."""
    plan = await make_plan(stack.db, broker=stack.broker)
    order = stack.broker._register(SimpleNamespace(client_order_id=f"{plan.id}-xtp400", qty=1))
    await stack.trade.fsm.update_fields(plan.id, tp_order_id=order.id)
    plan = await stack.trade.get_plan(plan.id)
    stack.broker.async_cancels = True

    async def fill_during_pending_cancel():
        await asyncio.sleep(0.3)  # cancel accepted, poll loop running
        filled = stack.broker.fill(order.id, 4.0)
        await deliver_fill(stack.trade, filled)  # stream event races the poll too

    filler = asyncio.create_task(fill_during_pending_cancel())
    try:
        absorbed = await stack.trade.cancel_resting_tp(plan)
        assert absorbed is True, "in-flight TP fill was not absorbed"
        done = await stack.trade.get_plan(plan.id)
        assert done.status == "closed"
        assert done.exit_reason == "tp"
        assert done.exit_premium == pytest.approx(4.0)
    finally:
        filler.cancel()


@pytest.mark.asyncio
async def test_unconfirmed_cancel_raises_instead_of_double_closing(stack):
    """A cancel that never reaches a terminal verdict must RAISE — returning
    False would green-light the ladder to stack a second close on top of a
    possibly-live TP order."""
    plan = await make_plan(stack.db, broker=stack.broker)
    order = stack.broker._register(SimpleNamespace(client_order_id=f"{plan.id}-xtp400", qty=1))
    await stack.trade.fsm.update_fields(plan.id, tp_order_id=order.id)
    plan = await stack.trade.get_plan(plan.id)
    stack.broker.async_cancels = True  # order stays "accepted" forever
    with pytest.raises(RuntimeError):
        await stack.trade.cancel_resting_tp(plan, confirm_s=0.6)
    # The field must NOT have been cleared: the TP is still live.
    assert (await stack.trade.get_plan(plan.id)).tp_order_id == order.id


class KeylessAlpaca:
    """The keyless surface: no keys, no trading client at all."""
    configured = False
    settings = None


@pytest.mark.asyncio
async def test_keyless_sl_simulates_fill_instead_of_force_close(stack):
    """Keyless mode must close a triggered plan with a SIMULATED fill at the
    mark — not fail every submit and FORCE_CLOSE it with no P&L (the cascade
    that would fire if the engine ever boots with its .env missing)."""
    keyless = KeylessAlpaca()
    from app.services.exit_enforcer import ExitEnforcer  # noqa: PLC0415
    from app.services.trade_service import TradeService  # noqa: PLC0415

    trade = TradeService(stack.db, keyless, stack.market, stack.trade.risk)
    enforcer = ExitEnforcer(stack.db, stack.market, trade)
    enforcer.escalation = [(0.02, 0.05), (0.06, 0.05), (None, 0.0)]
    enforcer.verify_poll_s = 0.04
    enforcer.rearm_delay_s = 0.05
    enforcer.quote_poll_s = 0.05
    enforcer.quote_poll_near_s = 0.05
    enforcer.resting_tp = False
    plan = await make_plan(stack.db)
    stack.market.quotes[SYM] = {"bid": 0.79, "ask": 0.81, "mid": 0.8}  # <= SL
    try:
        await enforcer.arm(plan.id)
        status = await wait_status(trade, plan.id, deadline=10.0)
        assert status == "closed", f"keyless plan stuck in {status}"
        done = await trade.get_plan(plan.id)
        assert done.exit_reason == "sl", f"expected simulated SL, got {done.exit_reason}"
        assert done.exit_premium == pytest.approx(0.8, abs=0.05)
        assert done.realized_pnl == pytest.approx((0.8 - 2.0) * 100, abs=6.0)
        assert done.exit_order_id and done.exit_order_id.startswith("sim-")
    finally:
        await enforcer.shutdown()


@pytest.mark.asyncio
async def test_underlying_tick_triggers_fast_sl(stack):
    """The underlying stock stream drives SL reaction: a spot tick that puts
    the MODEL value past the stop forces an option-quote refresh and the SL
    fires well inside the far-poll window."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.enforcer.quote_poll_s = 30.0  # far poll deliberately SLOW
    stack.enforcer.quote_poll_near_s = 30.0

    # refresh_option_quotes "REST" delivers the crash price when asked.
    async def refresh(symbols, max_age_s: float = 30.0):
        stack.market.refresh_calls += 1
        if max_age_s <= 1.0:  # only the forced (model-trigger) refresh
            stack.market.quotes[SYM] = {"bid": 0.59, "ask": 0.61, "mid": 0.6}

    stack.market.refresh_option_quotes = refresh
    # Cached option quotes start mid-bracket and the option stream is silent.
    stack.market.quotes[SYM] = {"bid": 1.99, "ask": 2.01, "mid": 2.0}
    filler = asyncio.create_task(
        auto_filler(stack.broker, stack.trade, deliver_events=False, price=0.6)
    )
    try:
        await stack.enforcer.arm(plan.id)
        await asyncio.sleep(0.3)  # monitor armed, waiting on its queue
        # Underlying crashes: SPY 450 -> 430 puts the modeled call value ~0.
        stack.market.broadcast.publish(
            "quote:SPY", {"t": "quote", "symbol": "SPY", "bid": 429.9, "ask": 430.1, "mid": 430.0},
        )
        t0 = time.monotonic()
        status = await wait_status(stack.trade, plan.id, deadline=10.0)
        elapsed = time.monotonic() - t0
        assert status == "closed", f"stuck in {status}"
        assert (await stack.trade.get_plan(plan.id)).exit_reason == "sl"
        assert elapsed < 5.0, f"SL took {elapsed:.1f}s - underlying trigger did not fire"
    finally:
        filler.cancel()


@pytest.mark.asyncio
async def test_sl_single_spike_does_not_shake_out(stack):
    """One tight-but-transient print below the stop must NOT fire the SL:
    it isn't deep enough for the crash fast-path, so it has to survive the
    confirmation dwell — and it doesn't."""
    await stack.trade.risk.update_settings({"sl_confirm_s": 0.4})
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.enforcer.quote_poll_s = 0.05
    stack.enforcer.quote_poll_near_s = 0.05
    try:
        await stack.enforcer.arm(plan.id)
        # Healthy mids, one spike, healthy mids again.
        for mid in (2.0, 2.0, 0.4, 2.0, 2.0, 2.0):
            stack.market.quotes[SYM] = {"bid": mid - 0.01, "ask": mid + 0.01, "mid": mid}
            stack.market.pump(SYM, mid)
            await asyncio.sleep(0.08)
        await asyncio.sleep(1.0)
        refreshed = await stack.trade.get_plan(plan.id)
        assert refreshed.status == "filled", (
            f"spike shook the position out (status={refreshed.status})"
        )
    finally:
        stack.enforcer.disarm(plan.id)


@pytest.mark.asyncio
async def test_sl_wide_junk_quote_cannot_fire_stop(stack):
    """A blown-out spread printing a 'mid' below the stop must not fire the
    SL even with the dwell OFF: the Kalman fair value weights quotes by
    their spread, so a junk observation carries almost no information."""
    # Fixture default: sl_confirm_s = 0 (instant fire on a fair-value breach).
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.enforcer.quote_poll_s = 0.05
    stack.enforcer.quote_poll_near_s = 0.05
    try:
        await stack.enforcer.arm(plan.id)
        # Establish fair value with tight healthy quotes at 2.0.
        for _ in range(5):
            stack.market.pump(SYM, 2.0)
            await asyncio.sleep(0.06)
        # Spread blows out: bid 0.10 / ask 1.70 -> "mid" 0.90 <= SL 1.0,
        # but the quote is 1.60 wide on a 3.0 bracket — pure noise.
        for _ in range(8):
            stack.market.quotes[SYM] = {"bid": 0.10, "ask": 1.70, "mid": 0.90}
            stack.market.broadcast.publish(f"oquote:{SYM}", {"t": "oquote", "symbol": SYM})
            await asyncio.sleep(0.06)
        refreshed = await stack.trade.get_plan(plan.id)
        assert refreshed.status == "filled", (
            f"junk quote fired the stop (status={refreshed.status})"
        )
        # The real market returns: still holding.
        for _ in range(5):
            stack.market.pump(SYM, 2.0)
            await asyncio.sleep(0.06)
        assert (await stack.trade.get_plan(plan.id)).status == "filled"
    finally:
        stack.enforcer.disarm(plan.id)


@pytest.mark.asyncio
async def test_sl_sustained_breach_fires_after_dwell(stack):
    """A REAL breach (persisting on filtered mids) fires after the dwell."""
    await stack.trade.risk.update_settings({"sl_confirm_s": 0.4})
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.enforcer.quote_poll_s = 0.05
    stack.enforcer.quote_poll_near_s = 0.05
    filler = asyncio.create_task(
        auto_filler(stack.broker, stack.trade, deliver_events=False, price=0.8)
    )
    try:
        await stack.enforcer.arm(plan.id)
        t0 = time.monotonic()

        async def pump_breach():
            while True:
                stack.market.quotes[SYM] = {"bid": 0.79, "ask": 0.81, "mid": 0.8}
                stack.market.pump(SYM, 0.8)
                await asyncio.sleep(0.05)

        pumper = asyncio.create_task(pump_breach())
        try:
            status = await wait_status(stack.trade, plan.id, deadline=10.0)
        finally:
            pumper.cancel()
        elapsed = time.monotonic() - t0
        assert status == "closed"
        assert (await stack.trade.get_plan(plan.id)).exit_reason == "sl"
        assert elapsed >= 0.35, f"dwell not respected ({elapsed:.2f}s)"
    finally:
        filler.cancel()


@pytest.mark.asyncio
async def test_sl_deep_breach_fires_immediately(stack):
    """A crash far past the stop (>=25% of the TP-SL span) must NOT wait for
    the dwell — blowup prevention beats shakeout prevention."""
    await stack.trade.risk.update_settings({"sl_confirm_s": 5.0})  # long dwell
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.enforcer.quote_poll_s = 0.05
    stack.enforcer.quote_poll_near_s = 0.05
    filler = asyncio.create_task(
        auto_filler(stack.broker, stack.trade, deliver_events=False, price=0.1)
    )
    try:
        await stack.enforcer.arm(plan.id)
        t0 = time.monotonic()

        async def pump_crash():
            while True:
                stack.market.quotes[SYM] = {"bid": 0.09, "ask": 0.11, "mid": 0.1}
                stack.market.pump(SYM, 0.1)
                await asyncio.sleep(0.05)

        pumper = asyncio.create_task(pump_crash())
        try:
            status = await wait_status(stack.trade, plan.id, deadline=10.0)
        finally:
            pumper.cancel()
        elapsed = time.monotonic() - t0
        assert status == "closed"
        assert (await stack.trade.get_plan(plan.id)).exit_reason == "sl"
        assert elapsed < 3.0, f"deep breach waited for dwell ({elapsed:.2f}s)"
    finally:
        filler.cancel()
