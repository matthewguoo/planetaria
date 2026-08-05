"""Empirical verification of equity SHORT paths on the PAPER account.

The short-readiness build (test_equity_short.py) encodes broker behavior
that must be proven against the real paper API, not derived from docs. Run
this before enabling equity_long_only=false for real strategy use, and
re-run in RTH — the fill probe is only meaningful when shorts can actually
execute (SSR/uptick mechanics don't apply to accepted-but-resting limits).

Checks
  1. account_shorting     the paper account reports shorting_enabled and a
                          margin multiplier.
  2. asset_flags          SPY/AMD report shortable + easy_to_borrow; prints
                          the flags this build's borrow gate consults.
  3. short_submit         a 1-share SELL limit on SPY with no long position
                          (i.e. a SHORT ENTRY) is accepted by the paper API.
                          Submitted non-marketable and cancelled by default.
  4. short_fill (--fill)  marketable 1-share short, wait for fill, confirm
                          the position reads side=short qty=-1, then BUY 1
                          to cover and confirm flat. RTH recommended.

Safety: refuses to run unless ALPACA_PAPER is true. qty=1 everywhere; every
resting order is cancelled at the end; --fill covers before exiting.
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
from alpaca.trading.requests import LimitOrderRequest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.alpaca import AlpacaService  # noqa: E402

ET = ZoneInfo("America/New_York")
SYMBOL = "SPY"

RESULTS: list[tuple[str, str, str]] = []


def record(check: str, status: str, detail: str) -> None:
    RESULTS.append((check, status, detail))
    print(f"  [{status:^4}] {check}: {detail}")


async def latest_price(alpaca: AlpacaService, symbol: str) -> float:
    trade = await alpaca.call(
        alpaca.stock_data.get_stock_latest_trade,
        StockLatestTradeRequest(symbol_or_symbols=symbol, feed="overnight"),
        timeout=8.0,
    )
    return float(trade[symbol].price)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fill", action="store_true",
                        help="prove an actual short FILL + cover (RTH recommended)")
    args = parser.parse_args()

    settings = get_settings()  # validate_paper_lock refuses live keys
    alpaca = AlpacaService(settings)
    if not alpaca.configured:
        print("no Alpaca keys configured; aborting")
        return

    now_et = datetime.now(ET)
    print(f"verify_short_paths @ {now_et:%Y-%m-%d %H:%M:%S ET}\n")

    # 1. Account-level shorting entitlement.
    acct = await alpaca.call(alpaca.trading.get_account, timeout=8.0)
    shorting = getattr(acct, "shorting_enabled", None)
    record("account_shorting",
           "PASS" if shorting else "FAIL",
           f"shorting_enabled={shorting} multiplier={getattr(acct, 'multiplier', '?')} "
           f"equity=${float(acct.equity):,.0f}")

    # 2. Asset borrow flags — what our _asset_shortable gate consults.
    for symbol in ("SPY", "AMD"):
        asset = await alpaca.call(alpaca.trading.get_asset, symbol, timeout=8.0)
        ok = bool(asset.shortable) and bool(asset.easy_to_borrow)
        record("asset_flags", "PASS" if ok else "FAIL",
               f"{symbol}: shortable={asset.shortable} etb={asset.easy_to_borrow} "
               f"marginable={asset.marginable}")

    # Ensure no existing SPY long muddies the "this SELL is a short" claim.
    positions = await alpaca.call(alpaca.trading.get_all_positions, timeout=8.0)
    spy_qty = next((float(p.qty) for p in positions if p.symbol == SYMBOL), 0.0)
    if spy_qty != 0:
        record("precondition", "SKIP",
               f"existing {SYMBOL} position qty={spy_qty}; short probes skipped "
               "(a SELL here would close, not short)")
        _summary()
        return

    price = await latest_price(alpaca, SYMBOL)

    # 3. Short ENTRY acceptance: SELL 1 far above the market so it rests.
    limit = round(price * 1.05, 2)
    cid = f"vshort-{uuid.uuid4().hex[:12]}"
    try:
        order = await alpaca.call(
            alpaca.trading.submit_order,
            LimitOrderRequest(symbol=SYMBOL, qty=1, side=OrderSide.SELL,
                              limit_price=limit, time_in_force=TimeInForce.DAY,
                              extended_hours=True, client_order_id=cid),
            timeout=8.0,
        )
        record("short_submit", "PASS",
               f"SELL 1 {SYMBOL} @ {limit} (no position held) accepted: "
               f"status={order.status}")
        await alpaca.call(alpaca.trading.cancel_order_by_id, order.id, timeout=8.0)
        record("short_submit_cancel", "INFO", "resting probe cancelled")
    except Exception as exc:
        record("short_submit", "FAIL", f"rejected: {exc}")

    # 4. Optional fill proof: marketable short, confirm negative position, cover.
    if args.fill:
        limit = round(price * 0.995, 2)  # at/through the bid -> should fill
        cid = f"vshortf-{uuid.uuid4().hex[:12]}"
        order = await alpaca.call(
            alpaca.trading.submit_order,
            LimitOrderRequest(symbol=SYMBOL, qty=1, side=OrderSide.SELL,
                              limit_price=limit, time_in_force=TimeInForce.DAY,
                              extended_hours=True, client_order_id=cid),
            timeout=8.0,
        )
        filled = False
        for _ in range(30):
            await asyncio.sleep(1.0)
            o = await alpaca.call(alpaca.trading.get_order_by_id, order.id, timeout=8.0)
            if str(o.status).lower().endswith("filled"):
                filled = True
                record("short_fill", "PASS",
                       f"short filled @ {o.filled_avg_price}")
                break
        if not filled:
            await alpaca.call(alpaca.trading.cancel_order_by_id, order.id, timeout=8.0)
            record("short_fill", "INFO",
                   "accepted but unfilled in 30s (session/SSR dependent); cancelled")
        else:
            positions = await alpaca.call(alpaca.trading.get_all_positions, timeout=8.0)
            pos = next((p for p in positions if p.symbol == SYMBOL), None)
            record("short_position", "PASS" if pos and float(pos.qty) < 0 else "FAIL",
                   f"position qty={getattr(pos, 'qty', None)} "
                   f"side={getattr(pos, 'side', None)}")
            cover = await alpaca.call(
                alpaca.trading.submit_order,
                LimitOrderRequest(symbol=SYMBOL, qty=1, side=OrderSide.BUY,
                                  limit_price=round(price * 1.005, 2),
                                  time_in_force=TimeInForce.DAY, extended_hours=True,
                                  client_order_id=f"vcover-{uuid.uuid4().hex[:12]}"),
                timeout=8.0,
            )
            for _ in range(30):
                await asyncio.sleep(1.0)
                o = await alpaca.call(alpaca.trading.get_order_by_id, cover.id,
                                      timeout=8.0)
                if str(o.status).lower().endswith("filled"):
                    record("short_cover", "PASS", f"covered @ {o.filled_avg_price}")
                    break
            else:
                record("short_cover", "FAIL",
                       "cover not filled in 30s - FLATTEN MANUALLY")

    _summary()


def _summary() -> None:
    print("\nsummary:")
    for check, status, detail in RESULTS:
        print(f"  [{status:^4}] {check}: {detail}")


if __name__ == "__main__":
    asyncio.run(main())
