"""Empirical probe: Alpaca advanced order classes for EQUITY exits on the
PAPER account — the facts the broker-resting-stop upgrade (manual swing book)
must stand on. Never derive these from docs; run this and read the verdicts.

Checks
  1. bracket_otoco      order_class=bracket (OTOCO): 1-share non-marketable
                        DAY limit entry + take_profit + stop_loss in one
                        submit. Accepted? What do the legs look like?
  2. bracket_gtc        same, TIF=GTC — the swing shape (multi-day bracket).
  3. bracket_extended   bracket + extended_hours=True — docs say brackets are
                        RTH-only; verify the actual rejection/acceptance.
  4. oco_exit_pair      order_class=oco against an EXISTING long position:
                        TP limit + stop, one cancels the other. Needs a
                        position — with --fill the script buys 1 share
                        marketable first (RTH/extended), else SKIPs.
  5. sl_only_stop_gtc   a plain GTC STOP (sell) order resting alone against
                        the position — the SL-only swing plan's resting stop.
  6. trailing_stop      order_class-less TrailingStopOrderRequest (percent):
                        accepted? GTC?

Safety: paper-only (validate_paper_lock), qty=1 everywhere, every surviving
order cancelled and any acquired share flattened at the end. Run outside
09:00-09:30 ET / 13:55-14:05 ET engine windows if the engine is up — the
external-exit capture will see these orders (they carry a 'verify-' client
id prefix and close same-run, so reconcile noise is bounded).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca.data.requests import StockLatestTradeRequest  # noqa: E402
from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: E402
from alpaca.trading.requests import (  # noqa: E402
    LimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
    TrailingStopOrderRequest,
)

from app.config import get_settings, validate_paper_lock  # noqa: E402
from app.services.alpaca import AlpacaService  # noqa: E402

ET = ZoneInfo("America/New_York")
SYMBOL = "SPY"

RESULTS: list[tuple[str, str, str]] = []
LIVE_ORDERS: list[object] = []


def record(check: str, status: str, detail: str) -> None:
    RESULTS.append((check, status, detail))
    print(f"  [{status:^4}] {check}: {detail}")


def cid(tag: str) -> str:
    return f"verify-oco-{tag}-{uuid.uuid4().hex[:8]}"


async def reference_price(alpaca: AlpacaService) -> float | None:
    for feed in ("iex", "overnight"):
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=SYMBOL, feed=feed)
            out = await alpaca.call(alpaca.stock_data.get_stock_latest_trade, req)
            price = float(out[SYMBOL].price)
            print(f"  reference price ({feed}): {SYMBOL} last={price}")
            return price
        except Exception as exc:
            print(f"  latest-trade via feed={feed} failed: {exc}")
    return None


async def submit(alpaca: AlpacaService, request, label: str):
    try:
        order = await alpaca.call(alpaca.trading.submit_order, request)
    except Exception as exc:
        print(f"  {label}: REJECTED at submit: {exc}")
        return None, str(exc)
    LIVE_ORDERS.append(order)
    legs = getattr(order, "legs", None)
    print(
        f"  {label}: accepted id={order.id} status={order.status} "
        f"class={getattr(order, 'order_class', None)} "
        f"tif={order.time_in_force} legs={len(legs) if legs else 0}"
    )
    return order, None


async def cleanup(alpaca: AlpacaService, flatten: bool) -> None:
    print("\ncleanup:")
    for order in LIVE_ORDERS:
        try:
            await alpaca.call(alpaca.trading.cancel_order_by_id, order.id)
            print(f"  cancelled {order.id}")
        except Exception as exc:
            print(f"  cancel {order.id}: {exc}")
    if flatten:
        try:
            await alpaca.call(alpaca.trading.close_position, SYMBOL)
            print(f"  flattened {SYMBOL}")
        except Exception as exc:
            print(f"  flatten {SYMBOL}: {exc}")


async def main(fill: bool) -> None:
    settings = get_settings()
    validate_paper_lock(settings)
    alpaca = AlpacaService(settings)
    if not alpaca.configured:
        print("no keys configured; aborting")
        return
    now = datetime.now(ET)
    print(f"verify_equity_oco @ {now:%Y-%m-%d %H:%M:%S} ET\n")
    ref = await reference_price(alpaca)
    if ref is None:
        print("no reference price; aborting")
        return

    lo = round(ref * 0.90, 2)   # non-marketable entry
    tp = round(ref * 1.10, 2)
    sl = round(ref * 0.80, 2)
    sl_lim = round(ref * 0.79, 2)

    # 1. OTOCO bracket, DAY
    req = LimitOrderRequest(
        symbol=SYMBOL, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        limit_price=lo, order_class="bracket",
        take_profit=TakeProfitRequest(limit_price=tp),
        stop_loss=StopLossRequest(stop_price=sl, limit_price=sl_lim),
        client_order_id=cid("brk-day"),
    )
    order, err = await submit(alpaca, req, "bracket DAY")
    record("bracket_otoco", "PASS" if order else "FAIL",
           err or f"accepted with {len(order.legs or [])} legs")

    # 2. OTOCO bracket, GTC — the swing shape
    req = LimitOrderRequest(
        symbol=SYMBOL, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
        limit_price=lo, order_class="bracket",
        take_profit=TakeProfitRequest(limit_price=tp),
        stop_loss=StopLossRequest(stop_price=sl, limit_price=sl_lim),
        client_order_id=cid("brk-gtc"),
    )
    order, err = await submit(alpaca, req, "bracket GTC")
    record("bracket_gtc", "PASS" if order else "FAIL", err or "accepted")

    # 3. bracket + extended_hours (expected: rejected per docs — verify)
    req = LimitOrderRequest(
        symbol=SYMBOL, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        limit_price=lo, extended_hours=True, order_class="bracket",
        take_profit=TakeProfitRequest(limit_price=tp),
        stop_loss=StopLossRequest(stop_price=sl),
        client_order_id=cid("brk-ext"),
    )
    order, err = await submit(alpaca, req, "bracket extended_hours")
    record("bracket_extended", "INFO",
           ("ACCEPTED — docs said RTH-only, broker disagrees"
            if order else f"rejected as documented: {err}"))

    # 4-6 need a real position.
    have_position = False
    if fill:
        entry = LimitOrderRequest(
            symbol=SYMBOL, qty=1, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(ref * 1.01, 2), extended_hours=True,
            client_order_id=cid("acquire"),
        )
        order, err = await submit(alpaca, entry, "acquire 1 share (marketable)")
        if order:
            for _ in range(30):
                await asyncio.sleep(2)
                got = await alpaca.call(alpaca.trading.get_order_by_id, order.id)
                if str(got.status).lower().endswith("filled"):
                    have_position = True
                    LIVE_ORDERS.remove(order)
                    break
            print(f"  acquire fill: {have_position}")
    if not have_position:
        record("oco_exit_pair", "SKIP", "needs a position (--fill, market open)")
        record("sl_only_stop_gtc", "SKIP", "needs a position")
        record("trailing_stop", "SKIP", "needs a position")
    else:
        # 4. OCO exit pair
        req = LimitOrderRequest(
            symbol=SYMBOL, qty=1, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, limit_price=tp, order_class="oco",
            take_profit=TakeProfitRequest(limit_price=tp),
            stop_loss=StopLossRequest(stop_price=sl, limit_price=sl_lim),
            client_order_id=cid("oco"),
        )
        order, err = await submit(alpaca, req, "OCO exit pair GTC")
        record("oco_exit_pair", "PASS" if order else "FAIL", err or "accepted")
        await cleanup_one(alpaca, order)

        # 5. plain GTC stop resting alone (the SL-only swing shape)
        req = StopOrderRequest(
            symbol=SYMBOL, qty=1, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, stop_price=sl,
            client_order_id=cid("stop"),
        )
        order, err = await submit(alpaca, req, "GTC stop (SL-only)")
        record("sl_only_stop_gtc", "PASS" if order else "FAIL", err or "accepted")
        await cleanup_one(alpaca, order)

        # 6. trailing stop
        req = TrailingStopOrderRequest(
            symbol=SYMBOL, qty=1, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, trail_percent=5.0,
            client_order_id=cid("trail"),
        )
        order, err = await submit(alpaca, req, "trailing stop 5% GTC")
        record("trailing_stop", "PASS" if order else "FAIL", err or "accepted")
        await cleanup_one(alpaca, order)

    await cleanup(alpaca, flatten=have_position)

    print("\n==== verdicts ====")
    for check, status, detail in RESULTS:
        print(f"[{status:^4}] {check}: {detail}")


async def cleanup_one(alpaca: AlpacaService, order) -> None:
    if order is None:
        return
    try:
        await alpaca.call(alpaca.trading.cancel_order_by_id, order.id)
        LIVE_ORDERS.remove(order)
        print(f"  cancelled {order.id}")
    except Exception as exc:
        print(f"  cancel {order.id}: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fill", action="store_true",
                        help="acquire 1 share to run the position-dependent checks")
    args = parser.parse_args()
    asyncio.run(main(args.fill))
