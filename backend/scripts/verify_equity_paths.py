"""Day-one empirical verification of equity order paths on the PAPER account.

Purpose: the strategy-runtime build (see plan) adds equity support on top of
assumptions that must be proven against the real paper API, not derived from
docs. Run this before building the branches that depend on them, and re-run
in each session window (premarket ~08:30 ET, post-market ~17:30 ET, overnight
after 20:00 ET) — several checks are only meaningful in a specific session.

Checks
  1. equity_limit_extended   1-share DAY limit with extended_hours=True is
                             accepted (submitted non-marketable, then
                             cancelled — never fills in default mode).
  2. no_position_intent      the same order omits position_intent entirely;
                             acceptance proves equities don't need it.
  3. market_after_hours      a market DAY order outside RTH should be
                             REJECTED by the broker (only runs outside RTH).
  4. news_stream             the Benzinga news WebSocket authenticates on
                             these keys (entitlement check for the future
                             AlpacaNewsFeed adapter).
  5. overnight_submit        only in 20:00-04:00 ET: what does the broker do
                             with an extended-hours limit during the Blue
                             Ocean session? (data access is verified on this
                             tier; EXECUTION is the open question).

--fill additionally proves an actual extended-hours FILL: submits a
marketable 1-share limit, waits for the fill, then flattens. Off by default.

Safety: refuses to run unless ALPACA_PAPER is true (validate_paper_lock).
Every order is qty=1, DAY, limit (except the deliberate market-order
rejection probe), and cancelled if still live at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca.data.requests import StockLatestTradeRequest  # noqa: E402
from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: E402
from alpaca.trading.requests import (  # noqa: E402
    LimitOrderRequest,
    MarketOrderRequest,
)

from app.config import get_settings  # noqa: E402
from app.services.alpaca import AlpacaService  # noqa: E402

ET = ZoneInfo("America/New_York")
SYMBOL = "SPY"

RESULTS: list[tuple[str, str, str]] = []  # (check, PASS|FAIL|SKIP|INFO, detail)


def record(check: str, status: str, detail: str) -> None:
    RESULTS.append((check, status, detail))
    print(f"  [{status:^4}] {check}: {detail}")


def session_now() -> str:
    """Coarse ET session label; weekend handling is deliberately crude —
    don't run this on weekends and trust the label."""
    now = datetime.now(ET)
    hhmm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "weekend"
    if 4 * 60 <= hhmm < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= hhmm < 16 * 60:
        return "rth"
    if 16 * 60 <= hhmm < 20 * 60:
        return "postmarket"
    return "overnight"  # 20:00-04:00


async def reference_price(alpaca: AlpacaService) -> float | None:
    """Last trade for SYMBOL; tries the overnight feed first (the only feed
    quoting at this hour on this tier), then IEX."""
    for feed in ("overnight", "iex"):
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=SYMBOL, feed=feed)
            out = await alpaca.call(alpaca.stock_data.get_stock_latest_trade, req)
            price = float(out[SYMBOL].price)
            print(f"  reference price ({feed}): {SYMBOL} last={price}")
            return price
        except Exception as exc:
            print(f"  latest-trade via feed={feed} failed: {exc}")
    return None


async def submit_and_report(
    alpaca: AlpacaService, request, label: str
) -> object | None:
    """Submit, print the broker's verdict, return the order (or None on
    rejection-at-submit). Caller is responsible for cancelling live orders."""
    try:
        order = await alpaca.call(alpaca.trading.submit_order, request)
    except Exception as exc:
        print(f"  {label}: submit REJECTED at API: {exc}")
        return None
    print(
        f"  {label}: accepted id={order.id} status={order.status} "
        f"tif={order.time_in_force} extended={getattr(order, 'extended_hours', None)}"
    )
    return order


async def cancel_quietly(alpaca: AlpacaService, order) -> None:
    if order is None:
        return
    try:
        await alpaca.call(alpaca.trading.cancel_order_by_id, order.id)
        print(f"  cancelled {order.id}")
    except Exception as exc:  # already filled/rejected/expired is fine here
        print(f"  cancel {order.id}: {exc}")


async def check_extended_limit(alpaca: AlpacaService, ref: float, session: str) -> None:
    """Checks 1 + 2 (+5 when overnight): non-marketable extended-hours DAY
    limit, no position_intent."""
    limit = round(ref * 0.5, 2)  # far below market: cannot fill
    req = LimitOrderRequest(
        symbol=SYMBOL,
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit,
        extended_hours=True,
        client_order_id=f"verify-ext-{uuid.uuid4().hex[:12]}",
    )
    order = await submit_and_report(alpaca, req, "extended-hours limit")
    name = "overnight_submit" if session == "overnight" else "equity_limit_extended"
    if order is None:
        record(name, "FAIL", f"extended_hours DAY limit rejected during {session}")
        record("no_position_intent", "SKIP", "order rejected, inconclusive")
        return
    status = str(order.status)
    record(name, "PASS" if "reject" not in status else "FAIL",
           f"accepted with status={status} during {session} at limit={limit}")
    record("no_position_intent", "PASS", "equity order accepted without position_intent")
    # Watch briefly: does it sit 'new' (live in book) or 'accepted' (queued)?
    await asyncio.sleep(3)
    try:
        fresh = await alpaca.call(alpaca.trading.get_order_by_id, order.id)
        record(f"{name}_state", "INFO", f"status after 3s = {fresh.status}")
    except Exception as exc:
        record(f"{name}_state", "INFO", f"re-fetch failed: {exc}")
    await cancel_quietly(alpaca, order)


