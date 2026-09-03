"""Self-contained API + market pressure test: the REAL routes, TradeService,
FSM and ExitEnforcer under concurrent load — no keys, no network, no market.

The chaos suite (tests/test_chaos.py) proves correctness under FAULTS; this
measures behavior under VOLUME and prices EXECUTION QUALITY — realized fills
vs intent — reusing the same fakes so the stack under test is identical.
Fills come from a marketable-limit filler: a limit order fills at its limit
only when marketable against the live fake mid, a market order fills at the
mid moved adversely — so slippage is a property of the exit path (resting TP
vs software SL ladder), not of the harness.

  1. entry storm     concurrent POST /api/orders through the full risk-gate
                     + placement path (resting TP arms at the broker on fill)
  2. poll + firehose N HTTP pollers hammer /api/positions, /api/account and
                     /api/orders/open while quote ticks pump at full rate
                     into every armed monitor (prices held inside the band)
  3. exit storms     half the book DRIFTS through its TP/SL, half GAPS deep
                     past it, under broker latency + transient submit
                     failures, racing manual POST /close calls; measures
                     tick-breach -> broker-submit latency and slippage
                     (specified SL/TP premium vs realized exit premium)
  4. fanout flood    the lossy Broadcaster under a many-subscriber flood

Invariants asserted at the end (exit code 1 on any breach): every plan
closed, exactly one filled exit order per plan, no live orders left behind,
entry fills never worse than the entry limit.

Run:  cd backend && .venv\\Scripts\\python.exe scripts\\pressure_test.py
      (--quick for a ~15s smoke; --help for knobs)
"""

import argparse
import asyncio
import logging
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.api.routes.trading import router as trading_router  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.services.broadcast import Broadcaster  # noqa: E402
from app.services.exit_enforcer import ExitEnforcer  # noqa: E402
from app.services.risk import RiskService  # noqa: E402
from app.services.trade_service import TradeService  # noqa: E402
from tests.test_chaos import (  # noqa: E402
    FakeAlpaca,
    FakeMarket,
    FlakyBroker,
    deliver_fill,
    make_plan,
)

log = logging.getLogger("pressure")

TP, SL, ENTRY = 4.0, 1.0, 2.0
SPAN = TP - SL


class PressureBroker(FlakyBroker):
    """FlakyBroker plus the read surface the API pollers hit."""

    def get_account(self):
        return SimpleNamespace(
            equity="1000000", cash="1000000", buying_power="2000000",
            daytrade_count=0, status="ACTIVE",
        )

    def get_portfolio_history(self, request):
        return SimpleNamespace(timestamp=[], equity=[], profit_loss=[], base_value=None)

    def get_clock(self):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(is_open=True, timestamp=now,
                               next_open=now + timedelta(hours=18),
                               next_close=now + timedelta(hours=2))

    def get_orders(self, request):
        return []


async def quality_filler(broker, trade, mids: dict[str, float],
                         plan_sym: dict[str, str], *,
                         latency: float = 0.01, market_slip: float = 0.02):
    """Marketable-limit fill model. Entry cids end in -e (buys), exit cids
    contain -x (sells, long book). A limit fills AT ITS LIMIT when marketable
    against the live mid; a market order fills at the mid moved adversely by
    market_slip. Non-marketable limits rest (the enforcer's escalation ladder
    reprices them)."""
    while True:
        await asyncio.sleep(0.005)
        now = time.monotonic()
        with broker.lock:
            due = [o for o in broker.orders.values()
                   if o.status == "accepted" and now - o.submitted_mono >= latency]
        for o in due:
            cid = o.client_order_id
            pid = cid.split("-x")[0].split("-e")[0]
            # Real-broker position truth: once a plan's position is gone, a
            # second close order REJECTS at the exchange instead of filling
            # (FlakyBroker models this at submit time; mirror it for fills,
            # or a straggler the enforcer is about to sweep double-fills).
            if "-x" in cid:
                with broker.lock:
                    if pid in broker.closed_plans:
                        o.status = "rejected"
                        continue
            mid = mids.get(plan_sym.get(pid, ""), None)
            if mid is None:
                continue
            is_buy = cid.endswith("-e")
            limit = o.limit_price
            if limit is None:
                price = mid * (1 + market_slip) if is_buy else mid * (1 - market_slip)
            elif (is_buy and float(limit) >= mid) or (not is_buy and float(limit) <= mid):
                price = float(limit)
            else:
                continue  # rests until marketable or repriced
            try:
                filled = broker.fill(o.id, round(price, 4))
            except (AssertionError, KeyError):
                continue
            await deliver_fill(trade, filled)


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def lat_line(name: str, values: list[float], unit: str = "ms") -> str:
    if not values:
        return f"  {name:<38} (no samples)"
    return (f"  {name:<38} n={len(values):<5} p50={pct(values, 0.50):8.2f}{unit}"
            f"  p95={pct(values, 0.95):8.2f}{unit}  max={max(values):8.2f}{unit}")


