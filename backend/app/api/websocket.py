"""Multiplexed client WebSocket: snapshot-then-stream protocol.

Client -> server:
    {"op": "subscribe",   "channel": "bars",    "symbol": "SPY", "tf": "1m"}
    {"op": "unsubscribe", "channel": "bars",    "symbol": "SPY", "tf": "1m"}
    (tf "5s" | "15s" | "30s" rolls bars from the trade tape - see fast_bars)
    {"op": "subscribe",   "channel": "quote",   "symbol": "SPY"}
    {"op": "subscribe",   "channel": "oquote",  "symbol": "SPY260904C00650000"}
    (option NBBO for one contract; at most OQUOTE_MAX_PER_SOCKET per socket -
    the free options feed budgets 200 symbols across the whole process)

Server -> client: on subscribe, the full current state (snapshot), then deltas.
Reconnect is therefore always safe — no client-side gap logic needed.
A status frame is pushed every 5s so the UI can show STALE/DISCONNECTED.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.broadcast import QUEUE_SIZE
from app.services.fast_bars import is_fast_tf

log = logging.getLogger("app.ws")

router = APIRouter()

# One browser may watch the legs of one ticket plus a handful of positions;
# anything past this is a bug or a chain scrape, and the enforcer's own leg
# subscriptions must not be starved by it.
OQUOTE_MAX_PER_SOCKET = 12


@router.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    app = ws.app
    market = app.state.market
    broadcaster = app.state.broadcaster
    await ws.accept()

    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    topics: set[str] = set()
    stock_subs: set[str] = set()
    fast_subs: set[str] = set()  # symbols this socket holds a tape ref on
    option_subs: set[str] = set()  # OCC symbols this socket holds a quote ref on

    async def pump() -> None:
        while True:
            msg = await queue.get()
            await ws.send_json(msg)

    async def heartbeat() -> None:
        while True:
            try:
                await ws.send_json(market.status())
            except Exception:
                return  # socket gone; the receive loop is tearing down
            await asyncio.sleep(5)

    pump_task = asyncio.create_task(pump())
    hb_task = asyncio.create_task(heartbeat())

    def join(topic: str) -> None:
        if topic not in topics:
            topics.add(topic)
            broadcaster.subscribe(topic, queue)

    def leave(topic: str) -> None:
        if topic in topics:
            topics.discard(topic)
            broadcaster.unsubscribe(topic, queue)

    async def handle(msg: dict) -> None:
        op, channel = msg.get("op"), msg.get("channel")

        if channel == "bars":
            symbol = (msg.get("symbol") or "").upper()
            tf = msg.get("tf") or "1m"
            if not symbol:
                return
            topic = f"bars:{symbol}:{tf}"
            if op == "subscribe":
                join(topic)
                if symbol not in stock_subs:
                    stock_subs.add(symbol)
                    await market.subscribe_stock(symbol)
                if is_fast_tf(tf) and symbol not in fast_subs:
                    fast_subs.add(symbol)
                    await market.subscribe_fast(symbol)
                # Cached snapshot immediately; backfill (running in the
                # background) pushes a fresh bars_snapshot when it lands.
                await ws.send_json(
                    {"t": "bars_snapshot", "symbol": symbol, "tf": tf,
                     "bars": market.get_bars(symbol, tf)}
                )
            elif op == "unsubscribe":
                leave(topic)
                # The tape ref is held per symbol, not per tf: release it
                # only once no fast topic of this symbol remains.
                if symbol in fast_subs and not any(
                    t.startswith(f"bars:{symbol}:") and is_fast_tf(t.rsplit(":", 1)[1])
                    for t in topics
                ):
                    fast_subs.discard(symbol)
                    await market.unsubscribe_fast(symbol)

        elif channel == "quote":
            symbol = (msg.get("symbol") or "").upper()
            if not symbol:
                return
            topic = f"quote:{symbol}"
            if op == "subscribe":
                join(topic)
                quote = market.latest_quote(symbol) or await market.fetch_latest_stock_quote(symbol)
                if quote:
                    await ws.send_json(quote)
            elif op == "unsubscribe":
                leave(topic)

        elif channel == "oquote":
            symbol = (msg.get("symbol") or "").upper()
            if not symbol:
                return
            topic = f"oquote:{symbol}"
            if op == "subscribe":
                if symbol not in option_subs:
                    if len(option_subs) >= OQUOTE_MAX_PER_SOCKET:
                        await ws.send_json({
                            "t": "error", "channel": "oquote", "symbol": symbol,
                            "error": f"at most {OQUOTE_MAX_PER_SOCKET} option quotes per socket",
                        })
                        return
                    option_subs.add(symbol)
                    await market.subscribe_options([symbol])
                join(topic)
                # Snapshot: the cached quote, refreshed over REST when the
                # stream has not delivered one yet.
                quote = market.latest_quote(symbol)
                if quote is None or not quote.get("bid"):
                    try:
                        await market.refresh_option_quotes([symbol], max_age_s=0)
                    except Exception as exc:
                        log.warning("oquote snapshot refresh %s failed: %s", symbol, exc)
                    quote = market.latest_quote(symbol)
                if quote:
                    await ws.send_json({**quote, "t": "oquote", "symbol": symbol})
            elif op == "unsubscribe":
                leave(topic)
                if symbol in option_subs:
                    option_subs.discard(symbol)
                    await market.unsubscribe_options([symbol])

        elif channel == "plans":
            if op == "subscribe":
                join("plans")
                plans = await app.state.risk.open_plans()
                await ws.send_json(
                    {"t": "plans_snapshot", "plans": [p.to_dict() for p in plans]}
                )
            elif op == "unsubscribe":
                leave("plans")

    try:
        while True:
            # One malformed frame must not kill the connection — the client
            # would reconnect with backoff and the feed looks "spotty".
            try:
                msg = await ws.receive_json()
            except (ValueError, TypeError) as exc:
                log.warning("ws bad frame ignored: %s", exc)
                continue
            if not isinstance(msg, dict):
                continue
            try:
                await handle(msg)
            except (WebSocketDisconnect, RuntimeError):
                raise
            except Exception:
                log.exception("ws message handling failed (op=%s channel=%s)",
                              msg.get("op"), msg.get("channel"))

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("ws error: %s", exc)
    finally:
        pump_task.cancel()
        hb_task.cancel()
        for topic in topics:
            broadcaster.unsubscribe(topic, queue)
        for symbol in stock_subs:
            await market.unsubscribe_stock(symbol)
        for symbol in fast_subs:
            await market.unsubscribe_fast(symbol)
        if option_subs:
            await market.unsubscribe_options(sorted(option_subs))
