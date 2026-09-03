# Quote relay: live owns the socket, paper drinks downstream

## Why

Alpaca's free data plan allows **one** data websocket per account, shared
across the login's paper and live keys. Two servers means one of them is
refused (`connection limit exceeded`, observed 2026-09-03 on the live box
while the Windows paper engine held the connection). The dependency must
point the safe way: **live owns the socket; paper consumes a copy.** Live
never reads from paper, and a paper crash cannot touch live's quotes.

Both servers live on the Linux box (`mikoyae-kojiki`) — the relay is a
Redis pub/sub channel on that box, so there is no network hop and no new
process to supervise.

## Design

### Live side: a tee (≈10 lines, the only code that runs in the money process)
In `MarketDataService._on_stock_quote` / `_on_option_quote` /
`_on_stock_bar` (`market_data.py`), after the existing cache update:

```python
if self.relay:                                   # RelayPublisher, live only
    self.relay.publish("quotes:stock", payload)  # fire-and-forget, never awaited on the hot path
```

`RelayPublisher` wraps `redis.asyncio.publish` with a bounded queue and a
drain task, so a slow Redis cannot back-pressure the quote handlers. A
publish failure is a counter in `/api/system/state`, never an exception.
Redis pub/sub is server-wide (not scoped by logical DB), so live's db 1 and
paper's db 0 stay separate while sharing the channel.

Enabled by `QUOTE_RELAY=publish` (live server). Off by default; the paper
lock forbids it on a `TRADING_MODE=paper` process.

### Subscription requests: paper asks, live decides
Paper publishes `{"op": "subscribe" | "unsubscribe", "kind": "stock" | "option", "symbols": [...]}`
on `quotes:subscribe`. Live's `RelaySubscriptionBroker` task consumes it and
calls the existing `subscribe_stock` / `subscribe_options`
(`market_data.py:357`, `:417`) **within a budget**:

- live's own symbols (open plan legs, UI subscriptions) are reserved first
  and never evicted by a relay request;
- relay symbols fill the remainder of the free plan's per-connection
  allowance (`RELAY_SYMBOL_BUDGET`, default 20 of the 30);
- over-budget requests are dropped and counted (`relay.dropped`), never
  queued — a paper bug asking for 500 symbols is ignored, not honoured.

Live keeps a ref-count per relay symbol (`_relay_refs`) alongside its own
`_stock_refs`, so paper's unsubscribes cannot pull a symbol live still
needs. Every 60 s live republishes its current subscribed set on
`quotes:subscribed` so paper can reconcile after either side restarts.

### Paper side: `QUOTE_SOURCE=redis`
`MarketDataService.start()` (`market_data.py:172`) branches: instead of
`make_stock_stream()` / `make_option_stream()` + the two supervised
`_run_forever` tasks, it starts one supervised `RelayConsumer` task that
`SUBSCRIBE`s `quotes:stock`, `quotes:option`, `quotes:bars`,
`quotes:subscribed` and feeds the same `_on_*` handlers. `subscribe_stock`
/ `subscribe_options` publish requests instead of touching a stream.
Everything downstream (bar store, broadcast, strategies, enforcer) is
unchanged — they never knew where quotes came from. REST gap-fill and the
extended-hours pollers keep running on paper's own keys (REST is not
subject to the websocket limit).

`stream_age_s` on paper reflects the last relayed message; the header pill
shows `RELAY` instead of `LIVE` for the feed so a dark relay is visible.

### Failure modes
| event | live | paper |
|---|---|---|
| live restarts | its own stream reconnects | quote-dark ~10 s, then `quotes:subscribed` reconcile re-requests its symbols; REST gap-fill covers bars |
| paper restarts | nothing — its relay refs age out after 5 min without a heartbeat | resubscribes on boot |
| Redis down | publishes dropped + counted; live's own quotes unaffected | falls back to REST polling (existing) |
| socket refused (another process holds it) | backoff (`PatientStockDataStream`), REST marks | dark until live reconnects |

## Moving the paper engine onto the box

Outside market hours, in this order:

1. **Data**: `docker compose exec db pg_dump -U trader -Fc trader > paper.dump` on
   Windows; `scp` to the box; `sudo -u postgres createdb -O trader trader`;
   `pg_restore -U trader -d trader paper.dump`. The bar cache in Redis is
   disposable (backfills from REST).
2. **Env**: `/etc/planetaria/paper.env` — the paper keys, `TRADING_MODE=paper`,
   `QUOTE_SOURCE=redis`, `DATABASE_URL=.../trader`, `REDIS_URL=redis://localhost:6379/0`,
   `LLM_BACKEND` — either `claude-cli` (install the CLI on the box and log
   in as `mikoyae`; the service runs as that user so subscription auth
   works) or `api` with `ANTHROPIC_API_KEY`. Same 0640 root:mikoyae hygiene.
3. **Unit**: `deploy/paper/planetaria-paper.service` — a copy of the live
   unit on port 8000, `EnvironmentFile=/etc/planetaria/paper.env`,
   `After=planetaria-live.service` (it wants the relay up), **bound to
   127.0.0.1** and published as a second `tailscale serve` path
   (`--set-path /paper` or a second port) — the phone reaches it over the
   tailnet, which retires the LAN-open `0.0.0.0` binding for good.
4. **Windows**: `nssm stop planetaria-engine; nssm remove planetaria-engine confirm`.
   The dev box keeps `dev.ps1` for hot-reload development against Docker
   infra; nothing supervised runs there any more.
5. Live gets `QUOTE_RELAY=publish` in `live.env` and a restart; paper
   starts; `/api/system/state` on paper shows feed `RELAY`, on live shows
   `relay.published` climbing and `relay.dropped` at 0.

## Config summary
| var | live | paper |
|---|---|---|
| `QUOTE_RELAY` | `publish` | unset |
| `QUOTE_SOURCE` | unset (own sockets) | `redis` |
| `RELAY_SYMBOL_BUDGET` | 20 | — |
| `REDIS_URL` | `.../1` | `.../0` (same server; pub/sub is shared) |

Boot lock additions: `QUOTE_RELAY=publish` refused unless
`TRADING_MODE=live_manual`; `QUOTE_SOURCE=redis` refused on the live
server (live must never consume quotes from anything but the broker).

## Tests
- `RelayPublisher`: bounded queue drops-and-counts under a stalled Redis.
- `RelaySubscriptionBroker`: budget arithmetic (live symbols reserved, relay
  fills the rest, over-budget dropped), ref-count merge with `_stock_refs`,
  heartbeat expiry.
- `RelayConsumer`: messages reach `_on_stock_quote` unchanged; reconnect on
  Redis drop; `stream_age_s` semantics.
- Boot-lock refusals for the two config combinations above.
- End-to-end on the box: both services up, `curl :8000/api/quote/SPY`
  returns a quote whose timestamp matches live's cache within 100 ms.

## Effort
Tee + broker + consumer + tests ≈ half a day; paper migration ≈ one
evening. Do the migration first (paper on the box, still on its own
sockets — it will be refused while live holds the connection, harmlessly,
thanks to the backoff), then flip `QUOTE_SOURCE=redis`.

## Still worth knowing
A paid data plan (SIP) removes the socket limit *and* replaces IEX's
partial tape with consolidated prints. The relay makes two servers work
on the free plan; it does not make the marks better. Revisit after a few
weeks of live `exec_quality` data.
