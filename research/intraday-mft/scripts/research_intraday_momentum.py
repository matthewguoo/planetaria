"""Market intraday momentum: does the open predict the close?

Gao, Han, Li, Zhou (JFE 2018): on SPY 1993-2013, the first half-hour return
(and the overnight-plus-first-half-hour return) positively predicts the
LAST half-hour return — the stated mechanism is late-informed trading and
day-trader position squaring into the close. Baltussen-Da-Lammers-Martens
(JFE 2021) find the related pattern in futures. Published R^2 is tiny but
the sign-trade (enter 15:30 in the direction of the morning, exit MOC) is
the retail-implementable form: two liquid crossings, ~1-1.5bp round trip on
SPY, MOC order supported by the broker.

This is the intraday-MFT profile: one decision per day, minutes-scale
execution, no colocation needed. The question is only whether it survived
publication — the paper's sample ends 2013.

Data: Alpaca SIP 30-minute bars, 2016 -> now, SPY and QQQ. Bars are
start-labelled ET (09:30 bar = 09:30-10:00). Predictors and target are
same-day or adjacent-close ratios, so adjustment basis cancels everywhere
it appears.

    A: overnight + first 30m   prev 16:00 close -> today 10:00
    B: first 30m only          09:30 open -> 10:00
    target                     15:30 -> 16:00 close

Run:  python scripts/research_intraday_momentum.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
COST_BP = 1.5          # SPY/QQQ round trip: cross at 15:30 + MOC


def fetch(symbol: str) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"bars30_{symbol}.parquet"
    if f.exists():
        return pd.read_parquet(f)
    from alpaca.data.enums import Adjustment
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    from app.config import get_settings
    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    out = api.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(30, TimeFrameUnit.Minute),
        start=datetime(2016, 1, 1, tzinfo=ET),
        end=datetime(2026, 8, 8, tzinfo=ET),
        feed="sip", adjustment=Adjustment.ALL, limit=None))
    rows = [{"ts": b.timestamp.astimezone(ET), "o": float(b.open),
             "c": float(b.close)} for b in out.data[symbol]]
    df = pd.DataFrame(rows)
    df.to_parquet(f)
    return df


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["d"] = df["ts"].dt.date
    df["hm"] = df["ts"].dt.strftime("%H:%M")
    first = df[df["hm"] == "09:30"].set_index("d")
    last = df[df["hm"] == "15:30"].set_index("d")
    days = sorted(set(first.index) & set(last.index))
    out = pd.DataFrame({
        "d": days,
        "open930": [first.loc[d, "o"] for d in days],
        "c1000": [first.loc[d, "c"] for d in days],
        "o1530": [last.loc[d, "o"] for d in days],
        "close": [last.loc[d, "c"] for d in days],
    })
    out["prev_close"] = out["close"].shift(1)
    out["A"] = out["c1000"] / out["prev_close"] - 1     # overnight + first 30m
    out["B"] = out["c1000"] / out["open930"] - 1        # first 30m only
    out["target"] = out["close"] / out["o1530"] - 1     # last half hour
    return out.dropna().reset_index(drop=True)


def main() -> None:
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Intraday momentum (GHLZ first->last half-hour) — {STAMP}")
    emit()
    emit(f"SIP 30-minute bars via Alpaca, 2016 -> 2026-08. Sign trade: enter "
         f"15:30 in the predictor's direction, exit at the close (MOC). Net "
         f"lines charge {COST_BP}bp per round trip. GHLZ's own sample ends "
         "2013; this is purely post-publication data.")
    emit()

    for sym in ("SPY", "QQQ"):
        d = daily_frame(fetch(sym))
        halves = {
            "all": np.ones(len(d), bool),
            "2016-2020": (pd.to_datetime(d["d"]) < "2021-01-01").to_numpy(),
            "2021-2026": (pd.to_datetime(d["d"]) >= "2021-01-01").to_numpy(),
        }
        emit(f"## {sym} — {len(d)} days")
        emit()
        emit("| predictor | window | n | bp/trade gross | t | net bp | ann net % | ann Sharpe (net) | win% |")
        emit("|---|---|---|---|---|---|---|---|---|")
        for pname in ("A", "B"):
            sig = np.sign(d[pname].to_numpy())
            pnl = sig * d["target"].to_numpy()
            for wname, m in halves.items():
                x = pnl[m & (sig != 0)] * 1e4
                net = x - COST_BP
                emit(f"| {pname} ({'on+30m' if pname == 'A' else '30m'}) "
                     f"| {wname} | {len(x)} | {x.mean():+.2f} | {tstat(x):+.2f} "
                     f"| {net.mean():+.2f} | {net.mean() * 252 / 100:+.1f} "
                     f"| {net.mean() / net.std(ddof=1) * np.sqrt(252):+.2f} "
                     f"| {(net > 0).mean() * 100:.1f} |")
        emit()
        # Conditional strength: does size of the morning move matter (GHLZ
        # report stronger prediction on big-move and high-vol days)?
        sig = np.sign(d["A"].to_numpy())
        pnl = sig * d["target"].to_numpy() * 1e4
        absA = np.abs(d["A"].to_numpy())
        q = np.nanquantile(absA, [0.5, 0.8])
        for label, m in (("small |A| (<p50)", absA < q[0]),
                         ("mid |A| (p50-p80)", (absA >= q[0]) & (absA < q[1])),
                         ("big |A| (>=p80)", absA >= q[1])):
            x = pnl[m]
            emit(f"- {label}: {x.mean():+.2f}bp gross (t {tstat(x):+.2f}, "
                 f"n={len(x)}, net {x.mean() - COST_BP:+.2f})")
        emit()

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"intraday_momentum_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
