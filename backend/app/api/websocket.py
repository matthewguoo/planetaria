"""Multiplexed client WebSocket: snapshot-then-stream protocol.

Client -> server:
    {"op": "subscribe",   "channel": "bars",    "symbol": "SPY", "tf": "1m"}
    {"op": "unsubscribe", "channel": "bars",    "symbol": "SPY", "tf": "1m"}
    {"op": "subscribe",   "channel": "quote",   "symbol": "SPY"}
    {"op": "subscribe",   "channel": "oquotes", "symbols": ["SPY260729C00450000", ...]}
    {"op": "unsubscribe", "channel": "oquotes", "symbols": [...]}

Server -> client: on subscribe, the full current state (snapshot), then deltas.
Reconnect is therefore always safe — no client-side gap logic needed.
A status frame is pushed every 5s so the UI can show STALE/DISCONNECTED.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.broadcast import QUEUE_SIZE

log = logging.getLogger("app.ws")

router = APIRouter()


@router.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    app = ws.app
    market = app.state.market
    broadcaster = app.state.broadcaster
    await ws.accept()

    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    topics: set[str] = set()
    stock_subs: set[str] = set()
    option_subs: set[str] = set()

    async def pump() -> None:
        while True:
            msg = await queue.get()
            await ws.send_json(msg)

    async def heartbeat() -> None:
        while True:
            await ws.send_json(market.status())
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

    try:
        while True:
            msg = await ws.receive_json()
            op, channel = msg.get("op"), msg.get("channel")

            if channel == "bars":
                symbol = (msg.get("symbol") or "").upper()
                tf = msg.get("tf") or "1m"
                if not symbol:
                    continue
                topic = f"bars:{symbol}:{tf}"
                if op == "subscribe":
                    join(topic)
                    if symbol not in stock_subs:
                        stock_subs.add(symbol)
                        await market.subscribe_stock(symbol)
                    await ws.send_json(
                        {"t": "bars_snapshot", "symbol": symbol, "tf": tf,
                         "bars": market.bars.get_bars(symbol, tf)}
                    )
                elif op == "unsubscribe":
                    leave(topic)

            elif channel == "quote":
                symbol = (msg.get("symbol") or "").upper()
                if not symbol:
                    continue
                topic = f"quote:{symbol}"
                if op == "subscribe":
                    join(topic)
                    quote = market.latest_quote(symbol) or await market.fetch_latest_stock_quote(symbol)
                    if quote:
                        await ws.send_json(quote)
                elif op == "unsubscribe":
                    leave(topic)

            elif channel == "oquotes":
                symbols = [s for s in (msg.get("symbols") or []) if s]
                if not symbols:
                    continue
                if op == "subscribe":
                    fresh = [s for s in symbols if s not in option_subs]
                    option_subs.update(fresh)
                    for sym in fresh:
                        join(f"oquote:{sym}")
                    await market.subscribe_options(fresh)
                    for sym in fresh:
                        cached = market.latest_quote(sym)
                        if cached:
                            await ws.send_json(cached)
                elif op == "unsubscribe":
                    stale = [s for s in symbols if s in option_subs]
                    option_subs.difference_update(stale)
                    for sym in stale:
                        leave(f"oquote:{sym}")
                    await market.unsubscribe_options(stale)

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
        if option_subs:
            await market.unsubscribe_options(sorted(option_subs))