async def check_market_after_hours(alpaca: AlpacaService, session: str) -> None:
    if session == "rth":
        record("market_after_hours", "SKIP", "market is open (RTH) - probe not meaningful")
        return
    req = MarketOrderRequest(
        symbol=SYMBOL,
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        client_order_id=f"verify-mkt-{uuid.uuid4().hex[:12]}",
    )
    order = await submit_and_report(alpaca, req, "after-hours market order")
    if order is None:
        record("market_after_hours", "PASS", "rejected at submit, as the enforcer assumes")
        return
    # Accepted: it queues for next open (the 2026-07-30 options incident's
    # equity twin). That's not a failure, but the session policy must know.
    record("market_after_hours", "INFO",
           f"NOT rejected - status={order.status}; it queues for next open. "
           "Session policy must never submit market orders outside RTH.")
    await cancel_quietly(alpaca, order)


async def check_news_stream(alpaca: AlpacaService, wait_s: float) -> None:
    try:
        from alpaca.data.live.news import NewsDataStream
    except ImportError as exc:
        record("news_stream", "FAIL", f"NewsDataStream missing from SDK: {exc}")
        return

    s = alpaca.settings
    stream = NewsDataStream(s.alpaca_api_key, s.alpaca_secret_key)
    got: asyncio.Future = asyncio.get_event_loop().create_future()

    async def on_news(news) -> None:
        if not got.done():
            got.set_result(news)

    stream.subscribe_news(on_news, "*")
    task = asyncio.create_task(stream._run_forever())
    try:
        news = await asyncio.wait_for(asyncio.shield(got), wait_s)
        headline = getattr(news, "headline", "?")
        record("news_stream", "PASS", f"authenticated and received: {headline[:80]!r}")
    except asyncio.TimeoutError:
        # No headline in the window is normal at night; the check is whether
        # the connection died (auth/entitlement error) or is simply quiet.
        if task.done():
            exc = task.exception()
            record("news_stream", "FAIL", f"stream task died: {exc}")
        else:
            record("news_stream", "PASS",
                   f"connected and stayed up {wait_s:.0f}s (no headline in window - "
                   "quiet hours, auth OK)")
    finally:
        task.cancel()
        try:
            await stream.close()
        except Exception:
            pass


async def check_fill(alpaca: AlpacaService, ref: float, session: str) -> None:
    """--fill only: marketable extended-hours limit, wait, flatten."""
    if session in ("rth", "weekend"):
        record("extended_fill", "SKIP", f"session={session}")
        return
    limit = round(ref * 1.002, 2)  # marketable but tight
    req = LimitOrderRequest(
        symbol=SYMBOL, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        limit_price=limit, extended_hours=True,
        client_order_id=f"verify-fill-{uuid.uuid4().hex[:12]}",
    )
    order = await submit_and_report(alpaca, req, "marketable extended limit")
    if order is None:
        record("extended_fill", "FAIL", "marketable extended limit rejected")
        return
    filled = False
    for _ in range(20):  # up to ~60s
        await asyncio.sleep(3)
        fresh = await alpaca.call(alpaca.trading.get_order_by_id, order.id)
        if str(fresh.status) == "OrderStatus.FILLED" or "filled" in str(fresh.status):
            record("extended_fill", "PASS",
                   f"FILLED at {fresh.filled_avg_price} during {session}")
            filled = True
            break
    if not filled:
        record("extended_fill", "INFO",
               f"no fill in 60s during {session} (status={fresh.status}) - cancelled")
        await cancel_quietly(alpaca, order)
        return
    # Flatten immediately.
    close = LimitOrderRequest(
        symbol=SYMBOL, qty=1, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        limit_price=round(ref * 0.998, 2), extended_hours=True,
        client_order_id=f"verify-flat-{uuid.uuid4().hex[:12]}",
    )
    flat = await submit_and_report(alpaca, close, "flatten")
    if flat is not None:
        for _ in range(20):
            await asyncio.sleep(3)
            fresh = await alpaca.call(alpaca.trading.get_order_by_id, flat.id)
            if "filled" in str(fresh.status).lower():
                record("extended_flatten", "PASS", f"flat at {fresh.filled_avg_price}")
                return
        record("extended_flatten", "INFO",
               "flatten did not fill in 60s - POSITION IS OPEN, close it manually "
               "or leave it to the next RTH session")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fill", action="store_true",
                        help="also prove an actual extended-hours fill (opens+closes 1 share)")
    parser.add_argument("--news-wait", type=float, default=45.0)
    args = parser.parse_args()

    settings = get_settings()
    settings.validate_paper_lock()  # belt: refuses live keys
    if not settings.alpaca_paper:
        raise SystemExit("refusing: not a paper account")
    alpaca = AlpacaService(settings)
    if not alpaca.configured:
        raise SystemExit("Alpaca keys not configured")

    session = session_now()
    now = datetime.now(ET)
    print(f"verify_equity_paths @ {now:%Y-%m-%d %H:%M ET} (UTC {datetime.now(timezone.utc):%H:%M}) "
          f"session={session} symbol={SYMBOL}")
    if session == "weekend":
        raise SystemExit("weekend - nothing to verify")

    ref = await reference_price(alpaca)
    if ref is None:
        record("reference_price", "FAIL", "no price from any feed - order checks skipped")
    else:
        await check_extended_limit(alpaca, ref, session)
        await check_market_after_hours(alpaca, session)
        if args.fill:
            await check_fill(alpaca, ref, session)
    await check_news_stream(alpaca, args.news_wait)

    print("\n=== summary ===")
    for check, status, detail in RESULTS:
        print(f"  [{status:^4}] {check}: {detail}")


if __name__ == "__main__":
    asyncio.run(main())