def slip_line(name: str, slips: list[float]) -> str:
    """Slippage in premium $/share vs intent; positive = worse than intent."""
    if not slips:
        return f"  {name:<38} (no fills)"
    return (f"  {name:<38} n={len(slips):<5} p50={pct(slips, 0.50):+8.3f}"
            f"  p95={pct(slips, 0.95):+8.3f}  max={max(slips):+8.3f}"
            f"   ({pct(slips, 0.50) / SPAN * 100:+.1f}% of TP-SL span at p50)")


def option_symbol(i: int) -> str:
    return f"SPY260731C{450 + i:05d}000"


def order_payload(i: int) -> dict:
    return {
        "underlying": "SPY", "strategy": "pressure",
        "legs": [{"symbol": option_symbol(i), "right": "C", "strike": 450.0 + i,
                  "expiry": "2026-07-31", "side": 1, "ratio": 1,
                  "entry": ENTRY, "iv": 0.2}],
        "qty": 1, "entry_limit": ENTRY, "tp_premium": TP, "sl_premium": SL,
        "time_stop_utc": (datetime.now(timezone.utc)
                          + timedelta(hours=2)).isoformat(),
    }


async def main(args: argparse.Namespace) -> int:
    random.seed(args.seed)
    tmp = tempfile.mkdtemp(prefix="pressure-")
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp}/pressure.db")
    broker = PressureBroker()
    alpaca = FakeAlpaca(broker, call_timeout=2.0)
    market = FakeMarket()
    market.status = lambda: {"t": "status", "stream_age_s": market.stream_age_s}
    market.spot = lambda symbol: 452.0
    risk = RiskService(db)
    trade = TradeService(db, alpaca, market, risk)
    enforcer = ExitEnforcer(db, market, trade)
    # Compressed-but-not-instant enforcement; resting TP ON (production shape).
    enforcer.escalation = [(0.05, 0.15), (0.1, 0.15), (None, 0.0)]
    enforcer.verify_poll_s = 0.05
    enforcer.verify_attempts = 200
    enforcer.rearm_delay_s = 0.05
    enforcer.reconcile_interval_s = 0.5
    enforcer.resting_tp = True
    await risk.update_settings({
        "sl_confirm_s": 0.0,          # deterministic reaction-latency samples
        "max_positions": 20,
        "max_trades_per_day": 200,
        "daily_loss_pct": 0.25,
    })

    app = FastAPI()
    app.include_router(trading_router)
    app.state.db, app.state.trade, app.state.risk = db, trade, risk
    app.state.market, app.state.enforcer = market, enforcer

    n_api = min(args.api_plans, 18)          # stays under max_positions=20
    n_direct = args.direct_plans
    failures: list[str] = []
    api_syms = [option_symbol(i) for i in range(n_api)]
    direct_syms = [option_symbol(100 + i) for i in range(n_direct)]
    all_syms = api_syms + direct_syms
    mids: dict[str, float] = {s: ENTRY for s in all_syms}
    plan_sym: dict[str, str] = {}

    filler = asyncio.create_task(quality_filler(broker, trade, mids, plan_sym))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 timeout=30.0) as client:
        # -------------------------------------------------- 1. entry storm
        for sym in api_syms:
            market.pump(sym, ENTRY)
        entry_lat: list[float] = []
        refused = 0

        async def place(i: int) -> None:
            nonlocal refused
            t0 = time.monotonic()
            r = await client.post("/api/orders", json=order_payload(i))
            entry_lat.append((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                refused += 1
                log.warning("order %d refused: %s %s", i, r.status_code, r.text[:200])
            else:
                plan_sym[r.json()["id"]] = option_symbol(i)

        t0 = time.monotonic()
        await asyncio.gather(*(place(i) for i in range(n_api)))
        entry_wall = time.monotonic() - t0
        deadline = time.monotonic() + 10
        filled_api: list = []
        while time.monotonic() < deadline:
            filled_api = [p for p in await risk.open_plans() if p.status == "filled"]
            if len(filled_api) >= n_api - refused:
                break
            await asyncio.sleep(0.05)
        if refused:
            failures.append(f"entry storm: {refused} orders refused")
        if len(filled_api) < n_api - refused:
            failures.append(f"entry storm: only {len(filled_api)}/{n_api} filled")

        # Direct-DB plans (the chaos suite's path) to push the armed-monitor
        # count past what the API's max_positions gate allows.
        direct_ids = []
        for sym in direct_syms:
            market.pump(sym, ENTRY)  # a monitor armed quoteless logs UNEVALUABLE
            p = await make_plan(db, symbol=sym, tp=TP, sl=SL, entry=ENTRY,
                                broker=broker)
            plan_sym[p.id] = sym
            await enforcer.arm(p.id)
            direct_ids.append(p.id)
        all_ids = [p.id for p in filled_api] + direct_ids

        # -------------------------------------------- 2. poll + tick firehose
        reconcile = asyncio.create_task(enforcer.reconcile_loop())
        poll_lat: dict[str, list[float]] = {"/api/positions": [], "/api/account": [],
                                            "/api/orders/open": []}
        loop_lag: list[float] = []
        ticks_sent = 0
        stop = asyncio.Event()

        async def poller(idx: int) -> None:
            while not stop.is_set():
                for path in poll_lat:
                    t0 = time.monotonic()
                    r = await client.get(path)
                    poll_lat[path].append((time.monotonic() - t0) * 1000)
                    if r.status_code != 200:
                        failures.append(f"poller {idx}: {path} -> {r.status_code}")
                        return

        async def pump_walk() -> None:
            # Random walk strictly inside (SL, TP): monitors evaluate every
            # tick but never trigger, and the resting TPs stay unmarketable.
            nonlocal ticks_sent
            while not stop.is_set():
                for s in all_syms:
                    mids[s] = min(3.6, max(1.4, mids[s] + random.uniform(-0.05, 0.05)))
                    market.pump(s, round(mids[s], 3))
                    ticks_sent += 1
                await asyncio.sleep(0.001)

        async def lag_probe() -> None:
            while not stop.is_set():
                t0 = time.monotonic()
                await asyncio.sleep(0.05)
                loop_lag.append((time.monotonic() - t0 - 0.05) * 1000)

        tasks = ([asyncio.create_task(poller(i)) for i in range(args.pollers)]
                 + [asyncio.create_task(pump_walk()), asyncio.create_task(lag_probe())])
        t0 = time.monotonic()
        await asyncio.sleep(args.seconds)
        stop.set()
        firehose_wall = time.monotonic() - t0
        await asyncio.gather(*tasks, return_exceptions=True)

        # -------------------------------- 3. exit storms: drift vs gap
        broker.latency = 0.005
        broker.fail_submits = 5
        half = len(all_syms) // 2
        drift_syms, gap_syms = set(all_syms[:half]), set(all_syms[half:])
        tp_half = set(all_syms[::2])           # every other symbol exits via TP
        scenario = {s: ("drift" if s in drift_syms else "gap") for s in all_syms}
        breach_t: dict[str, float] = {}
        targets = {s: (TP * 1.05 if s in tp_half else SL * 0.9) for s in all_syms}

        async def drive_prices() -> None:
            """Gap symbols teleport; drift symbols walk 0.04/5ms. Everyone
            holds at target so retries after injected failures still see the
            breach (a trending market, not a wick)."""
            for s in gap_syms:
                mids[s] = 4.5 if s in tp_half else 0.5
                breach_t[s] = time.monotonic()
                market.pump(s, mids[s])
            try:
                while True:
                    for s in drift_syms:
                        tgt = targets[s]
                        if mids[s] != tgt:
                            step = 0.04 if tgt > mids[s] else -0.04
                            nxt = mids[s] + step
                            mids[s] = tgt if (step > 0) == (nxt >= tgt) else nxt
                            crossed = (mids[s] >= TP if s in tp_half else mids[s] <= SL)
                            if crossed and s not in breach_t:
                                breach_t[s] = time.monotonic()
                    for s in all_syms:
                        market.pump(s, round(mids[s], 3))
                    await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                raise

        driver = asyncio.create_task(drive_prices())
        close_lat: list[float] = []

        async def manual_close(pid: str) -> None:
            t = time.monotonic()
            r = await client.post(f"/api/positions/{pid}/close")
            close_lat.append((time.monotonic() - t) * 1000)
            if r.status_code not in (200, 409, 422):
                failures.append(f"close {pid} -> {r.status_code}")

        racers = random.sample(all_ids, max(1, len(all_ids) // 3))
        t0 = time.monotonic()
        await asyncio.gather(*(manual_close(pid) for pid in racers))
        deadline = time.monotonic() + 45
        remaining = list(all_ids)
        while remaining and time.monotonic() < deadline:
            remaining = [pid for pid in remaining
                         if (await trade.get_plan(pid)).status != "closed"]
            await asyncio.sleep(0.1)
        exit_wall = time.monotonic() - t0
        driver.cancel()
        reconcile.cancel()

        # Tick-breach -> broker exit-submit reaction latency, from the
        # broker's own history clock. Resting-TP plans already had their TP
        # at the broker pre-breach, so this measures the SOFTWARE paths (SL,
        # and any escalation) — filter to cids submitted after the breach.
        reaction: list[float] = []
        with broker.lock:
            registers = [(t, cid) for (t, act, cid) in broker.history
                         if act == "register"]
        for pid in all_ids:
            sym = plan_sym[pid]
            if sym in tp_half or sym not in breach_t:
                continue
            subs = [t for (t, cid) in registers
                    if cid.startswith(f"{pid}-x") and t >= breach_t[sym]]
            if subs:
                reaction.append((min(subs) - breach_t[sym]) * 1000)

    filler.cancel()

    # --------------------------------------- invariants + execution quality
    entry_slip: list[float] = []
    slips: dict[tuple[str, str], list[float]] = {}
    reasons: dict[str, int] = {}
    stuck = []
    for pid in all_ids:
        plan = await trade.get_plan(pid)
        if plan.status != "closed":
            stuck.append((pid, plan.status))
            continue
        reasons[plan.exit_reason or "?"] = reasons.get(plan.exit_reason or "?", 0) + 1
        exits = broker.filled_exits_for(pid)
        if len(exits) != 1:
            failures.append(f"plan {pid}: {len(exits)} filled exit orders (want 1)")
        if broker.live_orders_for(pid):
            failures.append(f"plan {pid}: live orders left at the broker")
        if plan.fill_premium is not None:
            entry_slip.append(plan.fill_premium - (plan.entry_limit or ENTRY))
        if plan.exit_premium is None:
            continue
        # Long book: worse = selling lower. SL slip vs the specified stop,
        # TP slip vs the specified target (resting limit should pin it to 0).
        if plan.exit_reason == "sl":
            slip = (plan.sl_premium or SL) - plan.exit_premium
        elif plan.exit_reason == "tp":
            slip = (plan.tp_premium or TP) - plan.exit_premium
        else:
            slip = None
        if slip is not None:
            key = (scenario.get(plan_sym[pid], "?"), plan.exit_reason)
            slips.setdefault(key, []).append(slip)
    if stuck:
        failures.append(f"{len(stuck)} plans not closed: {stuck[:5]}")
    if entry_slip and max(entry_slip) > 1e-9:
        failures.append(f"entry filled WORSE than limit: max slip {max(entry_slip):+.4f}")

    # ------------------------------------------------- 4. fanout flood
    caster = Broadcaster()
    queues = [asyncio.Queue(maxsize=64) for _ in range(args.subscribers)]
    for q in queues:
        caster.subscribe("flood", q)
    t0 = time.monotonic()
    for i in range(args.flood_msgs):
        caster.publish("flood", {"t": "flood", "i": i})
    flood_wall = time.monotonic() - t0
    delivered = sum(q.qsize() for q in queues)

    await enforcer.shutdown()
    await db.close()

    print("\n=== pressure test report ===")
    print(f"book: {len(all_ids)} plans ({n_api} via POST /api/orders, "
          f"{n_direct} direct), TP {TP} / SL {SL} / entry {ENTRY}, seed={args.seed}")
    print("\n[1] entry storm")
    print(lat_line("POST /api/orders", entry_lat))
    print(slip_line("entry slippage vs limit (buy)", entry_slip))
    print(f"  {n_api} concurrent orders in {entry_wall:.2f}s wall, {refused} refused")
    print(f"\n[2] poll + tick firehose ({firehose_wall:.1f}s, {args.pollers} pollers, "
          f"{len(all_ids)} armed monitors)")
    for path, values in poll_lat.items():
        print(lat_line(f"GET {path}", values))
    print(f"  ticks pumped: {ticks_sent} ({ticks_sent / firehose_wall:,.0f}/s)")
    print(lat_line("event-loop lag (50ms sleep drift)", loop_lag))
    print("\n[3] exit storms (broker latency 5ms, 5 transient submit failures, "
          f"{len(racers)} racing manual closes)")
    print(lat_line("SL tick-breach -> broker submit", reaction))
    print(lat_line("POST /close (racing)", close_lat))
    for key in sorted(slips):
        scen, reason = key
        print(slip_line(f"{reason.upper()} slippage, {scen} market", slips[key]))
    print(f"  exit reasons: {dict(sorted(reasons.items()))}")
    print(f"  all plans closed in {exit_wall:.2f}s wall")
    print(f"\n[4] broadcaster flood: {args.flood_msgs} msgs x {args.subscribers} "
          f"subscribers in {flood_wall:.2f}s "
          f"({args.flood_msgs * args.subscribers / max(flood_wall, 1e-9):,.0f} "
          f"deliveries/s attempted); {delivered} retained in bounded queues, "
          "overflow dropped by design (lossy fanout)")

    if failures:
        print(f"\nFAILED invariants ({len(failures)}):")
        for f in failures[:20]:
            print(f"  - {f}")
        return 1
    print("\nall invariants held: every plan closed exactly once, one filled "
          "exit order each, no stray live orders, entries never beat by fills.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api-plans", type=int, default=18)
    ap.add_argument("--direct-plans", type=int, default=42)
    ap.add_argument("--pollers", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="firehose phase duration")
    ap.add_argument("--subscribers", type=int, default=200)
    ap.add_argument("--flood-msgs", type=int, default=50_000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--quick", action="store_true",
                    help="small book, 3s firehose (~15s total)")
    args = ap.parse_args()
    if args.quick:
        args.api_plans, args.direct_plans, args.seconds = 6, 10, 3.0
        args.subscribers, args.flood_msgs = 100, 10_000
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(main(args)))
