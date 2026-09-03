"""Symbol search + tradability: the curated liquid universe ranks first,
the broker's full active-equity list fills in behind it.

Two layers, deliberately:

* ``SYMBOLS`` is the static, liquidity-ordered core (the 0-3 DTE workhorses
  and the megacaps). It answers instantly, keyless, and its order is the
  tie-break inside every ranking tier.
* ``AssetUniverse`` is Alpaca's ``get_all_assets`` (active US equities,
  ~10k rows) fetched once in the background and refreshed daily. It is what
  makes the picker TradingView-shaped — any listed name is searchable — and
  what makes it *safe*: every hit carries the broker's own flags (tradable,
  fractionable, shortable/easy-to-borrow, options-enabled), so the UI can
  grey out what this account cannot trade instead of letting a typo or an
  index ticker reach the order path.

The universe never blocks a search: until it loads (or if the broker is
unreachable) results are curated-only with ``tradable: null`` = unverified.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

SYMBOLS: list[tuple[str, str]] = [
    # Index / sector ETFs (the 0-2 DTE workhorses first)
    ("SPY", "SPDR S&P 500 ETF"),
    ("QQQ", "Invesco Nasdaq-100 ETF"),
    ("IWM", "iShares Russell 2000 ETF"),
    ("DIA", "SPDR Dow Jones ETF"),
    ("SMH", "VanEck Semiconductor ETF"),
    ("XLF", "Financial Select SPDR"),
    ("XLE", "Energy Select SPDR"),
    ("XLK", "Technology Select SPDR"),
    ("TLT", "iShares 20+ Year Treasury ETF"),
    ("GLD", "SPDR Gold Shares"),
    ("SLV", "iShares Silver Trust"),
    ("USO", "United States Oil Fund"),
    ("UVXY", "ProShares Ultra VIX Futures"),
    ("VXX", "iPath VIX Short-Term Futures"),
    # Megacaps
    ("AAPL", "Apple Inc"),
    ("MSFT", "Microsoft Corp"),
    ("NVDA", "NVIDIA Corp"),
    ("GOOGL", "Alphabet Inc Class A"),
    ("GOOG", "Alphabet Inc Class C"),
    ("AMZN", "Amazon.com Inc"),
    ("META", "Meta Platforms Inc"),
    ("TSLA", "Tesla Inc"),
    ("BRK.B", "Berkshire Hathaway Class B"),
    ("AVGO", "Broadcom Inc"),
    ("LLY", "Eli Lilly & Co"),
    ("JPM", "JPMorgan Chase & Co"),
    ("V", "Visa Inc"),
    ("MA", "Mastercard Inc"),
    ("UNH", "UnitedHealth Group"),
    ("XOM", "Exxon Mobil Corp"),
    ("JNJ", "Johnson & Johnson"),
    ("WMT", "Walmart Inc"),
    ("PG", "Procter & Gamble"),
    ("HD", "Home Depot Inc"),
    ("COST", "Costco Wholesale"),
    ("ORCL", "Oracle Corp"),
    ("CVX", "Chevron Corp"),
    ("ABBV", "AbbVie Inc"),
    ("KO", "Coca-Cola Co"),
    ("PEP", "PepsiCo Inc"),
    ("MRK", "Merck & Co"),
    ("BAC", "Bank of America"),
    ("ADBE", "Adobe Inc"),
    ("CRM", "Salesforce Inc"),
    ("NFLX", "Netflix Inc"),
    ("AMD", "Advanced Micro Devices"),
    ("INTC", "Intel Corp"),
    ("QCOM", "Qualcomm Inc"),
    ("TXN", "Texas Instruments"),
    ("CSCO", "Cisco Systems"),
    ("TMO", "Thermo Fisher Scientific"),
    ("MCD", "McDonald's Corp"),
    ("NKE", "Nike Inc"),
    ("DIS", "Walt Disney Co"),
    ("WFC", "Wells Fargo & Co"),
    ("GS", "Goldman Sachs Group"),
    ("MS", "Morgan Stanley"),
    ("CAT", "Caterpillar Inc"),
    ("BA", "Boeing Co"),
    ("GE", "GE Aerospace"),
    ("HON", "Honeywell International"),
    ("UPS", "United Parcel Service"),
    ("PFE", "Pfizer Inc"),
    ("T", "AT&T Inc"),
    ("VZ", "Verizon Communications"),
    ("CMCSA", "Comcast Corp"),
    ("MU", "Micron Technology"),
    ("PLTR", "Palantir Technologies"),
    ("COIN", "Coinbase Global"),
    ("MSTR", "MicroStrategy Inc"),
    ("SQ", "Block Inc"),
    ("PYPL", "PayPal Holdings"),
    ("SHOP", "Shopify Inc"),
    ("UBER", "Uber Technologies"),
    ("ABNB", "Airbnb Inc"),
    ("SNOW", "Snowflake Inc"),
    ("PANW", "Palo Alto Networks"),
    ("CRWD", "CrowdStrike Holdings"),
    ("MRVL", "Marvell Technology"),
    ("SMCI", "Super Micro Computer"),
    ("ARM", "Arm Holdings"),
    ("TSM", "Taiwan Semiconductor ADR"),
    ("BABA", "Alibaba Group ADR"),
    ("NIO", "NIO Inc ADR"),
    ("F", "Ford Motor Co"),
    ("GM", "General Motors"),
    ("DAL", "Delta Air Lines"),
    ("AAL", "American Airlines"),
    ("CCL", "Carnival Corp"),
    ("SOFI", "SoFi Technologies"),
    ("HOOD", "Robinhood Markets"),
    ("DKNG", "DraftKings Inc"),
    ("RIVN", "Rivian Automotive"),
    ("LCID", "Lucid Group"),
    ("MARA", "Marathon Digital Holdings"),
    ("RIOT", "Riot Platforms"),
]

_CURATED = {s for s, _ in SYMBOLS}

UNIVERSE_TTL_S = 12 * 3600


def asset_row(asset) -> dict:
    """One broker asset -> the flags the picker shows. `options` reads the
    asset attributes Alpaca publishes ("has_options" / "options_enabled")."""
    attrs = {str(a) for a in (getattr(asset, "attributes", None) or [])}
    exchange = getattr(asset, "exchange", None)
    exchange = getattr(exchange, "value", exchange)
    return {
        "symbol": str(asset.symbol),
        "name": str(getattr(asset, "name", "") or asset.symbol),
        "exchange": str(exchange) if exchange else None,
        "tradable": bool(getattr(asset, "tradable", False)),
        "fractionable": bool(getattr(asset, "fractionable", False)),
        "shortable": bool(getattr(asset, "shortable", False))
        and bool(getattr(asset, "easy_to_borrow", False)),
        "options": bool(attrs & {"has_options", "options_enabled"}),
    }


class AssetUniverse:
    """Lazy, refreshed-daily cache of the broker's active US equities."""

    def __init__(self, alpaca=None):
        self.alpaca = alpaca
        self.assets: dict[str, dict] = {}
        self.loaded_at: float = 0.0
        self._task: asyncio.Task | None = None

    @property
    def loaded(self) -> bool:
        return bool(self.assets)

    def _stale(self) -> bool:
        return time.monotonic() - self.loaded_at > UNIVERSE_TTL_S

    def ensure(self) -> None:
        """Kick off (or refresh) the background load; never awaits it."""
        if self.alpaca is None or not getattr(self.alpaca, "configured", False):
            return
        if self._task and not self._task.done():
            return
        if self.loaded and not self._stale():
            return
        self._task = asyncio.create_task(self._load())

    async def _load(self) -> None:
        try:
            from alpaca.trading.enums import AssetClass, AssetStatus
            from alpaca.trading.requests import GetAssetsRequest

            req = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
            assets = await self.alpaca.call(self.alpaca.trading.get_all_assets, req, timeout=30.0)
            rows = {}
            for a in assets or []:
                row = asset_row(a)
                rows[row["symbol"]] = row
            if rows:
                self.assets = rows
                self.loaded_at = time.monotonic()
                log.info("asset universe loaded: %d active US equities", len(rows))
        except Exception as exc:  # broker down: stay curated-only, retry next call
            log.warning("asset universe load failed: %s", exc)

    async def lookup(self, symbol: str) -> dict | None:
        """One symbol's flags: from the cache, else a direct broker asset
        call (unknown/delisted -> None)."""
        symbol = symbol.upper()
        hit = self.assets.get(symbol)
        if hit:
            return hit
        if self.alpaca is None or not getattr(self.alpaca, "configured", False):
            return None
        try:
            asset = await self.alpaca.call(self.alpaca.trading.get_asset, symbol, timeout=5.0)
        except Exception:
            return None
        if asset is None:
            return None
        row = asset_row(asset)
        if self.assets:
            self.assets[row["symbol"]] = row
        return row

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """Rank: exact symbol > symbol prefix > name word-prefix > name
        substring; curated names lead each tier (their order ≈ liquidity),
        broker names follow alphabetically. Hits carry the broker flags when
        the universe knows them, else tradable=None (unverified)."""
        q = query.strip().upper()
        if not q:
            return [self._annotate(s, n) for s, n in SYMBOLS[:limit]]

        tiers: list[list[tuple[str, str]]] = [[], [], [], []]

        def place(sym: str, name: str) -> None:
            uname = name.upper()
            if sym == q:
                tiers[0].append((sym, name))
            elif sym.startswith(q):
                tiers[1].append((sym, name))
            elif any(w.startswith(q) for w in uname.split()):
                tiers[2].append((sym, name))
            elif len(q) >= 2 and q in uname:
                tiers[3].append((sym, name))

        for sym, name in SYMBOLS:
            place(sym, name)
        if self.assets:
            # Alphabetical so the broker tail is stable between keystrokes.
            for sym in sorted(self.assets):
                if sym in _CURATED:
                    continue
                row = self.assets[sym]
                if not row["tradable"]:
                    continue
                place(sym, row["name"])
        ranked = [x for tier in tiers for x in tier]
        return [self._annotate(s, n) for s, n in ranked[:limit]]

    def _annotate(self, symbol: str, name: str) -> dict:
        row = self.assets.get(symbol)
        if row:
            return dict(row, name=name if symbol in _CURATED else row["name"])
        return {
            "symbol": symbol,
            "name": name,
            "exchange": None,
            "tradable": None,
            "fractionable": None,
            "shortable": None,
            "options": None,
        }


# Keyless / test fallback: the curated list alone.
_static = AssetUniverse()


def search(query: str, limit: int = 8) -> list[dict]:
    return _static.search(query, limit)
