"""External after-hours quote sources for an account with no SIP entitlement.

WHY THIS EXISTS. Alpaca sells execution and data as separate products. The
paper account may submit extended-hours DAY limits that FILL 24/5 (verified
2026-08-04: SPY round trip at 20:38 ET) — but the free data tier is IEX-only,
and IEX operates 08:00-17:00 ET. From 17:00 to 20:00 the venues this account
can trade on keep printing while the feed it can see goes dark: the
`latest-quote` endpoint answers with the dead 17:00 book, observed 18 points
off the live tape. Trading is open; sight is what costs money.

The consolidated tape is not secret, though — it is licensed. Two public
sources republish enough of it to price an entry honestly:

  yahoo    the v8 chart API, keyless, the same endpoint portfolio_risk
           already uses for daily history. With includePrePost the 1m series
           extends through both extended sessions, so the last bar close is
           the latest AH print, and the meta block often carries an explicit
           postMarketPrice with its own timestamp.
  finnhub  /quote on the key the earnings calendar already uses: last trade
           `c` and its epoch-seconds timestamp `t`.

Neither carries an NBBO — these are trade prints, so the quote dicts they
produce follow the engine's own overnight-poller convention: bid == ask ==
last, a `src` tag for provenance, and an honest timestamp for the freshness
gates. Nothing here invents liquidity; a print from a source that has gone
quiet ages out at the same `_usable_price` checks as everything else.

This feed is consulted only when the broker's own data is stale (see
MarketDataService.ah_quote) — during RTH it never fires. It is a research
instrument for the no-SIP live test, not a data plane the exits depend on:
positions still mark and exit off the broker feeds, and the fill itself is
verified after the fact against the free tier's 15-minute-delayed SIP.
"""

import logging
import time

import httpx

log = logging.getLogger("app.ahquotes")

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) planetaria/1.0"}
FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"

# One result per symbol is reused for this long before a source is re-asked,
# and a source is never asked more often than the attempt floor — mirrors
# fetch_latest_stock_quote's 10s/5s posture so the external sources see less
# traffic than the broker does.
CACHE_TTL_S = 10.0
ATTEMPT_FLOOR_S = 5.0
# A yahoo print this fresh is good enough that finnhub is not consulted.
FRESH_ENOUGH_S = 90.0


def _print_quote(symbol: str, price: float, ts_ms: int, src: str) -> dict:
    """A trade print in the engine's quote shape (bid == ask == last), the
    same convention the overnight poller writes — zero spread reads as
    'no book information', not 'a tight market'."""
    return {"t": "quote", "symbol": symbol, "bid": price, "ask": price,
            "mid": price, "ts": int(ts_ms), "src": src}


def parse_yahoo_chart(symbol: str, doc: dict) -> dict | None:
    """The freshest print the chart document carries: the explicit
    postMarketPrice when the meta block has one, else the last non-null 1m
    close. Returns the engine quote shape or None."""
    try:
        result = ((doc.get("chart") or {}).get("result") or [None])[0] or {}
        meta = result.get("meta") or {}
        best: tuple[float, float] | None = None  # (ts_s, price)
        pm_px, pm_ts = meta.get("postMarketPrice"), meta.get("postMarketTime")
        if pm_px and pm_ts:
            best = (float(pm_ts), float(pm_px))
        rm_px, rm_ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
        if rm_px and rm_ts and (best is None or float(rm_ts) > best[0]):
            best = (float(rm_ts), float(rm_px))
        stamps = result.get("timestamp") or []
        closes = (((result.get("indicators") or {}).get("quote")
                   or [{}])[0].get("close")) or []
        for i in range(min(len(stamps), len(closes)) - 1, -1, -1):
            if closes[i] is not None:
                if best is None or float(stamps[i]) > best[0]:
                    best = (float(stamps[i]), float(closes[i]))
                break
        if best is None or best[1] <= 0:
            return None
        return _print_quote(symbol, float(best[1]), int(best[0] * 1000), "yahoo")
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def parse_finnhub_quote(symbol: str, doc: dict) -> dict | None:
    try:
        price, ts_s = float(doc.get("c") or 0), float(doc.get("t") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or ts_s <= 0:
        return None
    return _print_quote(symbol, price, int(ts_s * 1000), "finnhub")


class AhQuoteFeed:
    """Yahoo-then-Finnhub last-trade lookups with per-symbol caching. Every
    failure degrades to None; the caller's freshness gates do the refusing."""

    def __init__(self, settings,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport          # injected in tests
        self._http: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, dict]] = {}   # sym -> (mono, quote)
        self._attempt: dict[str, float] = {}              # sym -> mono

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers=YAHOO_HEADERS, timeout=6.0, transport=self._transport)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:                              # noqa: BLE001
                pass
            self._http = None

    async def _yahoo(self, symbol: str) -> dict | None:
        try:
            resp = await self._client().get(
                YAHOO_CHART_URL.format(symbol=symbol),
                params={"range": "1d", "interval": "1m", "includePrePost": "true"},
            )
            resp.raise_for_status()
            return parse_yahoo_chart(symbol, resp.json())
        except Exception as exc:                           # noqa: BLE001
            log.debug("yahoo AH quote %s failed: %s", symbol, exc)
            return None

    async def _finnhub(self, symbol: str) -> dict | None:
        key = getattr(self.settings, "finnhub_api_key", "") or ""
        if not key:
            return None
        try:
            resp = await self._client().get(
                FINNHUB_QUOTE_URL, params={"symbol": symbol},
                headers={"X-Finnhub-Token": key},
            )
            resp.raise_for_status()
            return parse_finnhub_quote(symbol, resp.json())
        except Exception as exc:                           # noqa: BLE001
            log.debug("finnhub AH quote %s failed: %s", symbol, exc)
            return None

    async def latest(self, symbol: str) -> dict | None:
        """Freshest print either source has, or the cached one inside its
        TTL, or None. Never raises."""
        symbol = symbol.upper()
        now = time.monotonic()
        cached = self._cache.get(symbol)
        if cached and now - cached[0] < CACHE_TTL_S:
            return cached[1]
        if now - self._attempt.get(symbol, -1e9) < ATTEMPT_FLOOR_S:
            return cached[1] if cached else None
        self._attempt[symbol] = now

        quote = await self._yahoo(symbol)
        age_s = ((time.time() * 1000 - quote["ts"]) / 1000) if quote else None
        if quote is None or age_s is None or age_s > FRESH_ENOUGH_S:
            other = await self._finnhub(symbol)
            if other and (quote is None or other["ts"] > quote["ts"]):
                quote = other
        if quote is not None:
            self._cache[symbol] = (now, quote)
        return quote
