"""RAW daily open/close panels, mirror of the Adjustment.ALL bars_ohlc_*.

Why this exists: `anchor` is post-release tape for early acceptances, so
the only clean way to measure a reaction against the official close on the
FULL panel is the official close itself — in the same RAW basis as the
reaction prints. The adjusted panels cannot be laundered into that basis
per-event (the factor is exactly what is unknown); a raw fetch can.

Writes cache/bars_rawoc_{lo}_{hi}_{n}.parquet with symbol/date/o/c. Same
windows and universes as the adjusted panels, so _bars_for-style range
matching works on these too.

Run:  python scripts/research_raw_closes.py
"""

from __future__ import annotations

import sys
import time as _time
from datetime import datetime

from _paths import BACKEND, CACHE, SCRIPTS

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(SCRIPTS))

import pandas as pd  # noqa: E402


def main() -> None:
    from alpaca.data.enums import Adjustment
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from zoneinfo import ZoneInfo

    from app.config import get_settings

    ET = ZoneInfo("America/New_York")
    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)

    for adj_path in sorted(CACHE.glob("bars_ohlc_*_1000.parquet")):
        parts = adj_path.stem.split("_")           # bars_ohlc_LO_HI_N
        lo, hi, n = parts[2], parts[3], parts[4]
        out = CACHE / f"bars_rawoc_{lo}_{hi}_{n}.parquet"
        if out.exists():
            print(f"{out.name}: cached")
            continue
        symbols = sorted(pd.read_parquet(adj_path, columns=["symbol"])
                         ["symbol"].unique())
        frames = []
        for i in range(0, len(symbols), 75):
            chunk = symbols[i:i + 75]
            try:
                res = api.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                    start=datetime.fromisoformat(lo).replace(tzinfo=ET),
                    end=datetime.fromisoformat(hi).replace(tzinfo=ET),
                    feed="sip", adjustment=Adjustment.RAW, limit=None))
            except Exception as exc:               # noqa: BLE001
                print(f"  {lo} chunk {i}: {str(exc)[:70]}")
                _time.sleep(1.0)
                continue
            for sym, bars in res.data.items():
                if bars:
                    frames.append(pd.DataFrame({
                        "symbol": sym,
                        "date": [b.timestamp.astimezone(ET).date().isoformat()
                                 for b in bars],
                        "o": [float(b.open) for b in bars],
                        "c": [float(b.close) for b in bars],
                    }))
            _time.sleep(0.3)
        df = pd.concat(frames, ignore_index=True)
        df.to_parquet(out)
        print(f"{out.name}: {len(df)} rows")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
