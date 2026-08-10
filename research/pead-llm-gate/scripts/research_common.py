"""The shared primitives of the study, in one importable place.

Phase-14 §2: research_holding_period and research_llm_contamination imported
each other, and the pair worked only because the names each needed happened to
be defined before first use — a property of line ordering, not of design.
Everything both sides (and half the other scripts) actually share lives here
instead: the selection constants, the universe loader, the timing filter, the
cached panel/benchmark readers, and the note-writer.

Names here are public. research_llm_contamination keeps `_bars_for` and
`_spy_daily` bound as aliases so nothing already written breaks, but new code
imports from research_common directly.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from _paths import BACKEND, CACHE, SCRIPTS

sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ET = ZoneInfo("America/New_York")

GATE = 5.0          # |reaction| gate, percent — the live min_move_pct
TOP_PER_DAY = 5     # the live watchlist rule, applied historically
MIN_DV = 5e7        # liquidity floor, prior-session dollar volume
COSTS_BP = 13.0     # 10bp AH entry + 3bp RTH exit


# --------------------------------------------------------------- selection

def bars_for(lo: str, hi: str) -> pd.DataFrame:
    """The daily panel COVERING [lo, hi]. Matching on a substring of the
    filename (the old harness's approach) silently picked the 2022 panel
    for the 2023 window — every dv/run5d came back NaN, the liquidity floor
    dropped every row, and the arm scored zero events. Match on the range."""
    for path in sorted(CACHE.glob("bars_ohlc_*.parquet")):
        span = re.findall(r"(\d{4}-\d{2}-\d{2})", path.name)[:2]
        if len(span) == 2 and span[0] <= lo and span[1] >= hi:
            return pd.read_parquet(path)
    raise SystemExit(f"no cached OHLC panel covering {lo}..{hi}")


def load_universe() -> pd.DataFrame:
    """Every gate-5% event in the five cached windows, reduced to the LIVE
    watchlist rule: per announce-day, the top-N most liquid reporters above
    the dollar-volume floor."""
    frames = []
    for path in sorted(CACHE.glob("pead_events_*.parquet")):
        ev = pd.read_parquet(path)
        ev["date"] = pd.to_datetime(ev["date"]).dt.date
        ev["move_pct"] = (ev["react"] / ev["anchor"] - 1) * 100
        ev["fwd_bp"] = (ev["exit"] / ev["react"] - 1) * 1e4
        g = ev[np.abs(ev["move_pct"]) >= GATE].copy()
        if g.empty:
            continue
        lo, hi = str(g["date"].min()), str(g["date"].max())
        bars = bars_for(lo, hi)
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        bars = bars.sort_values(["symbol", "date"])
        panel = {s: (gg["date"].to_list(), gg["c"].to_numpy(),
                     (gg["c"] * gg["v"]).to_numpy())
                 for s, gg in bars.groupby("symbol")}

        def ctx(row):
            d, c, dv = panel.get(row["symbol"], (None, None, None))
            if d is None:
                return pd.Series({"run5d": np.nan, "dv": np.nan})
            i = np.searchsorted(d, row["date"])
            if not (6 <= i < len(c)):
                return pd.Series({"run5d": np.nan, "dv": np.nan})
            # Strictly prior sessions: no lookahead into the event day.
            return pd.Series({"run5d": (c[i - 1] / c[i - 6] - 1) * 100,
                              "dv": dv[i - 1]})

        g = pd.concat([g, g.apply(ctx, axis=1)], axis=1)
        g = g[g["dv"] >= MIN_DV]
        g = g.sort_values("dv", ascending=False).groupby("date").head(TOP_PER_DAY)
        frames.append(g)
    out = pd.concat(frames, ignore_index=True)
    out["month"] = out["date"].astype(str).str[:7]
    out["year"] = out["date"].astype(str).str[:4]
    return out.drop_duplicates(subset=["symbol", "date"]).sort_values("date")


# ------------------------------------------------------- acceptance timing

PROVENANCE = CACHE / "lookahead_provenance.parquet"
ENTRY_MIN = 16 * 60 + 20    # the entry print, ET
CLOSE_MIN = 16 * 60         # the closing bell, ET


def timing_ok(df: pd.DataFrame) -> pd.Series:
    """True where the 8-K behind the event was accepted by EDGAR between the
    16:00 bell and the 16:20 entry print.

    FOUND 2026-08-06 by verify_no_lookahead.py, and it invalidates part of
    every panel built before that date. `fetch_calendar_window` classifies a
    filing's hour by slicing acceptanceDateTime as text and comparing to
    16:00, on the belief — written into its docstring — that the timestamp is
    ET wearing a spurious `Z`. It is not: it is real UTC. Two things prove it,
    both from the cached submissions: the median post-15:00 acceptance moves
    58 minutes between Apr-Oct and Nov-Feb (a DST shift, which issuer
    behaviour cannot produce), and read as ET the histogram puts filings at
    midnight and 02:00 when EDGAR is shut.

    So the calendar's "amc" bucket really means "after 16:00 UTC" — after
    noon ET in summer. Of the 1,552 scored events, 75 (4.8%) were accepted
    BEFORE the close and are not after-hours releases at all, and 152 (9.8%)
    were accepted AFTER 16:20, so the release was not public at the price the
    study enters at. Both are excluded by this filter. The remaining 1,325
    are clean, and score BETTER than the full set (+210.3bp gated vs +185.7),
    so the mis-timed events were diluting the result rather than creating it.

    What this filter does NOT repair: selection. The per-day top-5 was ranked
    over a candidate pool that included the mis-timed names, so on affected
    days the shipped watchlist is not the one a corrected calendar would
    produce. Fixing that needs the EDGAR calendars rebuilt, which changes the
    universe and would require re-running the study.
    """
    if not PROVENANCE.exists():
        raise SystemExit(
            "no lookahead_provenance.parquet — run "
            "`python scripts/verify_no_lookahead.py --only provenance` first "
            "(it fetches EDGAR acceptance times; free, ~3 min cold)")
    prov = pd.read_parquet(PROVENANCE)
    prov = prov[prov["status"] == "ok"][["symbol", "date", "acc_min"]]
    key = df["symbol"].astype(str) + "|" + df["date"].astype(str)
    lookup = dict(zip(prov["symbol"] + "|" + prov["date"], prov["acc_min"]))
    mins = key.map(lookup)
    return (mins >= CLOSE_MIN) & (mins <= ENTRY_MIN)


# ------------------------------------------------------------ cached data

PATHS = CACHE / "event_paths_multi.parquet"


def paths_file(panel: str = "v2") -> Path:
    """Where the per-event minute paths live for a given panel."""
    if panel == "v2":
        from research_event_panel import PATHS_V2
        return PATHS_V2
    return PATHS


def alpaca_client():
    """The one place the historical-data client is built."""
    from alpaca.data.historical.stock import StockHistoricalDataClient

    from app.config import get_settings

    s = get_settings()
    return StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)


def spy_daily(start: date, end: date, symbol: str = "SPY") -> pd.DataFrame:
    """Daily closes for a benchmark, cached. Split/dividend adjusted from the
    same source as everything else, not from memory.

    QQQ is here because "is this just market exposure?" is not answered by
    SPY alone — earnings reactions concentrate in tech, so the NASDAQ-100 is
    the benchmark the strategy could plausibly be a proxy for. TQQQ (3x QQQ)
    is the leverage control: if the returns were levered beta, TQQQ is what
    they would look like, drawdown included."""
    cache = CACHE / f"bench_{symbol}_{start}_{end}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    api = alpaca_client()
    out = api.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=datetime.combine(start, datetime.min.time(), ET),
        end=datetime.combine(end, datetime.min.time(), ET),
        feed="sip", adjustment=Adjustment.ALL, limit=None))
    bars = out.data[symbol]
    df = pd.DataFrame({"date": [b.timestamp.astimezone(ET).date() for b in bars],
                       "close": [float(b.close) for b in bars]})
    df.to_parquet(cache)
    return df


# ------------------------------------------------------------------- stats

def tstat(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


# ------------------------------------------------------------------- notes

def stamp() -> str:
    return datetime.now(ET).strftime("%Y%m%d_%H%M")


def provenance_line(extra: str = "") -> str:
    """The line every note was missing: which code, which arguments, which
    tree produced this number. 128 notes existed before any recorded it."""
    script = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "?"
    argv = " ".join(sys.argv[1:])
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=str(SCRIPTS),
        ).stdout.strip() or "?"
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", str(SCRIPTS)],
            capture_output=True, text=True, timeout=5, cwd=str(SCRIPTS),
        ).stdout.strip()
        if dirty:
            rev += "+dirty"
    except Exception:
        rev = "?"
    cmd = f"{script} {argv}".strip()
    tail = f"; {extra}" if extra else ""
    return (f"_Provenance: `{cmd}` at {rev}, "
            f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}{tail}_")


def write_note(out: Path, lines: list[str], extra: str = "") -> Path:
    """Write a note with the provenance footer and say where it went."""
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines).rstrip() + "\n\n" + provenance_line(extra) + "\n"
    out.write_text(body, encoding="utf-8")
    print(f"\nwrote {out}")
    return out
